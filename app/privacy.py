"""Данные ребёнка (SPEC 1.6): удаление переписки и материалов, срок хранения 180 дней.

Переписка - события занятия типов chat и voice (SPEC 1.8: `event.type`; отдельной таблицы
сообщений в SPEC нет). Итог занятия (`study_session.summary`) и остальные события - попытки,
подсказки, журнал показов родителю - переписку не включают и удаляются отдельно только вместе с
занятием, а занятие не удаляется никогда: итоги хранятся бессрочно.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import summary
from app.ingest.store import atomic

RETENTION_DAYS = 180
MESSAGE_TYPES = ("chat", "voice")
_IN = ",".join("?" for _ in MESSAGE_TYPES)

# Задание, которое нельзя удалять вместе с материалом: подтверждено сейчас или на него есть события
# (попытки ребёнка). Ревизия не признак: правка текста черновика тоже поднимает ревизию.
# Условие читает только факты в базе.
KEEP = "(t.import_status = 'checked' OR EXISTS (SELECT 1 FROM event e WHERE e.task_item_id = t.id))"


class MaterialBusy(Exception):
    """Импорт материала ещё идёт: удалять нельзя, пока поток пишет его страницы."""


def message_count(conn: sqlite3.Connection, session_id: int | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM event WHERE type IN ({_IN})"
    args: list = list(MESSAGE_TYPES)
    if session_id is not None:
        sql += " AND session_id = ?"
        args.append(session_id)
    return conn.execute(sql, args).fetchone()[0]


def conversations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.id, s.started_at, s.ended_at, s.summary IS NOT NULL AS has_summary, "
        f"(SELECT COUNT(*) FROM event e WHERE e.session_id = s.id AND e.type IN ({_IN})) "
        "AS messages FROM study_session s ORDER BY s.id DESC",
        MESSAGE_TYPES,
    ).fetchall()


def session_exists(conn: sqlite3.Connection, session_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM study_session WHERE id = ?", (session_id,)).fetchone()
        is not None
    )


def delete_messages(
    conn: sqlite3.Connection, session_id: int | None = None, now: datetime | None = None
) -> int:
    # Итоги, которых ещё нет, строятся до удаления: число сообщений берётся из событий.
    now = now or datetime.now(UTC)
    summary.ensure(conn, now)
    sql = f"DELETE FROM event WHERE type IN ({_IN})"
    args: list = list(MESSAGE_TYPES)
    if session_id is not None:
        sql += " AND session_id = ?"
        args.append(session_id)
    with atomic(conn):  # счёт и удаление - одна транзакция: сообщение между ними не теряется
        summary.preserve_message_counts(conn, session_id)
        return conn.execute(sql, args).rowcount


def cleanup_retention(conn: sqlite3.Connection, now: datetime) -> int:
    """Сообщения старше 180 дней удаляются; занятия, итоги и прочие события остаются."""
    summary.ensure(conn, now)
    cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
    with atomic(conn):
        summary.preserve_message_counts(conn, before=cutoff)
        return conn.execute(
            f"DELETE FROM event WHERE type IN ({_IN}) AND created_at < ?",
            (*MESSAGE_TYPES, cutoff),
        ).rowcount


# -------------------------------------------------------------------------- материалы


def material_summary(conn: sqlite3.Connection, material_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, kind, year, variant, import_status, deleted_at, "
        "(SELECT COUNT(*) FROM material_page_text p WHERE p.material_id = material.id) AS pages, "
        "(SELECT COUNT(*) FROM task_item t WHERE t.material_id = material.id "
        f" AND NOT {KEEP}) AS unconfirmed, "
        "(SELECT COUNT(*) FROM task_item t WHERE t.material_id = material.id "
        f" AND {KEEP}) AS confirmed "
        "FROM material WHERE id = ?",
        (material_id,),
    ).fetchone()
    return None if row is None else dict(row)


def _inside(root: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    path = (root / relative).resolve()
    return path if path.is_relative_to(root.resolve()) else None


def delete_material(ctx, conn: sqlite3.Connection, material_id: int) -> dict:
    """Файл, страницы поиска и ключи материала, неподтверждённые задания (и их вырезки).

    Подтверждённые задания остаются: ребёнок мог их решать, а попытки ссылаются на них.
    Нет подтверждённых - удаляется и сама строка, и тот же файл можно загрузить снова."""
    row = conn.execute(
        "SELECT file, sha256, import_status FROM material WHERE id = ?", (material_id,)
    ).fetchone()
    if row is None:
        raise LookupError(material_id)
    if row["import_status"] in ("queued", "running"):
        raise MaterialBusy(material_id)
    figures = []
    for (raw,) in conn.execute(
        f"SELECT t.figure FROM task_item t WHERE t.material_id = ? AND NOT {KEEP} "
        "AND t.figure IS NOT NULL",
        (material_id,),
    ):
        figures.append(json.loads(raw).get("file"))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM material_page WHERE material_id = ?", (material_id,))
        conn.execute("DELETE FROM material_page_text WHERE material_id = ?", (material_id,))
        conn.execute("DELETE FROM key_entry WHERE material_id = ?", (material_id,))
        tasks = conn.execute(
            f"DELETE FROM task_item WHERE material_id = ? AND id IN "
            f"(SELECT t.id FROM task_item t WHERE t.material_id = ? AND NOT {KEEP})",
            (material_id, material_id),
        ).rowcount
        kept = conn.execute(
            "SELECT COUNT(*) FROM task_item WHERE material_id = ?", (material_id,)
        ).fetchone()[0]
        if kept:
            conn.execute(
                "UPDATE material SET deleted_at = ?, pages_total = 0 WHERE id = ?",
                (datetime_now_iso(ctx), material_id),
            )
        else:
            conn.execute("DELETE FROM material WHERE id = ?", (material_id,))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    # Файлы - после фиксации базы: сбой посреди удаления не оставит базу, ссылающуюся на файл.
    root = ctx.data_dir
    for relative in [row["file"], *figures]:
        path = _inside(root, relative)
        if path is not None:
            path.unlink(missing_ok=True)
    ctx.page_cache.discard(row["sha256"])
    return {"tasks": tasks, "kept": kept}


def datetime_now_iso(ctx) -> str:
    return ctx.state.clock().isoformat()
