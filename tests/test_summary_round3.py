"""OH-T10 review round 3: a running session is not linked (its summary does not exist yet) and a
direct visit says so; «Закончить» after a long idle closes that session and opens its summary."""

from tests.bank_helpers import CHILD, PARENT
from tests.summary_helpers import attempt, boot_summary, finish, last_session, seed


def test_running_session_has_no_link_and_a_clear_answer(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "Б")
        sid = last_session(tmp_path)
        page = c.get("/parent/summaries", headers=PARENT)
        assert page.status_code == 200 and "идёт" in page.text
        assert f'href="/parent/summaries/{sid}"' not in page.text
        direct = c.get(f"/parent/summaries/{sid}", headers=PARENT)
        assert direct.status_code == 200 and "Занятие ещё идёт" in direct.text
        missing = c.get("/parent/summaries/9999", headers=PARENT)
        assert missing.status_code == 404


def test_finish_after_idle_close_opens_the_summary(tmp_path):
    c, clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "Б")
        sid = last_session(tmp_path)
        clock.advance(40 * 60)  # idle for longer than the limit, nobody listed it yet
        r = finish(c)
        assert r.status_code == 200, r.text
        assert r.json()["session_id"] == sid
        assert c.get(f"/api/my/sessions/{sid}", headers=CHILD).status_code == 200
        assert finish(c).status_code == 409  # a second press: nothing new
