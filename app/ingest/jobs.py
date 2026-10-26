"""Фоновый импорт со статусом и таймаутом (SPEC-1.0 1.7): HTTP не блокируется."""

import logging
import threading
import time
import uuid

from app.ingest.importer import fail_material, run_import
from app.ingest.limits import IMPORT_MAX_BASE_S, IMPORT_MAX_PER_CHUNK_S, IMPORT_STALL_S
from app.ingest.store import open_db

log = logging.getLogger("app.ingest")
TIMEOUT_MESSAGE = "Импорт прерван: превышено время ожидания. Повторите загрузку."


class ImportJobs:
    def __init__(
        self,
        ctx,
        *,
        sync: bool = False,
        stall: float = IMPORT_STALL_S,
        max_base: float = IMPORT_MAX_BASE_S,
        max_per_chunk: float = IMPORT_MAX_PER_CHUNK_S,
    ):
        self.ctx = ctx
        self.sync = sync
        self.stall = stall
        self.max_base = max_base
        self.max_per_chunk = max_per_chunk
        self._tick = max(0.02, min(1.0, stall / 5))
        self._threads: list[threading.Thread] = []

    def _new_run(self, material_id: int) -> str:
        """Новый запуск получает свой run_id: результат пишет только его владелец."""
        run_id = uuid.uuid4().hex
        conn = open_db(self.ctx.db_path)
        try:
            conn.execute("UPDATE material SET run_id = ? WHERE id = ?", (run_id, material_id))
        finally:
            conn.close()
        return run_id

    def submit(self, material_id: int) -> None:
        run_id = self._new_run(material_id)
        if self.sync:
            run_import(self.ctx, material_id, threading.Event(), run_id)
            return
        cancel = threading.Event()
        worker = threading.Thread(
            target=run_import,
            args=(self.ctx, material_id, cancel, run_id),
            daemon=True,
            name=f"import-{material_id}",
        )
        self._threads.append(worker)
        worker.start()
        threading.Thread(
            target=self._watch, args=(worker, material_id, cancel, run_id), daemon=True
        ).start()

    def _watch(self, worker, material_id: int, cancel: threading.Event, run_id: str) -> None:
        """Прервать запуск, который молчит дольше stall или идёт дольше потолка по размеру.
        Пульс - отчёт о ходе (ImportProgress); отчёт истёкшего запуска записи не оживляет."""
        started = last = time.monotonic()
        while True:
            worker.join(self._tick)
            if not worker.is_alive():
                return
            now = time.monotonic()
            item = self.ctx.progress.snapshot(material_id, run_id)
            if item is not None:
                last = item["beat"]
            cap = self.max_base + self.max_per_chunk * (item["cap_chunks"] if item else 0)
            if now - last > self.stall or now - started > cap:
                self._expire(material_id, cancel, run_id, item, now - last)
                return

    def _expire(self, material_id, cancel, run_id, item, silent_s: float) -> None:
        cancel.set()
        log.warning(
            "import expired: material=%s run=%s phase=%s chunk=%s/%s silent_s=%.1f",
            material_id,
            run_id,
            item["phase"] if item else "start",
            item["chunk"] if item else 0,
            item["chunks"] if item else 0,
            silent_s,
        )
        self.ctx.progress.clear(material_id, run_id)
        conn = open_db(self.ctx.db_path)
        try:
            fail_material(conn, material_id, run_id, TIMEOUT_MESSAGE)
        finally:
            conn.close()

    def wait_idle(self, seconds: float = 10) -> None:
        for thread in list(self._threads):
            thread.join(seconds)
