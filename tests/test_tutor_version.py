"""Версия панели (OH-T06b): хранимый монотонный счётчик на задание, а не вывод из других таблиц."""

import re
from datetime import UTC, datetime

from app import privacy
from app.ingest.store import update_task
from app.tutor import state, steps
from tests.bank_helpers import db
from tests.chat_helpers import chat_reply, say
from tests.test_tutor_figure import FIGURE, NUM, SHORT
from tests.tutor_helpers import CHILD, Scripted, attempt, boot, post, seed, view

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def ver(c, tid) -> int:
    return int(view(c, tid)["panel"]["version"])


def fragment_ver(c, tid) -> int:
    html = c.get(f"/tasks/{tid}/tutor", headers=CHILD).text
    m = re.search(r'data-version="(\d+)"', html)
    assert m, "ответ панели без версии"
    return int(m[1])


class Grows:
    """После каждого шага версия строго больше предыдущей."""

    def __init__(self, c, tid):
        self.c, self.tid, self.last = c, tid, ver(c, tid)

    def __call__(self, what):
        now = ver(self.c, self.tid)
        assert now > self.last, f"{what}: версия не выросла ({self.last} -> {now})"
        self.last = now


def test_every_panel_writer_bumps_the_version(tmp_path):
    model = Scripted(default=chat_reply("Клетка - основа живого."))
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        conn = db(tmp_path)
        grows = Grows(c, tid)
        assert c.get(f"/tasks/{tid}", headers=CHILD).text.count("data-version=") == 1
        grows("страница задания открыла прогон")
        assert attempt(c, tid, "А").status_code == 200
        grows("attempt")
        assert post(c, tid, "hint", level=1).status_code == 200
        grows("hint")
        assert say(c, tid, "что такое клетка?").status_code == 200
        grows("chat")
        run = state.latest_run(conn, tid)
        assert (
            state.store_analysis(conn, run["id"], run["attempts"], "<p>разбор</p>", False, "low")
            is None
        )
        grows("store_analysis")
        assert attempt(c, tid, "Б").status_code == 200  # решено
        grows("решение")
        state.add_explain(conn, run["id"], "simpler", "Проще", "<p>x</p>", "low", NOW)
        grows("explain")
        assert post(c, tid, "dispute").status_code == 200
        grows("dispute")
        assert post(c, tid, "repeat").status_code == 200
        grows("repeat (новый прогон)")
        task = conn.execute("SELECT * FROM task_item WHERE id = ?", (tid,)).fetchone()
        update_task(conn, tid, key_version=task["key_version"] + 1)
        grows("правка и подтверждение родителя")
        # статусы шага: pending -> running -> done и отказ
        from app.tutor import seq

        before = seq.current(conn, tid)
        step = steps.start(conn, tid, "hint", NOW)
        assert seq.current(conn, tid) > before
        before = seq.current(conn, tid)
        steps.activate(conn, step)
        assert seq.current(conn, tid) > before
        before = seq.current(conn, tid)
        steps.finish(conn, step, "done", NOW)
        assert seq.current(conn, tid) > before
        conn.close()


def test_show_answer_bumps_the_version(tmp_path):
    c, _clock, _model = boot(tmp_path)
    with c:
        tid = seed(tmp_path, key="5", options=NUM, figure={**FIGURE, "teacher_note": SHORT})
        attempt(c, tid, "1")
        attempt(c, tid, "2")
        grows = Grows(c, tid)
        assert post(c, tid, "show-answer", confirm=True).status_code == 200
        grows("show-answer")


def test_deleting_chat_raises_the_version(tmp_path):
    model = Scripted(default=chat_reply("Клетка - основа живого."))
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        assert say(c, tid, "что такое клетка?").status_code == 200
        before = ver(c, tid)
        conn = db(tmp_path)
        assert privacy.delete_messages(conn) >= 1
        conn.close()
        assert ver(c, tid) > before  # версия не откатывается, когда исчезает последнее событие


def test_a_response_carries_the_version_of_the_state_it_rendered(tmp_path, monkeypatch):
    """Ответ строится в одной транзакции чтения: пока он собирается, чужая запись (новый прогон) не
    попадает ни в HTML, ни в версию, и опоздавший ответ не получает версию нового прогона."""
    c, _clock, _model = boot(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        assert attempt(c, tid, "Б").status_code == 200  # решено
        real = state.active_run
        fired = []

        def racing(conn, task, now=None):
            if not fired:
                fired.append(1)
                other = db(tmp_path)  # «Разобрать ещё раз» приходит, пока ответ собирается
                state.start_repeat(other, tid, NOW)
                other.close()
            return real(conn, task, now)

        monkeypatch.setattr(state, "active_run", racing)
        old = c.get(f"/tasks/{tid}/tutor", headers=CHILD).text
        monkeypatch.setattr(state, "active_run", real)
        assert fired
        new = fragment_ver(c, tid)
        assert int(re.search(r'data-version="(\d+)"', old)[1]) < new
        assert "повтор" not in old  # HTML старого прогона и версия старого прогона
