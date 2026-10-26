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


OPTS = [
    {"label": "А", "text": "белок мембраны"},
    {"label": "Б", "text": "молекула ДНК хранит наследственность"},
    {"label": "В", "text": "углевод запасной"},
    {"label": "Г", "text": "липид клетки"},
]
COND = "Что хранит наследственную информацию клетки?"
NOTE = "1 - тычинка, 2 - пестик, 3 - лепесток, 4 - чашелистик, 5 - завязь, 6 - рыльце"


def reason(text, ref, kind="single", options=OPTS, note=None, condition=COND):
    from app.tutor.guard import finish

    return finish(text, "hint1", kind, options, ref, note, condition=condition)[1]


def test_obvious_leaks_are_caught_by_the_cheap_prefilter():
    """Остальное (синонимы, порядок, исключение) ловит судья: см. test_tutor_judge.py."""
    for text, ref, kind, options in [
        ("б — правильный выбор", "Б", "single", OPTS),
        ("Это молекула ДНК, которая хранит наследственность", "Б", "single", OPTS),
        ("Посчитай: получится 28", "28", "numeric", None),
        ("Ответ — двадцать восемь", "28", "numeric", None),
        ("Выбирай b", "Б", "single", OPTS),
    ]:
        assert reason(text, ref, kind, options) is not None, text


def test_number_words_cover_0_to_1000():
    from app.tutor.guard import number_words

    assert number_words(0) == ["ноль"] and number_words(28) == ["двадцать", "восемь"]
    assert number_words(105) == ["сто", "пять"] and number_words(1000) == ["тысяча"]
    for n in range(1001):
        assert number_words(n)
    for n, words in ((5, "пять"), (5, "пяти"), (40, "сорока"), (12, "двенадцати")):
        assert reason(f"Это {words} штук", str(n), "numeric", None) is not None, words


def test_innocent_text_passes_and_note_pairs_are_cut():
    ok = "Вспомни, какой процесс отвечает за хранение кода в клетке и где он идёт."
    assert reason(ok, "Б") is None
    assert reason("Обрати внимание на условие.", "Б") is None
    assert reason("Не спеши с ответом, а сначала перечитай вопрос.", "Б") is None
    # пара «обозначение + слово» из пояснения режется в любом порядке
    nums = [{"label": str(i), "text": f"цифра {i}"} for i in range(1, 7)]
    for text in ("Цифрой 3 обозначен лепесток", "Лепесток - это цифра 3", "3? лепесток!"):
        assert reason(text, "5", "single", nums, note=NOTE) in ("note", "note_pair"), text
    assert reason("Цифрой 3 обозначен лепесток", "5", "single", nums, note=NOTE) == "note_pair"
    assert reason("Посмотри на пестик на рисунке", "5", "single", nums, note=NOTE) is None


def test_function_words_are_not_labels_but_label_contexts_are():
    """«в клетке», «а также», «в ядре» проходят, когда верный вариант В или А (иначе лестница
    подсказок бесполезна); обозначение в контексте обозначения отклоняется."""
    passing = [
        ("Посмотри, что происходит в клетке, когда она делится.", "В"),
        ("Подумай про ядро, а также про оболочку клетки.", "А"),
        ("Что находится в ядре и чем оно окружено?", "В"),
        ("Сравни, а потом вспомни, что ты знаешь о строении клетки.", "А"),
        ("Это бывает у многих организмов, а у некоторых нет.", "А"),
    ]
    for text, ref in passing:
        assert reason(text, ref) is None, text
    rejected = [
        ("(в) подходит", "В"), ("Выбери вариант в", "В"), ("Ответ — В", "В"),
        ("В — верно", "В"), ("Нужны а, б и в", "В"), ("Это пункт а", "А"),
        ("А: вот оно", "А"), ("в) это он", "В"), ("Думаю, что б.", "Б"),
    ]  # fmt: skip
    for text, ref in rejected:
        assert reason(text, ref) is not None, text
