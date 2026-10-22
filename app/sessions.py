"""Занятие и события (SPEC 1.8). Часы подменяются, чтобы 30 минут простоя проверялись тестом."""

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

IDLE_TIMEOUT = timedelta(minutes=30)

EVENT_TYPES = frozenset(
    {
        "attempt", "hint", "answer_shown", "explain", "chat", "voice", "fallback",
        "disputed_key", "shown_to_parent", "parent_opened_chat",
    }
)  # fmt: skip


class SessionClosedError(Exception):
    """Занятие уже закрыто (явно или по простою); событие не записано."""


def utcnow() -> datetime:
    return datetime.now(UTC)


class SessionService:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime] = utcnow):
        self.conn = conn
        self.clock = clock
        conn.row_factory = None

    def _now(self) -> str:
        return self.clock().astimezone(UTC).isoformat()

    def open_session(self) -> int:
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO study_session (started_at, last_activity_at) VALUES (?, ?)", (now, now)
        )
        return cur.lastrowid

    def record_event(
        self, session_id: int, type: str, task_item_id: int | None = None, data: dict | None = None
    ) -> int:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {type}")
        self.close_idle()
        now = self._now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Привязка и продление простоя - одним условным UPDATE: закрытое занятие не оживает.
            cur = self.conn.execute(
                "UPDATE study_session SET last_activity_at = ? WHERE id = ? AND ended_at IS NULL",
                (now, session_id),
            )
            if cur.rowcount != 1:
                raise SessionClosedError(session_id)
            cur = self.conn.execute(
                "INSERT INTO event (session_id, type, task_item_id, data, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    type,
                    task_item_id,
                    None if data is None else json.dumps(data, ensure_ascii=False),
                    now,
                ),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return cur.lastrowid

    def close_session(self, session_id: int, summary: dict | None = None) -> None:
        cur = self.conn.execute(
            "UPDATE study_session SET ended_at = ?, end_reason = 'explicit', summary = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (self._now(), None if summary is None else json.dumps(summary), session_id),
        )
        if cur.rowcount != 1:
            raise SessionClosedError(session_id)

    def close_idle(self) -> list[int]:
        """Закрыть занятия без событий не менее 30 минут; конец = время последнего события."""
        threshold = (self.clock().astimezone(UTC) - IDLE_TIMEOUT).isoformat()
        rows = self.conn.execute(
            "UPDATE study_session SET ended_at = last_activity_at, end_reason = 'idle' "
            "WHERE ended_at IS NULL AND last_activity_at <= ? RETURNING id",
            (threshold,),
        ).fetchall()
        return sorted(r[0] for r in rows)

    def get_session(self, session_id: int) -> dict:
        cur = self.conn.execute("SELECT * FROM study_session WHERE id = ?", (session_id,))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))

    def list_events(self, session_id: int) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, session_id, type, task_item_id, data, created_at FROM event "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        cols = [c[0] for c in cur.description]
        out = []
        for row in cur.fetchall():
            item = dict(zip(cols, row, strict=True))
            item["data"] = None if item["data"] is None else json.loads(item["data"])
            out.append(item)
        return out
