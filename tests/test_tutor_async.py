"""Шаги с моделью идут в фоне (OH-T06b): POST отвечает сразу, панель опрашивает сервер.

Модель-заглушка стоит на событии, часы поддельные: реальных пауз нет.
"""

import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.tutor import state as tutor_state
from app.tutor import steps
from app.tutor.panel import LONG_WAIT_S, Inflight
from app.tutor.steps import STEP_TIMEOUT_S
from tests.bank_helpers import CHILD, build_app, db
from tests.chat_helpers import chat_reply, say
from tests.tutor_helpers import Blocking, Clock, Scripted, attempt, out, post, seed, view

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


# ---------------------------------------------------------------- опоздавший шаг (Codex P1/P2)


class Gated(Scripted):
    """Вызов модели с номером из `blocked` стоит на своём событии; остальные идут сразу."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.started = 0
        self._lock = threading.Lock()
        self._gates = {}
        self._entered = {}
        self.blocked = set()

    def _slot(self, table, k):
        with self._lock:
            return table.setdefault(k, threading.Event())

    def gate(self, k):
        return self._slot(self._gates, k)

    def entered(self, k):
        return self._slot(self._entered, k)

    def complete(self, *a, **kw):
        with self._lock:
            k = self.started
            self.started += 1
        self.entered(k).set()
        if k in self.blocked:
            assert self.gate(k).wait(20)
        return super().complete(*a, **kw)


def expire_blocked(c, model, clock, tid, trigger):
    """Запустить шаг, который встанет в модели, и снять его по сроку. (номер вызова, шаг)."""
    k = model.started
    model.blocked.add(k)
    trigger()
    assert model.entered(k).wait(10)
    step = view(c, tid)["panel"]["step"]
    clock.advance(STEP_TIMEOUT_S + 1)
    assert view(c, tid)["panel"]["state"] == "step_failed"
    assert c.app.state.inflight.since(tid) is None
    return k, step


def release_late(c, model, k, step):
    model.gate(k).set()
    c.app.state.steps.join_step(step)


def rows(tmp_path, sql):
    conn = db(tmp_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def normal_wrong(c, tid):
    attempt(c, tid, "А")
    c.app.state.steps.join()


def test_a_late_hint_publishes_nothing(tmp_path):
    model = Gated()
    c, clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        normal_wrong(c, tid)
        k, step = expire_blocked(c, model, clock, tid, lambda: post(c, tid, "hint", level=1))
        release_late(c, model, k, step)
        v = view(c, tid)
        assert v["hints"] == 0 and v["hints_list"] == []
        assert events_of(tmp_path, "hint") == 0


def test_a_late_mistake_analysis_publishes_nothing(tmp_path):
    model = Gated()
    c, clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        k, step = expire_blocked(c, model, clock, tid, lambda: attempt(c, tid, "А"))
        release_late(c, model, k, step)
        assert view(c, tid)["analysis"] is None
        other = seed(tmp_path, key="Б")  # контроль: без опоздания разбор есть
        normal_wrong(c, other)
        assert view(c, other)["analysis"]


def test_a_late_solution_explanation_publishes_nothing(tmp_path):
    model = Gated()
    c, clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        k, step = expire_blocked(c, model, clock, tid, lambda: attempt(c, tid, "Б"))
        release_late(c, model, k, step)
        assert view(c, tid)["solution"]["explanation"] is None
        other = seed(tmp_path, key="Б")
        attempt(c, other, "Б")
        c.app.state.steps.join()
        assert view(c, other)["solution"]["explanation"]


def test_a_late_explain_publishes_nothing(tmp_path):
    model = Gated()
    c, clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "Б")
        c.app.state.steps.join()
        k, step = expire_blocked(
            c, model, clock, tid, lambda: post(c, tid, "explain", mode="simpler")
        )
        release_late(c, model, k, step)
        assert view(c, tid)["explains"] == []
        assert events_of(tmp_path, "explain") == 0
        other = seed(tmp_path, key="Б")
        attempt(c, other, "Б")
        c.app.state.steps.join()
        post(c, other, "explain", mode="simpler")
        c.app.state.steps.join()
        assert len(view(c, other)["explains"]) == 1


def test_a_late_step_records_no_discrepancy(tmp_path):
    # ответ модели расходится с ключом Б; разбор и подсказка называют разное: запись подсказки не
    # схлопывается с уже лежащей записью разбора
    model = Gated(reply=lambda kind, _c: out("Г") if kind == "mistake" else out("В"))
    c, clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        normal_wrong(c, tid)
        before = len(rows(tmp_path, "SELECT 1 FROM tutor_discrepancy"))
        k, step = expire_blocked(c, model, clock, tid, lambda: post(c, tid, "hint", level=1))
        release_late(c, model, k, step)
        assert len(rows(tmp_path, "SELECT 1 FROM tutor_discrepancy")) == before
        assert view(c, tid)["hints"] == 0
        other = seed(tmp_path, key="Б")  # контроль: без опоздания расхождение записано
        normal_wrong(c, other)
        mid = len(rows(tmp_path, "SELECT 1 FROM tutor_discrepancy"))
        post(c, other, "hint", level=1)
        c.app.state.steps.join()
        assert len(rows(tmp_path, "SELECT 1 FROM tutor_discrepancy")) == mid + 1


def test_two_chats_late_one_is_not_published_beside_the_other(tmp_path):
    model = Gated(default=chat_reply("Ответ."))
    c, clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        k, step = expire_blocked(c, model, clock, tid, lambda: say(c, tid, "Исключи А и В"))
        say(c, tid, "Исключи Г")  # замена идёт и записывается
        c.app.state.steps.join_step(rows(tmp_path, "SELECT MAX(id) FROM tutor_step")[0][0])
        assert [m["question"] for m in view(c, tid)["chat"]] == ["Исключи Г"]
        release_late(c, model, k, step)
        assert [m["question"] for m in view(c, tid)["chat"]] == ["Исключи Г"]
        assert events_of(tmp_path, "chat") == 1


def test_the_panel_never_shows_idle_with_a_stale_run(tmp_path, monkeypatch):
    model = Gated()
    c, _clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        normal_wrong(c, tid)
        k = model.started
        model.blocked.add(k)
        post(c, tid, "hint", level=1)
        assert model.entered(k).wait(10)
        step = view(c, tid)["panel"]["step"]
        real = tutor_state.active_run
        armed = [True]

        def spy(*a, **kw):
            run = real(*a, **kw)  # прогон прочитан ДО конца шага...
            if armed and not threading.current_thread().name.startswith("step-"):
                armed.clear()
                release_late(c, model, k, step)  # ...шаг заканчивается между чтениями
            return run

        monkeypatch.setattr(tutor_state, "active_run", spy)
        v = view(c, tid)
        monkeypatch.undo()
        assert not armed, "шаг должен был закончиться посреди чтения"
        assert v["panel"]["busy"] and v["panel"]["step"] == step  # опрос продолжится
        done = c.get(f"/api/tasks/{tid}/tutor?step={step}", headers=CHILD).json()
        assert done["panel"]["state"] == "idle" and done["hints"] == 1


def test_a_late_worker_cannot_release_the_next_steps_lock(tmp_path):
    model = Gated()
    c, clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        normal_wrong(c, tid)
        k, step = expire_blocked(c, model, clock, tid, lambda: post(c, tid, "hint", level=1))
        model.blocked.add(model.started)  # замена тоже встанет
        k2 = model.started
        post(c, tid, "hint", level=1)
        assert model.entered(k2).wait(10)
        holder = c.app.state.inflight.step_of(tid)
        assert holder is not None and holder != step
        release_late(c, model, k, step)
        assert c.app.state.inflight.step_of(tid) == holder  # опоздавший замок не снял
        started = c.app.state.steps.started
        assert post(c, tid, "hint", level=1).json()["panel"]["state"] == "thinking"
        assert c.app.state.steps.started == started, "третий шаг не должен стартовать"
        model.gate(k2).set()
        c.app.state.steps.join()
        assert view(c, tid)["hints"] == 1


def test_inflight_lock_always_has_an_owner_step():
    lock = Inflight()
    with pytest.raises(TypeError):
        lock.acquire(1, 0)  # без номера шага замок не берётся
    assert lock.acquire(1, 0, 7)
    assert not lock.acquire(1, 0, 8)
    with pytest.raises(TypeError):
        lock.release(1)  # снять без номера шага нельзя
    lock.release(1, 3)  # чужой шаг
    assert lock.step_of(1) == 7
    lock.release(1, 7)
    assert lock.since(1) is None and lock.acquire(1, 0, 9)


def events_of(tmp_path, kind):
    return len(rows(tmp_path, f"SELECT 1 FROM event WHERE type = '{kind}'"))


def expire_after_discrepancy(c, clock, monkeypatch):
    """Шаг снимается по сроку сразу после записи расхождения - между двумя записями одного шага."""
    real = tutor_state.record_discrepancy

    def wrapped(conn, task, run_id, out, now, step_id=None):
        real(conn, task, run_id, out, now, step_id=step_id)
        side = db(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent)
        try:
            steps.finish(side, step_id, "failed", clock(), error="timeout")
        finally:
            side.close()

    monkeypatch.setattr(tutor_state, "record_discrepancy", wrapped)


def test_a_step_expired_between_its_writes_does_not_mark_the_solution_disputed(
    tmp_path, monkeypatch
):
    model = Gated(default=out("В"))  # решение модели расходится с ключом
    c, clock = boot_bg(tmp_path, model)
    with c:
        expire_after_discrepancy(c, clock, monkeypatch)
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "Б")
        c.app.state.steps.join()
        assert len(rows(tmp_path, "SELECT 1 FROM tutor_discrepancy")) == 1
        assert view(c, tid)["solution"]["disputed"] is False
        assert [r[0] for r in rows(tmp_path, "SELECT error FROM tutor_step")][-1] == "timeout"


def test_an_explain_step_expired_between_its_writes_does_not_mark_the_solution_disputed(
    tmp_path, monkeypatch
):
    model = Gated(reply=lambda kind, _c: out("В") if kind.startswith("explain_") else out("Б"))
    c, clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "Б")
        c.app.state.steps.join()
        expire_after_discrepancy(c, clock, monkeypatch)
        post(c, tid, "explain", mode="simpler")
        c.app.state.steps.join()
        assert len(rows(tmp_path, "SELECT 1 FROM tutor_discrepancy")) == 1
        assert view(c, tid)["solution"]["disputed"] is False
        assert [r[0] for r in rows(tmp_path, "SELECT error FROM tutor_step")][-1] == "timeout"


def test_a_finished_step_never_reports_a_stale_run(tmp_path, monkeypatch):
    """Перегенерация не удалась: шаг пишет общую подсказку и кончается с «помощник не отвечает».
    Ответ, который сообщает об окончании шага, обязан показывать эту подсказку."""
    seen = {"hint1": 0}

    def reply(kind, _content):
        if kind == "hint1":
            seen["hint1"] += 1
            return None if seen["hint1"] > 1 else out()  # первая попытка есть, вторая падает
        return out()

    model = Gated(reply=reply, judge=lambda _c: {"reveals": True, "reason": "раскрывает"})
    c, _clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        normal_wrong(c, tid)
        k = model.started
        model.blocked.add(k)
        post(c, tid, "hint", level=1)
        assert model.entered(k).wait(10)
        step = view(c, tid)["panel"]["step"]
        real = tutor_state.active_run
        armed = [True]

        def spy(*a, **kw):
            run = real(*a, **kw)  # прогон прочитан до конца шага
            if armed and not threading.current_thread().name.startswith("step-"):
                armed.clear()
                release_late(c, model, k, step)
            return run

        monkeypatch.setattr(tutor_state, "active_run", spy)
        mid = c.get(f"/api/tasks/{tid}/tutor?step={step}", headers=CHILD).json()
        monkeypatch.undo()
        assert not armed
        assert mid["panel"]["busy"] or mid["hints"] == 1, "итог шага при устаревшем прогоне"
        done = c.get(f"/api/tasks/{tid}/tutor?step={step}", headers=CHILD).json()
        assert done["panel"]["state"] == "down" and done["hints"] == 1


def test_an_older_steps_result_never_hides_a_newer_running_step(tmp_path):
    """Шаг A кончился отказом модели, шаг B уже идёт: ответ занят шагом B и несёт его номер."""
    seen = {"hint1": 0}

    def reply(kind, _content):
        if kind == "hint1":
            seen["hint1"] += 1
            return None if seen["hint1"] == 1 else out()  # A: модель не отвечает
        return out()

    model = Gated(reply=reply)
    c, _clock = boot_bg(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        normal_wrong(c, tid)
        post(c, tid, "hint", level=1)  # шаг A
        c.app.state.steps.join()
        step_a = rows(tmp_path, "SELECT MAX(id) FROM tutor_step")[0][0]
        k = model.started
        model.blocked.add(k)
        post(c, tid, "hint", level=1)  # шаг B встаёт в модели
        assert model.entered(k).wait(10)
        step_b = rows(tmp_path, "SELECT MAX(id) FROM tutor_step")[0][0]
        assert step_b != step_a
        for resp in (
            c.get(f"/api/tasks/{tid}/tutor?step={step_a}", headers=CHILD),  # GET с номером A
            post(c, tid, "hint", level=1),  # POST, отказанный из-за шага B
        ):
            panel = resp.json()["panel"]
            assert panel["busy"] and panel["step"] == step_b, panel
        html = c.get(f"/tasks/{tid}/tutor?step={step_a}", headers=CHILD).text
        assert f'data-step="{step_b}"' in html
        model.gate(k).set()
        c.app.state.steps.join()
        # итог B показывается по его номеру; итог A с устаревшим номером - нет
        done = c.get(f"/api/tasks/{tid}/tutor?step={step_b}", headers=CHILD).json()
        assert done["hints"] == 1 and done["panel"]["state"] == "idle"
        stale = c.get(f"/api/tasks/{tid}/tutor?step={step_a}", headers=CHILD).json()
        assert stale["panel"]["state"] == "idle"
