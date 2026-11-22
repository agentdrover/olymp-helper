"""Run queued model operations without keeping request bodies in admission metadata."""

import fcntl
import logging
import threading
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass

from app.db import connect
from app.ingest.store import atomic
from app.model.admission import Admission
from app.model.client import configured_channel
from app.model.control import EXECUTORS, worker_status, workers_ready
from app.model.dispatch import AMBIGUOUS, record_outcome
from app.model.ledger import Ledger
from app.model.types import BoundaryWait

log = logging.getLogger(__name__)


@dataclass
class OperationScope:
    id: int
    unknown: bool = False
    failed: bool = False
    deferred: bool = False
    ordinal: int = 0
    replayed_call: bool = False
    replay_only: bool = False
    receipts: dict = None


operation = ContextVar("model_operation", default=None)


class ModelWork:
    def __init__(self, path):
        self.path = path
        self.handlers = {}
        self.wake = threading.Event()
        self.stop = threading.Event()
        self._lock = None
        self._thread = None
        self._lifespans = 0

    def start(self, prepare):
        if self._lifespans:
            self._lifespans += 1
            return
        lock = self.path.with_name(self.path.name + ".model-work.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prepare()
            with self._connection() as conn:
                Admission(conn).recover()
        except BaseException:
            lock.close()
            raise
        self._lock = lock
        self._lifespans = 1
        self.stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="model-work")
        self._thread.start()

    def close(self):
        self._lifespans -= 1
        if self._lifespans:
            return
        self.stop.set()
        self.wake.set()
        if self._thread is not None:
            self._thread.join(1)
        # A live thread owns the lock until it stops; don't let another app
        # mark its work uncertain while it can still send a request.
        if self._thread is None or not self._thread.is_alive():
            self._release_lock()

    def _release_lock(self):
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def _connection(self):
        return closing(connect(self.path))

    def submit(self, kind, object_key, *, conn=None):
        if conn is None:
            with self._connection() as opened:
                op_id = Admission(opened).enqueue(kind, str(object_key))
        else:
            op_id = Admission(conn).enqueue(kind, str(object_key))
        self.wake.set()
        return op_id

    def submit_again(self, kind, key: str, *, conn):
        """Explicit retry after a known terminal state, deduping outstanding attempts."""
        with atomic(conn):
            rows = conn.execute(
                "SELECT id, status FROM model_operation WHERE kind = ? "
                "AND (object_key = ? OR object_key GLOB ?) ORDER BY id DESC",
                (kind, key, key + ":retry:*"),
            ).fetchall()
            for op_id, status in rows:
                if status in ("queued", "active", "unknown"):
                    return op_id
            retry_key = f"{key}:retry:{rows[0][0]}" if rows else key
            return self.submit(kind, retry_key, conn=conn)

    def _loop(self):
        try:
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001
                    log.warning("model admission loop refused work: %s", type(exc).__name__)
                self.wake.wait(1)
                self.wake.clear()
        finally:
            self._release_lock()

    def tick(self):
        with self._connection() as conn:
            gate = Admission(conn)
            unknown = gate.unknown()
            if unknown is not None:
                return self._reconcile(conn, gate, unknown)
            if gate.status()["mode"] != "open":
                return False
            first = gate.queued(limit=1)
            if not first or first[0][1] not in self.handlers:
                return False
            if not workers_ready({name: worker_status(name) for name in EXECUTORS}):
                return False
            op_id, kind, key = first[0]
            if self.stop.is_set() or gate.claim(op_id) is None:
                return False
            self._execute(gate, op_id, kind, key)
            return True

    def _reconcile(self, conn, gate, entry):
        op_id, kind, key = entry
        if kind not in self.handlers:
            return False
        attempts = conn.execute(
            "SELECT d.attempt, d.channel, r.model, d.run_id FROM model_dispatch d "
            "JOIN model_run r ON r.id = d.run_id WHERE d.operation_id = ? "
            "ORDER BY d.ordinal",
            (op_id,),
        ).fetchall()
        if not attempts:
            return False
        receipts = {}
        for attempt, channel, model, _ in attempts:
            receipt = configured_channel(channel).outcome(attempt, model)
            if receipt is None or receipt.error_kind in AMBIGUOUS:
                return False
            receipts[attempt] = receipt
        if self.stop.is_set() or not gate.claim_for_reconciliation(op_id):
            return False
        with atomic(conn):
            ledger = Ledger(conn, None)
            for attempt, _, _, run_id in attempts:
                record_outcome(conn, ledger, run_id, receipts[attempt])
        self._execute(gate, op_id, kind, key, receipts=receipts)
        return True

    def _execute(self, gate, op_id, kind, key, *, receipts=None):
        scope = OperationScope(op_id, replay_only=receipts is not None, receipts=receipts)
        token = operation.set(scope)
        try:
            self.handlers[kind](key)
        except BoundaryWait:
            pass
        except Exception as exc:  # noqa: BLE001
            scope.unknown = gate.has_unresolved_dispatch(op_id)
            scope.failed = not scope.unknown
            log.warning("model operation %s stopped: %s", op_id, type(exc).__name__)
        finally:
            operation.reset(token)
            gate.finish(op_id, unknown=scope.unknown, deferred=scope.deferred, failed=scope.failed)
