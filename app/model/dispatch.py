"""Bind a durable logical operation to individual worker receipts."""

import hashlib
import uuid

from app.ingest.store import atomic
from app.model.client import encode_payload, request_payload
from app.model.types import LIMIT_MESSAGE, BoundaryStale, BoundaryWait, ModelResult

AMBIGUOUS = {"executor_unreachable", "dispatch_unknown", "exception", "timeout", "internal"}
NOT_SENT = {"route_unavailable", "receipt_capacity", "busy", "faulty"}


def record_outcome(conn, ledger, run_id, result):
    with atomic(conn):
        conn.execute(
            "UPDATE model_dispatch SET disposition = 'confirmed' WHERE run_id = ?", (run_id,)
        )
        ledger.finish(run_id, result.ok, result.duration_ms, result.error_kind)


def remove_unspent(conn, run_id):
    with atomic(conn):
        conn.execute("DELETE FROM model_dispatch WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM model_run WHERE id = ?", (run_id,))


class Dispatch:
    def __init__(self, conn, ledger, scope, channel, model):
        self.conn = conn
        self.ledger = ledger
        self.scope = scope
        self.channel = channel
        self.model = model
        self.ordinal = scope.ordinal + 1
        scope.ordinal = self.ordinal

    def call(self, worker, system, messages, schema, fallback, images):
        digest = hashlib.sha256(
            encode_payload(
                request_payload(self.channel, self.model, system, messages, schema, images)
            )
        ).hexdigest()
        with atomic(self.conn):
            prior = self.conn.execute(
                "SELECT attempt, channel, request_sha256, run_id FROM model_dispatch "
                "WHERE operation_id = ? AND ordinal = ?",
                (self.scope.id, self.ordinal),
            ).fetchone()
            self.scope.replayed_call = prior is not None
            if prior is not None:
                attempt, channel, expected, run_id = prior
                if (channel, expected) != (self.channel, digest):
                    self.scope.failed = True
                    raise BoundaryStale
            else:
                if self.scope.replay_only:
                    self.scope.deferred = True
                    raise BoundaryWait
                run_id = self.ledger.reserve(self.channel, self.model, fallback)
                if run_id is None:
                    return ModelResult.failure(
                        self.channel, self.model, "limit", message=LIMIT_MESSAGE
                    )
                attempt = uuid.uuid4().hex
                self.conn.execute(
                    "INSERT INTO model_dispatch "
                    "(operation_id, ordinal, attempt, channel, request_sha256, run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (self.scope.id, self.ordinal, attempt, self.channel, digest, run_id),
                )
        if prior is None:
            result = worker.complete(
                system, messages, schema, model=self.model, images=images, attempt=attempt
            )
            if result.error_kind in NOT_SENT:
                return self.defer(run_id)
            if result.error_kind not in AMBIGUOUS:
                return self.finish(run_id, result)
        return self.reconcile(worker, attempt, run_id)

    def reconcile(self, worker, attempt, run_id):
        cached = self.scope.receipts or {}
        result = cached.get(attempt) if attempt in cached else worker.outcome(attempt, self.model)
        if result is not None and result.error_kind in NOT_SENT:
            return self.defer(run_id)
        if result is not None and result.error_kind not in AMBIGUOUS:
            return self.finish(run_id, result)
        self.conn.execute(
            "UPDATE model_dispatch SET disposition = 'unknown' WHERE run_id = ?", (run_id,)
        )
        return self.unknown()

    def defer(self, run_id):
        remove_unspent(self.conn, run_id)
        self.scope.deferred = True
        raise BoundaryWait

    def finish(self, run_id, result):
        record_outcome(self.conn, self.ledger, run_id, result)
        return result

    def unknown(self):
        self.scope.unknown = True
        raise BoundaryWait
