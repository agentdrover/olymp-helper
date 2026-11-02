"""OH-T10: «Мои итоги» of the child, the journal, the parent opening a chat (AC-5, AC-6, AC-7)."""

from tests.bank_helpers import CHILD, PARENT, db
from tests.summary_helpers import (
    PRAISE,
    attempt,
    boot_summary,
    finish,
    last_session,
    my,
    new_session,
    say,
    seed,
)


def event_types(tmp_path, session_id):
    rows = db(tmp_path).execute(
        "SELECT type FROM event WHERE session_id = ? ORDER BY id", (session_id,)
    )
    return [r[0] for r in rows]


def test_ac5(tmp_path):
    c, clock, _model = boot_summary(tmp_path)
    with c:
        ids = []
        for n in ("1", "2", "3"):
            tid = seed(tmp_path, key="Б", number=n)
            attempt(c, tid, "А" if n != "2" else "Б")
            say(c, tid, f"Вопрос {n}")
            ids.append(last_session(tmp_path))
            new_session(clock, 40)
        assert len(set(ids)) == 3
        # the child shows the second session; the parent opens the first (after the question)
        assert (
            c.post(f"/my/sessions/{ids[1]}/show-parent", json={}, headers=CHILD).status_code == 200
        )
        r = c.post(f"/parent/sessions/{ids[0]}/chat", headers=PARENT, data={"confirm": "1"})
        assert r.status_code == 200
        data = my(c)
        assert [s["id"] for s in data["sessions"]] == sorted(ids, reverse=True)
        # the journal holds only opening and showing records, newest first
        assert [(j["type"], j["session_id"]) for j in data["journal"]] == [
            ("parent_opened_chat", ids[0]),
            ("shown_to_parent", ids[1]),
        ]
        page = c.get("/my", headers=CHILD).text
        for sid in ids:
            assert f"Занятие №{sid}" in page
        journal = page[page.index('aria-label="Журнал"') :]
        assert f"Родитель открыл переписку занятия №{ids[0]}" in journal
        assert f"Ты показал родителю переписку занятия №{ids[1]}" in journal
        assert journal.count("<li") == 2  # not one record about attempts, hints or chat
        assert 'href="/how"' in page and "Как это работает" in page
        assert c.get("/how", headers=CHILD).status_code == 200


def test_ac6(tmp_path):
    c, clock, _model = boot_summary(tmp_path)
    with c:
        sessions = {}
        for name in ("A", "B"):
            tid = seed(tmp_path, key="Б")
            say(c, tid, f"Вопрос сессии {name}")
            sessions[name] = last_session(tmp_path)
            new_session(clock, 40)
        a, b = sessions["A"], sessions["B"]
        shown = c.post(f"/my/sessions/{a}/show-parent", json={}, headers=CHILD)
        assert shown.status_code == 200 and shown.json()["recorded"] is True
        # a second press does not widen the journal
        assert (
            c.post(f"/my/sessions/{a}/show-parent", json={}, headers=CHILD).json()["recorded"]
            is False
        )

        opened = c.get(f"/parent/sessions/{a}/chat", headers=PARENT)
        assert opened.status_code == 200
        assert "Вопрос сессии A" in opened.text
        assert "Запись появится в журнале ребёнка" not in opened.text

        ask = c.get(f"/parent/sessions/{b}/chat", headers=PARENT)
        assert ask.status_code == 200
        assert "Запись появится в журнале ребёнка" in ask.text
        assert "Вопрос сессии B" not in ask.text
        assert "parent_opened_chat" not in event_types(tmp_path, b)
        # a POST without the confirmation is still the question
        again = c.post(f"/parent/sessions/{b}/chat", headers=PARENT, data={})
        assert "Запись появится в журнале ребёнка" in again.text
        assert "parent_opened_chat" not in event_types(tmp_path, b)
        done = c.post(f"/parent/sessions/{b}/chat", headers=PARENT, data={"confirm": "1"})
        assert "Вопрос сессии B" in done.text

        assert event_types(tmp_path, a).count("shown_to_parent") == 1
        assert event_types(tmp_path, a).count("parent_opened_chat") == 1
        assert event_types(tmp_path, b).count("parent_opened_chat") == 1
        assert "shown_to_parent" not in event_types(tmp_path, b)
        kinds = sorted((j["type"], j["session_id"]) for j in my(c)["journal"])
        assert kinds == sorted(
            [("shown_to_parent", a), ("parent_opened_chat", a), ("parent_opened_chat", b)]
        )
        # every actual view writes one record: opening A again writes another
        c.get(f"/parent/sessions/{a}/chat", headers=PARENT)
        assert event_types(tmp_path, a).count("parent_opened_chat") == 2
        # an unknown session is a 404, not a record
        assert c.get("/parent/sessions/999/chat", headers=PARENT).status_code == 404
        assert c.post("/my/sessions/999/show-parent", json={}, headers=CHILD).status_code == 404


def test_ac7(tmp_path):
    c, clock, _model = boot_summary(tmp_path)
    with c:
        day = 24 * 60
        # the day before yesterday: a mistake, «сомневаюсь»; yesterday: «уверен»; today: no mark
        spec = [("3", "doubt"), ("1", "sure"), ("2", None)]
        for number, mark in spec:
            tid = seed(tmp_path, key="Б", number=number)
            attempt(c, tid, "А", mark)
            clock.advance(60)
            finish(c)
            clock.advance(day * 60)  # one day later (seconds)
        order = [r["number"] for r in my(c)["revisit"]]
        assert order == ["1", "2", "3"], order  # «уверен» first, then newest first
        reasons = {r["number"]: r["reason"] for r in my(c)["revisit"]}
        assert reasons["1"] == "был уверен, но ошибся" and reasons["3"] == "была ошибка"
        page = c.get("/my", headers=CHILD).text
        block = page[page.index('aria-label="Стоит вернуться"') : page.index('aria-label="Журнал"')]
        assert block.index("№ 1") < block.index("№ 2") < block.index("№ 3")
        assert not any(w in page.lower() for w in PRAISE)

        # the same order inside one session
        for number, mark in (("6", "doubt"), ("4", "sure"), ("5", None)):
            tid = seed(tmp_path, key="Б", number=number)
            attempt(c, tid, "А", mark)
            clock.advance(5)
        full = finish(c).json()["summary"]
        assert [r["number"] for r in full["revisit"]] == ["4", "5", "6"]
        assert full["conclusion"].startswith("К чему вернуться: задание 4 (был уверен, но ошибся)")
        assert not any(w in full["conclusion"].lower() for w in PRAISE)


def test_child_doors_are_locked_like_the_others(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "Б")
        # a foreign origin and a form post are refused before anything is written
        sid = last_session(tmp_path)
        for url in ("/session/finish", f"/my/sessions/{sid}/show-parent"):
            assert (
                c.post(url, json={}, headers={"Origin": "http://evil.example"}).status_code == 403
            )
            assert c.post(url, data={"x": "1"}).status_code == 415
        assert event_types(tmp_path, sid) == ["attempt"]
        # until the notice is read, nothing opens
        conn = db(tmp_path)
        conn.execute("UPDATE settings SET child_notice_seen_at = NULL")
        conn.close()
        assert c.get("/my", follow_redirects=False).status_code == 303
        assert c.get("/api/my").status_code == 403
        assert c.post("/session/finish", json={}).status_code == 403
