"""OH-T10: a session summary is built from the session's events (AC-1..AC-4, AC-8)."""

from datetime import timedelta

from tests.bank_helpers import CHILD, PARENT, db
from tests.summary_helpers import (
    PRAISE,
    attempt,
    boot_summary,
    fallback,
    finish,
    last_session,
    my,
    new_session,
    post,
    say,
    seed,
    set_topic,
    summary_of,
)
from tests.test_tutor_figure import FIGURE, NUM, SHORT


def test_ac1(tmp_path):
    c, clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б", number="7")
        set_topic(tmp_path, tid, "b05")
        c.get(f"/tasks/{tid}")
        attempt(c, tid, "А", "sure")
        clock.advance(61)
        assert post(c, tid, "hint", level=1).status_code == 200
        assert say(c, tid, "Что такое клетка?").status_code == 200
        sid = last_session(tmp_path)
        fallback(tmp_path, sid)
        last_event = clock.now
        new_session(clock, 40)  # the tab was closed; nobody closes the session
        assert db(tmp_path).execute("SELECT ended_at FROM study_session").fetchone()[0] is None

        data = my(c)
        s = next(x for x in data["sessions"] if x["id"] == sid)
        assert s["end_reason"] == "idle" and not s["running"]
        full = summary_of(c, sid)
        assert full["end_reason"] == "idle"
        assert full["ended_at"] <= (last_event + timedelta(seconds=1)).isoformat()
        assert full["topics"] == ["Царство растений"]
        assert (full["attempts"], full["wrong"], full["correct"], full["unknown"]) == (1, 1, 0, 0)
        assert full["hints"] == 1
        assert full["messages"] == 1
        assert [f["to"] for f in full["fallbacks"]] == ["cursor"]
        assert full["tasks"][0]["number"] == "7"
        page = c.get(f"/my?session={sid}", headers=CHILD).text
        assert "Царство растений" in page and "Переключение канала" in page
        assert "сообщений помощнику: 1" in page


def test_ac2(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        unkeyed = seed(tmp_path, key=None, model_answer="Б", number="3")
        attempt(c, unkeyed, "А")
        keyed = seed(tmp_path, key="Б", number="4")
        assert attempt(c, keyed, "Б").json()["state"] == "solved"
        assert post(c, keyed, "dispute").status_code == 200
        sid = last_session(tmp_path)
        fallback(tmp_path, sid, "cursor", "claude", "limit")
        full = (finish(c)).json()["summary"]
        assert full["session_id"] == sid
        assert full["unknown"] == 1 and full["correct"] == 1
        line = next(t for t in full["tasks"] if t["number"] == "3")["attempts"][0]
        assert line["note"] == "ответ модели, не проверен"
        assert [d["number"] for d in full["disputed"]] == ["4"]
        assert full["fallbacks"][0]["from"] == "cursor"
        page = c.get(f"/my?session={sid}", headers=CHILD).text
        assert "ответ модели, не проверен" in page
        assert "Переключение канала: cursor → claude" in page
        assert "Спорный ключ: 1" in page and "задание 4" in page
        # the parent sees the same facts
        parent_page = c.get(f"/parent/summaries/{sid}", headers=PARENT).text
        assert "ответ модели, не проверен" in parent_page
        assert "Переключение канала: cursor → claude" in parent_page


def test_ac3(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        say(c, tid, "Вопрос")
        sid = last_session(tmp_path)
        assert not [j for j in my(c)["journal"]]
        # the confirmation page alone writes nothing
        ask = c.get(f"/parent/sessions/{sid}/chat", headers=PARENT)
        assert ask.status_code == 200 and "Запись появится в журнале ребёнка" in ask.text
        assert my(c)["journal"] == []
        opened = c.post(f"/parent/sessions/{sid}/chat", headers=PARENT, data={"confirm": "1"})
        assert opened.status_code == 200 and "Вопрос" in opened.text
        journal = my(c)["journal"]
        assert [(j["type"], j["session_id"]) for j in journal] == [("parent_opened_chat", sid)]
        page = c.get("/my", headers=CHILD).text
        assert f"Родитель открыл переписку занятия №{sid}" in page


def test_ac4(tmp_path):
    c, clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "Б")
        sid = last_session(tmp_path)
        clock.advance(120)  # far from the idle limit: only the button can close it now
        assert db(tmp_path).execute("SELECT ended_at FROM study_session").fetchone()[0] is None
        r = finish(c)
        assert r.status_code == 200 and r.json()["session_id"] == sid
        row = db(tmp_path).execute("SELECT * FROM study_session WHERE id = ?", (sid,)).fetchone()
        assert row["end_reason"] == "explicit" and row["ended_at"] == clock.now.isoformat()
        assert row["summary"] is not None  # built at once, not on the next listing
        assert summary_of(c, sid)["tasks"][0]["outcome"] == "верно с 1-й попытки"
        assert finish(c).status_code == 409  # nothing is running now
        # work after "закончить" opens a new session
        other = seed(tmp_path, key="Б")
        attempt(c, other, "Б")
        assert last_session(tmp_path) != sid


def test_a_shown_answer_is_not_counted_as_solved(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="5", options=NUM, figure={**FIGURE, "teacher_note": SHORT},
                   number="11")  # fmt: skip
        attempt(c, tid, "1")
        attempt(c, tid, "2")
        assert post(c, tid, "show-answer", confirm=True).status_code == 200
        sid = last_session(tmp_path)
        full = finish(c).json()["summary"]
        assert full["tasks"][0]["outcome"] == "ответ показан после 2 попыток, не разобрано"
        assert full["answer_shown"][0]["text"] == "ответ показан после 2 попыток, не разобрано"
        page = c.get(f"/my?session={sid}", headers=CHILD).text
        assert "ответ показан после 2 попыток, не разобрано" in page
        card = page[page.index('class="card summary"') : page.index("Стоит вернуться")]
        assert "верно" not in card.lower()
        assert full["correct"] == 0
        assert [r["reason"] for r in full["revisit"]] == [
            "ответ показан после 2 попыток, не разобрано"
        ]
        assert not any(w in page.lower() for w in PRAISE)


def test_summary_survives_chat_deletion_and_retention(tmp_path):
    from app.privacy import cleanup_retention

    c, clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "А", "sure")
        say(c, tid, "Вопрос")
        sid = last_session(tmp_path)
        before = finish(c).json()["summary"]
        assert before["messages"] == 1
        # delete the chat of the session through the parent screen
        page = c.get(f"/parent/conversations/{sid}/delete", headers=PARENT)
        assert page.status_code == 200
        r = c.post(
            f"/parent/conversations/{sid}/delete", headers=PARENT,
            data={"confirm": "1", "expected": "1"}, follow_redirects=False,
        )  # fmt: skip
        assert r.status_code == 303
        assert (
            db(tmp_path).execute("SELECT COUNT(*) FROM event WHERE type='chat'").fetchone()[0] == 0
        )
        assert summary_of(c, sid) == before
        assert c.get(f"/parent/summaries/{sid}", headers=PARENT).status_code == 200
        # retention: a session older than 180 days keeps its summary too
        conn = db(tmp_path)
        cleanup_retention(conn, clock.now + timedelta(days=400))
        conn.close()
        assert summary_of(c, sid) == before


def test_summary_is_built_before_chat_is_deleted(tmp_path):
    """A session closed by idle, listed by nobody yet: deleting the chat first must not lose
    the message count of the summary."""
    c, clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        say(c, tid, "Вопрос")
        sid = last_session(tmp_path)
        new_session(clock, 40)
        r = c.post(
            f"/parent/conversations/{sid}/delete", headers=PARENT,
            data={"confirm": "1", "expected": "1"}, follow_redirects=False,
        )  # fmt: skip
        assert r.status_code == 303
        assert summary_of(c, sid)["messages"] == 1
