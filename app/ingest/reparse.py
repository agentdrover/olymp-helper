"""«Разобрать заново» (SPEC-1.0, импорт): повторный разбор сборника, пока родитель его не трогал.

Безопасный случай один: ни одно задание материала не проверено, не правилось, не исключено,
без подтверждённого рисунка и без единой попытки ребёнка. Иначе разбор заново стёр бы работу."""

import json
import logging
import sqlite3

from app.ingest.review import Refusal
from app.ingest.store import atomic

log = logging.getLogger("app.ingest")
TOUCHED = (
    "SELECT DISTINCT t.material_id FROM task_item t WHERE t.material_id {scope} AND ("
    "t.import_status = 'checked' OR t.revision > 1 OR t.excluded = 1 "
    "OR t.key_source = 'parent' OR t.key_version > 1 OR t.discrepancy_ack IS NOT NULL "
    "OR COALESCE(json_extract(t.figure, '$.confirmed'), 0) = 1 "
    "OR EXISTS (SELECT 1 FROM event e WHERE e.task_item_id = t.id) "
    "OR EXISTS (SELECT 1 FROM tutor_run r WHERE r.task_item_id = t.id) "
    "OR EXISTS (SELECT 1 FROM tutor_discrepancy d WHERE d.task_item_id = t.id))"
)
UNAVAILABLE = "Есть проверенные задания — разбор заново недоступен"


def touched_materials(conn: sqlite3.Connection, material_id: int | None = None) -> set[int]:
    """Материалы, чьи задания родитель или ребёнок уже трогал; material_id - только этот."""
    if material_id is None:
        rows = conn.execute(TOUCHED.format(scope="IS NOT NULL"))
    else:
        rows = conn.execute(TOUCHED.format(scope="= ?"), (material_id,))
    return {r[0] for r in rows}


def _figure_files(ctx, conn, ids: list[int]) -> set:
    files = set()
    for task_id, figure in conn.execute(
        f"SELECT id, figure FROM task_item WHERE id IN ({','.join('?' * len(ids))})", ids
    ):
        relative = json.loads(figure).get("file") if figure else None
        if relative:
            files.add(ctx.material_path(relative))
        files.update(ctx.material_path("figures").glob(f"{task_id}-r*.png"))
    return files


def reset_material(ctx, conn: sqlite3.Connection, material_id: int) -> bool:
    """Одна транзакция: проверка условия, удаление заданий материала (вместе с ревизиями,
    рамками и рисунками) и status=queued. True - разбор нужно запустить; False - он уже идёт.
    Отказ - Refusal 409 (ничего не удалено)."""
    with atomic(conn):
        row = conn.execute(
            "SELECT kind, import_status, run_id, deleted_at FROM material WHERE id = ?",
            (material_id,),
        ).fetchone()
        if row is None:
            raise Refusal("Материал не найден", 404)
        if row["deleted_at"] is not None:
            raise Refusal("Файл материала удалён: разобрать заново нельзя.", 409)
        if row["kind"] != "tasks":
            raise Refusal("Разобрать заново можно только сборник заданий.", 409)
        if row["import_status"] == "running" or (
            row["import_status"] == "queued"
            and ctx.progress.get(material_id, row["run_id"]) is not None
        ):
            return False  # разбор идёт
        # queued без записи хода - зависший материал (запуск потерян): запускаем снова
        if touched_materials(conn, material_id):
            raise Refusal(UNAVAILABLE + ".", 409)
        ids = [
            r[0]
            for r in conn.execute("SELECT id FROM task_item WHERE material_id = ?", (material_id,))
        ]
        files = _figure_files(ctx, conn, ids) if ids else set()
        # task_revision, tutor_run и tutor_discrepancy уходят каскадом; у untouched-заданий
        # событий ребёнка нет по определению
        conn.execute("DELETE FROM task_item WHERE material_id = ?", (material_id,))
        conn.execute(
            "UPDATE material SET import_status = 'queued', import_error = NULL WHERE id = ?",
            (material_id,),
        )
    for path in files:  # файлы - после фиксации: откат транзакции не должен их терять
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # уборка файлов не должна блокировать разбор
            log.warning("reparse: figure file %s not removed: %s", path, exc)
    return True
