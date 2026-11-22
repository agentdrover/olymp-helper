"""Bind a durable logical operation to individual worker receipts."""

import hashlib
import uuid

from app.ingest.store import atomic
from app.model.client import encode_payload, request_payload
from app.model.types import LIMIT_MESSAGE, BoundaryWait, ModelResult

AMBIGUOUS = {"executor_unreachable", "dispatch_unknown", "exception", "timeout", "internal"}
NOT_SENT = {"route_unavailable", "receipt_capacity", "busy", "faulty"}


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
                    return self.unknown()
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
                with atomic(self.conn):
                    self.conn.execute("DELETE FROM model_dispatch WHERE run_id = ?", (run_id,))
                    self.conn.execute("DELETE FROM model_run WHERE id = ?", (run_id,))
                self.scope.deferred = True
                raise BoundaryWait
            if result.error_kind not in AMBIGUOUS:
                return self.finish(run_id, result)
        return self.reconcile(worker, attempt, run_id)

    def reconcile(self, worker, attempt, run_id):
        cached = self.scope.receipts or {}
        result = cached.get(attempt) if attempt in cached else worker.outcome(attempt, self.model)
        if result is not None and result.error_kind not in AMBIGUOUS:
            return self.finish(run_id, result)
        self.conn.execute(
            "UPDATE model_dispatch SET disposition = 'unknown' WHERE run_id = ?", (run_id,)
        )
        return self.unknown()

    def finish(self, run_id, result):
        with atomic(self.conn):
            self.conn.execute(
                "UPDATE model_dispatch SET disposition = 'confirmed' WHERE run_id = ?", (run_id,)
            )
            self.ledger.finish(run_id, result.ok, result.duration_ms, result.error_kind)
        return result

    def unknown(self):
        self.scope.unknown = True
        raise BoundaryWait
