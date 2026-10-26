"""Состояния панели помощника (OH-T06, AC-21)."""

import threading

from app.model.types import LIMIT_MESSAGE, ModelResult
from tests.bank_helpers import db
from tests.tutor_helpers import Blocking, Scripted, attempt, boot, post, seed, view


def _p(base, name):
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_ac21(tmp_path):
    # 1. запрос в работе: второй не уходит, проверка по ключу работает
    model = Blocking()
    model.release.set()
    c, _clock, _ = boot(_p(tmp_path, "a"), model)
    with c:
        tid = seed(_p(tmp_path, "a"), key="Б")
        attempt(c, tid, "А")
        model.release.clear()
        model.entered.clear()
        first = {}
        t = threading.Thread(
            target=lambda: first.update(r=post(c, tid, "hint", level=1)), daemon=True
        )
        t.start()
        assert model.entered.wait(10)
        n = len(model.calls)
        second = post(c, tid, "hint", level=1)
        assert second.json()["panel"]["state"] == "thinking"
        assert "обычно 5–15 с" in second.json()["panel"]["text"]
        assert len(model.calls) == n, "второй запрос к модели не должен уйти"
        assert view(c, tid)["panel"]["state"] == "thinking"
        assert "disabled" in c.get(f"/tasks/{tid}/tutor").text  # кнопки заблокированы
        # канал не ответил: родитель/сервис записал fallback, панель сообщает о втором канале
        conn = db(_p(tmp_path, "a"))
        conn.execute(
            "INSERT INTO event (session_id, type, data, created_at) "
            "SELECT id, 'fallback', '{}', '2026-10-22T10:00:00+00:00' FROM study_session LIMIT 1"
        )
        panel = view(c, tid)["panel"]
        assert panel["state"] == "second_channel" and "пробую второй" in panel["text"]
        # проверка по ключу работает, пока модель думает
        solved = attempt(c, tid, "Б")
        assert solved.status_code == 200 and solved.json()["state"] == "solved"
        model.release.set()
        t.join(10)
        assert first["r"].status_code == 200
        assert view(c, tid)["panel"]["state"] == "idle"

    # 2. канал упал
    down = Scripted(
        reply=lambda k, content: ModelResult.failure("claude", "stub", "executor_unreachable")
    )
    c, _clock, _ = boot(_p(tmp_path, "b"), down)
    with c:
        tid = seed(_p(tmp_path, "b"), key="Б")
        attempt(c, tid, "А")
        r = post(c, tid, "hint", level=1).json()
        assert r["panel"]["state"] == "down"
        assert (
            "не отвечает" in r["panel"]["text"]
            and "проверка по ключу работает" in r["panel"]["text"]
        )
        assert r["hints"] == 0
        assert attempt(c, tid, "Б").json()["state"] == "solved"

    # 3. дневной лимит
    limit = Scripted(
        reply=lambda k, content: ModelResult.failure(
            "claude", "stub", "limit", message=LIMIT_MESSAGE
        )
    )
    c, _clock, _ = boot(_p(tmp_path, "c"), limit)
    with c:
        tid = seed(_p(tmp_path, "c"), key="Б")
        attempt(c, tid, "А")
        r = post(c, tid, "hint", level=1).json()
        assert r["panel"]["state"] == "limit"
        assert "на сегодня всё" in r["panel"]["text"].lower()
        assert attempt(c, tid, "Б").json()["state"] == "solved"
        html = c.get(f"/tasks/{tid}/tutor").text
        assert "helper-status" in html
