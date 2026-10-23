"""Учёт запусков CLI и дневной лимит (SPEC-1.0 1.4).

Попытка записывается ДО запуска, в одной транзакции с проверкой лимита: параллельные
запросы (потоки и процессы) лимит не обходят.
"""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


def moscow_day(moment: datetime) -> str:
    return moment.astimezone(MOSCOW).strftime("%Y-%m-%d")


class Ledger:
    def __init__(self, conn: sqlite3.Connection, clock):
        self.conn = conn
        self.clock = clock

    def reserve(self, channel: str, model: str, fallback: bool = False) -> int | None:
        """Записать попытку и вернуть её id; None - дневной лимит исчерпан."""
        now = self.clock()
        day = moscow_day(now)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            limit = self.conn.execute(
                "SELECT daily_run_limit FROM settings WHERE id = 1"
            ).fetchone()[0]
            used = self.conn.execute(
                "SELECT COUNT(*) FROM model_run WHERE day = ?", (day,)
            ).fetchone()[0]
            if used >= limit:
                self.conn.execute("ROLLBACK")
                return None
            cur = self.conn.execute(
                "INSERT INTO model_run (day, started_at, channel, model, fallback) VALUES (?, ?, ?, ?, ?)",
                (day, now.isoformat(), channel, model, int(fallback)),
            )
            self.conn.execute("COMMIT")
            return cur.lastrowid
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def finish(self, run_id: int, ok: bool, duration_ms: int, error_kind: str | None) -> None:
        self.conn.execute(
            "UPDATE model_run SET status = ?, duration_ms = ?, error_kind = ? WHERE id = ?",
            ("ok" if ok else "error", duration_ms, error_kind, run_id),
        )


def usage_report(conn: sqlite3.Connection, day: str | None = None) -> list[dict]:
    """Данные экрана расхода родителя: по каждому запуску канал, модель, длительность."""
    sql = (
        "SELECT id, day, started_at, channel, model, duration_ms, status, fallback, error_kind "
        "FROM model_run"
    )
    args: tuple = ()
    if day is not None:
        sql += " WHERE day = ?"
        args = (day,)
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    keys = (
        "id",
        "day",
        "started_at",
        "channel",
        "model",
        "duration_ms",
        "status",
        "fallback",
        "error_kind",
    )
    out = []
    for row in rows:
        item = dict(zip(keys, row, strict=True))
        item["fallback"] = bool(item["fallback"])
        out.append(item)
    return out
