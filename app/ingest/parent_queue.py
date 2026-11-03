"""Очередь родителя (OH-K5c): проверка выборки, «Снять задание», журнал конвейера."""

import sqlite3

from app.ingest import sampling
from app.ingest.publish import child_view, is_published
from app.ingest.review import SAMPLE_ERROR_NOTE, Refusal
from app.ingest.store import atomic, get_task, mark_sample, new_revision, now, update_task


def _task(conn: sqlite3.Connection, task_id: int):
    task = get_task(conn, task_id)
    if task is None:
        raise Refusal("Задание не найдено.", 404)
    return task


def review_sample(conn: sqlite3.Connection, task_id: int, *, found_error: bool) -> None:
    """Родитель проверил задание из выборки. Ошибка найдена - задание уходит в черновики (как
    любая правка родителя: новая ревизия, ребёнок его больше не видит) и выборка учащается."""
    with atomic(conn):
        task = _task(conn, task_id)
        if task["sample_state"] != "pending" or not is_published(task):
            raise Refusal("Задания нет в выборке или оно уже проверено.", 409)
        if found_error:
            new_revision(conn, task_id, review_note=SAMPLE_ERROR_NOTE)  # пометит ошибку выборки
        else:
            mark_sample(conn, task_id, "ok")


def withdraw(conn: sqlite3.Connection, task_id: int) -> None:
    """«Снять задание»: ребёнок его больше не видит (единый признак публикации), события
    ребёнка остаются и в итогах помечаются «задание снято». Снятое из выборки - найденная ошибка."""
    with atomic(conn):
        task = _task(conn, task_id)
        if not is_published(task):
            raise Refusal("Снять можно только опубликованное задание.", 409)
        update_task(conn, task_id, withdrawn_at=now())
        mark_sample(conn, task_id, "error")


def published(conn: sqlite3.Connection) -> list[dict]:
    """Все задания, которые сейчас видит ребёнок: у каждого можно нажать «Снять задание»."""
    out = []
    for task in conn.execute("SELECT * FROM task_item ORDER BY id").fetchall():
        if not is_published(task):
            continue
        material = conn.execute(
            "SELECT year, original_name FROM material WHERE id = ?", (task["material_id"],)
        ).fetchone()
        out.append(
            {
                "task_id": task["id"],
                "number": task["source_number"],
                "text": " ".join(task["text"].split())[:200],
                "source": child_view(task, material)["source"],
                "without_parent": bool(task["auto_published"] or task["auto_checked"]),
            }
        )
    return out


def _count(conn: sqlite3.Connection, where: str, *args) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM task_item WHERE {where}", args).fetchone()[0]


def journal(conn: sqlite3.Connection) -> dict:
    """Сколько заданий прошло конвейер, сколько не прошло и почему, доля ошибок в выборке."""
    reasons = [
        {"gate": gate, "label": sampling.GATES.get(gate, sampling.GATES["other"]), "count": count}
        for gate, count in conn.execute(
            "SELECT COALESCE(pipeline_gate, 'other'), COUNT(*) FROM task_item "
            "WHERE pipeline_outcome = 'failed' GROUP BY 1 ORDER BY 2 DESC, 1"
        ).fetchall()
    ]
    errors = _count(conn, "sample_state = 'error'")
    reviewed = _count(conn, "sample_state IN ('ok', 'error')")
    return {
        "passed": _count(conn, "pipeline_outcome = 'passed'"),
        "rejected": sum(r["count"] for r in reasons),
        "reasons": reasons,
        "sample": {
            "every": sampling.every(conn),
            "pending": _count(
                conn,
                "sample_state = 'pending' AND import_status = 'checked' AND excluded = 0 "
                "AND withdrawn_at IS NULL",
            ),
            "reviewed": reviewed,
            "errors": errors,
            "error_rate": errors / reviewed if reviewed else None,
            "clean_streak": sampling.clean_streak(conn),
        },
    }
