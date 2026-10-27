"""Серверное состояние разбора: new -> attempted_wrong* -> hint1 -> hint2 -> hint3 | solved |
solved_unknown | answer_shown (SPEC-1.0 1.3). Ступени идут по порядку, параметром не перескочить.

Здесь только база и правила; к модели ходит app.tutor.flow. Каждый переход - одна транзакция.
"""

import json
import math
from datetime import UTC, datetime

from app.ingest.answers import display, key_value
from app.ingest.attempts import attempt_ref
from app.ingest.model_answer import model_value
from app.ingest.publish import assist_mode
from app.ingest.store import atomic, get_task, options_of
from app.sessions import IDLE_TIMEOUT
from app.tutor import steps
from app.tutor.check import BadAnswer, parse_answer
from app.tutor.guard import leaks_note, render_markdown, strip_claims
from app.tutor.outcomes import outcome_label
from app.tutor.seq import bump

TERMINAL = ("solved", "solved_unknown", "hint3", "answer_shown")
HINT_WAIT_S = 60
BASE_CHECKS = 2
CONFIDENCE = {"sure": "sure", "doubt": "doubt", "уверен": "sure", "сомневаюсь": "doubt"}
UNVERIFIED = "ответ модели, не проверен"
DISPUTED = "ответ уточняется у родителя"


class Refused(Exception):
    """Действие отклонено. code - машинное имя, message читает ребёнок."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


# ----------------------------------------------------------- занятие и события


def session_id_for(conn, now: datetime) -> int:
    """Открытое занятие; простоявшее 30 минут закрывается по простою (SPEC 1.8)."""
    threshold = iso(now - IDLE_TIMEOUT)
    conn.execute(
        "UPDATE study_session SET ended_at = last_activity_at, end_reason = 'idle' "
        "WHERE ended_at IS NULL AND last_activity_at <= ?",
        (threshold,),
    )
    row = conn.execute(
        "SELECT id FROM study_session WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row[0]
    stamp = iso(now)
    return conn.execute(
        "INSERT INTO study_session (started_at, last_activity_at) VALUES (?, ?)", (stamp, stamp)
    ).lastrowid


def add_event(conn, now: datetime, type: str, task_id: int, data: dict) -> int:
    with atomic(conn):  # событие и версия панели - одна транзакция
        session = session_id_for(conn, now)
        stamp = iso(now)
        conn.execute("UPDATE study_session SET last_activity_at = ? WHERE id = ?", (stamp, session))
        bump(conn)  # попытка, подсказка, объяснение, чат, ответ, отметка ключа - всё через событие
        return conn.execute(
            "INSERT INTO event (session_id, type, task_item_id, data, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session, type, task_id, json.dumps(data, ensure_ascii=False), stamp),
        ).lastrowid


# ---------------------------------------------------------------------- прогоны


def latest_run(conn, task_id: int):
    return conn.execute(
        "SELECT * FROM tutor_run WHERE task_item_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()


def get_run(conn, run_id: int):
    return conn.execute("SELECT * FROM tutor_run WHERE id = ?", (run_id,)).fetchone()


def _new_run(conn, task, now: datetime, repeat: bool):
    cur = conn.execute(
        "INSERT INTO tutor_run (task_item_id, session_id, revision, key_version, is_repeat, "
        "opened_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            task["id"],
            session_id_for(conn, now),
            task["revision"],
            task["key_version"],
            int(repeat),
            iso(now),
        ),
    )
    bump(conn)  # новый прогон (повтор или после правки родителя)
    return get_run(conn, cur.lastrowid)


def _current(run, task) -> bool:
    return run is not None and (run["revision"], run["key_version"]) == (
        task["revision"],
        task["key_version"],
    )


def active_run(conn, task, now: datetime | None = None):
    """Прогон по ТЕКУЩИМ ревизии и версии ключа. Прогон, начатый до правки родителя, устарел:
    его решение показывало бы старый ответ. С `now` вместо него начинается новый (после решённого -
    с пометкой «повтор»), без `now` возвращается None."""
    run = latest_run(conn, task["id"])
    if _current(run, task):
        return run
    if now is None:
        return None
    with atomic(conn):
        run = latest_run(conn, task["id"])
        if _current(run, task):
            return run
        return _new_run(conn, task, now, run is not None and is_terminal(run))


def ensure_run(conn, task_id: int, now: datetime):
    return active_run(conn, get_task(conn, task_id), now)


def reference(task):
    """(эталон, есть ли подтверждённый ключ). Без ключа эталон - model_answer."""
    if task["answer_key"] is not None:
        return key_value(task["answer_key"]), True
    return model_value(task), False


def is_terminal(run) -> bool:
    return run["state"] in TERMINAL


def checks_allowed(task, run) -> int:
    """Проверок разрешено 2 + число взятых подсказок; без подсказок (рисунок) - ровно две."""
    return BASE_CHECKS + (0 if assist_mode(task) == "key_only" else run["hints"])


def hint_wait(run, now: datetime) -> int:
    """Секунд до подсказки 1; 0 - уже можно (после попытки или через 60 секунд на экране)."""
    if run["attempts"] >= 1:
        return 0
    elapsed = (now - datetime.fromisoformat(run["opened_at"])).total_seconds()
    return max(0, math.ceil(HINT_WAIT_S - elapsed))


# --------------------------------------------------------------------- решение


def initial_solution(task, keyed: bool) -> dict:
    """Что открывается ребёнку. С ключом - ответ ключа (объяснение добавляет модель позже);
    без ключа - ответ и объяснение модели с импорта, с пометкой «не проверен»."""
    ref, _ = reference(task)
    sol = {
        "answer": display(ref),
        "unverified": not keyed,
        "disputed": False,
        "explanation_html": None,
    }
    if not keyed and task["model_answer"]:
        text = json.loads(task["model_answer"]).get("explanation") or ""
        figure = json.loads(task["figure"]) if task["figure"] else {}
        text = strip_claims(text)  # ответ модели не проверен: «верно/правильно» не утверждаем
        if text.strip() and not leaks_note(text, (figure or {}).get("teacher_note")):
            sol["explanation_html"] = render_markdown(text)
    return sol


def _confidence(raw):
    if raw in (None, ""):
        return None
    if raw not in CONFIDENCE:
        raise Refused(422, "bad_confidence", "Отметь «уверен» или «сомневаюсь», либо пропусти.")
    return CONFIDENCE[raw]


def record_attempt(conn, task_id: int, raw, confidence, now: datetime) -> dict:
    conf = _confidence(confidence)
    with atomic(conn):
        task = get_task(conn, task_id)
        run = active_run(conn, task, now)
        if is_terminal(run):
            raise Refused(409, "closed", "Это задание уже разобрано. Можно разобрать его ещё раз.")
        try:
            value = parse_answer(task["type"], options_of(task), raw)
        except BadAnswer as exc:
            raise Refused(422, "bad_answer", str(exc)) from exc
        key_only = assist_mode(task) == "key_only"
        if run["attempts"] >= checks_allowed(task, run):
            if key_only:
                raise Refused(409, "need_show", "Проверок больше нет. Можно показать ответ.")
            raise Refused(
                409, "need_hint", "Сначала возьми подсказку — потом можно проверить ещё раз."
            )
        ref, keyed = reference(task)
        if ref is None:
            raise Refused(409, "no_reference", "Для этого задания пока нет эталона.")
        matched = value == ref
        attempts = run["attempts"] + 1
        if matched:
            state = "solved" if keyed else "solved_unknown"
        else:
            state = run["state"] if run["hints"] else "attempted_wrong"
        history = json.loads(run["attempts_json"])
        history.append({"answer": display(value), "confidence": conf})
        data = {
            "answer": display(value),
            "correctness": ("correct" if matched else "incorrect") if keyed else "unknown",
            "confidence": conf,
            "repeat": bool(run["is_repeat"]),
            "n": attempts,
            "hints": run["hints"],
            "run": run["id"],
            **attempt_ref(conn, task_id),
        }
        if not keyed:
            data["matched_model"] = matched
        solution = initial_solution(task, keyed) if matched else None
        conn.execute(
            "UPDATE tutor_run SET state = ?, attempts = ?, attempts_json = ?, last_answer = ?, "
            "last_confidence = ?, analysis_json = NULL, solution_json = ?, outcome = ?, "
            "closed_at = ? WHERE id = ?",
            (
                state,
                attempts,
                json.dumps(history, ensure_ascii=False),
                display(value),
                conf,
                None if solution is None else json.dumps(solution, ensure_ascii=False),
                outcome_label(keyed, state, attempts, run["hints"]) if matched else None,
                iso(now) if matched else None,
                run["id"],
            ),
        )
        add_event(conn, now, "attempt", task_id, data)
        return {"run_id": run["id"], "matched": matched, "keyed": keyed}


# ------------------------------------------------------------------- подсказки


def begin_hint(conn, task_id: int, level, confirm: bool, now: datetime):
    """Проверка очередности и барьеров; возвращает прогон. Ничего не пишет."""
    task = get_task(conn, task_id)
    run = active_run(conn, task, now)
    if is_terminal(run):
        raise Refused(409, "closed", "Решение уже открыто.")
    nxt = run["hints"] + 1
    level = nxt if level is None else level
    if isinstance(level, bool) or level not in (1, 2, 3):
        raise Refused(422, "bad_level", "Подсказки бывают трёх ступеней: 1, 2 и 3.")
    if level != nxt:
        raise Refused(409, "order", "Подсказки идут по порядку: сначала нужна предыдущая.")
    if level == 1:
        wait = hint_wait(run, now)
        if wait:
            raise Refused(
                409, "too_early",
                "Подсказка откроется после первой попытки или через минуту.", wait_s=wait,
            )  # fmt: skip
    if level == 3 and not confirm:
        raise Refused(
            409, "needs_confirm",
            "Открыть решение? Задание засчитается как разобранное по подсказке 3.",
        )  # fmt: skip
    if assist_mode(task) == "key_only":
        raise Refused(409, "no_hints", "К этому заданию подсказок нет.")
    return run


def _plain(html: str) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _remember(conn, run_id: int, html: str) -> None:
    """Показанный ребёнку текст модели - в память прогона (читает судья)."""
    run = get_run(conn, run_id)
    seen = json.loads(run["shown_json"])
    seen.append(_plain(html))
    conn.execute(
        "UPDATE tutor_run SET shown_json = ? WHERE id = ?",
        (json.dumps(seen, ensure_ascii=False), run_id),
    )


def store_hint(
    conn, run_id: int, level: int, html: str, generic: bool, uncertainty: str, now, reason=None,
    step_id=None,
):  # fmt: skip
    """Записать подсказку 1 или 2, если прогон не ушёл вперёд за время запроса к модели."""
    with atomic(conn):
        steps.hold(conn, step_id)
        run = get_run(conn, run_id)
        if is_terminal(run) or run["hints"] != level - 1:
            return False
        hints = json.loads(run["hints_json"])
        hints.append({"level": level, "html": html, "generic": generic})
        conn.execute(
            "UPDATE tutor_run SET hints = ?, state = ?, hints_json = ? WHERE id = ?",
            (level, f"hint{level}", json.dumps(hints, ensure_ascii=False), run_id),
        )
        _remember(conn, run_id, html)
        add_event(conn, now, "hint", run["task_item_id"], {
            "level": level, "generic": generic, "rejected_by": reason, "uncertainty": uncertainty,
            "text": html,
            "run": run_id, "repeat": bool(run["is_repeat"]),
        })  # fmt: skip
        return True


def open_hint3(conn, task_id: int, run_id: int, now):
    with atomic(conn):
        task = get_task(conn, task_id)
        run = get_run(conn, run_id)
        if is_terminal(run) or run["hints"] != 2:
            raise Refused(409, "order", "Подсказки идут по порядку: сначала нужна предыдущая.")
        _, keyed = reference(task)
        solution = initial_solution(task, keyed)
        conn.execute(
            "UPDATE tutor_run SET hints = 3, state = 'hint3', solution_json = ?, outcome = ?, "
            "closed_at = ? WHERE id = ?",
            (
                json.dumps(solution, ensure_ascii=False),
                outcome_label(keyed, "hint3", run["attempts"], 3),
                iso(now),
                run_id,
            ),
        )
        add_event(conn, now, "hint", task_id, {
            "level": 3, "confirmed": True, "run": run_id, "repeat": bool(run["is_repeat"]),
            "keyed": keyed,
        })  # fmt: skip


def show_answer(conn, task_id: int, confirm: bool, now):
    """«Показать ответ» в режиме «только проверка по ключу» после двух проверок."""
    with atomic(conn):
        task = get_task(conn, task_id)
        run = active_run(conn, task, now)
        if assist_mode(task) != "key_only":
            raise Refused(409, "not_key_only", "Здесь ответ показывается по подсказкам.")
        if is_terminal(run):
            raise Refused(409, "closed", "Ответ уже открыт.")
        if run["attempts"] < BASE_CHECKS:
            raise Refused(409, "too_early", "Сначала используй обе проверки.")
        if not confirm:
            raise Refused(
                409, "needs_confirm", "Показать ответ? Задание засчитается как показанное."
            )
        solution = initial_solution(task, True)
        conn.execute(
            "UPDATE tutor_run SET state = 'answer_shown', solution_json = ?, outcome = ?, "
            "closed_at = ? WHERE id = ?",
            (
                json.dumps(solution, ensure_ascii=False),
                outcome_label(True, "answer_shown", run["attempts"], 0),
                iso(now),
                run["id"],
            ),
        )
        add_event(conn, now, "answer_shown", task_id, {
            "attempts": run["attempts"], "run": run["id"], "repeat": bool(run["is_repeat"]),
        })  # fmt: skip


def start_repeat(conn, task_id: int, now):
    with atomic(conn):
        task = get_task(conn, task_id)
        run = active_run(conn, task)
        if run is None or not is_terminal(run):
            raise Refused(409, "not_solved", "Повторить можно задание, которое уже разобрано.")
        return _new_run(conn, task, now, True)


def dispute_key(conn, task_id: int, now):
    with atomic(conn):
        run = active_run(conn, get_task(conn, task_id))
        if run is None or not is_terminal(run):
            raise Refused(409, "not_solved", "Отметить ключ спорным можно, когда решение открыто.")
        add_event(conn, now, "disputed_key", task_id, {"from_child": True, "run": run["id"]})


# ------------------------------------------------- записи по ответам модели


def update_run_json(conn, run_id: int, column: str, value) -> None:
    assert column in ("analysis_json", "explains_json", "solution_json")
    conn.execute(
        f"UPDATE tutor_run SET {column} = ? WHERE id = ?",
        (json.dumps(value, ensure_ascii=False), run_id),
    )


def merge_solution(conn, run_id: int, step_id=None, **fields) -> None:
    with atomic(conn):
        steps.hold(conn, step_id)
        run = get_run(conn, run_id)
        if run["solution_json"] is None:
            return
        sol = json.loads(run["solution_json"])
        sol.update(fields)
        update_run_json(conn, run_id, "solution_json", sol)
        bump(conn)


def store_analysis(
    conn, run_id: int, attempts: int, html: str, generic: bool, uncertainty: str, reason=None,
    step_id=None,
):  # fmt: skip
    """Разбор ошибки; пишется, только если с тех пор не было новой попытки."""
    with atomic(conn):
        steps.hold(conn, step_id)
        run = get_run(conn, run_id)
        if is_terminal(run) or run["attempts"] != attempts:
            return
        _remember(conn, run_id, html)
        update_run_json(
            conn, run_id, "analysis_json",
            {"html": html, "generic": generic, "rejected_by": reason, "uncertainty": uncertainty},
        )  # fmt: skip
        bump(conn)  # разбор ошибки меняет экран без нового события


def add_explain(
    conn, run_id: int, mode: str, label: str, html: str, uncertainty: str, now, step_id=None
):
    with atomic(conn):
        steps.hold(conn, step_id)
        run = get_run(conn, run_id)
        items = json.loads(run["explains_json"])
        items.append({"mode": mode, "label": label, "html": html})
        update_run_json(conn, run_id, "explains_json", items)
        add_event(conn, now, "explain", run["task_item_id"], {
            "mode": mode, "uncertainty": uncertainty, "run": run_id,
            "repeat": bool(run["is_repeat"]),
        })  # fmt: skip


def record_discrepancy(conn, task, run_id: int, out, now, step_id=None) -> None:
    """Ответ модели расходится с ключом: запись родителю (ребёнок её не видит)."""
    answer = json.dumps(out.value, ensure_ascii=False)
    with atomic(conn):
        steps.hold(conn, step_id)
        same = conn.execute(
            "SELECT 1 FROM tutor_discrepancy WHERE task_item_id = ? AND revision = ? "
            "AND key_version = ? AND model_answer = ?",
            (task["id"], task["revision"], task["key_version"], answer),
        ).fetchone()
        if same:
            return
        conn.execute(
            "INSERT INTO tutor_discrepancy (task_item_id, run_id, revision, key_version, "
            "model_answer, explanation, uncertainty, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task["id"], run_id, task["revision"], task["key_version"], answer, out.explanation,
             out.uncertainty, iso(now)),
        )  # fmt: skip
        bump(conn)  # расхождение прячет объяснение модели от ребёнка


def is_disputed(conn, task) -> bool:
    """Ответ модели расходится с ключом и родитель ещё не разобрал расхождение: объяснение
    модели ребёнку не показывается. Расхождение бывает и до первой попытки (SPEC 1.2): тогда
    оно лежит в model_answer задания."""
    ref, keyed = reference(task)
    if keyed and task["model_answer"]:
        seen = model_value(task)
        if seen != ref and task["discrepancy_ack"] != json.dumps(seen, ensure_ascii=False):
            return True
    return (
        conn.execute(
            "SELECT 1 FROM tutor_discrepancy WHERE task_item_id = ? AND revision = ? "
            "AND key_version = ? AND resolved_at IS NULL LIMIT 1",
            (task["id"], task["revision"], task["key_version"]),
        ).fetchone()
        is not None
    )


def resolve_discrepancies(conn, task_id: int) -> None:
    """Родитель разобрал расхождение в ленте проверки."""
    with atomic(conn):
        conn.execute(
            "UPDATE tutor_discrepancy SET resolved_at = ? WHERE task_item_id = ? "
            "AND resolved_at IS NULL",
            (iso(datetime.now(UTC)), task_id),
        )
        bump(conn)  # расхождение разобрано: объяснение снова допустимо


def discrepancies(conn, task_id: int) -> list[dict]:
    """Записи расхождений для родителя: ответ и объяснение модели."""
    rows = conn.execute(
        "SELECT * FROM tutor_discrepancy WHERE task_item_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    return [
        {
            "model_answer": json.loads(r["model_answer"]),
            "explanation": r["explanation"],
            "uncertainty": r["uncertainty"],
            "revision": r["revision"],
            "key_version": r["key_version"],
            "resolved": r["resolved_at"] is not None,
        }
        for r in rows
    ]
