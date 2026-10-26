"""OH-T08: what the parent and the child see in each state (AC5-AC7)."""

import re
import sqlite3
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient

from app.exam_countdown import exam_countdown
from app.ingest.web import TEMPLATES
from tests.bank_helpers import StubModel, build_app, db
from tests.test_auth import PIN, Clock, csrf_of, fresh, login, set_pin


def test_ac5(tmp_path):
    clock = Clock()
    with TestClient(fresh(tmp_path, clock)) as c:
        set_pin(c)
        c.post("/parent/logout", data={"csrf": csrf_of(c)})
        page = c.get("/parent/login").text
        assert "После 5 ошибок" in page
        for left in (4, 3, 2):
            r = login(c, "1111")
            assert r.status_code == 401
            assert f"Неверный PIN. Осталось попыток: {left}" in r.text
        assert "Осталось попыток: 2" in r.text
        r = login(c, "1111")
        assert "Осталось попыток: 1" in r.text
        r = login(c, "1111")
        until = (clock.now + timedelta(minutes=5)).astimezone().strftime("%H:%M")
        assert r.status_code == 429
        assert f"Вход закрыт до {until}" in r.text
        assert "disabled" in r.text  # the input and keypad are closed
        # the closed screen also shows it to whoever opens the page later
        assert f"Вход закрыт до {until}" in c.get("/parent/login").text
        # a correct PIN while closed does not count and does not let in
        assert login(c, PIN).status_code == 429
        # a success after the lock resets the counter
        clock.advance(minutes=5, seconds=1)
        assert login(c, "1111").status_code == 401
        assert login(c, PIN).status_code == 303
        c.post("/parent/logout", data={"csrf": csrf_of(c)})
        assert "Осталось попыток: 4" in login(c, "9999").text


def test_the_pin_screen_has_a_keypad_and_submits_after_the_fourth_digit(tmp_path):
    with TestClient(fresh(tmp_path)) as c:
        set_pin(c)
        c.post("/parent/logout", data={"csrf": csrf_of(c)})
        html = c.get("/parent/login").text
        keys = re.findall(r'data-key="([^"]+)"', html)
        assert sorted(keys) == sorted([*"0123456789", "back"])
        assert 'inputmode="numeric"' in html and 'maxlength="4"' in html
        assert "/static/pin.js" in html and "<style" not in html
        js = c.get("/static/pin.js").text
        assert "length === 4" in js and "submit" in js and "keydown" in js


def seed_messages(tmp_path, per_session):
    conn = db(tmp_path)
    ids = []
    now = datetime.now(UTC).isoformat()
    for count in per_session:
        sid = conn.execute(
            "INSERT INTO study_session (started_at, last_activity_at, summary) "
            "VALUES (?, ?, '{\"solved\": 3}')",
            (now, now),
        ).lastrowid
        for i in range(count):
            conn.execute(
                "INSERT INTO event (session_id, type, data, created_at) VALUES (?, ?, ?, ?)",
                (sid, "chat" if i % 5 else "voice", '{"role": "child", "text": "q"}', now),
            )
        conn.execute(
            "INSERT INTO event (session_id, type, data, created_at) VALUES (?, 'attempt', NULL, ?)",
            (sid, now),
        )
        ids.append(sid)
    conn.close()
    return ids


def counts(tmp_path):
    conn = sqlite3.connect(tmp_path / "olymp.db")
    try:
        return {
            sid: conn.execute(
                "SELECT COUNT(*) FROM event WHERE session_id = ? AND type IN ('chat', 'voice')",
                (sid,),
            ).fetchone()[0]
            for (sid,) in conn.execute("SELECT id FROM study_session")
        }
    finally:
        conn.close()


def test_ac6(tmp_path):
    app = build_app(tmp_path, StubModel())
    first, second = seed_messages(tmp_path, [14, 6])
    with TestClient(app) as c:
        token = {"X-CSRF-Token": "test-csrf-token-0123456789abcdef"}
        from tests.bank_helpers import PARENT

        listing = c.get("/parent/conversations", headers=PARENT).text
        assert "14" in listing and "6" in listing
        page = c.get(f"/parent/conversations/{first}/delete", headers=PARENT)
        assert page.status_code == 200
        assert "14 сообщений" in page.text
        assert counts(tmp_path) == {first: 14, second: 6}  # looking deletes nothing
        # a POST without the confirmation shows the question again and deletes nothing
        r = c.post(f"/parent/conversations/{first}/delete", headers=PARENT, data={})
        assert r.status_code == 200 and "14 сообщений" in r.text
        assert counts(tmp_path) == {first: 14, second: 6}
        # the count seen by the parent is part of the confirmation: it went stale -> ask again
        r = c.post(
            f"/parent/conversations/{first}/delete",
            headers=PARENT,
            data={"confirm": "1", "expected": "13"},
        )
        assert r.status_code == 200 and counts(tmp_path)[first] == 14
        r = c.post(
            f"/parent/conversations/{first}/delete",
            headers=PARENT,
            data={"confirm": "1", "expected": "14"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert counts(tmp_path) == {first: 0, second: 6}  # only that session's messages
        conn = sqlite3.connect(tmp_path / "olymp.db")
        # the session, its summary and non-message events stay
        assert conn.execute("SELECT summary FROM study_session WHERE id = ?", (first,)).fetchone()[
            0
        ]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event WHERE session_id = ? AND type = 'attempt'", (first,)
            ).fetchone()[0]
            == 1
        )
        conn.close()
        # the whole conversation, with its own count
        page = c.get("/parent/conversations/delete", headers=PARENT)
        assert "6 сообщений" in page.text
        r = c.post(
            "/parent/conversations/delete",
            headers=PARENT,
            data={"confirm": "1", "expected": "6"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and counts(tmp_path) == {first: 0, second: 0}
        assert token  # the shared header is what the seeded session expects


def test_ac7(tmp_path):
    app = build_app(tmp_path, StubModel())
    path = tmp_path / "olymp.db"
    conn = sqlite3.connect(path, isolation_level=None)
    today = date(2026, 10, 26)
    # off by default, even with a date
    conn.execute("UPDATE settings SET stage_date = '2026-11-05'")
    assert exam_countdown(conn, today) is None
    html = TEMPLATES.get_template("_countdown.html").render(countdown=exam_countdown(conn, today))
    assert "exam-countdown" not in html
    # on, with a date: days and the date
    conn.execute("UPDATE settings SET show_exam_countdown = 1")
    shown = exam_countdown(conn, today)
    assert shown["days"] == 10
    html = TEMPLATES.get_template("_countdown.html").render(countdown=shown)
    assert "exam-countdown" in html and "10" in html and "5 ноября" in html
    # on but no date, or a date in the past: nothing to count
    conn.execute("UPDATE settings SET stage_date = NULL")
    assert exam_countdown(conn, today) is None
    conn.execute("UPDATE settings SET stage_date = '2026-10-01'")
    assert exam_countdown(conn, today) is None
    conn.execute("UPDATE settings SET stage_date = '2026-10-26'")
    assert exam_countdown(conn, today)["days"] == 0
    conn.close()

    # the parent switches it through the settings page
    from tests.bank_helpers import PARENT

    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("UPDATE settings SET show_exam_countdown = 0, stage_date = NULL")
    conn.close()
    with TestClient(app) as c:
        page = c.get("/parent/settings", headers=PARENT).text
        assert 'name="show_exam_countdown"' in page and " checked" not in page
        r = c.post(
            "/parent/settings",
            headers=PARENT,
            data={"show_exam_countdown": "1", "stage_date": "2026-11-05"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        row = (
            db(tmp_path).execute("SELECT show_exam_countdown, stage_date FROM settings").fetchone()
        )
        assert tuple(row) == (1, "2026-11-05")
        bad = c.post(
            "/parent/settings",
            headers=PARENT,
            data={"show_exam_countdown": "1", "stage_date": "not-a-date"},
        )
        assert bad.status_code == 422
        c.post("/parent/settings", headers=PARENT, data={"stage_date": "2026-11-05"})
        row = db(tmp_path).execute("SELECT show_exam_countdown FROM settings").fetchone()
        assert row[0] == 0
