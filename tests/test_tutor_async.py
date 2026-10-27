"""Шаги с моделью идут в фоне (OH-T06b): POST отвечает сразу, панель опрашивает сервер.

Модель-заглушка стоит на событии, часы поддельные: реальных пауз нет.
"""

import re
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.tutor.panel import LONG_WAIT_S
from app.tutor.steps import STEP_TIMEOUT_S
from tests.bank_helpers import CHILD, build_app
from tests.chat_helpers import chat_reply, say
from tests.tutor_helpers import Blocking, Clock, attempt, post, seed, view

STATIC = Path(__file__).parent.parent / "app" / "static" / "tutor.js"


def boot_bg(tmp_path, model):
    clock = Clock()
    c = TestClient(build_app(tmp_path, model, clock=clock, sync_steps=False))
    c.__enter__()
    return c, clock


def wrong_attempt(c, tid, model):
    """Неверная попытка: её разбор тоже идёт в фоне; ждём его конца."""
    model.release.set()
    assert attempt(c, tid, "А").status_code == 200
    c.app.state.steps.join()
    model.release.clear()
    model.entered.clear()


def fragment(c, tid):
    r = c.get(f"/tasks/{tid}/tutor", headers=CHILD)
    assert r.status_code == 200
    return r.text


def hint_button_enabled(html):
    tag = re.search(r'<button[^>]*data-act="hint"[^>]*>', html)
    return tag is not None and "disabled" not in tag.group(0)


def test_a_hint_runs_in_the_background_and_the_panel_picks_it_up(tmp_path):
    model = Blocking()
    c, _clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        wrong_attempt(c, tid, model)
        started = c.app.state.steps.started
        t0 = time.monotonic()
        first = post(c, tid, "hint", level=1)
        assert time.monotonic() - t0 < 1.0, "POST не должен ждать модель"
        assert first.status_code == 200
        assert first.json()["panel"]["state"] == "thinking"
        assert first.json()["hints"] == 0
        assert model.entered.wait(10)
        assert c.app.state.steps.started == started + 1
        assert view(c, tid)["panel"]["state"] == "thinking"
        assert "Думаю" in fragment(c, tid)
        # второй POST во время работы: «думаю…», новой работы нет
        second = post(c, tid, "hint", level=1)
        assert second.json()["panel"]["state"] == "thinking"
        assert c.app.state.steps.started == started + 1
        n = len(model.calls)
        model.release.set()
        c.app.state.steps.join()
        done = view(c, tid)
        assert done["panel"]["state"] == "idle" and done["hints"] == 1
        assert len(done["hints_list"]) == 1
        assert len(model.calls) == n + 1
        assert c.app.state.inflight.since(tid) is None


def test_a_failed_background_step_frees_the_panel(tmp_path, monkeypatch):
    model = Blocking()
    c, clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        wrong_attempt(c, tid, model)

        # 1. работа упала: замок свободен, в панели понятная ошибка, кнопки доступны
        def boom(*a, **kw):
            raise RuntimeError("сбой записи")

        monkeypatch.setattr("app.tutor.state.store_hint", boom)
        model.release.set()
        post(c, tid, "hint", level=1)
        c.app.state.steps.join()
        v = view(c, tid)
        assert v["panel"]["state"] == "step_failed" and not v["panel"]["busy"]
        assert c.app.state.inflight.since(tid) is None
        assert hint_button_enabled(fragment(c, tid))
        monkeypatch.undo()
        # 2. работа зависла дольше срока шага (часы поддельные): замок снят, ошибка, кнопки
        model.release.clear()
        model.entered.clear()
        post(c, tid, "hint", level=1)
        assert model.entered.wait(10)
        assert view(c, tid)["panel"]["state"] == "thinking"
        clock.advance(STEP_TIMEOUT_S + 1)
        v = view(c, tid)
        assert v["panel"]["state"] == "step_failed"
        assert c.app.state.inflight.since(tid) is None
        assert hint_button_enabled(fragment(c, tid))
        model.release.set()
        c.app.state.steps.join()
        assert STEP_TIMEOUT_S >= 2 * 130 * 2


def test_a_reloaded_page_shows_the_running_step_with_elapsed_time(tmp_path):
    model = Blocking()
    c, clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        wrong_attempt(c, tid, model)
        post(c, tid, "hint", level=1)
        assert model.entered.wait(10)
        started = clock.now.isoformat()
        clock.advance(7)
        page = c.get(f"/tasks/{tid}", headers=CHILD).text  # новая вкладка или другое устройство
        assert "Думаю… 7 с" in page
        assert f'data-started="{started}"' in page and 'data-elapsed="7"' in page
        assert re.search(r'data-step="\d+"', page)
        clock.advance(LONG_WAIT_S)
        assert "Модель отвечает долго, жду…" in fragment(c, tid)
        js = STATIC.read_text(encoding="utf-8")
        assert "setTimeout(refresh" in js and "setInterval" not in js
        assert "if (polling) return;" in js  # запросы опроса не накладываются
        assert "5–15" not in js and "5–15" not in page
        model.release.set()
        c.app.state.steps.join()


def test_a_chat_question_runs_in_the_background(tmp_path):
    model = Blocking(default=chat_reply("Клетка - основа живого."))
    c, _clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        started = c.app.state.steps.started
        t0 = time.monotonic()
        first = say(c, tid, "что такое клетка?")
        assert time.monotonic() - t0 < 1.0
        assert first.status_code == 200 and first.json()["panel"]["state"] == "thinking"
        assert model.entered.wait(10)
        assert view(c, tid)["panel"]["state"] == "thinking" and view(c, tid)["chat"] == []
        second = say(c, tid, "второй вопрос")
        assert second.json()["panel"]["state"] == "thinking"
        assert c.app.state.steps.started == started + 1
        model.release.set()
        c.app.state.steps.join()
        done = view(c, tid)
        assert done["panel"]["state"] == "idle" and len(done["chat"]) == 1
        assert c.app.state.inflight.since(tid) is None
