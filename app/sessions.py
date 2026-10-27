"""Занятие и события (SPEC 1.8). Часы подменяются, чтобы 30 минут простоя проверялись тестом."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from app.tutor.seq import bump

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

    def _read_clock(self) -> datetime:
        """Одно чтение часов на операцию: проверка простоя и запись видят одно и то же now."""
        return self.clock().astimezone(UTC)

    @contextmanager
    def _tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            # COMMIT мог упасть при открытой транзакции: без отката следующий BEGIN сломается.
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _expire_idle(self, now: datetime) -> list[int]:
        """Закрыть занятия без событий не менее 30 минут; конец = время последнего события."""
        threshold = (now - IDLE_TIMEOUT).isoformat()
        rows = self.conn.execute(
            "UPDATE study_session SET ended_at = last_activity_at, end_reason = 'idle' "
            "WHERE ended_at IS NULL AND last_activity_at <= ? RETURNING id",
            (threshold,),
        ).fetchall()
        return sorted(r[0] for r in rows)

    def open_session(self) -> int:
        now = self._read_clock().isoformat()
        cur = self.conn.execute(
            "INSERT INTO study_session (started_at, last_activity_at) VALUES (?, ?)", (now, now)
        )
        return cur.lastrowid

    def record_event(
        self, session_id: int, type: str, task_item_id: int | None = None, data: dict | None = None
    ) -> int:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {type}")
        now_dt = self._read_clock()
        now = now_dt.isoformat()
        closed = False
        with self._tx():
            self._expire_idle(now_dt)
            # Продление простоя - условный UPDATE: закрытое занятие не оживает.
            cur = self.conn.execute(
                "UPDATE study_session SET last_activity_at = ? WHERE id = ? AND ended_at IS NULL",
                (now, session_id),
            )
            if cur.rowcount != 1:
                closed = True  # закрытие по простою должно сохраниться: выходим после COMMIT
            else:
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
                event_id = cur.lastrowid
                bump(self.conn, task_item_id)  # событие по заданию меняет его панель
        if closed:
            raise SessionClosedError(session_id)
        return event_id

    def close_session(self, session_id: int, summary: dict | None = None) -> None:
        """Явное «закончить». Если занятие уже простаивало 30 минут, оно закрывается как idle."""
        now_dt = self._read_clock()
        threshold = (now_dt - IDLE_TIMEOUT).isoformat()
        text = None if summary is None else json.dumps(summary, ensure_ascii=False)
        with self._tx():
            row = self.conn.execute(
                "SELECT ended_at, last_activity_at FROM study_session WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None or row[0] is not None:
                raise SessionClosedError(session_id)
            if row[1] <= threshold:
                self.conn.execute(
                    "UPDATE study_session SET ended_at = last_activity_at, end_reason = 'idle', "
                    "summary = ? WHERE id = ?",
                    (text, session_id),
                )
            else:
                self.conn.execute(
                    "UPDATE study_session SET ended_at = ?, end_reason = 'explicit', summary = ? "
                    "WHERE id = ?",
                    (now_dt.isoformat(), text, session_id),
                )

    def close_idle(self) -> list[int]:
        with self._tx():
            return self._expire_idle(self._read_clock())

    def get_session(self, session_id: int) -> dict | None:
        """Занятие по id или None, если его нет (запись изменений для неизвестного id - ошибка)."""
        cur = self.conn.execute("SELECT * FROM study_session WHERE id = ?", (session_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row, strict=True))

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
