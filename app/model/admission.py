"""SQLite admission for logical work across both model executors."""

from contextlib import contextmanager


@contextmanager
def write(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


class Admission:
    def __init__(self, conn):
        self.conn = conn

    def enqueue(self, kind: str, object_key: str) -> int:
        self.conn.execute(
            "INSERT OR IGNORE INTO model_operation (kind, object_key) VALUES (?, ?)",
            (kind, object_key),
        )
        return self.conn.execute(
            "SELECT id FROM model_operation WHERE kind = ? AND object_key = ?", (kind, object_key)
        ).fetchone()[0]

    def queued(self, limit=100):
        return self.conn.execute(
            "SELECT id, kind, object_key FROM model_operation "
            "WHERE status = 'queued' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def claim(self, op_id: int):
        with write(self.conn):
            row = self.conn.execute(
                "UPDATE model_operation SET status = 'active' WHERE id = ? AND status = 'queued' "
                "AND (SELECT accepting FROM model_admission WHERE id = 1) = 1 "
                "AND NOT EXISTS (SELECT 1 FROM model_operation WHERE status IN ('active', 'unknown')) "
                "RETURNING kind, object_key",
                (op_id,),
            ).fetchone()
        return tuple(row) if row is not None else None

    def pause(self):
        self.conn.execute(
            "UPDATE model_admission SET accepting = 0, revision = revision + 1 "
            "WHERE id = 1 AND accepting = 1"
        )

    def resume(self, expected=None) -> bool:
        with write(self.conn):
            accepting, revision = self.conn.execute(
                "SELECT accepting, revision FROM model_admission WHERE id = 1"
            ).fetchone()
            if expected is not None and revision != expected:
                return bool(accepting and revision == expected + 1)
            if not accepting:
                changed = self.conn.execute(
                    "UPDATE model_admission SET accepting = 1, revision = revision + 1 "
                    "WHERE id = 1 AND NOT EXISTS (SELECT 1 FROM model_operation "
                    "WHERE status IN ('active', 'unknown'))"
                )
                return changed.rowcount == 1
        return True

    def recover(self):
        # An active process may have sent a call before committing an answer.
        # Never return it to the queue merely because its caller disappeared.
        self.conn.execute(
            "UPDATE model_operation SET status = CASE WHEN EXISTS ("
            "SELECT 1 FROM model_dispatch d WHERE d.operation_id = model_operation.id"
            ") THEN 'unknown' ELSE 'queued' END WHERE status = 'active'"
        )

    def unknown(self):
        return self.conn.execute(
            "SELECT id, kind, object_key FROM model_operation "
            "WHERE status = 'unknown' ORDER BY id LIMIT 1"
        ).fetchone()

    def claim_for_reconciliation(self, op_id: int) -> bool:
        with write(self.conn):
            return (
                self.conn.execute(
                    "UPDATE model_operation SET status = 'active' WHERE id = ? AND status = 'unknown' "
                    "AND NOT EXISTS (SELECT 1 FROM model_operation WHERE status = 'active')",
                    (op_id,),
                ).rowcount
                == 1
            )

    def has_unresolved_dispatch(self, op_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM model_dispatch WHERE operation_id = ? AND disposition <> 'confirmed'",
                (op_id,),
            ).fetchone()
            is not None
        )

    def finish(self, op_id: int, *, unknown=False, deferred=False, failed=False):
        disposition = "failed" if failed else "done"
        if unknown:
            disposition = "unknown"
        elif deferred:
            disposition = "queued"
        self.conn.execute(
            "UPDATE model_operation SET status = ? WHERE id = ? AND status = 'active'",
            (disposition, op_id),
        )

    def status(self) -> dict:
        row = self.conn.execute(
            "SELECT accepting, revision, "
            "(SELECT COUNT(*) FROM model_operation WHERE status = 'queued'), "
            "(SELECT COUNT(*) FROM model_operation WHERE status = 'active'), "
            "(SELECT COUNT(*) FROM model_operation WHERE status = 'unknown') "
            "FROM model_admission WHERE id = 1"
        ).fetchone()
        accepting, revision, queued, active, unknown = row
        mode = "open" if accepting else ("draining" if active or unknown else "closed")
        return {
            "mode": mode,
            "revision": revision,
            "queued": queued,
            "active": active,
            "unknown": unknown,
        }
