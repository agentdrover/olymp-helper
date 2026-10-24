"""Попытки ссылаются на ревизию и версию ключа (SPEC-1.0 1.1); T06 пишет событие attempt."""

import json

from app.ingest.answers import display
from app.ingest.store import get_task

KEY_CORRECTED_NOTE = "ключ исправлен после попытки"


def attempt_ref(conn, task_id: int) -> dict:
    """Что записать в data события attempt, чтобы итоги знали, по какому ключу оценено."""
    task = get_task(conn, task_id)
    return {"revision": task["revision"], "key_version": task["key_version"]}


def describe_attempt(conn, event: dict) -> dict:
    """Условие и ключ ТОЙ ревизии, по которой решал ребёнок, и пометка об исправленном ключе."""
    data = event["data"] or {}
    task_id = event["task_item_id"]
    snap_row = conn.execute(
        "SELECT snapshot FROM task_revision WHERE task_item_id = ? AND revision = ?",
        (task_id, data.get("revision")),
    ).fetchone()
    snapshot = json.loads(snap_row[0]) if snap_row else {}
    key = snapshot.get("answer_key")
    current = get_task(conn, task_id)
    corrected = current is not None and current["key_version"] > data.get("key_version", 0)
    return {
        "revision": data.get("revision"),
        "text": snapshot.get("text"),
        "graded_by_key": display(key["value"]) if key else None,
        "key_corrected_after_attempt": corrected,
        "note": KEY_CORRECTED_NOTE if corrected else None,
    }
