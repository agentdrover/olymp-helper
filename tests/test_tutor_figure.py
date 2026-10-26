"""Задания с рисунком (OH-T06, AC-9..AC-12)."""

import json
from pathlib import Path

from app.tutor.guard import GENERIC
from tests.tutor_helpers import CHILD, Scripted, attempt, boot, out, post, seed, view

NUM = [{"label": str(i), "text": f"цифра {i}"} for i in range(1, 7)]
NOTE = "1 - тычинка, 2 - пестик, 3 - лепесток, 4 - чашелистик, 5 - завязь, 6 - рыльце"
SHORT = "1 - тычинка"
FIGURE = {"alt": "Продольный разрез цветка, цифрами 1-6 обозначены части", "teacher_note": NOTE}


def test_ac9(tmp_path):
    c, _clock, _model = boot(tmp_path)
    with c:
        shown = seed(tmp_path, key="5", options=NUM, figure=FIGURE)
        hidden = seed(tmp_path, key="5", options=NUM, figure={**FIGURE, "confirmed": False})
        html = c.get(f"/tasks/{shown}", headers=CHILD).text
        assert "<figure" in html
        assert FIGURE["alt"] in html
        assert "сборник 2022/23" in html and "стр. 3" in html
        assert html.index("<figure") < html.index('class="options')
        # крупно: окно не меньше 560 px, закрывается Esc и кнопкой
        assert "min-width: 560px" in Path("app/static/app.css").read_text()
        assert "<dialog" in html and "Закрыть" in html
        assert "Escape" in Path("app/static/tutor.js").read_text()
        assert c.get(f"/tasks/{hidden}", headers=CHILD).status_code == 404
        assert hidden not in [t["id"] for t in c.get("/api/tasks").json()["tasks"]]
        assert c.get(f"/tasks/{hidden}/figure.png").status_code == 404


def sweep(c, tid):
    """Every route of the task before the solution; returns all bodies."""
    bodies = [
        c.get(f"/tasks/{tid}", headers=CHILD),
        c.get(f"/tasks/{tid}/tutor", headers=CHILD),
        c.get(f"/api/tasks/{tid}/tutor", headers=CHILD),
        c.get(f"/api/tasks/{tid}", headers=CHILD),
        post(c, tid, "hint", level=3),
        post(c, tid, "explain", mode="simpler"),
    ]
    return bodies


def test_ac10(tmp_path):
    def reply(kind, content):
        return out("5", "Смотри: 5 — завязь, это ответ.")

    c, _clock, model = boot(tmp_path, Scripted(reply=reply))
    with c:
        tid = seed(tmp_path, key="5", options=NUM, figure=FIGURE)
        bodies = sweep(c, tid)
        bodies.append(attempt(c, tid, "2"))
        bodies += sweep(c, tid)
        h1 = post(c, tid, "hint", level=1)
        h2 = post(c, tid, "hint", level=2)
        bodies += [h1, h2] + sweep(c, tid)
        for r in bodies:
            assert "завязь" not in r.text and "тычинка" not in r.text, r.request.url
            assert "teacher_note" not in r.text
        html = json.dumps([h["html"] for h in h2.json()["hints_list"]], ensure_ascii=False)
        assert GENERIC["hint1"] in html and GENERIC["hint2"] in html
        # заметка уходит модели (только как данные запроса), но не ребёнку
        assert any("5 - завязь" in call["content"] for call in model.calls)
        assert "завязь" not in c.get(f"/tasks/{tid}").text


def test_ac11(tmp_path):
    c, _clock, model = boot(tmp_path)
    with c:
        tid = seed(tmp_path, key="5", options=NUM, figure={**FIGURE, "teacher_note": SHORT})
        assert view(c, tid)["assist"] == "key_only"
        attempt(c, tid, "2")
        r = post(c, tid, "hint", level=1)
        assert r.status_code == 200
        assert "к этому заданию подсказок нет" in r.json()["panel"]["text"].lower()
        assert r.json()["hints"] == 0
        r = attempt(c, tid, "5").json()
        assert r["state"] == "solved"
        assert r["solution"]["answer"] == "5" and r["solution"]["explanation"] is None
        assert model.calls == [], "в режиме «только проверка по ключу» модель не вызывается"


def test_ac12(tmp_path):
    c, _clock, model = boot(tmp_path)
    with c:
        tid = seed(tmp_path, key="5", options=NUM, figure={**FIGURE, "teacher_note": SHORT})
        assert attempt(c, tid, "1").status_code == 200
        assert attempt(c, tid, "2").status_code == 200
        third = attempt(c, tid, "3")
        assert third.status_code == 409 and third.json()["code"] == "need_show"
        no = post(c, tid, "show-answer")
        assert (
            no.status_code == 409 and no.json()["solution"] is None
            if "solution" in no.json()
            else True
        )
        r = post(c, tid, "show-answer", confirm=True).json()
        assert r["state"] == "answer_shown"
        assert r["solution"]["answer"] == "5" and r["solution"]["explanation"] is None
        assert r["outcome"] == "ответ показан после 2 попыток"
        assert attempt(c, tid, "5").status_code == 409
        from tests.tutor_helpers import events

        shown = events(tmp_path, "answer_shown")
        assert len(shown) == 1 and shown[0]["data"]["attempts"] == 2
        assert model.calls == []
        # раньше двух проверок показать ответ нельзя
        other = seed(tmp_path, key="5", options=NUM, figure={**FIGURE, "teacher_note": SHORT})
        attempt(c, other, "1")
        assert post(c, other, "show-answer", confirm=True).status_code == 409


def test_teacher_note_is_never_echoed_to_the_child(tmp_path):
    """A model that repeats the hidden note about ANOTHER label (not the answer) is cut as well."""

    def reply(kind, content):
        return out("5", "Помни: 3 - лепесток, это часть цветка.")

    c, _clock, _model = boot(tmp_path, Scripted(reply=reply))
    with c:
        tid = seed(tmp_path, key="5", options=NUM, figure=FIGURE)
        attempt(c, tid, "2")
        got = post(c, tid, "hint", level=1).json()
        assert "лепесток" not in json.dumps(got, ensure_ascii=False)
        assert GENERIC["hint1"] in got["hints_list"][0]["html"]
