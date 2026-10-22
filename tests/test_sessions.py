import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.db import connect, migrate
from app.sessions import IDLE_TIMEOUT, SessionClosedError, SessionService


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "olymp.db")
    migrate(conn)
    clock = FakeClock()
    return conn, clock, SessionService(conn, clock)


def test_ac3(env):
    _, clock, svc = env
    assert IDLE_TIMEOUT == timedelta(minutes=30)
    sid = svc.open_session()
    clock.advance(minutes=5)
    event_time = clock()
    svc.record_event(sid, "attempt", data={"confident": True, "repeat": False})

    row = svc.get_session(sid)
    assert row["ended_at"] is None

    clock.advance(minutes=29, seconds=59)
    assert svc.close_idle() == []
    assert svc.get_session(sid)["ended_at"] is None

    clock.advance(seconds=1)  # exactly 30 minutes without events
    assert svc.close_idle() == [sid]
    row = svc.get_session(sid)
    assert row["ended_at"] == event_time.isoformat()
    assert row["end_reason"] == "idle"

    events = svc.list_events(sid)
    assert len(events) == 1
    assert events[0]["session_id"] == sid
    assert events[0]["type"] == "attempt"
    assert events[0]["data"] == {"confident": True, "repeat": False}
    assert events[0]["created_at"] == event_time.isoformat()

    with pytest.raises(SessionClosedError):
        svc.record_event(sid, "chat")
    assert len(svc.list_events(sid)) == 1


def test_ac3_event_after_idle_closes_instead_of_extending(env):
    _, clock, svc = env
    sid = svc.open_session()
    clock.advance(minutes=31)
    with pytest.raises(SessionClosedError):
        svc.record_event(sid, "hint")
    assert svc.get_session(sid)["end_reason"] == "idle"
    assert svc.list_events(sid) == []


def test_ac3_event_resets_idle_timer(env):
    _, clock, svc = env
    sid = svc.open_session()
    clock.advance(minutes=20)
    svc.record_event(sid, "chat")
    clock.advance(minutes=20)
    assert svc.close_idle() == []
    clock.advance(minutes=10)
    assert svc.close_idle() == [sid]


def test_ac3_explicit_close_and_unknown_type(env):
    _, clock, svc = env
    sid = svc.open_session()
    with pytest.raises(ValueError):
        svc.record_event(sid, "bogus")
    clock.advance(minutes=1)
    svc.close_session(sid, summary={"solved": 0})
    row = svc.get_session(sid)
    assert row["end_reason"] == "explicit"
    assert row["ended_at"] == clock().isoformat()
    assert svc.close_idle() == []


class SeqClock:
    """Часы, отдающие заранее заданные значения по одному на чтение."""

    def __init__(self, values):
        self.values = list(values)
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def _t0():
    return datetime(2026, 10, 23, 10, 0, tzinfo=UTC)


def test_ac3_close_after_idle_is_idle_not_explicit(env):
    _, clock, svc = env
    sid = svc.open_session()
    clock.advance(minutes=5)
    last = clock()
    svc.record_event(sid, "chat")
    clock.advance(minutes=31)
    svc.close_session(sid, summary={"solved": 1})
    row = svc.get_session(sid)
    assert row["end_reason"] == "idle"
    assert row["ended_at"] == last.isoformat()
    assert row["summary"] == '{"solved": 1}'
    with pytest.raises(SessionClosedError):
        svc.close_session(sid)


def test_ac3_close_boundary(env):
    _, clock, svc = env
    a = svc.open_session()
    clock.advance(minutes=29, seconds=59)
    svc.close_session(a)
    assert svc.get_session(a)["end_reason"] == "explicit"
    b = svc.open_session()
    clock.advance(minutes=30)
    svc.close_session(b)
    assert svc.get_session(b)["end_reason"] == "idle"


def test_ac3_event_exactly_at_threshold_is_rejected(env):
    _, clock, svc = env
    sid = svc.open_session()
    clock.advance(minutes=30)
    with pytest.raises(SessionClosedError):
        svc.record_event(sid, "chat")
    row = svc.get_session(sid)
    assert row["end_reason"] == "idle"
    assert row["last_activity_at"] == _t0().isoformat()
    assert svc.list_events(sid) == []


def test_ac3_one_clock_read_per_operation(env):
    conn, _, _ = env
    start = _t0()
    # Первое чтение - до порога, второе - ровно на пороге: событие не должно "проскочить"
    # по расходящимся чтениям и записаться временем второго чтения.
    clock = SeqClock([start, start + timedelta(minutes=29, seconds=59), start + IDLE_TIMEOUT])
    svc = SessionService(conn, clock)
    sid = svc.open_session()
    assert clock.reads == 1
    svc.record_event(sid, "chat")
    assert clock.reads == 2
    ev = svc.list_events(sid)[0]
    assert ev["created_at"] == (start + timedelta(minutes=29, seconds=59)).isoformat()
    assert svc.get_session(sid)["last_activity_at"] == ev["created_at"]
    svc.close_idle()
    assert clock.reads == 3
    svc.open_session()
    svc.close_session(1)
    assert clock.reads == 5


def test_ac3_failed_event_does_not_extend_session(env):
    _, clock, svc = env
    sid = svc.open_session()
    clock.advance(minutes=10)
    with pytest.raises(sqlite3.IntegrityError):
        svc.record_event(sid, "chat", task_item_id=999)
    row = svc.get_session(sid)
    assert row["last_activity_at"] == _t0().isoformat()
    assert svc.list_events(sid) == []
