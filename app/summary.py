"""Итоги занятий и журнал просмотров (OH-T10, SPEC-1.0 1.3а, 1.6, 1.8).

Итог строится только из событий занятия (`event`), а не из текущего состояния прогонов: попытка,
подсказка, показ ответа, переключение канала, спорный ключ. Готовый итог лежит в
`study_session.summary` (JSON) и переживает удаление переписки и срок хранения: переписка - это
события chat и voice, итог их не содержит, а число сообщений записано в него при построении.

Тон: факты и один вывод «к чему вернуться»; похвалы нет (SPEC 1.3, правило 8). Слова об исходе
берутся из `app.tutor.outcomes`, второго словаря нет.
"""

import json
import sqlite3
from datetime import datetime

from app.ingest.attempts import KEY_CORRECTED_NOTE
from app.ingest.store import atomic
from app.sessions import IDLE_TIMEOUT
from app.tutor.guard import render_markdown
from app.tutor.outcomes import outcome_label
from app.tutor.state import TERMINAL, UNVERIFIED, iso

JOURNAL_TYPES = ("parent_opened_chat", "shown_to_parent")
MESSAGE_TYPES = ("chat", "voice")
NOT_ANALYZED = ", не разобрано"
SURE_WRONG = "был уверен, но ошибся"
WAS_WRONG = "была ошибка"
KEYLESS_MISS = "не совпало с ответом модели · не проверено"
SOLVED_BEFORE = "решено в прошлый раз"


# ---------------------------------------------------------------------- построение


def _data(raw) -> dict:
    return json.loads(raw) if raw else {}


def _task_info(conn, task_id: int | None, cache: dict) -> dict:
    if task_id is None:
        return {"number": None, "topic": None}
    if task_id not in cache:
        row = conn.execute(
            "SELECT t.source_number, c.title FROM task_item t "
            "LEFT JOIN topic c ON c.code = t.topic WHERE t.id = ?",
            (task_id,),
        ).fetchone()
        cache[task_id] = (
            {"number": row[0], "topic": row[1]} if row else {"number": None, "topic": None}
        )
    return cache[task_id]


def _attempt_line(data: dict) -> dict:
    correctness = data.get("correctness")
    line = {
        "n": data.get("n"),
        "answer": data.get("answer"),
        "confidence": data.get("confidence"),
        "correctness": correctness,
        "matched": bool(data.get("matched_model")) if correctness == "unknown" else None,
        "revision": data.get("revision"),
        "key_version": data.get("key_version"),
        "note": None,
    }
    if correctness == "unknown":
        line["note"] = UNVERIFIED
        line["result"] = (
            "совпало с ответом модели"
            if data.get("matched_model")
            else "не совпало с ответом модели"
        )
    else:
        line["result"] = "верно" if correctness == "correct" else "не то"
    return line


def _group_outcome(group: dict) -> str:
    shown, hint3, attempts = group["shown"], group["hint3"], group["attempts"]
    if shown is not None:
        return outcome_label(True, "answer_shown", shown.get("attempts", 0), 0) + NOT_ANALYZED
    if hint3 is not None:
        return outcome_label(bool(hint3.get("keyed")), "hint3", len(attempts), 3)
    if attempts:
        last = attempts[-1]
        if last.get("correctness") == "correct":
            return outcome_label(True, "solved", last.get("n", len(attempts)), last.get("hints", 0))
        if last.get("correctness") == "unknown" and last.get("matched_model"):
            return outcome_label(False, "solved_unknown", len(attempts), 0)
    return "разбор не закончен"


def _misses(attempts: list[dict]) -> list[dict]:
    return [
        a for a in attempts
        if a.get("correctness") == "incorrect"
        or (a.get("correctness") == "unknown" and a.get("matched_model") is False)
    ]  # fmt: skip


def _revisit_item(task_id, info, group, outcome) -> dict | None:
    misses = _misses(group["attempts"])
    keyed_misses = [a for a in misses if a.get("correctness") == "incorrect"]
    sure = any(a.get("confidence") == "sure" for a in misses)
    if any(a.get("confidence") == "sure" for a in keyed_misses):
        reason = SURE_WRONG
    elif group["shown"] is not None or group["hint3"] is not None:
        reason = outcome
    elif keyed_misses:
        reason = WAS_WRONG
    elif misses:
        reason = KEYLESS_MISS  # без ключа «ошибся» не говорим (SPEC 1.2)
    elif group["attempts"] and group["attempts"][-1].get("correctness") == "unknown":
        reason = UNVERIFIED
    else:
        return None
    at = misses[-1]["at"] if misses else group["last_at"]
    return {
        "task_id": task_id, "number": info["number"], "reason": reason, "sure_wrong": sure,
        "at": at,
    }  # fmt: skip


def revisit_order(items: list[dict]) -> list[dict]:
    """«Уверен и ошибся» первым, затем остальные по давности (новые выше)."""
    newest_first = sorted(items, key=lambda r: r["at"], reverse=True)
    return sorted(newest_first, key=lambda r: not r["sure_wrong"])


def _conclusion(revisit: list[dict], attempts: int) -> str:
    if revisit:
        top = revisit[0]
        text = f"К чему вернуться: задание {top['number']} ({top['reason']})."
        if len(revisit) > 1:
            text += f" Ещё к повторению: {len(revisit) - 1}."
        return text
    if not attempts:
        return "В этом занятии не было попыток, возвращаться не к чему."
    return "К чему вернуться: по этому занятию ничего не отмечено."


def _group_events(events) -> tuple[dict, list, list, int, int]:
    groups: dict = {}
    fallbacks, disputed = [], []
    messages = explains = 0
    for ev in events:
        data = _data(ev["data"])
        kind, task_id = ev["type"], ev["task_item_id"]
        if kind in MESSAGE_TYPES:
            messages += 1
            continue
        if kind == "fallback":
            fallbacks.append(
                {"from": data.get("from"), "to": data.get("to"), "reason": data.get("reason"),
                 "at": ev["created_at"]}
            )  # fmt: skip
            continue
        if kind in JOURNAL_TYPES:
            continue
        key = (task_id, data.get("run"))
        group = groups.setdefault(
            key,
            {"task_id": task_id, "attempts": [], "hints": [], "hint3": None, "shown": None,
             "explains": 0, "repeat": False, "last_at": ev["created_at"]},
        )  # fmt: skip
        group["last_at"] = ev["created_at"]
        group["repeat"] = group["repeat"] or bool(data.get("repeat"))
        if kind == "attempt":
            group["attempts"].append({**data, "at": ev["created_at"]})
        elif kind == "hint":
            group["hints"].append(data.get("level"))
            if data.get("level") == 3:
                group["hint3"] = data
        elif kind == "answer_shown":
            group["shown"] = data
        elif kind == "explain":
            group["explains"] += 1
            explains += 1
        elif kind == "disputed_key":
            disputed.append({"task_id": task_id, "from_child": bool(data.get("from_child"))})
    return groups, fallbacks, disputed, messages, explains


def build_summary(conn: sqlite3.Connection, session_id: int) -> dict:
    """Итог занятия по его событиям. Состояние прогонов (`tutor_run`) не читается."""
    session = conn.execute("SELECT * FROM study_session WHERE id = ?", (session_id,)).fetchone()
    events = conn.execute(
        "SELECT type, task_item_id, data, created_at FROM event WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    groups, fallbacks, disputed, messages, explains = _group_events(events)
    cache: dict = {}
    tasks, revisit, topics = [], [], []
    totals = {"correct": 0, "wrong": 0, "unknown": 0}
    hints = 0
    for group in groups.values():
        info = _task_info(conn, group["task_id"], cache)
        outcome = _group_outcome(group)
        for a in group["attempts"]:
            c = a.get("correctness")
            totals["correct" if c == "correct" else "wrong" if c == "incorrect" else "unknown"] += 1
        hints += len(group["hints"])
        if info["topic"] and info["topic"] not in topics:
            topics.append(info["topic"])
        item = _revisit_item(group["task_id"], info, group, outcome)
        if item:
            revisit.append(item)
        tasks.append({
            "task_id": group["task_id"], "number": info["number"], "topic": info["topic"],
            "repeat": group["repeat"], "outcome": outcome,
            "attempts": [_attempt_line(a) for a in group["attempts"]],
            "hints": len(group["hints"]), "explains": group["explains"],
            "answer_shown": group["shown"] is not None,
        })  # fmt: skip
    # Одно задание - одна запись «к чему вернуться»: самая свежая.
    latest: dict = {}
    for item in revisit:
        known = latest.get(item["task_id"])
        if known is None or item["at"] >= known["at"]:
            latest[item["task_id"]] = item
    revisit = revisit_order(list(latest.values()))
    n_attempts = sum(totals.values())
    shown = [
        {"task_id": t["task_id"], "number": t["number"], "text": t["outcome"]}
        for t in tasks if t["answer_shown"]
    ]  # fmt: skip
    return {
        "session_id": session_id,
        "started_at": session["started_at"],
        "ended_at": session["ended_at"],
        "end_reason": session["end_reason"],
        "topics": topics,
        "attempts": n_attempts,
        **totals,
        "hints": hints,
        "explains": explains,
        "messages": messages,
        "fallbacks": fallbacks,
        "disputed": [
            {**d, "number": _task_info(conn, d["task_id"], cache)["number"]} for d in disputed
        ],
        "answer_shown": shown,
        "tasks": tasks,
        "revisit": revisit,
        "conclusion": _conclusion(revisit, n_attempts),
    }


# ------------------------------------------------------------------ закрытие, хранение


def _store(conn: sqlite3.Connection, session_id: int) -> dict:
    summary = build_summary(conn, session_id)
    row = conn.execute("SELECT summary FROM study_session WHERE id = ?", (session_id,)).fetchone()
    prior = json.loads(row[0]) if row and row[0] else {}
    if prior.get("partial"):  # сообщения, удалённые до закрытия занятия
        summary["messages"] += prior.get("messages", 0)
    conn.execute(
        "UPDATE study_session SET summary = ? WHERE id = ?",
        (json.dumps(summary, ensure_ascii=False), session_id),
    )
    return summary


def ensure(conn: sqlite3.Connection, now: datetime) -> None:
    """Закрыть простоявшие 30 минут занятия (конец = последнее событие) и построить итоги всем
    закрытым занятиям, у которых его ещё нет. Вкладку могли закрыть: событий больше нет, а
    закрывать занятие некому."""
    conn.row_factory = sqlite3.Row
    with atomic(conn):
        conn.execute(
            "UPDATE study_session SET ended_at = last_activity_at, end_reason = 'idle' "
            "WHERE ended_at IS NULL AND last_activity_at <= ?",
            (iso(now - IDLE_TIMEOUT),),
        )
        for (sid,) in conn.execute(
            "SELECT id FROM study_session WHERE ended_at IS NOT NULL AND summary IS NULL"
        ).fetchall():
            _store(conn, sid)


def running_session(conn: sqlite3.Connection, now: datetime) -> int | None:
    row = conn.execute(
        "SELECT id FROM study_session WHERE ended_at IS NULL AND last_activity_at > ? "
        "ORDER BY id DESC LIMIT 1",
        (iso(now - IDLE_TIMEOUT),),
    ).fetchone()
    return row[0] if row else None


def finish(conn: sqlite3.Connection, now: datetime) -> dict | None:
    """«Закончить»: идущее занятие закрывается сейчас, итог строится сразу. Нет идущего - None."""
    ensure(conn, now)
    with atomic(conn):
        sid = running_session(conn, now)
        if sid is None:
            return None
        conn.execute(
            "UPDATE study_session SET ended_at = ?, end_reason = 'explicit' "
            "WHERE id = ? AND ended_at IS NULL",
            (iso(now), sid),
        )
        return _store(conn, sid)


# ------------------------------------------------------------------------- чтение


def list_sessions(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    """Занятия, новые первыми: закрытые - с итогом, идущее - без него."""
    ensure(conn, now)
    out = []
    running = running_session(conn, now)
    for row in conn.execute(
        "SELECT id, started_at, ended_at, end_reason, summary FROM study_session ORDER BY id DESC"
    ):
        summary = json.loads(row["summary"]) if row["summary"] else None
        if summary and summary.get("partial"):
            summary = None
        out.append({
            "id": row["id"], "started_at": row["started_at"], "ended_at": row["ended_at"],
            "end_reason": row["end_reason"], "running": row["id"] == running,
            "summary": summary,
        })  # fmt: skip
    return out


def get_summary(conn: sqlite3.Connection, session_id: int, now: datetime) -> dict | None:
    ensure(conn, now)
    row = conn.execute("SELECT summary FROM study_session WHERE id = ?", (session_id,)).fetchone()
    found = json.loads(row[0]) if row and row[0] else None
    return None if found and found.get("partial") else found


def overall_revisit(sessions: list[dict]) -> list[dict]:
    """«Стоит вернуться» по всем итогам: одно задание - самая свежая запись."""
    latest: dict = {}
    for s in sessions:
        for item in (s["summary"] or {}).get("revisit", []):
            known = latest.get(item["task_id"])
            if known is None or item["at"] >= known["at"]:
                latest[item["task_id"]] = item
    return revisit_order(list(latest.values()))


# -------------------------------------------------- журнал и просмотр переписки родителем


def journal(conn: sqlite3.Connection) -> list[dict]:
    """Только открытия переписки родителем и показы ребёнком; других записей здесь нет."""
    marks = ",".join("?" for _ in JOURNAL_TYPES)
    out = []
    for row in conn.execute(
        f"SELECT id, session_id, type, created_at FROM event WHERE type IN ({marks}) "
        "ORDER BY id DESC",
        JOURNAL_TYPES,
    ):
        parent = row["type"] == "parent_opened_chat"
        out.append({
            "id": row["id"], "type": row["type"], "session_id": row["session_id"],
            "at": row["created_at"],
            "text": (
                f"Родитель открыл переписку занятия №{row['session_id']}"
                if parent
                else f"Ты показал родителю переписку занятия №{row['session_id']}"
            ),
        })  # fmt: skip
    return out


def session_exists(conn: sqlite3.Connection, session_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM study_session WHERE id = ?", (session_id,)).fetchone()
        is not None
    )


def shown_by_child(conn: sqlite3.Connection, session_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM event WHERE session_id = ? AND type = 'shown_to_parent' LIMIT 1",
            (session_id,),
        ).fetchone()
        is not None
    )


def _log(conn: sqlite3.Connection, session_id: int, type: str, now: datetime) -> None:
    # Закрытое занятие тоже принимает запись журнала: last_activity_at не трогаем.
    conn.execute(
        "INSERT INTO event (session_id, type, data, created_at) VALUES (?, ?, NULL, ?)",
        (session_id, type, iso(now)),
    )


def show_to_parent(conn: sqlite3.Connection, session_id: int, now: datetime) -> bool:
    """Ребёнок открывает переписку занятия родителю. True - запись добавлена сейчас; повторное
    нажатие журнал не раздувает."""
    with atomic(conn):
        if shown_by_child(conn, session_id):
            return False
        _log(conn, session_id, "shown_to_parent", now)
        return True


def log_parent_open(conn: sqlite3.Connection, session_id: int, now: datetime) -> None:
    """Фактический просмотр переписки родителем: каждый раз одна запись."""
    with atomic(conn):
        _log(conn, session_id, "parent_opened_chat", now)


def chat_of(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    """Переписка занятия для родителя: вопросы, ответы помощника и распознанная речь."""
    marks = ",".join("?" for _ in MESSAGE_TYPES)
    out = []
    for row in conn.execute(
        f"SELECT e.type, e.data, e.created_at, t.source_number FROM event e "
        f"LEFT JOIN task_item t ON t.id = e.task_item_id "
        f"WHERE e.session_id = ? AND e.type IN ({marks}) ORDER BY e.id",
        (session_id, *MESSAGE_TYPES),
    ):
        data = _data(row["data"])
        if row["type"] == "voice":
            out.append({"kind": "voice", "number": row["source_number"], "at": row["created_at"],
                        "question": data.get("text", ""), "reply_html": None})  # fmt: skip
        else:
            out.append({
                "kind": "chat", "number": row["source_number"], "at": row["created_at"],
                "question": data.get("question", ""),
                "reply_html": render_markdown(data.get("reply") or ""),
            })  # fmt: skip
    return out


def preserve_message_counts(
    conn: sqlite3.Connection, session_id: int | None = None, before: str | None = None
) -> None:
    """Перед удалением переписки: число удаляемых сообщений запоминается в занятии. Закрытое
    занятие уже с итогом; у идущего итога ещё нет - факт ждёт закрытия и вливается в итог."""
    marks = ",".join("?" for _ in MESSAGE_TYPES)
    sql = (
        f"SELECT session_id, COUNT(*) FROM event WHERE type IN ({marks})"
        + (" AND session_id = ?" if session_id is not None else "")
        + (" AND created_at < ?" if before is not None else "")
        + " GROUP BY session_id"
    )
    args = [*MESSAGE_TYPES, *([session_id] if session_id is not None else []),
            *([before] if before is not None else [])]  # fmt: skip
    with atomic(conn):
        for sid, count in conn.execute(sql, args).fetchall():
            row = conn.execute(
                "SELECT ended_at, summary FROM study_session WHERE id = ?", (sid,)
            ).fetchone()
            if row[0] is not None and row[1] is not None and not json.loads(row[1]).get("partial"):
                continue  # итог закрытого занятия уже хранит число
            prior = json.loads(row[1]) if row[1] else {}
            total = prior.get("messages", 0) if prior.get("partial") else 0
            conn.execute(
                "UPDATE study_session SET summary = ? WHERE id = ?",
                (json.dumps({"partial": True, "messages": total + count}), sid),
            )


# --------------------------------------------------------- представление для показа


def _current_terminal(conn: sqlite3.Connection, task_id: int | None) -> bool:
    if task_id is None:
        return True
    row = conn.execute(
        "SELECT state FROM tutor_run WHERE task_item_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    return row is not None and row[0] in TERMINAL


def present(conn: sqlite3.Connection, found: dict, *, child: bool) -> dict:
    """Итог для показа. Ключ могли исправить после попытки (SPEC 1.1) - пометка. Ребёнку, пока
    текущий прогон задания не закрыт (повтор), верные ответы старых попыток не показываются
    (SPEC 1.3, правило 3): остаётся формулировка «решено в прошлый раз»."""
    out = json.loads(json.dumps(found))
    for task in out["tasks"]:
        task["key_note"] = None
        current = None
        if task["task_id"] is not None:
            row = conn.execute(
                "SELECT key_version FROM task_item WHERE id = ?", (task["task_id"],)
            ).fetchone()
            current = row[0] if row else None
        if current is not None and any(
            a.get("key_version") is not None and a["key_version"] < current
            for a in task["attempts"]
        ):
            task["key_note"] = KEY_CORRECTED_NOTE
        if child and not _current_terminal(conn, task["task_id"]):
            for a in task["attempts"]:
                if a["correctness"] == "correct" or a.get("matched"):
                    a["answer"], a["result"] = None, SOLVED_BEFORE
            if task["outcome"].startswith(("верно", "совпало")):
                task["outcome"] = SOLVED_BEFORE
    return out
