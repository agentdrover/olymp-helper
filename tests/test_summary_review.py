"""OH-T10 review round: no answer leaks through old summaries, message counts of an open
session survive deleting its chat, keyless results are not «ошибся», a corrected key is noted."""

import json

from tests.bank_helpers import CHILD, PARENT, db
from tests.summary_helpers import (
    attempt,
    boot_summary,
    finish,
    last_session,
    my,
    post,
    say,
    seed,
    summary_of,
)


def test_old_summary_does_not_reveal_answer_during_repeat(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б", number="5")
        assert attempt(c, tid, "Б").json()["state"] == "solved"
        sid = last_session(tmp_path)
        assert finish(c).status_code == 200
        assert post(c, tid, "repeat").status_code == 200
        assert c.get(f"/api/tasks/{tid}/tutor", headers=CHILD).json()["solution"] is None
        shown = [
            json.dumps(summary_of(c, sid), ensure_ascii=False),
            json.dumps(my(c), ensure_ascii=False),
            c.get(f"/my?session={sid}", headers=CHILD).text,
        ]
        for text in shown:
            assert "Б —" not in text and '"answer": "Б"' not in text
            assert "верно" not in text.replace("Закончить", "")
            assert "решено в прошлый раз" in text
        # the parent still sees the full facts
        assert "Б — верно" in c.get(f"/parent/summaries/{sid}", headers=PARENT).text
        # once the repeat is solved, the child may see the answers again
        assert attempt(c, tid, "Б").json()["state"] == "solved"
        assert summary_of(c, sid)["tasks"][0]["attempts"][0]["answer"] == "Б"


def test_deleting_chat_of_open_session_keeps_message_count(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        say(c, tid, "Вопрос")
        sid = last_session(tmp_path)
        r = c.post(
            f"/parent/conversations/{sid}/delete",
            headers=PARENT,
            data={"confirm": "1", "expected": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert my(c)["sessions"][0]["running"] is True  # the half-made facts are not shown
        assert finish(c).json()["summary"]["messages"] == 1
        assert summary_of(c, sid)["messages"] == 1


def test_keyless_miss_is_not_called_a_mistake(tmp_path):
    c, clock, _model = boot_summary(tmp_path)
    with c:
        keyless = seed(tmp_path, key=None, model_answer="Б", number="8")
        attempt(c, keyless, "А", "sure")
        keyed = seed(tmp_path, key="Б", number="9")
        clock.advance(5)
        attempt(c, keyed, "А", "doubt")
        full = finish(c).json()["summary"]
        reasons = {r["number"]: r["reason"] for r in full["revisit"]}
        assert reasons["8"] == "не совпало с ответом модели · не проверено"
        assert reasons["9"] == "была ошибка"
        assert [r["number"] for r in full["revisit"]] == ["8", "9"]  # «уверен» is still first
        line = full["tasks"][0]["attempts"][0]
        assert line["note"] == "ответ модели, не проверен"
        page = c.get("/my", headers=CHILD).text
        block = page[page.index('aria-label="Стоит вернуться"') :]
        assert "№ 8" in block and "ошибся" not in block


def test_corrected_key_is_noted(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "Б")
        sid = last_session(tmp_path)
        finish(c)
        assert summary_of(c, sid)["tasks"][0]["key_note"] is None
        conn = db(tmp_path)
        conn.execute("UPDATE task_item SET key_version = key_version + 1 WHERE id = ?", (tid,))
        conn.close()
        assert summary_of(c, sid)["tasks"][0]["key_note"] == "ключ исправлен после попытки"
        assert "ключ исправлен после попытки" in c.get(f"/my?session={sid}", headers=CHILD).text
        parent = c.get(f"/parent/summaries/{sid}", headers=PARENT).text
        assert "ключ исправлен после попытки" in parent
