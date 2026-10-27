"""Ручной запуск автопубликации по уже загруженным черновикам (K5b): фоновый поток и ход работы."""

import logging
import threading

from app.ingest.store import open_db

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
        self._state: dict = {"state": "idle", "text": ""}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def start(self, ctx, material_id: int | None) -> bool:
        with self._lock:
            if self._state["state"] == "running":
                return False
            self._state = {"state": "running", "material_id": material_id, "text": "начинаем…"}
        if self.sync:
            self._run(ctx, material_id)
        else:
            threading.Thread(target=self._run, args=(ctx, material_id), daemon=True).start()
        return True

    def _run(self, ctx, material_id: int | None) -> None:
        from app.ingest.autopublish import publish_pending

        def report(checked, total, published, limit):
            self._set(text=progress_line(checked, total, published, limit))

        conn = open_db(ctx.db_path)
        try:
            publish_pending(ctx, conn, material_id, force=True, report=report)
            with self._lock:
                if self._state["text"] == "начинаем…":
                    self._state["text"] = "Черновиков для проверки нет."
                self._state["state"] = "done"
        except Exception:
            log.exception("manual auto publish failed")
            self._set(state="done", text="Не получилось: подробности в журнале сервера.")
        finally:
            conn.close()
