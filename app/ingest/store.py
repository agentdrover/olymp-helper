"""Доступ к task_item: чтение, ревизии, снимки (SPEC-1.0 1.1, 1.8)."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.db import connect

JSON_COLUMNS = {"options", "answer_key", "model_answer", "figure"}


def open_db(path: str | Path) -> sqlite3.Connection:
    # Соединение живёт один запрос, но FastAPI может выполнить зависимость и маршрут в разных
    # потоках пула; одновременно им пользуется только один.
    conn = connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def now() -> str:
    return datetime.now(UTC).isoformat()


def get_task(conn: sqlite3.Connection, task_id: int):
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute("SELECT * FROM task_item WHERE id = ?", (task_id,)).fetchone()


def figure_of(task) -> dict | None:
    return json.loads(task["figure"]) if task["figure"] else None


def options_of(task):
    return json.loads(task["options"]) if task["options"] else None


def update_task(conn: sqlite3.Connection, task_id: int, **cols) -> None:
    """UPDATE по именованным колонкам; dict и list пишутся как JSON."""
    if not cols:
        return
    values = []
    for name, value in cols.items():
        if name in JSON_COLUMNS and value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        values.append(value)
    sets = ", ".join(f"{name} = ?" for name in cols)
    conn.execute(f"UPDATE task_item SET {sets} WHERE id = ?", (*values, task_id))


def save_snapshot(conn: sqlite3.Connection, task_id: int) -> None:
    """Записать (или обновить) снимок ТЕКУЩЕЙ ревизии: текст, варианты, рисунок, ключ."""
    task = get_task(conn, task_id)
    snapshot = {
        "text": task["text"],
        "options": options_of(task),
        "figure": figure_of(task),
        "answer_key": json.loads(task["answer_key"]) if task["answer_key"] else None,
        "key_source": task["key_source"],
        "model_answer": json.loads(task["model_answer"]) if task["model_answer"] else None,
    }
    conn.execute(
        "INSERT OR REPLACE INTO task_revision (task_item_id, revision, key_version, snapshot, "
        "created_at) VALUES (?, ?, ?, ?, COALESCE((SELECT created_at FROM task_revision "
        "WHERE task_item_id = ? AND revision = ?), ?))",
        (
            task_id,
            task["revision"],
            task["key_version"],
            json.dumps(snapshot, ensure_ascii=False),
            task_id,
            task["revision"],
            now(),
        ),
    )


def new_revision(conn: sqlite3.Connection, task_id: int, **cols) -> int:
    """Правка, создающая новую ревизию: «проверено» снимается, model_answer сбрасывается."""
    task = get_task(conn, task_id)
    status = "draft" if task["import_status"] == "checked" else task["import_status"]
    fields = {"revision": task["revision"] + 1, "import_status": status, "model_answer": None}
    fields.update(cols)
    update_task(conn, task_id, **fields)
    save_snapshot(conn, task_id)
    return task["revision"] + 1
