"""Разбор задания: проверка по ключу, подсказки, разбор ошибки, решение, «объясни иначе».

Проверка ответа идёт по ключу (или, без ключа, по model_answer) и модели не требует. Модель
приглашается только за текстом: подсказка, разбор ошибки, объяснение решения. Её ответ -
структурированный и проверенный (`parse_output`), расхождение с ключом уходит родителю, текст
проходит `guard.finish`. Запрос к модели на задание - один за раз (`Inflight`).
"""

import json
from datetime import datetime

from app.ingest.publish import assist_mode
from app.ingest.store import figure_of, get_task
from app.tutor import state
from app.tutor.guard import finish
from app.tutor.panel import Inflight, failure_state, panel
from app.tutor.prompts import (
    EXPLAIN_MODES,
    OUTPUT_SCHEMA,
    ModelOutputError,
    build_request,
    parse_output,
)
from app.tutor.state import DISPUTED, Refused


class Flow:
    def __init__(self, conn, model, inflight: Inflight, clock):
        self.conn = conn
        self.model = model
        self.inflight = inflight
        self.clock = clock
        self.panel_state: str | None = None  # состояние панели, которое покажет ответ
        self.notice: str | None = None

    @property
    def now(self) -> datetime:
        return self.clock()

    # ------------------------------------------------------------- обращение к модели

    def _ask(self, task, run, mode: str, **extra):
        """ModelOutput или None (тогда panel_state назван)."""
        last = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()[0]
        if not self.inflight.acquire(task["id"], last):
            self.panel_state = "thinking"
            return None
        try:
            attempts = json.loads(run["attempts_json"])
            hints = [h["html"] for h in json.loads(run["hints_json"])]
            system, messages = build_request(
                task,
                mode,
                attempts=attempts,
                hints=_plain_hints(hints),
                use_note=assist_mode(task) == "hints",
                **extra,
            )
            with state.atomic(self.conn):
                session = state.session_id_for(self.conn, self.now)
            try:
                result = self.model.complete(system, messages, OUTPUT_SCHEMA, session_id=session)
            except Exception:  # noqa: BLE001 - модель любого вида: ребёнку «не отвечает»
                self.panel_state = "down"
                return None
        finally:
            self.inflight.release(task["id"])
        if not getattr(result, "ok", False):
            self.panel_state = failure_state(result)
            return None
        payload = result.json if getattr(result, "json", None) is not None else result.text
        try:
            return parse_output(payload, task["type"])
        except ModelOutputError:
            self.panel_state = "down"
            return None

    def _judge(self, task, run, out) -> bool:
        """С ключом: ответ модели расходится с ключом -> запись родителю, True."""
        ref, keyed = state.reference(task)
        if keyed and out.value != ref:
            state.record_discrepancy(self.conn, task, run["id"], out, self.now)
            return True
        return False

    def _safe(self, task, text: str, kind: str, *, reveal=False):
        ref, _ = state.reference(task)
        figure = figure_of(task) or {}
        options = json.loads(task["options"]) if task["options"] else None
        return finish(
            text, kind, task["type"], options, ref, figure.get("teacher_note"), reveal=reveal
        )

    # ------------------------------------------------------------------- действия

    def attempt(self, task_id: int, raw, confidence) -> int:
        info = state.record_attempt(self.conn, task_id, raw, confidence, self.now)
        task = get_task(self.conn, task_id)
        run = state.get_run(self.conn, info["run_id"])
        if assist_mode(task) != "hints":
            return run["id"]
        if info["matched"]:
            if info["keyed"]:
                self._explain_solution(task, run)
        else:
            self._mistake(task, run)
        return run["id"]

    def _mistake(self, task, run) -> None:
        out = self._ask(task, run, "mistake")
        if out is None:
            return
        mismatch = self._judge(task, run, out)
        if mismatch:
            html, generic = self._safe(task, "", "mistake")
        else:
            html, generic = self._safe(task, out.explanation, "mistake")
        state.store_analysis(self.conn, run["id"], run["attempts"], html, generic, out.uncertainty)

    def _explain_solution(self, task, run) -> None:
        """Объяснение решения при ключе: независимое решение модели сверяется с ключом."""
        if state.is_disputed(self.conn, task):
            state.merge_solution(self.conn, run["id"], disputed=True)
            return
        out = self._ask(task, run, "solution")
        if out is None:
            return
        if self._judge(task, run, out):
            state.merge_solution(self.conn, run["id"], disputed=True, explanation_html=None)
            return
        html, generic = self._safe(task, out.explanation, "explain", reveal=True)
        state.merge_solution(self.conn, run["id"], explanation_html=None if generic else html)

    def hint(self, task_id: int, level, confirm: bool) -> None:
        task = get_task(self.conn, task_id)
        if assist_mode(task) == "key_only":
            self.panel_state = "no_hints"
            return
        run = state.begin_hint(self.conn, task_id, level, confirm, self.now)
        level = run["hints"] + 1
        if level == 3:
            state.open_hint3(self.conn, task_id, run["id"], self.now)
            run = state.get_run(self.conn, run["id"])
            if state.reference(task)[1]:
                self._explain_solution(task, run)
            return
        out = self._ask(task, run, f"hint{level}")
        if out is None:
            return
        if self._judge(task, run, out):
            html, generic = self._safe(task, "", f"hint{level}")
        else:
            html, generic = self._safe(task, out.explanation, f"hint{level}")
        state.store_hint(self.conn, run["id"], level, html, generic, out.uncertainty, self.now)

    def explain(self, task_id: int, mode) -> None:
        if mode not in EXPLAIN_MODES:
            raise Refused(422, "bad_mode", "Такого способа объяснить нет.")
        task = get_task(self.conn, task_id)
        run = state.latest_run(self.conn, task_id)
        if run is None or not state.is_terminal(run):
            raise Refused(409, "not_open", "Объяснить иначе можно, когда решение открыто.")
        if assist_mode(task) == "key_only" or run["state"] == "answer_shown":
            self.panel_state = "no_hints"
            return
        keyed = state.reference(task)[1]
        if keyed and state.is_disputed(self.conn, task):
            self.notice = DISPUTED[0].upper() + DISPUTED[1:] + "."
            return
        label, instruction = EXPLAIN_MODES[mode]
        solution = json.loads(run["solution_json"])
        out = self._ask(
            task, run, f"explain_{mode}", key_text=solution["answer"], explain=instruction
        )
        if out is None:
            return
        html, generic = self._safe(task, out.explanation, "explain", reveal=True)
        if generic:
            self.notice = "Объяснение не получилось, попробуй другой способ."
            return
        state.add_explain(self.conn, run["id"], mode, label, html, out.uncertainty, self.now)

    def show_answer(self, task_id: int, confirm: bool) -> None:
        state.show_answer(self.conn, task_id, confirm, self.now)

    def repeat(self, task_id: int) -> None:
        state.start_repeat(self.conn, task_id, self.now)

    def dispute(self, task_id: int) -> None:
        state.dispute_key(self.conn, task_id, self.now)
        self.notice = "Записано: родитель проверит ключ."

    def panel(self) -> dict:
        return panel(self.panel_state or "idle")


def _plain_hints(items: list[str]) -> list[str]:
    import re

    return [re.sub(r"<[^>]+>", " ", h).strip() for h in items]
