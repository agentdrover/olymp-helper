"""OH-T10 review round 2: old stored summaries and stale runs do not leak answers, a partial
summary is completed by idle close, counting and deleting messages are one transaction."""

import json
import sqlite3
import threading

from tests.bank_helpers import CHILD, PARENT, db
from tests.summary_helpers import (
    attempt,
    boot_summary,
    finish,
    last_session,
    new_session,
    post,
    say,
    seed,
    summary_of,
)


def delete_chat(c, sid, expected):
    return c.post(
        f"/parent/conversations/{sid}/delete",
        headers=PARENT,
        data={"confirm": "1", "expected": str(expected)},
        follow_redirects=False,
    )


def test_old_stored_summary_is_not_trusted(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key=None, model_answer="Б", number="5")
        assert attempt(c, tid, "Б").json()["state"] == "solved_unknown"
        sid = last_session(tmp_path)
        finish(c)
        conn = db(tmp_path)
        old = json.loads(conn.execute("SELECT summary FROM study_session").fetchone()[0])
        old.pop("v")
        for line in old["tasks"][0]["attempts"]:
            line.pop("matched")  # the format of the first version
        conn.execute("UPDATE study_session SET summary = ?", (json.dumps(old),))
        conn.close()
        assert post(c, tid, "repeat").status_code == 200
        text = json.dumps(summary_of(c, sid), ensure_ascii=False)
        page = c.get(f"/my?session={sid}", headers=CHILD).text
        assert '"answer": "Б"' not in text and "Б —" not in page and "Б —" not in text
        assert all(a["answer"] is None for a in summary_of(c, sid)["tasks"][0]["attempts"])
        # the old summary was rebuilt from the events on read
        stored = json.loads(db(tmp_path).execute("SELECT summary FROM study_session").fetchone()[0])
        assert stored["v"] == 2


def test_stale_run_after_parent_edit_hides_answer(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б", number="6")
        attempt(c, tid, "Б")
        sid = last_session(tmp_path)
        finish(c)
        assert summary_of(c, sid)["tasks"][0]["attempts"][0]["answer"] == "Б"
        conn = db(tmp_path)
        conn.execute("UPDATE task_item SET revision = revision + 1 WHERE id = ?", (tid,))
        conn.close()
        # the tutor itself has no valid run now; the summary agrees with it
        got = summary_of(c, sid)["tasks"][0]
        assert got["attempts"][0]["answer"] is None
        assert got["outcome"] == "решено в прошлый раз"
        assert "Б —" not in c.get(f"/my?session={sid}", headers=CHILD).text
        conn = db(tmp_path)
        conn.execute("UPDATE task_item SET key_version = key_version + 1 WHERE id = ?", (tid,))
        conn.close()
        assert summary_of(c, sid)["tasks"][0]["attempts"][0]["answer"] is None


def test_partial_summary_completes_on_idle_close(tmp_path):
    c, clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        say(c, tid, "Вопрос")
        attempt(c, tid, "А")
        sid = last_session(tmp_path)
        assert delete_chat(c, sid, 1).status_code == 303
        new_session(clock, 40)  # the tab was closed
        full = summary_of(c, sid)  # 200, not 404
        assert full["messages"] == 1 and full["attempts"] == 1 and "partial" not in full


def test_counting_and_deleting_messages_are_one_transaction(tmp_path, monkeypatch):
    from app import summary

    c, clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        say(c, tid, "Первый")
        say(c, tid, "Второй")
        sid = last_session(tmp_path)
        original = summary.preserve_message_counts
        late = []

        def insert_late():
            conn = sqlite3.connect(str(tmp_path / "olymp.db"), timeout=10, isolation_level=None)
            conn.execute(
                "INSERT INTO event (session_id, type, data, created_at) "
                'VALUES (?, \'chat\', \'{"question": "Поздний", "reply": "x"}\', ?)',
                (sid, clock.now.isoformat()),
            )
            conn.close()

        def counted_then_interleaved(*args, **kw):
            original(*args, **kw)
            thread = threading.Thread(target=insert_late)
            thread.start()
            thread.join(0.5)  # with one transaction the writer waits for the commit
            late.append(thread)

        monkeypatch.setattr(summary, "preserve_message_counts", counted_then_interleaved)
        assert delete_chat(c, sid, 2).status_code == 303
        late[0].join(10)
        assert finish(c).json()["summary"]["messages"] == 3  # two deleted + the late one
