"""Имитация олимпиады (OH-K3a): формат родителя, допуск, сборка, запрет помощи, срок, итог."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import exam
from app.ingest.answers import key_json
from app.ingest.store import open_db
from tests.tutor_helpers import CHILD, PARENT, Scripted, attempt, boot, db, post, seed

NUM = [{"label": str(i), "text": f"цифра {i}"} for i in range(1, 7)]
FIGURE = {
    "alt": "Продольный разрез цветка, цифрами 1-6 обозначены части",
    "teacher_note": "1 - тычинка, 2 - пестик, 3 - лепесток, 4 - чашелистик, 5 - завязь, 6 - рыльце",
}
FORMAT = {
    "stage": "school", "grade": "7", "minutes": "10",
    "count_single": "1", "count_multiple": "1", "count_match": "1", "count_numeric": "1",
    "points_single": "1", "points_multiple": "2", "points_match": "3", "points_numeric": "4",
}  # fmt: skip

NODE = shutil.which("node")


def save_format(c, tmp_path, materials=True, **over):
    data = {**FORMAT, **over}
    if materials:
        conn = db(tmp_path)
        data["material"] = [r["id"] for r in conn.execute("SELECT id FROM material")]
        conn.close()
    r = c.post("/parent/exam", data=data, headers=PARENT, follow_redirects=False)
    assert r.status_code == 303, r.text


def sql(tmp_path, query, *args):
    conn = db(tmp_path)
    try:
        conn.execute(query, args)
    finally:
        conn.close()


def four(tmp_path):
    """Одно задание каждого типа в одном разделе; ключи: Б, А+В, 1-А 2-Б, 28."""
    ids = [
        seed(tmp_path, type="single", key="Б", number="1"),
        seed(tmp_path, type="multiple", key="А, В", number="2"),
        seed(tmp_path, type="match", key="1-А, 2-Б", number="3"),
        seed(tmp_path, type="numeric", key="28", number="4"),
    ]
    return ids


def start_real(c, **over):
    body = {"mode": "real", "material_id": 1, "section": None, "variant": None, "stage": None}
    return c.post("/exam/start", json={**body, **over}, headers=CHILD)


def form_of(c):
    """Скрытые поля бланка: прохождение, ревизия, показанная позиция, идентификатор действия."""
    r = c.get("/exam/run", headers=CHILD)
    assert r.status_code == 200, r.text
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', r.text))


def act(c, action, form=None, **fields):
    data = {**(form or form_of(c)), "action": action, **fields}
    return c.post(
        "/exam/act", data=data, headers={**CHILD, "Origin": "http://testserver"},
        follow_redirects=False,
    )  # fmt: skip


def keep(c):
    """Поля показанного задания с сохранённым ответом: браузер шлёт их с каждым переходом."""
    view = current(c)[1]
    it = view["items"][view["current"] - 1]
    answer = it["answer"]
    if it["type"] == "numeric":
        return {"n": answer or ""}
    if it["type"] == "match":
        return {f"m.{k}": v for k, v in (answer or {}).items()}
    return {"a": answer or []}


def nav(c, position):
    return act(c, f"open:{position}", **keep(c))


def at(c, position, action="save", **fields):
    """Показать задание position (переход) и выполнить действие над ним; всегда 303."""
    if int(form_of(c)["shown"]) != position:
        assert nav(c, position).status_code == 303
    r = act(c, action, **(fields or keep(c)))
    assert r.status_code == 303, r.text
    return r


def submit(c):
    r = act(c, "submit", **keep(c))
    assert r.status_code == 303 and "/exam/results/" in r.headers["location"], r.text
    return c.get("/api" + r.headers["location"], headers=CHILD).json()


def current(c):
    r = c.get("/api/exam/current", headers=CHILD)
    return r.status_code, r.json()


def test_no_format_means_no_variant(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        state = c.get("/api/exam", headers=CHILD).json()
        assert state["format"] is None and "формат не задан" in state["message"].lower()
        page = c.get("/exam", headers=CHILD)
        assert "формат не задан — попроси родителя" in page.text.lower()
        r = start_real(c)
        assert r.status_code == 409 and r.json()["code"] == "no_format"
        r = c.post("/exam/start", json={"mode": "format"}, headers=CHILD)
        assert r.status_code == 409 and r.json()["code"] == "no_format"
        assert db(tmp_path).execute("SELECT COUNT(*) FROM exam_attempt").fetchone()[0] == 0
        # родитель сохраняет формат: числа проверяются, ребёнок не может
        assert c.post("/parent/exam", data=FORMAT, headers=CHILD).status_code == 403
        bad = c.post("/parent/exam", data={**FORMAT, "stage": "x"}, headers=PARENT)
        assert bad.status_code == 422
        save_format(c, tmp_path)
        state = c.get("/api/exam", headers=CHILD).json()
        assert state["format"]["minutes"] == 10 and state["format"]["counts"]["match"] == 1
        assert state["format"]["stage_label"] == "школьный" and state["format"]["grade"] == 7
        assert start_real(c).status_code == 200


def test_real_variant_takes_only_admitted_tasks_in_order(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        official = seed(tmp_path, key="А", number="1")
        parent_key = seed(tmp_path, key="Б", number="2")
        model_only = seed(tmp_path, key=None, model_answer="В", number="3")
        generated = seed(tmp_path, key="Г", number="4")
        draft = seed(tmp_path, key="А", number="5")
        last = seed(tmp_path, type="numeric", key="7", number="6")
        sql(tmp_path, "UPDATE task_item SET key_source = 'official' WHERE id = ?", official)
        sql(tmp_path, "UPDATE task_item SET key_source = 'generated' WHERE id = ?", generated)
        sql(tmp_path, "UPDATE task_item SET import_status = 'draft' WHERE id = ?", draft)
        save_format(c, tmp_path)
        group = c.get("/api/exam", headers=CHILD).json()["groups"][0]
        assert group["admitted"] == 3 and sorted(group["skipped"]) == ["3", "4", "5"]
        page = c.get("/exam", headers=CHILD).text
        assert "не войдут (нет подтверждённого ключа)" in page
        assert start_real(c).status_code == 200
        code, view = current(c)
        assert code == 200
        assert [i["number"] for i in view["items"]] == ["1", "2", "6"]
        assert model_only and parent_key and last
        assert "answer_key" not in json.dumps(view) and "key" not in view["items"][0]
        # снимок: те же задания и в базе
        rows = (
            db(tmp_path).execute("SELECT task_item_id FROM exam_item ORDER BY position").fetchall()
        )
        assert [r[0] for r in rows] == [official, parent_key, last]


def test_format_variant_reports_shortage_per_type(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        seed(tmp_path, type="single", key="А", number="1")
        seed(tmp_path, type="single", key="Б", number="2")
        seed(tmp_path, type="single", key=None, model_answer="А", number="3")
        seed(tmp_path, type="numeric", key="5", number="4")
        save_format(c, tmp_path, count_single="3", count_multiple="1", count_match="0",
                    count_numeric="2", points_match="0")  # fmt: skip
        state = c.get("/api/exam", headers=CHILD).json()
        assert state["stock"]["single"] == {"label": "один ответ", "need": 3, "have": 2}
        assert state["stock"]["multiple"]["have"] == 0 and state["stock"]["numeric"]["have"] == 1
        r = c.post("/exam/start", json={"mode": "format"}, headers=CHILD)
        body = r.json()
        assert r.status_code == 409 and body["code"] == "shortage"
        assert body["shortage"]["single"]["need"] == 3 and body["shortage"]["single"]["have"] == 2
        assert (
            body["shortage"]["multiple"]["have"] == 0 and body["shortage"]["numeric"]["have"] == 1
        )
        assert "match" not in body["shortage"]
        assert db(tmp_path).execute("SELECT COUNT(*) FROM exam_attempt").fetchone()[0] == 0
        # меньший вариант - только по согласию; недопущенное задание (3) не входит
        r = c.post("/exam/start", json={"mode": "format", "accept_reduced": True}, headers=CHILD)
        assert r.status_code == 200
        _, view = current(c)
        assert sorted(i["number"] for i in view["items"]) == ["1", "2", "4"] and view["reduced"]
        done = submit(c)
        assert done["max_score"] == 1 + 1 + 4 and done["total"] == 3  # максимум уменьшенного
        # нет ни одного допущенного задания: вариант не создаётся и по согласию
        sql(tmp_path, "UPDATE task_item SET key_source = 'generated'")
        r = c.post("/exam/start", json={"mode": "format", "accept_reduced": True}, headers=CHILD)
        assert r.status_code == 422 and r.json()["code"] == "empty"
        assert db(tmp_path).execute("SELECT COUNT(*) FROM exam_attempt").fetchone()[0] == 1
        r = start_real(c)
        assert r.status_code == 422 and r.json()["code"] == "empty"


def test_running_variant_blocks_every_route_and_hides_key(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        inside = four(tmp_path)
        figure = seed(tmp_path, key="5", number="9", options=NUM, figure=FIGURE)
        free = seed(tmp_path, key="А", number="500")
        sql(tmp_path, "UPDATE task_item SET section = 'S' WHERE id = ?", figure)
        sql(tmp_path, "UPDATE task_item SET section = 'S' WHERE id = ?", free)
        # прошлое занятие: ошибка по заданию варианта и подсказка
        task = inside[0]
        sql(tmp_path, "UPDATE task_item SET source_number = '777' WHERE id = ?", task)
        assert attempt(c, task, "А", "sure").status_code == 200
        assert post(c, task, "hint").status_code == 200
        before = c.get("/api/my", headers=CHILD).text
        assert "777" in c.get("/today", headers=CHILD).text
        assert before
        save_format(c, tmp_path)
        assert start_real(c, section=None).status_code == 200
        # десять маршрутов задания: 409 «идёт олимпиада», в том числе для HTMX
        calls = [
            ("POST", f"/tasks/{task}/attempt", {"answer": "Б"}),
            ("POST", f"/tasks/{task}/hint", {}),
            ("POST", f"/tasks/{task}/explain", {"mode": "simpler"}),
            ("POST", f"/tasks/{task}/chat", {"message": "что это?", "mode": "chat"}),
            ("POST", f"/tasks/{task}/chat", {"message": "что это?", "mode": "formulate"}),
            ("POST", f"/tasks/{task}/show-answer", {"confirm": True}),
            ("POST", f"/tasks/{task}/repeat", {}),
            ("POST", f"/tasks/{task}/dispute", {}),
            ("POST", f"/tasks/{task}/voice", {"audio": "AAAA", "mime": "audio/webm"}),
            ("GET", f"/api/tasks/{task}/tutor", None),
            ("GET", f"/tasks/{task}/tutor", None),
            ("GET", f"/tasks/{task}", None),
        ]
        for extra in ({}, {"HX-Request": "true"}):
            for method, url, body in calls:
                r = c.request(method, url, json=body, headers={**CHILD, **extra})
                assert r.status_code == 409, (method, url, extra, r.status_code)
                assert "идёт олимпиада" in r.text, (method, url)
                assert "Б" not in r.text.replace("Сейчас", "") or "вариант" in r.text
        # задание вне варианта работает как обычно
        assert attempt(c, free, "А").status_code == 200
        assert c.get(f"/tasks/{free}").status_code == 200
        # в итогах, «Сегодня» и «Закончить» заданий варианта нет, в том числе прошлых занятий
        sid = db(tmp_path).execute("SELECT MAX(id) FROM study_session").fetchone()[0]
        fin = c.post("/session/finish", json={}, headers=CHILD)
        assert fin.status_code == 200 and "777" not in fin.text and "не то" not in fin.text
        for url in ("/my", "/api/my", f"/api/my/sessions/{sid}", "/today"):
            r = c.get(url, headers=CHILD)
            assert r.status_code == 200, url
            assert "777" not in r.text, url
            assert f"/tasks/{task}" not in r.text, url
        # каталог, поиск, рисунок: ни ключа, ни признака верности
        for url in ("/api/tasks", f"/api/tasks/{task}", "/tasks", "/api/search?q=Что",
                    f"/api/tasks/{figure}", f"/tasks/{figure}/figure.png"):  # fmt: skip
            r = c.get(url, headers=CHILD)
            assert r.status_code == 200, url
            for word in ("answer_key", "model_answer", "correct", "key_source", "teacher_note"):
                assert word not in r.text, (url, word)
        code, view = current(c)
        assert code == 200
        for word in ("answer_key", "correct", "hint", "key"):
            assert word not in json.dumps(view), word
        # итог идущего варианта недоступен
        assert c.get("/api/exam/results/1", headers=CHILD).status_code == 409


class StartsExam(Scripted):
    """Модель, ответ которой приходит уже после старта олимпиады по этому заданию."""

    def __init__(self, tmp_path, task_id):
        super().__init__()
        self.path, self.task_id, self.fired = tmp_path / "olymp.db", task_id, False

    def complete(self, *a, **kw):
        if not self.fired:
            self.fired = True
            conn = open_db(self.path)
            try:
                exam.start(conn, kw_now(), mode="real", material_id=1)
            finally:
                conn.close()
        return super().complete(*a, **kw)


NOW = {}


def kw_now():
    return NOW["clock"]()


def test_pending_helper_answer_is_withheld_after_start(tmp_path):
    model = StartsExam(tmp_path, 0)
    c, clock, _ = boot(tmp_path, model=model)
    NOW["clock"] = clock
    with c:
        task = seed(tmp_path, key="Б", number="1")
        save_format(c, tmp_path, count_multiple="0", count_match="0", count_numeric="0",
                    points_multiple="0", points_match="0", points_numeric="0")  # fmt: skip
        r = attempt(c, task, "А", "sure")  # неверно: нужен разбор ошибки у модели
        assert r.status_code == 409 and "идёт олимпиада" in r.text
        assert "Разбор." not in r.text
        run = db(tmp_path).execute("SELECT analysis_json FROM tutor_run").fetchone()
        assert run["analysis_json"] is None
        assert db(tmp_path).execute("SELECT COUNT(*) FROM exam_attempt").fetchone()[0] == 1


def test_pending_helper_answer_is_withheld_for_htmx(tmp_path):
    model = StartsExam(tmp_path, 0)
    c, clock, _ = boot(tmp_path, model=model)
    NOW["clock"] = clock
    with c:
        task = seed(tmp_path, key="Б", number="1")
        save_format(c, tmp_path, count_multiple="0", count_match="0", count_numeric="0",
                    points_multiple="0", points_match="0", points_numeric="0")  # fmt: skip
        r = c.post(
            f"/tasks/{task}/attempt",
            json={"answer": "А", "confidence": "sure"},
            headers={**CHILD, "HX-Request": "true"},
        )
        assert r.status_code == 409 and "идёт олимпиада" in r.text
        assert "Разбор." not in r.text and "не то" not in r.text


def test_deadline_single_attempt_and_idempotent_submit(tmp_path):
    c, clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        first = start_real(c).json()["attempt_id"]
        _, view = current(c)
        at(c, 1, "save", a="Б")
        at(c, 2, "mark")
        clock.advance(30)
        # повторный старт и «перезагрузка»: то же прохождение, срок не сброшен, ответы на месте
        again = start_real(c).json()["attempt_id"]
        assert again == first
        _, reloaded = current(c)
        assert reloaded["deadline_at"] == view["deadline_at"]
        assert reloaded["items"][0]["answer"] == "Б" and reloaded["items"][1]["marked"]
        assert db(tmp_path).execute("SELECT COUNT(*) FROM exam_attempt").fetchone()[0] == 1
        # переход закрывает прежний интервал просмотра
        at(c, 3, "save")
        at(c, 2, "save")
        opened = db(tmp_path).execute("SELECT COUNT(*) FROM exam_view WHERE closed_at IS NULL")
        assert opened.fetchone()[0] == 1
        # последняя секунда ещё внутри срока, граница - уже нет
        clock.advance(569)
        at(c, 4, "save", n="28")
        form = form_of(c)
        clock.advance(1)
        late = act(c, "save", form, a="А")
        assert late.status_code == 303 and "/exam/results/" in late.headers["location"]
        row = db(tmp_path).execute("SELECT * FROM exam_attempt").fetchone()
        assert row["status"] == "submitted" and row["end_reason"] == "time"
        assert (
            db(tmp_path).execute("SELECT answer FROM exam_item WHERE position = 1").fetchone()[0]
            == '"Б"'
        )

    # срок вышел, пока сервер не работал: после перезапуска вариант закрыт по времени
    c2, clock2, _ = boot(tmp_path / "second")
    with c2:
        four(tmp_path / "second")
        save_format(c2, tmp_path / "second")
        start_real(c2)
        clock2.advance(601)
        app2 = c2.app
        with TestClient(app2) as restarted:  # новый запуск приложения на той же базе
            assert restarted.get("/api/exam/current", headers=CHILD).status_code == 404
            res = restarted.get("/api/exam/results/1", headers=CHILD).json()
            assert res["status"] == "submitted" and res["end_reason"] == "time"
            assert restarted.get("/api/exam", headers=CHILD).json()["running_id"] is None

    # сдача: повтор той же формы идемпотентен, ответы после неё не меняются
    c3, clock3, _ = boot(tmp_path / "third")
    with c3:
        four(tmp_path / "third")
        save_format(c3, tmp_path / "third")
        attempt_id = start_real(c3).json()["attempt_id"]
        at(c3, 1, "save", a="Б")
        form, kept = form_of(c3), keep(c3)
        one = act(c3, "submit", form, **kept)
        clock3.advance(5)
        two = act(c3, "submit", form, **kept)  # второе нажатие «сдать» с той же страницы
        assert one.status_code == two.status_code == 303
        assert one.headers["location"] == two.headers["location"] == f"/exam/results/{attempt_id}"
        done = c3.get(f"/api/exam/results/{attempt_id}", headers=CHILD).json()
        assert done["score"] == 1
        after = act(c3, "save", form, a="А")
        assert after.status_code == 303  # просто ведёт к итогу
        row = db(tmp_path / "third").execute("SELECT answer FROM exam_item WHERE position = 2")
        assert row.fetchone()[0] is None
        assert c3.get(f"/api/exam/results/{attempt_id}", headers=CHILD).json() == done
        assert c3.get(f"/exam/results/{attempt_id}", headers=CHILD).status_code == 200


def test_result_uses_start_snapshot_score_and_view_time(tmp_path):
    c, clock, _ = boot(tmp_path)
    with c:
        single, multiple, match, numeric = four(tmp_path)
        save_format(c, tmp_path)
        attempt_id = start_real(c).json()["attempt_id"]  # позиция 1 открыта в момент старта

        # после старта родитель меняет ключ и баллы: на это прохождение они не влияют
        sql(tmp_path, "UPDATE task_item SET answer_key = ? WHERE id = ?",
            key_json("А", "single"), single)  # fmt: skip
        save_format(c, tmp_path, points_single="9", points_numeric="9")
        clock.advance(10)
        assert act(c, "save", a="Б").status_code == 303  # верно по ключу на момент старта
        assert nav(c, 2).status_code == 303
        clock.advance(20)
        assert act(c, "save", a="А").status_code == 303  # неверно: ключ А+В
        assert nav(c, 3).status_code == 303  # пропуск
        clock.advance(5)
        assert nav(c, 1).status_code == 303  # возврат
        clock.advance(7)
        assert nav(c, 4).status_code == 303
        assert act(c, "save", n="28,0").status_code == 303  # число в другой записи
        assert (
            db(tmp_path).execute("SELECT answer FROM exam_item WHERE position = 4").fetchone()[0]
            == '"28"'
        )
        clock.advance(3)
        done = submit(c)
        assert done["score"] == 1 + 4 and done["max_score"] == 1 + 2 + 3 + 4
        assert done["correct"] == 2 and done["total"] == 4
        by_pos = {i["position"]: i for i in done["items"]}
        assert [by_pos[p]["seconds"] for p in (1, 2, 3, 4)] == [17, 20, 5, 3]
        assert [by_pos[p]["points"] for p in (1, 2, 3, 4)] == [1, 0, 0, 4]
        assert not by_pos[3]["answered"] and by_pos[2]["answered"]
        assert by_pos[1]["review_url"] is None
        assert by_pos[2]["review_url"] == f"/tasks/{multiple}"
        assert by_pos[3]["review_url"] == f"/tasks/{match}"
        assert done["items"][3]["max_points"] == 4 and numeric
        page = c.get(f"/exam/results/{attempt_id}", headers=CHILD).text
        assert "5 из 10" in page and f"/tasks/{multiple}" in page
        # §1.3 снова действует: разбор неверного задания работает обычным порядком
        r = attempt(c, multiple, "А")
        assert r.status_code == 200 and r.json()["state"] in ("attempted_wrong", "hint1")
        assert c.get(f"/tasks/{match}").status_code == 200
        # решение не открыто само: ответа в итоге нет
        assert "answer_key" not in json.dumps(done)


def test_stale_tab_old_results_and_expired_start(tmp_path):
    c, clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        a = start_real(c).json()["attempt_id"]
        at(c, 1, "save", a="Б")
        old = form_of(c)
        assert act(c, "submit", old, **keep(c)).status_code == 303
        assert c.get(f"/api/exam/results/{a}", headers=CHILD).status_code == 200
        b = start_real(c).json()["attempt_id"]
        assert b != a
        # старая вкладка не меняет новый вариант; результаты старого скрыты, пока идёт новый
        stale = act(c, "save", {**old, "action_id": "other"}, a="А")
        assert stale.status_code == 409
        assert (
            db(tmp_path)
            .execute("SELECT answer FROM exam_item WHERE attempt_id = ? AND position = 1", (b,))
            .fetchone()[0]
            is None
        )
        assert c.get(f"/api/exam/results/{a}", headers=CHILD).status_code == 409
        assert act(c, "submit", {**old, "action_id": "o2"}, **keep(c)).status_code == 303
        assert c.get("/api/exam", headers=CHILD).json()["history"] == []
        # срок вышел, новый старт отклонён нехваткой: закрытие по времени сохранено
        clock.advance(601)
        c.post(
            "/parent/exam",
            data={**FORMAT, "count_single": "5", "material": [1]},
            headers=PARENT,
        )
        r = c.post("/exam/start", json={"mode": "format"}, headers=CHILD)
        assert r.status_code == 409 and r.json()["code"] == "shortage"
        row = db(tmp_path).execute("SELECT status, end_reason FROM exam_attempt WHERE id = ?", (b,))
        assert tuple(row.fetchone()) == ("submitted", "time")
        assert c.get(f"/api/exam/results/{a}", headers=CHILD).status_code == 200


def test_figure_comes_from_snapshot(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        fig = seed(tmp_path, key="5", number="1", options=NUM, figure=FIGURE)
        save_format(c, tmp_path, count_multiple="0", count_match="0", count_numeric="0",
                    points_multiple="0", points_match="0", points_numeric="0")  # fmt: skip
        start_real(c)
        first = c.get("/exam/figure/1.png", headers=CHILD)
        assert first.status_code == 200 and first.content[:4] == b"\x89PNG"
        sql(
            tmp_path,
            "UPDATE task_item SET import_status = 'draft', figure = NULL WHERE id = ?",
            fig,
        )
        assert c.get("/exam/figure/1.png", headers=CHILD).content == first.content
        assert "/exam/figure/1.png" in json.dumps(current(c)[1])


def test_empty_collection_clears_answer(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        start_real(c)
        at(c, 2, "save", a=["А"])
        assert current(c)[1]["items"][1]["answer"] == ["А"]
        at(c, 2, "save", cleared="1")  # ничего не выбрано
        at(c, 3, "save", **{"m.1": "А"})
        assert current(c)[1]["items"][2]["answer"] == {"1": "А"}  # частичное соответствие как есть
        at(c, 3, "save", cleared="1")
        items = current(c)[1]["items"]
        assert items[1]["answer"] is None and items[2]["answer"] is None


def test_lock_wins_over_404_and_422_and_skips_list_everything(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        task, *_ = four(tmp_path)
        extra = seed(tmp_path, key="А", number="8")
        sql(tmp_path, "UPDATE task_item SET excluded = 1 WHERE id = ?", extra)
        weird = seed(tmp_path, key="А", number="9")
        sql(tmp_path, "UPDATE task_item SET import_status = 'unsupported' WHERE id = ?", weird)
        save_format(c, tmp_path)
        group = c.get("/api/exam", headers=CHILD).json()["groups"][0]
        assert sorted(group["skipped"]) == ["8", "9"]
        assert start_real(c).status_code == 200
        # родитель правит условие во время варианта: задание уже не опубликовано
        sql(tmp_path, "UPDATE task_item SET import_status = 'draft' WHERE id = ?", task)
        assert c.get(f"/tasks/{task}", headers=CHILD).status_code == 409
        assert c.get(f"/api/tasks/{task}/tutor", headers=CHILD).status_code == 409
        r = c.post(f"/tasks/{task}/hint", json={"level": "не число"}, headers=CHILD)
        assert r.status_code == 409
        r = c.post(
            f"/tasks/{task}/attempt", content=b"{bad", headers={"content-type": "application/json"}
        )
        assert r.status_code == 409
        assert "server_now" in current(c)[1]


def test_background_step_finishing_during_the_exam_shows_nothing(tmp_path):
    """Шаг в фоне начат до старта и кончается во время олимпиады: ни ответ POST, ни опрос
    (с номером шага и без) его итога не показывают; отложенный итог не сохраняется, а после сдачи
    помощь работает штатно: подсказку можно взять заново."""
    from tests.bank_helpers import build_app
    from tests.tutor_helpers import Blocking, Clock

    model, clock = Blocking(), Clock()
    c = TestClient(build_app(tmp_path, model, clock=clock, sync_steps=False))
    with c:
        task = seed(tmp_path, key="Б", number="1")
        save_format(c, tmp_path, count_multiple="0", count_match="0", count_numeric="0",
                    points_multiple="0", points_match="0", points_numeric="0")  # fmt: skip
        model.release.set()
        assert attempt(c, task, "А", "sure").status_code == 200  # неверно: разбор в фоне
        c.app.state.steps.join()
        model.release.clear()
        model.entered.clear()
        first = post(c, task, "hint", level=1)
        assert first.status_code == 200 and first.json()["panel"]["state"] == "thinking"
        step = first.json()["panel"]["step"]
        assert model.entered.wait(10)
        conn = open_db(tmp_path / "olymp.db")
        try:
            exam.start(conn, clock(), mode="real", material_id=1)
        finally:
            conn.close()
        model.release.set()
        c.app.state.steps.join()
        for extra in ({}, {"HX-Request": "true"}):
            for url in (
                f"/api/tasks/{task}/tutor",
                f"/api/tasks/{task}/tutor?step={step}",
                f"/tasks/{task}/tutor?step={step}",
            ):
                r = c.get(url, headers={**CHILD, **extra})
                assert r.status_code == 409 and "идёт олимпиада" in r.text, url
                assert "hints_list" not in r.text and "Подсказка" not in r.text, url
        # после сдачи §1.3 действует как обычно: экран открыт, подсказка берётся заново
        assert act(c, "submit", **keep(c)).status_code == 303
        assert c.get(f"/api/tasks/{task}/tutor", headers=CHILD).status_code == 200
        again = post(c, task, "hint", level=1)
        assert again.status_code == 200 and again.json()["panel"]["state"] != "exam_running"


def _start_in_between(tmp_path, clock, monkeypatch):
    """snapshot_live (между замком маршрута и снимком чтения) запускает олимпиаду: гонка."""
    from app.tutor import web

    real, fired = web.snapshot_live, []

    def racing(*a, **kw):
        if not fired:
            fired.append(1)
            conn = open_db(tmp_path / "olymp.db")
            try:
                exam.start(conn, clock(), mode="real", material_id=1)
            finally:
                conn.close()
        return real(*a, **kw)

    monkeypatch.setattr(web, "snapshot_live", racing)
    return fired


def test_exam_started_between_the_gate_and_the_snapshot_blocks_the_view(tmp_path, monkeypatch):
    c, clock, _ = boot(tmp_path)
    with c:
        task = seed(tmp_path, key="Б", number="1")
        save_format(c, tmp_path, count_multiple="0", count_match="0", count_numeric="0",
                    points_multiple="0", points_match="0", points_numeric="0")  # fmt: skip
        assert attempt(c, task, "Б", "sure").status_code == 200  # решено: ответ виден на экране
        urls = (f"/api/tasks/{task}/tutor?step=1", f"/tasks/{task}/tutor?step=1", f"/tasks/{task}")
        for url in urls:
            sql(tmp_path, "UPDATE exam_attempt SET status = 'submitted', end_reason = 'manual'")
            fired = _start_in_between(tmp_path, clock, monkeypatch)
            r = c.get(url, headers={**CHILD, "HX-Request": "true"})
            assert fired and r.status_code == 409, url
            assert "идёт олимпиада" in r.text and "solution" not in r.text, url
            assert "Решение" not in r.text and "верно" not in r.text.lower(), url
            monkeypatch.undo()


def test_exam_start_and_finish_bump_the_version_and_the_block_carries_it(tmp_path):
    from app.tutor import seq

    c, clock, _ = boot(tmp_path)
    with c:
        task = seed(tmp_path, key="Б", number="1")
        save_format(c, tmp_path, count_multiple="0", count_match="0", count_numeric="0",
                    points_multiple="0", points_match="0", points_numeric="0")  # fmt: skip
        before = seq.current(db(tmp_path))
        pre = c.get(f"/api/tasks/{task}/tutor", headers=CHILD).json()
        assert int(pre["panel"]["version"]) == before
        assert start_real(c).status_code == 200
        started = seq.current(db(tmp_path))
        assert started > before
        blocked = c.get(f"/api/tasks/{task}/tutor", headers=CHILD)
        assert blocked.status_code == 409 and blocked.json()["version"] == started > before
        html = c.get(f"/tasks/{task}/tutor", headers={**CHILD, "HX-Request": "true"})
        assert html.status_code == 409 and f'data-version="{started}"' in html.text
        clock.advance(11 * 60)
        assert c.get("/api/exam/current", headers=CHILD).status_code in (200, 404, 409)
        exam.expire_due(db(tmp_path), clock())
        assert seq.current(db(tmp_path)) > started


def _start_before_snapshot(tmp_path, clock, monkeypatch):
    """Вход в снимок чтения (после предварительной проверки) запускает новый вариант: гонка."""
    from app.tutor import seq

    real, fired = seq.read_snapshot, []

    def racing(conn):
        if not fired:
            fired.append(1)
            other = open_db(tmp_path / "olymp.db")
            try:
                exam.start(other, clock(), mode="real", material_id=1)
            finally:
                other.close()
        return real(conn)

    monkeypatch.setattr(seq, "read_snapshot", racing)
    return fired


def _submitted_attempt(c):
    a = start_real(c).json()["attempt_id"]
    at(c, 1, "save", a="Б")
    assert act(c, "submit", **keep(c)).status_code == 303
    return a


def test_result_started_between_the_check_and_the_read_gives_no_key(tmp_path, monkeypatch):
    c, clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        a = _submitted_attempt(c)
        fired = _start_before_snapshot(tmp_path, clock, monkeypatch)
        r = c.get(f"/api/exam/results/{a}", headers=CHILD)
        assert fired and r.status_code == 409, r.text
        assert "correct" not in r.text and "points" not in r.text


def test_history_started_between_the_check_and_the_read_lists_nothing(tmp_path, monkeypatch):
    c, clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        _submitted_attempt(c)
        fired = _start_before_snapshot(tmp_path, clock, monkeypatch)
        r = c.get("/api/exam", headers=CHILD)
        assert fired and r.json()["history"] == [], r.text


def test_option_labels_carry_the_token_the_answer_check_uses(tmp_path):
    from app.ingest.answers import label_key

    c, _clock, _ = boot(tmp_path)
    with c:
        sets = (["А)", "Б)", "В)", "Г)"], ["а", "б", "в", "г"], ["A", "B", "C", "D"])
        for n, labels in enumerate(sets, 1):
            opts = [{"label": x, "text": f"вариант {x}"} for x in labels]
            seed(tmp_path, options=opts, key="Б", number=str(n))
        save_format(c, tmp_path)
        assert start_real(c).status_code == 200
        items = current(c)[1]["items"]
        assert len(items) == 3
        for it in items:
            for o in it["options"]:
                assert o["token"] == label_key(o["label"])
            assert [o["token"] for o in it["options"]][1] in ("Б", "В")
        assert [o["token"] for o in items[0]["options"]] == ["А", "Б", "В", "Г"]
        assert [o["token"] for o in items[1]["options"]] == ["А", "Б", "В", "Г"]
        assert [o["token"] for o in items[2]["options"]] == ["А", "В", "С", "D"]
        # ответ, выбранный по метке «Б)», сохраняется ключом и совпадает с ключом варианта
        pos = items[0]["position"]
        at(c, pos, "save", a="Б)")
        assert current(c)[1]["items"][0]["answer"] == items[0]["options"][1]["token"]


def test_screens_with_past_work_started_between_the_check_and_the_read_hide_the_tasks(
    tmp_path, monkeypatch
):
    """Сегодня, Мои итоги и итог занятия: вариант стартовал между проверкой и чтением - заданий
    варианта (текст, ответ, верность) в ответе нет. Без гонки они на месте (контроль)."""
    c, clock, _ = boot(tmp_path)
    with c:
        task = seed(tmp_path, key="Б", number="1")
        save_format(c, tmp_path, count_multiple="0", count_match="0", count_numeric="0",
                    points_multiple="0", points_match="0", points_numeric="0")  # fmt: skip
        assert (
            attempt(c, task, "А", "sure").status_code == 200
        )  # неверно: будет в итоге и в «Сегодня»
        sid = c.post("/session/finish", json={}, headers=CHILD).json()["session_id"]
        urls = ("/today", "/my", "/api/my", f"/api/my/sessions/{sid}")
        marks = (
            "цитология",
            f"/tasks/{task}",
            "ошибся",
            "ошибка",
        )  # текст, ссылка, причина разбора
        for url in urls:
            sql(tmp_path, "UPDATE exam_attempt SET status = 'submitted', end_reason = 'manual'")
            r = c.get(url, headers=CHILD)
            assert r.status_code == 200 and any(m in r.text for m in marks), url
            fired = _start_before_snapshot(tmp_path, clock, monkeypatch)
            r = c.get(url, headers=CHILD)
            assert fired and r.status_code == 200, url
            assert not any(m in r.text for m in marks), url
            monkeypatch.undo()
        # «Закончить» новое занятие: итог в ответе тоже без заданий идущего варианта
        sql(tmp_path, "UPDATE exam_attempt SET status = 'submitted', end_reason = 'manual'")
        clock.advance(60)
        assert attempt(c, task, "А", "sure").status_code == 200
        fired = _start_before_snapshot(tmp_path, clock, monkeypatch)
        r = c.post("/session/finish", json={}, headers=CHILD)
        assert fired and r.status_code == 200, r.text
        assert not any(m in r.text for m in marks)


def _start_after_first_read(tmp_path, clock, monkeypatch):
    """Вариант коммитится ВНУТРИ снимка: сразу после чтения замка (первое чтение снимка)."""
    real, fired = exam.running_task_ids, []

    def racing(conn):
        found = real(conn)
        if not fired:
            fired.append(1)
            other = open_db(tmp_path / "olymp.db")
            try:
                exam.start(other, clock(), mode="real", material_id=1)
            finally:
                other.close()
        return found

    monkeypatch.setattr(exam, "running_task_ids", racing)
    return fired


def test_my_summaries_survive_a_variant_committed_inside_the_snapshot(tmp_path, monkeypatch):
    c, clock, _ = boot(tmp_path)
    with c:
        task = seed(tmp_path, key="Б", number="1")
        save_format(c, tmp_path, count_multiple="0", count_match="0", count_numeric="0",
                    points_multiple="0", points_match="0", points_numeric="0")  # fmt: skip
        assert attempt(c, task, "А", "sure").status_code == 200
        sid = c.post("/session/finish", json={}, headers=CHILD).json()["session_id"]
        for url in ("/my", "/api/my", f"/api/my/sessions/{sid}"):
            sql(tmp_path, "UPDATE exam_attempt SET status = 'submitted', end_reason = 'manual'")
            fired = _start_after_first_read(tmp_path, clock, monkeypatch)
            r = c.get(url, headers=CHILD)  # запись внутри снимка упала бы «database is locked»
            assert fired and r.status_code == 200, url
            monkeypatch.undo()


def test_nothing_writes_inside_a_read_snapshot(tmp_path, monkeypatch):
    """Каждый маршрут со снимком чтения проходит с запретом INSERT/UPDATE/DELETE на время снимка:
    запись внутри снимка падает «database is locked», если чужой коммит успел раньше."""
    import sqlite3

    from app.tutor import seq

    real, entered = seq.read_snapshot, []

    def guarded(conn):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            def deny(action, *_):
                writes = (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)
                return sqlite3.SQLITE_DENY if action in writes else sqlite3.SQLITE_OK

            with real(conn):
                entered.append(1)
                conn.set_authorizer(deny)
                try:
                    yield
                finally:
                    conn.set_authorizer(None)

        return cm()

    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        task = 1
        save_format(c, tmp_path)
        assert attempt(c, task, "А", "sure").status_code == 200
        sid = c.post("/session/finish", json={}, headers=CHILD).json()["session_id"]
        a = _submitted_attempt(c)
        monkeypatch.setattr(seq, "read_snapshot", guarded)
        urls = ("/today", "/my", "/api/my", f"/api/my/sessions/{sid}", f"/api/exam/results/{a}",
                "/api/exam", f"/tasks/{task}", f"/api/tasks/{task}/tutor", "/tasks", "/tasks?status=wrong")  # fmt: skip
        for url in urls:
            before = len(entered)
            r = c.get(url, headers=CHILD)
            assert r.status_code == 200 and len(entered) > before, (url, r.status_code)
        assert attempt(c, task, "А", "sure").status_code == 200
        before = len(entered)
        assert c.post("/session/finish", json={}, headers=CHILD).status_code == 200
        assert len(entered) > before


def test_task_map_hides_statuses_of_the_running_variant(tmp_path):
    """Карта заданий (#2110) не выдаёт «ошибся/верно» заданий идущего варианта ни в строках,
    ни в сводке, ни через фильтр по статусу."""
    c, _clock, _ = boot(tmp_path)
    with c:
        task = seed(tmp_path, key="Б", number="1")
        save_format(c, tmp_path, count_multiple="0", count_match="0", count_numeric="0",
                    points_multiple="0", points_match="0", points_numeric="0")  # fmt: skip
        assert attempt(c, task, "А", "sure").status_code == 200
        before = c.get("/tasks?status=wrong", headers=CHILD)
        assert "status-wrong" in before.text and f"/tasks/{task}" in before.text  # контроль
        assert start_real(c).status_code == 200
        for url in ("/tasks", "/tasks?status=wrong", "/tasks?status=ok", "/tasks?q=цитология"):
            r = c.get(url, headers=CHILD)
            assert r.status_code == 200, url
            assert "status-wrong" not in r.text and "status-hint" not in r.text, url
            if "status=" in url:
                assert 'class="pill' not in r.text, url  # строк с заданиями нет


def test_parent_preview_is_not_reachable_for_the_child(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        task = seed(tmp_path, key="Б", number="1")
        r = c.get(f"/parent/tasks/{task}/figure-preview.png", headers=CHILD)
        assert r.status_code in (401, 403) and r.headers.get("content-type") != "image/png"
        plain = c.get(f"/tasks/{task}", headers=CHILD)
        for query in ("preview=1", "preview=true", "mode=preview"):
            r = c.get(f"/tasks/{task}?{query}", headers=CHILD)
            assert r.status_code == 200 and 'data-act="attempt"' in r.text, query
            assert r.text.count("disabled") == plain.text.count("disabled"), query


def test_start_during_assembly_is_caught_by_the_fresh_check(tmp_path, monkeypatch):
    """Старт зафиксирован, пока собирался ответ (внутри снимка, который его не видит): свежее
    чтение после сборки отдаёт 409, а не экран с данными задания варианта."""
    from app.tutor import web

    c, clock, _ = boot(tmp_path)
    with c:
        task = seed(tmp_path, key="Б", number="1")
        save_format(c, tmp_path, count_multiple="0", count_match="0", count_numeric="0",
                    points_multiple="0", points_match="0", points_numeric="0")  # fmt: skip
        assert attempt(c, task, "Б", "sure").status_code == 200
        real = web.build_view
        urls = (f"/api/tasks/{task}/tutor", f"/tasks/{task}/tutor", f"/tasks/{task}")
        for url in urls:
            sql(tmp_path, "UPDATE exam_attempt SET status = 'submitted', end_reason = 'manual'")
            fired = []

            def racing(*a, _fired=fired, **kw):
                view = real(*a, **kw)
                if not _fired:
                    _fired.append(1)
                    other = open_db(tmp_path / "olymp.db")
                    try:
                        exam.start(other, clock(), mode="real", material_id=1)
                    finally:
                        other.close()
                return view

            monkeypatch.setattr(web, "build_view", racing)
            r = c.get(url, headers={**CHILD, "HX-Request": "true"})
            assert fired and r.status_code == 409 and "идёт олимпиада" in r.text, url
            assert "Решение" not in r.text and "верно" not in r.text.lower(), url
            monkeypatch.undo()


def test_blank_is_plain_forms_with_revision_and_origin_check(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        a = start_real(c).json()["attempt_id"]
        page = c.get("/exam/run", headers=CHILD).text
        assert (
            page.count("fetch(") == 1 and "hx-" not in page
        )  # одно: автосохранение перед сроком and 'method="post"' in page
        views = lambda: db(tmp_path).execute("SELECT COUNT(*) FROM exam_view").fetchone()[0]
        # успех - 303 на страницу; повтор того же действия не применяется второй раз
        form = form_of(c)
        first = act(c, "open:2", form, **keep(c))
        assert first.status_code == 303 and first.headers["location"] == "/exam/run"
        n = views()
        again = act(c, "open:2", form)
        assert again.status_code == 303 and views() == n
        assert int(form_of(c)["rev"]) == int(form["rev"]) + 1
        # вторая вкладка с прежней ревизией: данные и интервал не меняются, ввод показан
        stale = act(c, "save", {**form, "action_id": "tab2"}, a="В")
        assert stale.status_code == 409 and "В" in stale.text and "не сохранён" in stale.text
        assert views() == n and current(c)[1]["items"][1]["answer"] is None
        # выбор восстанавливается по token, «28,0» сохраняется как 28
        at(c, 2, "save", a=["А", "В"])
        html = c.get("/exam/run", headers=CHILD).text
        assert 'value="А" checked' in html and 'value="В" checked' in html
        assert 'value="Б" checked' not in html
        at(c, 3, "save", **{"m.1": "А"})  # частичное соответствие сохраняется как есть
        assert 'value="А" selected' in c.get("/exam/run", headers=CHILD).text
        at(c, 4, "save", n="28,0")
        assert current(c)[1]["items"][3]["answer"] == "28"
        # невалидное число: страница с ошибкой и исходным вводом, действие не выполнено
        rev = form_of(c)["rev"]
        bad = act(c, "save", n="двадцать")
        assert bad.status_code == 422 and 'value="двадцать"' in bad.text
        assert form_of(c)["rev"] == rev and current(c)[1]["items"][3]["answer"] == "28"
        # сдача не блокируется; пустое и частичное оценены как 0 по §1.1
        done = submit(c)
        assert done["id"] == a and done["score"] == 2 + 4
        assert {i["position"]: i["points"] for i in done["items"]}[3] == 0


def test_form_post_needs_its_own_origin(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        start_real(c)
        data = {**form_of(c), "action": "save", "a": "Б"}
        for headers in (
            {"Origin": "http://evil.example"},
            {"Referer": "http://evil.example/x"},
            {"Origin": "null"},
            {},
        ):
            r = c.post("/exam/act", data=data, headers={**CHILD, **headers}, follow_redirects=False)
            assert r.status_code == 403, headers
        assert current(c)[1]["items"][0]["answer"] is None
        for headers in ({"Origin": "http://testserver"}, {"Referer": "http://testserver/exam/run"}):
            data = {**form_of(c), "action": "save", "a": "Б"}
            r = c.post("/exam/act", data=data, headers={**CHILD, **headers}, follow_redirects=False)
            assert r.status_code == 303, headers


def test_every_child_route_is_classified(tmp_path):
    """Таблица детских маршрутов (метод, путь) -> класс; новый маршрут без класса роняет тест."""
    from fastapi.routing import APIRoute

    from tests.test_auth import flat_routes

    blocked, filtered, safe, exam_own, neutral = "blocked", "filtered", "safe", "exam", "neutral"
    classes = {
        # задания варианта закрыты 409 «идёт олимпиада»
        **{("POST", f"/tasks/{{task_id}}/{x}"): blocked for x in (
            "attempt", "hint", "explain", "chat", "show-answer", "repeat", "dispute", "voice")},
        ("GET", "/api/tasks/{task_id}/tutor"): blocked,
        ("GET", "/tasks/{task_id}/tutor"): blocked,
        ("GET", "/tasks/{task_id}"): blocked,
        # выдача с фильтрацией заданий идущего варианта
        ("GET", "/my"): filtered, ("GET", "/api/my"): filtered,
        ("GET", "/api/my/sessions/{session_id}"): filtered, ("GET", "/today"): filtered,
        ("GET", "/tasks"): filtered, ("POST", "/session/finish"): filtered,
        ("GET", "/exam"): filtered, ("GET", "/api/exam"): filtered,
        ("GET", "/exam/results/{attempt_id}"): filtered,
        ("GET", "/api/exam/results/{attempt_id}"): filtered,
        # безопасная выдача условия: без ключа и признака верности
        ("GET", "/api/tasks"): safe, ("GET", "/api/tasks/{task_id}"): safe,
        ("GET", "/api/search"): safe, ("GET", "/tasks/{task_id}/figure.png"): safe,
        ("GET", "/exam/figure/{position}.png"): safe,
        # бланк: собственные ответы ребёнка текущего прохождения
        ("GET", "/exam/run"): exam_own, ("POST", "/exam/act"): exam_own,
        ("POST", "/exam/start"): exam_own, ("GET", "/api/exam/current"): exam_own,
        # служебные: данных заданий нет
        ("GET", "/"): neutral, ("GET", "/healthz"): neutral, ("GET", "/how"): neutral,
        ("GET", "/notice"): neutral, ("POST", "/notice/ack"): neutral,
        ("POST", "/my/sessions/{session_id}/show-parent"): neutral,
    }  # fmt: skip
    parent_only = {
        ("GET", "/materials/{material_id}/pdf"), ("GET", "/materials/{material_id}/pages/{page}.png"),
    }  # fmt: skip
    c, _clock, _ = boot(tmp_path)
    seen = set()
    for route in flat_routes(c.app.routes):
        if not isinstance(route, APIRoute) or route.path.startswith("/parent"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if (method, route.path) not in parent_only:
                seen.add((method, route.path))
    assert seen - set(classes) == set(), f"маршрут без класса: {sorted(seen - set(classes))}"
    assert set(classes) - seen == set(), "в таблице есть несуществующий маршрут"


def test_any_accepted_action_id_is_idempotent_not_only_the_last(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        start_real(c)
        form_a = form_of(c)
        assert act(c, "mark", form_a).status_code == 303  # A принято на ревизии 0
        assert at(c, 1, "save", a="Б").status_code == 303  # B принято позже
        rev = form_of(c)["rev"]
        n = db(tmp_path).execute("SELECT COUNT(*) FROM exam_view").fetchone()[0]
        again = act(c, "mark", form_a)  # повтор A
        assert again.status_code == 303 and again.headers["location"] == "/exam/run"
        assert form_of(c)["rev"] == rev and current(c)[1]["items"][0]["marked"]
        assert db(tmp_path).execute("SELECT COUNT(*) FROM exam_view").fetchone()[0] == n


def test_results_and_history_started_during_assembly_give_nothing(tmp_path, monkeypatch):
    c, clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        a = _submitted_attempt(c)
        real = exam._result_data

        def racing(conn, attempt_id):
            data = real(conn, attempt_id)
            other = open_db(tmp_path / "olymp.db")
            try:
                exam.start(other, clock(), mode="real", material_id=1)
            finally:
                other.close()
            return data

        monkeypatch.setattr(exam, "_result_data", racing)
        r = c.get(f"/api/exam/results/{a}", headers=CHILD)
        assert r.status_code == 409 and "score" not in r.text


def test_task_map_has_no_status_for_the_running_variant_tasks(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        assert attempt(c, 1, "Б", "sure").status_code == 200
        start_real(c)
        for url in ("/tasks?q=цифр", "/tasks?status=new", "/tasks?status=ok", "/tasks"):
            r = c.get(url, headers=CHILD)
            assert r.status_code == 200 and '/tasks/1"' not in r.text, url
            assert "Решать по теме" not in r.text and '/tasks/1"' not in r.text, url


def test_stable_refuses_an_unchecked_snapshot(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException

    c, _clock, _ = boot(tmp_path)
    with c:
        conn = db(tmp_path)
        grow = iter(range(1, 100))
        monkeypatch.setattr(exam, "running_task_ids", lambda _c: frozenset({next(grow)}))
        with pytest.raises(HTTPException) as err:
            exam.stable(conn, lambda hidden: hidden)
        assert err.value.status_code == 503


def test_reused_action_id_with_another_action_is_a_conflict_not_a_replay(tmp_path):
    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        start_real(c)
        form = form_of(c)
        assert act(c, "save", form, a="Б").status_code == 303
        same = act(c, "save", form, a="Б")  # точный повтор
        assert same.status_code == 303
        other = act(c, "save", form, a="А")  # тот же id, другой ввод
        assert other.status_code == 409 and "не сохранён" in other.text
        sub = act(c, "submit", form)  # тот же id, другое действие
        assert sub.status_code == 409
        assert current(c)[1]["items"][0]["answer"] == "Б"
        assert db(tmp_path).execute("SELECT status FROM exam_attempt").fetchone()[0] == "running"


def test_blank_view_is_rechecked_after_assembly(tmp_path, monkeypatch):
    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        a = start_real(c).json()["attempt_id"]
        at(c, 1, "save", a="Б")
        real, fired = exam.active, []

        def racing(conn, now):
            got = real(conn, now)
            if not fired:
                fired.append(1)
                other = open_db(tmp_path / "olymp.db")
                try:
                    cur = other.execute("SELECT * FROM exam_attempt WHERE status = 'running'")
                    exam._finish(other, cur.fetchone(), "manual", now)
                    exam.start(other, now, mode="real", material_id=1)
                finally:
                    other.close()
            return got

        monkeypatch.setattr(exam, "active", racing)
        code, view = current(c)
        assert fired and code == 200 and view["id"] != a
        assert all(i["answer"] is None for i in view["items"])


def test_submit_and_navigation_use_the_field_as_it_is_at_send_time(tmp_path):
    """Правило бланка: одно действие = одна отправка формы; поле читается в момент отправки и
    сохраняется первым; отказ не теряет ввод и не двигает интервал просмотра."""
    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        start_real(c)
        views = lambda: (
            db(tmp_path).execute("SELECT position, closed_at FROM exam_view ORDER BY id").fetchall()
        )
        # правка и сразу сдача: ответ из поля в момент отправки сохранён и оценён
        at(c, 1, "save", a=["А"])
        done_form = form_of(c)
        r = act(c, "submit", done_form, a=["Б"])
        assert r.status_code == 303 and "/exam/results/" in r.headers["location"]
        done = c.get("/api" + r.headers["location"], headers=CHILD).json()
        assert {i["position"]: i["points"] for i in done["items"]}[1] > 0
        # новое прохождение: отказ перехода (невалидное число) не трогает интервал и позицию
        start_real(c)
        at(c, 4, "save", n="5")
        before, rev = views(), form_of(c)["rev"]
        bad = act(c, "open:1", n="двадцать")
        assert bad.status_code == 422 and 'value="двадцать"' in bad.text
        assert views() == before and form_of(c)["rev"] == rev
        assert form_of(c)["shown"] == "4"
        # правка во второй вкладке (старая ревизия) тоже не сдвигает интервал и показывает ввод
        stale = act(c, "open:2", {**form_of(c), "rev": str(int(rev) - 1)}, n="7")
        assert stale.status_code == 409 and "не сохранён" in stale.text
        assert views() == before


def test_save_one_second_before_deadline_is_taken_and_template_autosaves(tmp_path):
    c, clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        start_real(c)
        form = form_of(c)
        clock.advance(599)
        assert act(c, "save", form, a="Б").status_code == 303
        assert current(c)[1]["items"][0]["answer"] == "Б"
        late_form = {**form_of(c), "action_id": "late"}
        clock.advance(1)
        late = act(c, "save", late_form, a="А")
        assert "/exam/results/" in late.headers["location"]
        row = db(tmp_path).execute("SELECT answer FROM exam_item WHERE position = 1").fetchone()
        assert row[0] == '"Б"'


def test_second_tab_save_makes_the_first_tab_submit_stale_and_keeps_the_newer_answer(tmp_path):
    """Серверная часть гонки двух вкладок: старая ревизия получает 409, чужой ответ не затирается.

    Клиентская часть (после окна 1,5 с бланк закрыт, отложенных действий нет) - в node-сценариях ниже.
    """
    c, _clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        start_real(c)
        tab_a = form_of(c)
        assert act(c, "save", {**tab_a, "action_id": "tab-b"}, a="Б").status_code == 303
        stale_auto = act(c, "save", {**tab_a, "action_id": "auto-a"}, a="А")
        assert stale_auto.status_code == 409 and "не сохранён" in stale_auto.text
        assert act(c, "submit", tab_a, a="А").status_code == 409
        row = db(tmp_path).execute("SELECT answer FROM exam_item WHERE position = 1").fetchone()
        assert row[0] == '"Б"' and current(c)[1]["status"] == "running"


def test_autosave_arriving_after_the_deadline_redirects_to_results_and_saves_nothing(tmp_path):
    """Запрос после срока: 303 на итог (не на бланк), ответ не сохранён - вкладка не вправе считать его сохранённым."""
    c, clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        a = start_real(c).json()["attempt_id"]
        tab = form_of(c)
        clock.advance(601)
        late = act(c, "save", {**tab, "action_id": "auto-late"}, a="А")
        assert late.status_code == 303 and late.headers["location"] == f"/exam/results/{a}"
        row = db(tmp_path).execute("SELECT answer FROM exam_item WHERE position = 1").fetchone()
        assert row[0] is None


def test_results_page_closes_an_attempt_whose_deadline_passed_without_submit(tmp_path):
    """Бланк уводит на итог по сроку без нажатия «Сдать»: страница итога сама закрывает попытку."""
    c, clock, _ = boot(tmp_path)
    with c:
        four(tmp_path)
        save_format(c, tmp_path)
        a = start_real(c).json()["attempt_id"]
        assert act(c, "save", a="Б").status_code == 303
        clock.advance(601)
        page = c.get(f"/exam/results/{a}", headers=CHILD, follow_redirects=False)
        assert page.status_code == 200
        res = c.get(f"/api/exam/results/{a}", headers=CHILD).json()
        assert res["status"] == "submitted" and res["end_reason"] == "time"


_AUTOSAVE_SCENARIOS = [
    "closed_after_window_sends_nothing",
    "stale_409_with_broken_body_shows_alert_and_sends_nothing",
    "network_error_shows_alert",
    "success_one_save_and_form_unchanged",
    "deadline_goes_to_results",
    "redirect_to_results_is_not_a_saved_answer",
    "page_opened_inside_the_window_is_closed_at_once",
    "handlers_compare_the_deadline_not_only_the_flag",
]


@pytest.mark.skipif(NODE is None, reason="node не установлен")
@pytest.mark.parametrize("scenario", _AUTOSAVE_SCENARIOS)
def test_exam_autosave_script_runs_in_the_harness(scenario):
    """Скрипт бланка исполняется на модели DOM: последние 1,5 с бланк закрыт, ответ ничего не меняет."""
    harness = Path(__file__).parent / "js" / "exam_autosave_harness.js"
    r = subprocess.run(
        [NODE, str(harness), scenario], check=False, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stdout + r.stderr
