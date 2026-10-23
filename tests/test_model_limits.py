"""Учёт запусков и дневной лимит (OH-T03, SPEC 1.4): AC-3."""

import threading
from datetime import UTC, datetime

from app.db import connect, migrate
from app.model.ledger import Ledger, moscow_day
from app.model.service import LIMIT_MESSAGE, ModelService
from app.model.types import ModelResult

# 22:30 UTC 23 октября = 01:30 24 октября по Москве.
NOW = datetime(2026, 10, 23, 22, 30, tzinfo=UTC)


def clock():
    return NOW


class CountingChannel:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0

    def complete(self, system, messages, schema=None, *, model=None):
        with self.lock:
            self.calls += 1
        threading.Event().wait(0.05)
        return ModelResult("ok", None, "claude", model or "", 50, None)


def test_ac3(tmp_path):
    path = tmp_path / "olymp.db"
    conn = connect(path)
    migrate(conn)
    assert moscow_day(NOW) == "2026-10-24"
    # Вчерашние по Москве запуски не считаются; сегодняшних 197.
    old = Ledger(connect(path), lambda: datetime(2026, 10, 23, 20, 59, tzinfo=UTC))
    for _ in range(50):
        assert old.reserve("claude", "m") is not None
    today = Ledger(conn, clock)
    for _ in range(197):
        assert today.reserve("claude", "m") is not None
    channel = CountingChannel()
    results, barrier = [], threading.Barrier(10)

    def worker():
        svc = ModelService(connect(path), {"claude": channel, "cursor": channel}, clock)
        barrier.wait()
        results.append(svc.complete("s", [{"role": "user", "content": "q"}]))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 10
    ok = [r for r in results if r.error is None]
    limited = [r for r in results if r.error_kind == "limit"]
    assert len(ok) == 3 and len(limited) == 7
    assert channel.calls == 3
    assert all("на сегодня всё" in r.error.lower() for r in limited)
    assert LIMIT_MESSAGE.lower().startswith("на сегодня всё")
    day = conn.execute("SELECT COUNT(*) FROM model_run WHERE day='2026-10-24'").fetchone()[0]
    assert day == 200
    # Лимит берётся из настроек.
    conn.execute("UPDATE settings SET daily_run_limit=201")
    assert today.reserve("claude", "m") is not None
    assert today.reserve("claude", "m") is None
