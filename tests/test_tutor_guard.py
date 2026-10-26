"""Защита вывода модели: имена верного варианта и разметка (OH-T06, AC-6, AC-7)."""

import json

from app.tutor.guard import GENERIC, render_markdown
from tests.tutor_helpers import Scripted, attempt, boot, out, post, seed

NAMING = [
    "Верный вариант — Б.",
    "Правильный ответ Б, потому что клетка.",
    "Ответ: Б",
    "Подумай про вариант Б: он единственный подходит.",
    "Это вариант б.",
    "Выбери «вариант: митохондрия»",
]


def options():
    return [
        {"label": "А", "text": "ядро"},
        {"label": "Б", "text": "митохондрия"},
        {"label": "В", "text": "вакуоль"},
        {"label": "Г", "text": "клеточная стенка"},
    ]


def shown(r):
    data = r.json()
    return json.dumps(
        [h["html"] for h in data["hints_list"]] + [data.get("analysis") or ""], ensure_ascii=False
    )


def test_ac6(tmp_path):
    for keyed in (True, False):
        for text in NAMING:

            def reply(kind, content, text=text):
                return out("Б", text)

            c, _clock, _model = boot(
                tmp_path / f"{keyed}{NAMING.index(text)}", Scripted(reply=reply)
            )
            with c:
                tid = seed(
                    tmp_path / f"{keyed}{NAMING.index(text)}",
                    key="Б" if keyed else None,
                    model_answer=None if keyed else "Б",
                    options=options(),
                )
                r = attempt(c, tid, "А")  # разбор ошибки
                assert r.json()["analysis"] == f"<p>{GENERIC['mistake']}</p>", (keyed, text)
                for level, kind in ((1, "hint1"), (2, "hint2")):
                    r = post(c, tid, "hint", level=level)
                    html = shown(r)
                    assert GENERIC[kind] in html, (keyed, text, level)
                    assert text.split("Б")[0] not in html or "Верный" not in html
                    assert "Правильный" not in html and "Ответ:" not in html
        # безобидная подсказка проходит без замены
    ok = "Вспомни, какой органоид даёт клетке энергию."
    c, _clock, _model = boot(tmp_path / "ok", Scripted(reply=lambda k, content: out("Б", ok)))
    with c:
        tid = seed(tmp_path / "ok", key="Б", options=options())
        attempt(c, tid, "А")
        assert ok in shown(post(c, tid, "hint", level=1))


def test_ac7(tmp_path):
    dirty = (
        '<script>alert(1)</script>**жирный** и *курсив*\n\n- пункт <b onclick="x()">один</b>\n'
        "<img src=x onerror=alert(2)> [ссылка](javascript:alert(3)) ![рис](http://x/y.png)"
    )
    c, _clock, _model = boot(tmp_path, Scripted(reply=lambda k, content: out("Б", dirty)))
    with c:
        tid = seed(tmp_path, key="Б", options=options())
        attempt(c, tid, "А")
        r = post(c, tid, "hint", level=1)
        body = r.text
        for bad in ("<script", "onerror", "onclick", "<img", "javascript:", "alert(1)"):
            assert bad not in body.replace("\\u003c", "<"), bad
        html = r.json()["hints_list"][0]["html"]
        assert "<strong>жирный</strong>" in html and "<em>курсив</em>" in html
        assert "<li>" in html
        page = c.get(f"/tasks/{tid}/tutor").text
        assert "<script" not in page.split("<script", 1)[0] + "" or "alert(1)" not in page
        assert "alert(1)" not in page


def test_render_markdown_escapes_everything_else():
    html = render_markdown("a < b & c > d <iframe src=x></iframe> <style>p{}</style>")
    assert "<iframe" not in html and "<style" not in html
    assert "&lt;" in html and "&amp;" in html
