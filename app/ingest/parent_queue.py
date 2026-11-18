"""Очередь родителя (OH-K5c): проверка выборки, «Снять задание», журнал конвейера."""

import sqlite3

from app.ingest import sampling
from app.ingest.publish import child_view, is_published, leak_notice
from app.ingest.review import SAMPLE_ERROR_NOTE, Refusal
from app.ingest.store import atomic, get_task, new_revision, now, update_task


def _task(conn: sqlite3.Connection, task_id: int):
    task = get_task(conn, task_id)
    if task is None:
        raise Refusal("Задание не найдено.", 404)
    return task


def review_sample(
    conn: sqlite3.Connection, task_id: int, *, revision: int, digest: str, found_error: bool
) -> None:
    """Подтвердить показанную ревизию или вернуть её на правку."""
    with atomic(conn):
        task = _task(conn, task_id)
        if (
            task["import_status"] != "draft"
            or task["auto_hold"] != sampling.HOLD_TEXT_SAMPLE
            or task["excluded"]
            or task["withdrawn_at"]
            or task["revision"] != revision
            or sampling.text_digest(task) != digest
            or not sampling.matches_pending(conn, task_id, revision, digest)
        ):
            raise Refusal("Выборка изменилась: обновите карточку перед подтверждением.", 409)
        if found_error:
            new_revision(conn, task_id, review_note=SAMPLE_ERROR_NOTE)
            return
        if warning := leak_notice(task):
            raise Refusal(warning)
        sampling.parent_outcome(conn, task_id, revision, "clean")
        update_task(conn, task_id, import_status="checked", auto_hold=None)


def withdraw(conn: sqlite3.Connection, task_id: int) -> None:
    """«Снять задание»: ребёнок его больше не видит (единый признак публикации), события
    ребёнка остаются и в итогах помечаются «задание снято». Снятое из выборки - найденная ошибка."""
    with atomic(conn):
        task = _task(conn, task_id)
        if not is_published(task):
            raise Refusal("Снять можно только опубликованное задание.", 409)
        update_task(conn, task_id, withdrawn_at=now())
        sampling.parent_outcome(conn, task_id, task["revision"], "excluded")


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
    sample = sampling.state(conn)
    pending, reviewed, errors = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE s.outcome IS NULL AND s.revision = t.revision "
        "AND t.import_status = 'draft' AND t.excluded = 0 AND t.withdrawn_at IS NULL), "
        "COUNT(s.outcome), COUNT(*) FILTER (WHERE s.outcome IN ('edited', 'excluded')) "
        "FROM pipeline_sample s JOIN task_item t ON t.id = s.task_item_id WHERE s.chosen = 1"
    ).fetchone()
    return {
        "passed": _count(conn, "pipeline_outcome = 'passed'"),
        "rejected": sum(r["count"] for r in reasons),
        "reasons": reasons,
        "sample": {
            "every": sample["interval"],
            "pending": pending,
            "reviewed": reviewed,
            "errors": errors,
            "error_rate": errors / reviewed if reviewed else None,
            "clean_streak": sample["clean_streak"],
        },
    }
