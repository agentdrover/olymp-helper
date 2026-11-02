"""Builders for the session-summary tests (OH-T10): one app with a movable clock, sessions made
through the real child routes, events the routes cannot make written straight into the table."""

import json
from datetime import timedelta

from tests.bank_helpers import CHILD, PARENT, db
from tests.chat_helpers import boot_chat, chat_reply, say
from tests.tutor_helpers import Scripted, attempt, out, post, seed

__all__ = [
    "CHILD",
    "PARENT",
    "attempt",
    "boot_summary",
    "chat_reply",
    "fallback",
    "finish",
    "last_session",
    "my",
    "new_session",
    "post",
    "say",
    "seed",
    "set_topic",
    "summary_of",
]

PRAISE = ("молодец", "отлично", "умница", "супер", "здорово", "браво", "прекрасно", "так держать")


def boot_summary(tmp_path):
    model = Scripted(reply=lambda kind, content: chat_reply() if kind == "chat" else out())
    c, model, _voice, _audio = boot_chat(tmp_path, model)
    return c, c.clock, model


def new_session(clock, minutes=40):
    """The tab was closed: nothing happens for longer than the idle limit."""
    clock.now += timedelta(minutes=minutes)


def last_session(tmp_path) -> int:
    return db(tmp_path).execute("SELECT MAX(id) FROM study_session").fetchone()[0]


def set_topic(tmp_path, task_id, code="b05"):
    conn = db(tmp_path)
    conn.execute("UPDATE task_item SET topic = ? WHERE id = ?", (code, task_id))
    conn.close()


def fallback(tmp_path, session_id, src="claude", dst="cursor", reason="timeout"):
    conn = db(tmp_path)
    conn.execute(
        "INSERT INTO event (session_id, type, data, created_at) VALUES (?, 'fallback', ?, "
        "(SELECT last_activity_at FROM study_session WHERE id = ?))",
        (session_id, json.dumps({"from": src, "to": dst, "reason": reason}), session_id),
    )
    conn.close()


def finish(c):
    return c.post("/session/finish", json={}, headers=CHILD)


def my(c, **params):
    r = c.get("/api/my", params=params, headers=CHILD)
    assert r.status_code == 200, r.text
    return r.json()


def summary_of(c, session_id):
    r = c.get(f"/api/my/sessions/{session_id}", headers=CHILD)
    assert r.status_code == 200, r.text
    return r.json()
