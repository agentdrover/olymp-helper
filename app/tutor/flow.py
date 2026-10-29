"""Разбор задания: проверка по ключу, подсказки, разбор ошибки, решение, «объясни иначе».

Проверка ответа идёт по ключу (или, без ключа, по model_answer) и модели не требует. Модель
приглашается только за текстом: подсказка, разбор ошибки, объяснение решения. Её ответ -
структурированный и проверенный (`parse_output`), расхождение с ключом уходит родителю, текст
проходит `guard.finish`. Запрос к модели на задание - один за раз (`Inflight`).

Текст до открытия решения проходит три ступени: дешёвый фильтр очевидного, независимый судья
(ДРУГОЙ канал модели: «раскрывает ли текст ответ?») и одна перегенерация с причиной от судьи; потом
общая подсказка. Судья недоступен или ответил не по форме - текст модели до решения не показывается вовсе:
общая подсказка (ступень потрачена), `rejected_by="judge_unavailable"`.
"""

import json
import logging
from datetime import datetime

from app.chat import store as chat_store
from app.ingest.answers import display
from app.ingest.publish import assist_mode
from app.ingest.store import figure_of, get_task
from app.model.client import MAX_REQUEST_BYTES, request_size
from app.tutor import state, steps
from app.tutor.guard import GENERIC, finish, render_markdown
from app.tutor.panel import Inflight, failure_state, panel
from app.tutor.prompts import (
    EXPLAIN_MODES,
    JUDGE_SCHEMA,
    OUTPUT_SCHEMA,
    ModelOutputError,
    build_judge_request,
    build_request,
    parse_output,
    parse_verdict,
)
from app.tutor.state import DISPUTED, Refused

log = logging.getLogger(__name__)

BUSY_ATTEMPT = "Помощник ещё думает — ответ не отправлен, попробуй через минуту."
LONG_TALK = "Слишком длинный разговор по этому заданию. Начни новое занятие или открой решение."


def fits_limit(model, system, messages, schema, channel=None) -> bool:
    """Мерка размера по фактической модели канала (ModelService.fits). Модель без неё -
    тестовая подмена: тогда мерка по умолчанию."""
    fits = getattr(model, "fits", None)
    if fits is not None:
        return bool(fits(system, messages, schema, channel))
    return request_size(system, messages, schema) <= MAX_REQUEST_BYTES


def too_large(result) -> bool:
    return getattr(result, "error_kind", None) == "request_too_large"


class Flow:
    def __init__(self, conn, model, inflight: Inflight, clock, runner=None):
        self.conn = conn
        self.runner = runner  # None или sync: работа шага идёт в потоке запроса
        self.step_id: int | None = None
        self.launched = False
        self.model = model
        self.inflight = inflight
        self.clock = clock
        self.panel_state: str | None = None  # состояние панели, которое покажет ответ
        self.notice: str | None = None
        self.attempt_state: str | None = None  # accepted | refused: ответ называет это клиенту
        self.channel: str | None = None  # канал, который сгенерировал текст: судья - другой
        self.session: int | None = None

    @property
    def now(self) -> datetime:
        return self.clock()

    # ----------------------------------------------------- шаг с обращением к модели

    def _begin(self, task, kind: str = "step") -> bool:
        """Весь шаг (генерация, судья, перегенерация) - один запрос в работе на задание."""
        if self.inflight.since(task["id"]) is not None:
            self.panel_state = "thinking"
            return False
        last = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()[0]
        step_id = steps.start(self.conn, task["id"], kind, self.now)
        try:
            got = self.inflight.acquire(task["id"], last, step_id, self.conn)  # замок и шаг вместе
        except BaseException:
            try:
                steps.discard(self.conn, step_id)  # строка pending не остаётся висеть
            except Exception:  # noqa: BLE001 - база занята: строка pending панель не меняет
                log.warning("не удалось снять строку шага %s", step_id)
            raise
        if not got:
            steps.discard(self.conn, step_id)
            self.panel_state = "thinking"
            return False
        try:
            steps.activate(self.conn, step_id)
        except BaseException:
            self.inflight.release(task["id"], step_id, self.conn)
            raise
        self.step_id = step_id
        return True

    def _end(self, task, error: str | None = None) -> None:
        """Шаг закончен: итог в строку шага, потом замок (ни при каком исходе он не остаётся)."""
        try:
            if self.step_id is not None:
                steps.finish(
                    self.conn, self.step_id, "failed" if error else "done", self.now,
                    panel_state=None if error else self.panel_state,
                    notice=None if error else self.notice, error=error,
                )  # fmt: skip
        finally:
            self.inflight.release(task["id"], self.step_id, self.conn)

    def _launch(self, task, name: str, *args) -> None:
        """Работа шага с моделью: в фоне, когда есть рабочий поток, иначе тут же. Замок после
        вызова принадлежит работе, не запросу."""
        self.launched = True
        if self.runner is None or self.runner.sync:
            self.execute(task["id"], name, args, reraise=True)
            return
        try:
            self.runner.submit(type(self), task["id"], self.step_id, name, args)
        except BaseException as exc:
            self._end(task, f"{type(exc).__name__}")
            raise

    def execute(self, task_id: int, name: str, args: tuple, *, reraise: bool) -> None:
        """Тело шага. Любой сбой - failed в строке шага и свободный замок."""
        task = get_task(self.conn, task_id)
        error = None
        failure: Exception | None = None
        try:
            getattr(self, name)(task, *args)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            failure = exc
            log.exception("шаг %s упал", self.step_id)
        self._end(task, error)
        if failure is not None and reraise:
            raise failure

    def _ask(self, task, run, mode: str, **extra):
        """ModelOutput или None (тогда panel_state назван). Вызывать внутри _begin/_end."""
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
            self.session = state.session_id_for(self.conn, self.now)
        if not fits_limit(self.model, system, messages, OUTPUT_SCHEMA):
            self.notice = LONG_TALK  # не помещается в лимит исполнителя: модель не зовём
            return None
        try:
            result = self.model.complete(system, messages, OUTPUT_SCHEMA, session_id=self.session)
        except Exception:  # noqa: BLE001 - модель любого вида: ребёнку «не отвечает»
            self.panel_state = "down"
            return None
        if not getattr(result, "ok", False):
            if too_large(result):  # не лимит и не сбой: разговор не помещается
                self.notice = LONG_TALK
            else:
                self.panel_state = failure_state(result)
            return None
        self.channel = getattr(result, "channel", None)
        payload = result.json if getattr(result, "json", None) is not None else result.text
        try:
            return parse_output(payload, task["type"])
        except ModelOutputError:
            self.panel_state = "down"
            return None

    def _discrepant(self, task, run, out) -> bool:
        """С ключом: ответ модели расходится с ключом -> запись родителю, True."""
        ref, keyed = state.reference(task)
        if keyed and out.value != ref:
            state.record_discrepancy(
                self.conn, task, run["id"], out, self.now, step_id=self.step_id
            )
            return True
        return False

    # ------------------------------------------------------------- судья и фильтры

    def _args(self, task):
        ref, keyed = state.reference(task)
        figure = figure_of(task) or {}
        options = json.loads(task["options"]) if task["options"] else None
        return ref, keyed, options, figure.get("teacher_note")

    def _verdict(self, task, run, text: str, purpose: str = "reveal"):
        """(раскрывает ли, причина) от ДРУГОГО канала; None - судья недоступен. Судья видит и то,
        что ребёнку уже показано по заданию (прошлые подсказки, разборы, попытки)."""
        call = getattr(self.model, "complete_on", None)
        if call is None or self.channel not in ("claude", "cursor"):
            return None
        other = "cursor" if self.channel == "claude" else "claude"
        ref, _, options, _ = self._args(task)
        fresh = state.get_run(self.conn, run["id"])
        system, messages = build_judge_request(
            task,
            text,
            _answer_text(ref, options),
            purpose,
            shown=json.loads(fresh["shown_json"])
            + chat_store.visible_texts(self.conn, self.session, task["id"], run["id"]),
            attempts=json.loads(fresh["attempts_json"]),
        )
        if not fits_limit(self.model, system, messages, JUDGE_SCHEMA, other):
            self.notice = LONG_TALK  # улики не режутся: судью не зовём, текст не показываем
            return None
        try:
            result = call(other, system, messages, JUDGE_SCHEMA, session_id=self.session)
        except Exception:  # noqa: BLE001
            return None
        if not getattr(result, "ok", False):
            if too_large(result):
                self.notice = LONG_TALK
            return None
        payload = result.json if getattr(result, "json", None) is not None else result.text
        try:
            return parse_verdict(payload)
        except ModelOutputError:
            return None

    def _check_text(self, task, run, text: str, kind: str, own) -> tuple[str | None, str]:
        """(код отклонения или None, пояснение для перегенерации)."""
        ref, keyed, options, note = self._args(task)
        pre = finish(
            text, kind, task["type"], options, ref, note,
            reveal=False, keyed=keyed, condition=task["text"], own=own,
        )[1]  # fmt: skip
        if pre:
            return pre, f"в тексте очевидный признак ответа ({pre})"
        verdict = self._verdict(task, run, text)
        if verdict is None:  # без судьи текст модели до решения не показываем
            return "judge_unavailable", ""
        if verdict[0]:
            return "judge", verdict[1] or "текст раскрывает ответ"
        return None, ""

    def _vet(self, task, run, mode: str, out, own=()):
        """(HTML, причина отклонения или None, uncertainty). Одна перегенерация с причиной
        от судьи (ответ в текст для ребёнка не подставляется), затем общая подсказка."""
        text, uncertainty, reason = out.explanation, out.uncertainty, None
        for attempt in (0, 1):
            reason, feedback = self._check_text(task, run, text, mode, own)
            if reason is None:
                return render_markdown(text), None, uncertainty
            if attempt == 1 or reason == "judge_unavailable":
                break
            again = self._ask(task, run, mode, feedback=feedback)
            if again is None or self._discrepant(task, run, again):
                break
            text, uncertainty = again.explanation, again.uncertainty
        return render_markdown(GENERIC[mode]), reason, uncertainty

    def _safe(self, task, text: str, kind: str, *, reveal=False, own=()):
        """Открытое решение: ответ называть можно; без ключа утверждения о верности вырезаются."""
        ref, keyed, options, note = self._args(task)
        return finish(
            text, kind, task["type"], options, ref, note,
            reveal=reveal, keyed=keyed, condition=task["text"], own=own,
        )  # fmt: skip

    # ------------------------------------------------------------------- действия

    def attempt(self, task_id: int, raw, confidence) -> int | None:
        """Проверка по ключу. В режиме с подсказками сначала берётся замок задания: занят -
        попытка не записывается (иначе её разбор потерялся бы), ребёнок отправит снова."""
        task = get_task(self.conn, task_id)
        locked = assist_mode(task) == "hints"
        if locked and not self._begin(task, "attempt"):
            self.attempt_state = "refused"
            self.notice = BUSY_ATTEMPT
            return None
        try:
            info = state.record_attempt(self.conn, task_id, raw, confidence, self.now)
            self.attempt_state = "accepted"
            run = state.get_run(self.conn, info["run_id"])
            if locked and not (info["matched"] and not info["keyed"]):
                self._launch(task, "_attempt_work", run["id"], bool(info["matched"]))
            return run["id"]
        finally:
            if locked and not self.launched:
                self._end(task)

    def _attempt_work(self, task, run_id: int, matched: bool) -> None:
        run = state.get_run(self.conn, run_id)
        if matched:
            self._explain_solution(task, run)
        else:
            self._mistake(task, run)

    def _mistake(self, task, run) -> None:
        out = self._ask(task, run, "mistake")
        if out is None:
            return
        own = [json.loads(run["attempts_json"])[-1]["answer"]] if run["attempts"] else []
        own = [x.strip() for part in own for x in part.replace(";", ",").split(",")]
        if self._discrepant(task, run, out):
            html, reason, unc = render_markdown(GENERIC["mistake"]), "discrepancy", out.uncertainty
        else:
            html, reason, unc = self._vet(task, run, "mistake", out, own)
        state.store_analysis(
            self.conn,
            run["id"],
            run["attempts"],
            html,
            reason is not None,
            unc,
            reason,
            step_id=self.step_id,
        )

    def _explain_solution(self, task, run) -> None:
        """Объяснение решения при ключе: независимое решение модели сверяется с ключом."""
        if state.is_disputed(self.conn, task):
            state.merge_solution(self.conn, run["id"], self.step_id, disputed=True)
            return
        out = self._ask(task, run, "solution")
        if out is None:
            return
        if self._discrepant(task, run, out):
            state.merge_solution(
                self.conn, run["id"], self.step_id, disputed=True, explanation_html=None
            )
            return
        html, reason = self._safe(task, out.explanation, "explain", reveal=True)
        state.merge_solution(
            self.conn, run["id"], self.step_id, explanation_html=None if reason else html
        )

    def hint(self, task_id: int, level, confirm: bool) -> None:
        task = get_task(self.conn, task_id)
        if assist_mode(task) == "key_only":
            self.panel_state = "no_hints"
            return
        run = state.begin_hint(self.conn, task_id, level, confirm, self.now)
        level = run["hints"] + 1
        if not self._begin(task, f"hint{level}"):
            return
        try:
            if level == 3:
                state.open_hint3(self.conn, task_id, run["id"], self.now)
                if state.reference(task)[1]:
                    self._launch(task, "_solution_work", run["id"])
                return
            self._launch(task, "_hint_work", run["id"], level)
        finally:
            if not self.launched:
                self._end(task)

    def _solution_work(self, task, run_id: int) -> None:
        self._explain_solution(task, state.get_run(self.conn, run_id))

    def _hint_work(self, task, run_id: int, level: int) -> None:
        run = state.get_run(self.conn, run_id)
        out = self._ask(task, run, f"hint{level}")
        if out is None:
            return
        if self._discrepant(task, run, out):
            html, reason, unc = (
                render_markdown(GENERIC[f"hint{level}"]), "discrepancy", out.uncertainty,
            )  # fmt: skip
        else:
            html, reason, unc = self._vet(task, run, f"hint{level}", out)
        state.store_hint(
            self.conn,
            run["id"],
            level,
            html,
            reason is not None,
            unc,
            self.now,
            reason,
            step_id=self.step_id,
        )

    def explain(self, task_id: int, mode) -> None:
        if mode not in EXPLAIN_MODES:
            raise Refused(422, "bad_mode", "Такого способа объяснить нет.")
        task = get_task(self.conn, task_id)
        run = state.active_run(self.conn, task)
        if run is None or not state.is_terminal(run):
            raise Refused(409, "not_open", "Объяснить иначе можно, когда решение открыто.")
        if assist_mode(task) == "key_only" or run["state"] == "answer_shown":
            self.panel_state = "no_hints"
            return
        keyed = state.reference(task)[1]
        if keyed and state.is_disputed(self.conn, task):
            self.notice = DISPUTED[0].upper() + DISPUTED[1:] + "."
            return
        if not self._begin(task, f"explain_{mode}"):
            return
        try:
            self._launch(task, "_explain_work", run["id"], mode)
        finally:
            if not self.launched:
                self._end(task)

    def _explain_work(self, task, run_id: int, mode: str) -> None:
        run = state.get_run(self.conn, run_id)
        keyed = state.reference(task)[1]
        label, instruction = EXPLAIN_MODES[mode]
        solution = json.loads(run["solution_json"])
        out = self._ask(
            task, run, f"explain_{mode}", key_text=solution["answer"], explain=instruction
        )
        if out is None:
            return
        # свежее объяснение расходится с ключом: родителю, не ребёнку
        if self._discrepant(task, run, out):
            state.merge_solution(
                self.conn, run["id"], self.step_id, disputed=True, explanation_html=None
            )
            self.notice = DISPUTED[0].upper() + DISPUTED[1:] + "."
            return
        html, reason = self._safe(task, out.explanation, "explain", reveal=True)
        if not reason and not keyed:
            # без ключа судья отдельно проверяет утверждения о верности; без судьи - ничего
            verdict = self._verdict(task, run, out.explanation, "claims")
            if verdict is None:
                reason = "judge_unavailable"
            elif verdict[0]:
                reason = "judge"
        if reason:
            if self.notice != LONG_TALK:  # «слишком длинный разговор» точнее и не перезаписывается
                self.notice = "Объяснение не получилось, попробуй другой способ."
            return
        state.add_explain(
            self.conn, run["id"], mode, label, html, out.uncertainty, self.now, self.step_id
        )

    def show_answer(self, task_id: int, confirm: bool) -> None:
        state.show_answer(self.conn, task_id, confirm, self.now)

    def repeat(self, task_id: int) -> None:
        state.start_repeat(self.conn, task_id, self.now)

    def dispute(self, task_id: int) -> None:
        state.dispute_key(self.conn, task_id, self.now)
        self.notice = "Записано: родитель проверит ключ."

    def panel(self) -> dict:
        return panel(self.panel_state or "idle")


def _answer_text(ref, options) -> str:
    """Верный ответ словами для судьи: обозначения и тексты вариантов."""
    items = options if isinstance(options, list) else []
    if isinstance(options, dict):
        items = [o for side in options.values() for o in side]
    names = ref if isinstance(ref, list) else [ref] if isinstance(ref, str) else []
    texts = [f"{o['label']}) {o['text']}" for o in items if o["label"] in names]
    return display(ref) + ("; " + "; ".join(texts) if texts else "")


def _plain_hints(items: list[str]) -> list[str]:
    import re

    return [re.sub(r"<[^>]+>", " ", h).strip() for h in items]
