"""Переписка занятия: события chat (SPEC-1.8) этого занятия и этого задания.

Одно событие chat - один обмен: вопрос ученика и ответ помощника (или общий текст, если ответ
не показан). Сообщения других занятий и других заданий в запрос к модели не попадают (SPEC 1.6).
"""

import json
from datetime import datetime

from app.sessions import IDLE_TIMEOUT
from app.tutor.guard import render_markdown
from app.tutor.state import iso

HISTORY_FOR_MODEL = 4  # последних обменов этого занятия и задания - модели
HISTORY_ON_SCREEN = 20


def open_session(conn, now: datetime) -> int | None:
    """Идущее занятие (без записи в базу); простоявшее 30 минут уже не идёт."""
    row = conn.execute(
        "SELECT id FROM study_session WHERE ended_at IS NULL AND last_activity_at > ? "
        "ORDER BY id DESC LIMIT 1",
        (iso(now - IDLE_TIMEOUT),),
    ).fetchone()
    return row[0] if row else None


def exchanges(conn, session_id: int | None, task_id: int, limit: int) -> list[dict]:
    if session_id is None:
        return []
    rows = conn.execute(
        "SELECT data FROM event WHERE type = 'chat' AND session_id = ? AND task_item_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (session_id, task_id, limit),
    ).fetchall()
    return [json.loads(r[0]) for r in reversed(rows) if r[0]]


def history_for_model(conn, session_id: int | None, task_id: int) -> list[dict]:
    return [
        {"question": x["question"], "reply": x["reply"]}
        for x in exchanges(conn, session_id, task_id, HISTORY_FOR_MODEL)
    ]


def screen_items(conn, session_id: int | None, task_id: int) -> list[dict]:
    """Что показать в панели: ответ - безопасный HTML из Markdown, подсказки - обычный текст."""
    return [
        {
            "mode": x["mode"],
            "question": x["question"],
            "reply": render_markdown(x["reply"]),
            "citations": x.get("citations", []),
            "suggestions": x.get("suggestions", []),
            "withheld": bool(x.get("withheld")),
        }
        for x in exchanges(conn, session_id, task_id, HISTORY_ON_SCREEN)
    ]


def compose_of(items: list[dict]) -> list[str]:
    """Готовые формулировки последнего обмена: в поле для правки, не в отправку (SPEC 1.3 п. 8)."""
    if items and items[-1]["mode"] == "formulate" and not items[-1]["withheld"]:
        return list(items[-1]["suggestions"])
    return []
