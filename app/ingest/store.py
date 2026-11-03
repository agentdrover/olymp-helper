"""Доступ к task_item: чтение, ревизии, снимки (SPEC-1.0 1.1, 1.8)."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.db import connect
from app.tutor.seq import bump

JSON_COLUMNS = {"options", "answer_key", "model_answer", "figure"}


def open_db(path: str | Path) -> sqlite3.Connection:
    # Соединение живёт один запрос, но FastAPI может выполнить зависимость и маршрут в разных
    # потоках пула; одновременно им пользуется только один.
    conn = connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def atomic(conn: sqlite3.Connection):
    """Правка и проверка рисунков, которые от неё зависят, - одна транзакция: при отказе проверки
    не остаётся ни правки, ни опубликованного рисунка с устаревшим решением. Внутри уже открытой
    транзакции владелец - вызывающий."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def now() -> str:
    return datetime.now(UTC).isoformat()


def get_task(conn: sqlite3.Connection, task_id: int, *, with_import: bool = False):
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    if with_import:
        return cur.execute(
            "SELECT t.*, m.run_id AS import_run_id FROM task_item t "
            "LEFT JOIN material m ON m.id=t.material_id WHERE t.id=?",
            (task_id,),
        ).fetchone()
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
    with atomic(conn):
        conn.execute(f"UPDATE task_item SET {sets} WHERE id = ?", (*values, task_id))
        bump(conn)  # правка родителя (ключ, ревизия, подтверждение, рисунок) меняет экран ребёнка


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


def mark_sample(conn: sqlite3.Connection, task_id: int, state: str) -> bool:
    """Исход проверки родителем задания из выборки: 'ok' (только из 'pending') или 'error' (из
    'pending' и из принятого 'ok': ошибку находят и позже). Номер проверки задаёт порядок, по
    нему считается серия без ошибок. True - исход записан."""
    before = ("pending",) if state == "ok" else ("pending", "ok")
    marks = ",".join("?" * len(before))
    cur = conn.execute(
        "UPDATE task_item SET sample_state = ?, sample_review_no = "
        "(SELECT COALESCE(MAX(sample_review_no), 0) + 1 FROM task_item) "
        f"WHERE id = ? AND sample_state IN ({marks})",
        (state, task_id, *before),
    )
    return cur.rowcount > 0


def new_revision(conn: sqlite3.Connection, task_id: int, **cols) -> int:
    """Правка, создающая новую ревизию: «проверено» снимается, model_answer сбрасывается."""
    task = get_task(conn, task_id)
    status = "draft" if task["import_status"] == "checked" else task["import_status"]
    fields = {
        "revision": task["revision"] + 1,
        "import_status": status,
        "model_answer": None,
        "auto_published": 0,
        "auto_hold": None,
    }
    if status == "draft":  # правка родителя: «проверено двумя моделями» больше не про это задание
        fields["auto_checked"] = 0
    fields.update(cols)
    update_task(conn, task_id, **fields)
    mark_sample(conn, task_id, "error")  # родитель правит задание из выборки: нашёл ошибку (K5c)
    save_snapshot(conn, task_id)
    return task["revision"] + 1
