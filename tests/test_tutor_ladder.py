"""Лестница подсказок, лимиты проверок, итоги, уверенность (OH-T06, AC-14..AC-18)."""

from app.tutor.outcomes import outcome_label
from tests.tutor_helpers import Scripted, attempt, boot, events, out, post, seed, view


def test_ac14(tmp_path):
    c, clock, model = boot(tmp_path)
    with c:
        a = seed(tmp_path, key="Б")
        assert c.get(f"/tasks/{a}").status_code == 200  # экран открыт: идут секунды
        clock.advance(30)
        r = post(c, a, "hint", level=1)
        assert r.status_code == 409 and r.json()["code"] == "too_early"
        assert r.json()["wait_s"] == 30
        assert "hint1" not in model.kinds()
        clock.advance(31)  # 61-я секунда
        r = post(c, a, "hint", level=1)
        assert r.status_code == 200 and r.json()["hints"] == 1
        b = seed(tmp_path, key="Б")
        c.get(f"/tasks/{b}")
        attempt(c, b, "А")  # сразу после первой попытки
        assert post(c, b, "hint", level=1).status_code == 200


def test_ac15(tmp_path):
    c, clock, model = boot(tmp_path)
    with c:
        a = seed(tmp_path, key="Б")
        attempt(c, a, "А")
        post(c, a, "hint", level=1)
        post(c, a, "hint", level=2)
        r = post(c, a, "hint", level=3)
        assert r.status_code == 409 and r.json()["code"] == "needs_confirm"
        assert view(c, a)["solution"] is None and view(c, a)["state"] == "hint2"
        r = post(c, a, "hint", level=3, confirm=True).json()
        assert r["solution"]["answer"] == "Б"
        assert r["outcome"] == "решение по подсказке 3"


def test_ac16(tmp_path):
    c, clock, model = boot(tmp_path)
    with c:
        a = seed(tmp_path, key="Б")
        r1 = attempt(c, a, "А").json()
        r2 = attempt(c, a, "В").json()
        assert r1["last"] == {"answer": "А", "mark": "не то"}
        assert r2["last"] == {"answer": "В", "mark": "не то"}  # «не то» только у последнего
        assert all("disabled" not in str(o) for o in r2["task"]["options"])
        third = attempt(c, a, "Г")
        assert third.status_code == 409 and third.json()["code"] == "need_hint"
        assert view(c, a)["attempts"] == 2 and view(c, a)["checks_left"] == 0
        assert post(c, a, "hint", level=1).status_code == 200
        assert attempt(c, a, "Г").status_code == 200  # после подсказки проверка разрешена
        assert view(c, a)["solution"] is None  # верный вариант не обозначен


def test_ac17(tmp_path):
    assert outcome_label(True, "solved", 1, 0) == "верно с 1-й попытки"
    assert outcome_label(True, "solved", 3, 0) == "верно с 3-й попытки"
    assert outcome_label(True, "solved", 2, 2) == "верно после подсказки 2"
    assert outcome_label(True, "hint3", 2, 3) == "решение по подсказке 3"
    assert outcome_label(False, "solved_unknown", 1, 0) == "совпало с ответом модели, не проверено"
    assert (
        outcome_label(False, "hint3", 2, 3) == "решение модели по подсказке 3, не проверено"
    )
    assert outcome_label(True, "answer_shown", 2, 0) == "ответ показан после 2 попыток"
    for keyed, state in ((False, "solved_unknown"), (False, "hint3")):
        assert "верно" not in outcome_label(keyed, state, 2, 1).split(",")[0].lower()
    c, clock, model = boot(tmp_path)
    with c:
        first = seed(tmp_path, key="Б")
        assert attempt(c, first, "Б").json()["outcome"] == "верно с 1-й попытки"
        second = seed(tmp_path, key="Б")
        attempt(c, second, "А")
        post(c, second, "hint", level=1)
        post(c, second, "hint", level=2)
        assert attempt(c, second, "Б").json()["outcome"] == "верно после подсказки 2"
        nokey = seed(tmp_path, key=None, model_answer="Б")
        assert attempt(c, nokey, "Б").json()["outcome"] == "совпало с ответом модели, не проверено"


def test_ac18(tmp_path):
    c, clock, model = boot(tmp_path)
    with c:
        sure = seed(tmp_path, key="Б")
        attempt(c, sure, "А", confidence="sure")
        doubt = seed(tmp_path, key="Б")
        attempt(c, doubt, "А", confidence="doubt")
        none = seed(tmp_path, key="Б")
        attempt(c, none, "А")
        mistakes = [call for call in model.calls if call["kind"] == "mistake"]
        assert len(mistakes) == 3
        assert "что именно сбило" in mistakes[0]["content"]
        assert "что именно сбило" not in mistakes[1]["content"]
        assert "что именно сбило" not in mistakes[2]["content"]
        got = [e["data"]["confidence"] for e in events(tmp_path, "attempt")]
        assert got == ["sure", "doubt", None]
        assert attempt(c, none, "Б", confidence="не знаю").status_code == 422
