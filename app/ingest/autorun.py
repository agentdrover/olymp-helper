"""Ручной запуск автопубликации по уже загруженным черновикам (K5b): фоновый поток и ход работы."""

import logging
import threading
from contextlib import closing

from app.ingest.store import atomic, get_task, open_db
from app.model.types import BoundaryWait

log = logging.getLogger("app.ingest")
LIMIT_TEXT = " Дневной лимит запросов к модели исчерпан: остальные задания остаются вам."


def progress_line(checked: int, total: int, published: int, limit: bool) -> str:
    text = (
        f"проверено {checked} из {total}, опубликовано {published}, "
        f"оставлено вам {checked - published}"
    )
    return text + ("." + LIMIT_TEXT if limit else ".")


class AutoRun:
    """Один запуск за раз; повторный запрос во время работы ничего не запускает."""

    def __init__(self, sync: bool = False):
        self.sync = sync
        self._lock = threading.Lock()
        # Один обход черновиков за раз на всё приложение: хук импорта и ручной запуск
        # не гонят модель и подтверждение одного задания параллельно.
        self._exec = threading.Lock()
        self._state: dict = {"state": "idle", "text": ""}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def run_inline(self, ctx, conn, material_id: int | None) -> int:
        """Обход после импорта (в потоке импорта): ждёт своей очереди на общем замке."""
        from app.ingest.autopublish import publish_pending

        with self._exec:
            return publish_pending(ctx, conn, material_id)

    def enqueue_candidates(self, ctx, conn, material_id: int | None, *, force: bool) -> list[int]:
        if not force and not ctx.auto_publish:
            return []
        work = ctx.state.model_work
        ids = []
        with atomic(conn):
            rows = conn.execute(
                "SELECT t.id, t.revision, m.run_id FROM task_item t "
                "LEFT JOIN material m ON m.id = t.material_id "
                "WHERE t.excluded = 0 AND (t.import_status = 'draft' OR "
                "(t.import_status = 'unsupported' AND t.figure IS NOT NULL)) "
                "AND (? IS NULL OR t.material_id = ?) ORDER BY t.id",
                (material_id, material_id),
            )
            for task_id, revision, run_id in rows:
                ids.append(work.submit("autopublish", f"{task_id}:{revision}:{run_id}", conn=conn))
        return ids

    def start(self, ctx, material_id: int | None) -> bool:
        if getattr(getattr(ctx, "state", None), "model_work", None) is not None:
            with closing(open_db(ctx.db_path)) as conn:
                ids = self.enqueue_candidates(ctx, conn, material_id, force=True)
            return bool(ids)
        with self._lock:
            if self._state["state"] == "running":
                return False
            self._state = {"state": "running", "material_id": material_id, "text": "начинаем…"}
        if self.sync:
            self._run(ctx, material_id)
        else:
            threading.Thread(target=self._run, args=(ctx, material_id), daemon=True).start()
        return True

    def run_queued(self, ctx, key: str) -> None:
        from app.ingest.autopublish import try_publish

        task_id, revision, run = key.split(":", 2)
        with closing(open_db(ctx.db_path)) as conn:
            task = get_task(conn, int(task_id), with_import=True)
            if (
                task is not None
                and not task["excluded"]
                and task["import_status"] in ("draft", "unsupported")
                and str(task["revision"]) == revision
                and str(task["import_run_id"]) == run
            ):
                try_publish(ctx, conn, int(task_id), {})

    def model_snapshot(self, ctx) -> dict:
        with closing(open_db(ctx.db_path)) as conn:
            counts = dict(
                conn.execute(
                    "SELECT status, COUNT(*) FROM model_operation "
                    "WHERE kind = 'autopublish' GROUP BY status"
                ).fetchall()
            )
        if counts.get("unknown"):
            return {"state": "running", "text": "Ожидаем подтверждения результата."}
        if counts.get("active"):
            return {"state": "running", "text": "Проверяем задания."}
        if counts.get("queued"):
            return {"state": "running", "text": "Ожидаем исполнителя."}
        return {"state": "done", "text": "Нет заданий в очереди автопубликации."}

    def _run(self, ctx, material_id: int | None) -> None:
        from app.ingest.autopublish import publish_pending

        def report(checked, total, published, limit):
            self._set(text=progress_line(checked, total, published, limit))

        conn = open_db(ctx.db_path)
        try:
            with self._exec:
                publish_pending(ctx, conn, material_id, force=True, report=report)
            with self._lock:
                if self._state["text"] == "начинаем…":
                    self._state["text"] = "Черновиков для проверки нет."
                self._state["state"] = "done"
        except BoundaryWait:
            raise
        except Exception:
            log.exception("manual auto publish failed")
            self._set(state="done", text="Не получилось: подробности в журнале сервера.")
        finally:
            conn.close()
