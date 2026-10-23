"""Фоновый импорт со статусом и таймаутом (SPEC-1.0 1.7): HTTP не блокируется."""

import logging
import threading

from app.ingest.importer import fail_material, run_import
from app.ingest.limits import IMPORT_TIMEOUT_S
from app.ingest.store import open_db

log = logging.getLogger("app.ingest")
TIMEOUT_MESSAGE = "Импорт прерван: превышено время ожидания. Повторите загрузку."


class ImportJobs:
    def __init__(self, ctx, *, sync: bool = False, timeout: float = IMPORT_TIMEOUT_S):
        self.ctx = ctx
        self.sync = sync
        self.timeout = timeout
        self._threads: list[threading.Thread] = []

    def submit(self, material_id: int) -> None:
        if self.sync:
            run_import(self.ctx, material_id, threading.Event())
            return
        cancel = threading.Event()
        worker = threading.Thread(
            target=run_import,
            args=(self.ctx, material_id, cancel),
            daemon=True,
            name=f"import-{material_id}",
        )
        watchdog = threading.Timer(self.timeout, self._expire, args=(material_id, cancel))
        watchdog.daemon = True
        self._threads.append(worker)
        worker.start()
        watchdog.start()
        threading.Thread(
            target=self._cancel_timer_when_done, args=(worker, watchdog), daemon=True
        ).start()

    def _cancel_timer_when_done(self, worker, watchdog) -> None:
        worker.join()
        watchdog.cancel()

    def _expire(self, material_id: int, cancel: threading.Event) -> None:
        cancel.set()
        conn = open_db(self.ctx.db_path)
        try:
            fail_material(conn, material_id, TIMEOUT_MESSAGE)
        finally:
            conn.close()

    def wait_idle(self, seconds: float = 10) -> None:
        for thread in list(self._threads):
            thread.join(seconds)
