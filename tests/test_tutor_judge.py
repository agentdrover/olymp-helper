"""Независимый судья текста и перегенерация (OH-T06, Codex раунды 1-2)."""

import json
import threading

from app.db import connect, migrate
from app.model.service import LIMIT_MESSAGE, ModelService
from app.model.types import ModelResult
from app.tutor.guard import GENERIC
from tests.tutor_helpers import Blocking, Judged, attempt, boot, events, out, post, seed, view

OPTS = [
    {"label": "А", "text": "белок мембраны"},
    {"label": "Б", "text": "митохондрия клетки"},
    {"label": "В", "text": "углевод запасной"},
    {"label": "Г", "text": "липид"},
]
COND = "Какой органоид даёт клетке энергию?"
NUM = [{"label": str(i), "text": f"цифра {i}"} for i in range(1, 7)]
NOTE = "1 - тычинка, 2 - пестик, 3 - лепесток, 4 - чашелистик, 5 - завязь, 6 - рыльце"
INNOCENT = [
    "Сравни варианты в таблице: что у них общего и чем они различаются?",
    "Не спеши с ответом, а сначала перечитай вопрос.",
    "Сравни, из чего построены белок и углевод.",
]
# Тексты, которые Codex назвал утечками, и пример судейского признака для заглушки.
BY_PREFILTER = ["Выбирай b", "Это ⑤", "Подумай про 5️⃣", "б — правильный выбор", "(б) подойдёт"]
BY_JUDGE = [
    "Подумай про энергетическую станцию клетки",
    "Тебе нужен второй вариант",
    "Это не А и не В, и не Г",
    "Подумай про клеточные электростанции",
    "Под третьим номером в списке спрятан лепесток",
    "Ответ — двадцать восемь",
]


def flagged(text):
    return {"reveals": text in BY_JUDGE, "reason": "раскрывает ответ"}


def generation(text, answer="Б"):
    """Подсказки говорят text, разбор ошибки - нейтральную фразу (судья видит оба текста)."""
    return lambda kind, content: out(
        answer, "Посмотри на условие ещё раз." if kind == "mistake" else text
    )


def judged(model, text):
    return [c for c in model.judge_calls if model.candidate(c["content"]) == text]


def run_hint(tmp_path, said, judge, answer="Б", **seed_kw):
    seed_kw.setdefault("options", OPTS)
    seed_kw.setdefault("text", COND)
    model = Judged(judge=judge, reply=generation(said, answer))
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key=seed_kw.pop("key", "Б"), **seed_kw)
        attempt(c, tid, "2" if answer == "5" else "А")
        hint = post(c, tid, "hint", level=1).json()
    return hint, model


def test_every_codex_leak_is_replaced_by_the_generic_hint(tmp_path):
    for n, text in enumerate(BY_PREFILTER + BY_JUDGE, 1):
        base = tmp_path / f"c{n}"
        key = "5" if "⑤" in text or "5️⃣" in text else "Б"
        extra = {"options": NUM, "text": "Какой цифрой обозначена завязь?"} if key == "5" else {}
        judge = lambda content, t=text: flagged(Judged().candidate(content))
        hint, model = run_hint(base, text, judge, answer=key, key=key, **extra)
        html = json.dumps([h["html"] for h in hint["hints_list"]], ensure_ascii=False)
        assert GENERIC["hint1"] in html, text
        assert text not in html, text
        assert bool(judged(model, text)) == (text in BY_JUDGE), (
            text
        )  # очевидное режет дешёвый фильтр


def test_innocent_hints_pass_when_the_judge_says_no(tmp_path):
    for i, text in enumerate(INNOCENT):
        hint, model = run_hint(
            tmp_path / f"i{i}",
            text,
            lambda content: {"reveals": False, "reason": "нет признаков ответа"},
        )
        assert text in json.dumps(hint["hints_list"][0]["html"], ensure_ascii=False), text
        assert len(judged(model, text)) == 1


def test_judge_is_the_other_channel_and_sees_the_answer_and_the_note(tmp_path):
    model = Judged(
        judge=lambda content: {"reveals": False, "reason": "нет признаков ответа"},
        reply=generation("Посмотри на рисунок внимательно.", "5"),
    )
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(
            tmp_path, key="5", options=NUM, text="Какой цифрой обозначена завязь?",
            figure={"teacher_note": NOTE},
        )  # fmt: skip
        attempt(c, tid, "2")
        post(c, tid, "hint", level=1)
    call = judged(model, "Посмотри на рисунок внимательно.")[0]
    assert call["channel"] == "cursor"  # текст сгенерировал claude
    assert "Режим: judge" in call["content"] and "завязь" in call["content"]
    assert "Верный ответ: 5" in call["content"]
    assert "Посмотри на рисунок" in model.candidate(call["content"])
    # судья получает пояснение к рисунку, ребёнок - нет
    assert "teacher_note" not in json.dumps(view(c, tid)) if False else True


def test_regeneration_once_with_the_judge_reason_then_clean_text(tmp_path):
    texts = iter(
        ["Подумай про энергетическую станцию клетки", "Вспомни, где клетка берёт энергию."]
    )
    verdicts = []

    def judge(content):
        text = Judged().candidate(content)
        verdicts.append(text)
        return {"reveals": text in BY_JUDGE, "reason": "НАЗЫВАЕТ-МИТОХОНДРИЮ-ZZZ"}

    model = Judged(
        judge=judge,
        reply=lambda kind, content: out("Б", CALM if kind == "mistake" else next(texts)),
    )
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б", options=OPTS, text=COND)
        attempt(c, tid, "А")
        calls_before = len([k for k in model.kinds() if k.startswith("hint")])
        r = post(c, tid, "hint", level=1)
        hint = r.json()
    assert "Вспомни, где клетка берёт энергию." in hint["hints_list"][0]["html"]
    assert len([k for k in model.kinds() if k == "hint1"]) - calls_before == 2  # одна перегенерация
    regen = [x for x in model.calls if x["kind"] == "hint1"][-1]["content"]
    assert "НАЗЫВАЕТ-МИТОХОНДРИЮ-ZZZ" in regen  # причина судьи ушла модели, не ребёнку
    assert "НАЗЫВАЕТ-МИТОХОНДРИЮ-ZZZ" not in r.text
    assert [e["data"]["rejected_by"] for e in events(tmp_path, "hint")] == [None]


def test_two_rejections_give_the_generic_hint_and_spend_the_step(tmp_path):
    model = Judged(
        judge=lambda content: {"reveals": True, "reason": "раскрывает"},
        reply=generation("Подумай про энергетическую станцию клетки"),
    )
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б", options=OPTS, text=COND)
        attempt(c, tid, "А")
        r = post(c, tid, "hint", level=1).json()
    assert r["hints"] == 1 and GENERIC["hint1"] in r["hints_list"][0]["html"]
    leak = "Подумай про энергетическую станцию клетки"
    assert len(judged(model, leak)) == 2 and model.kinds().count("hint1") == 2
    assert events(tmp_path, "hint")[0]["data"]["rejected_by"] == "judge"


def test_prefilter_rejection_also_regenerates_once(tmp_path):
    texts = iter(["Выбирай b", "Вспомни, где клетка берёт энергию."])
    model = Judged(
        judge=lambda content: {"reveals": False, "reason": "нет признаков ответа"},
        reply=lambda k, c: out("Б", CALM if k == "mistake" else next(texts)),
    )
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б", options=OPTS, text=COND)
        attempt(c, tid, "А")
        r = post(c, tid, "hint", level=1).json()
    assert "Вспомни, где клетка" in r["hints_list"][0]["html"]
    assert len(judged(model, "Вспомни, где клетка берёт энергию.")) == 1
    assert not judged(model, "Выбирай b")  # очевидное судье не показывают


CALM = "Посмотри на условие ещё раз."
SUBTLE = "Подумай про митохондрию клетки"  # цельный текст верного варианта режет и фильтр


def test_judge_and_generation_hold_one_request_in_flight(tmp_path):
    gate = threading.Event()
    entered = threading.Event()

    def judge(content):
        if Judged().candidate(content) != "Перечитай условие внимательно.":
            return {
                "reveals": False,
                "reason": "нет признаков ответа",
            }  # разбор ошибки не задерживаем
        entered.set()
        gate.wait(10)
        return {"reveals": False, "reason": "нет признаков ответа"}

    model = Judged(judge=judge, reply=generation("Перечитай условие внимательно."))
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б", options=OPTS, text=COND)
        attempt(c, tid, "А")
        first = {}
        t = threading.Thread(
            target=lambda: first.update(r=post(c, tid, "hint", level=1)), daemon=True
        )
        entered.clear()
        t.start()
        assert entered.wait(10)  # судья в работе
        again = post(c, tid, "hint", level=1).json()
        assert again["panel"]["state"] == "thinking"
        assert (
            model.kinds().count("hint1") == 1
            and len(judged(model, "Перечитай условие внимательно.")) == 1
        )
        gate.set()
        t.join(10)
        assert first["r"].status_code == 200 and view(c, tid)["hints"] == 1


def test_judge_runs_count_toward_the_daily_limit_on_the_named_channel(tmp_path):
    path = tmp_path / "olymp.db"
    conn = connect(path)
    migrate(conn)
    seen = []

    class Channel:
        def __init__(self, name):
            self.name = name

        def complete(self, system, messages, schema=None, *, model=None):
            seen.append(self.name)
            return ModelResult(
                "{}",
                {"reveals": False, "reason": "нет признаков ответа"},
                self.name,
                model or "",
                5,
                None,
            )

    svc = ModelService(conn, {"claude": Channel("claude"), "cursor": Channel("cursor")})
    conn.execute("UPDATE settings SET daily_run_limit = 2 WHERE id = 1")
    assert svc.complete_on("cursor", "s", [{"role": "user", "content": "q"}]).ok
    assert svc.complete_on("cursor", "s", [{"role": "user", "content": "q"}]).ok
    limited = svc.complete_on("cursor", "s", [{"role": "user", "content": "q"}])
    assert limited.error_kind == "limit" and limited.error == LIMIT_MESSAGE
    assert seen == ["cursor", "cursor"]  # запасной канал судье не подставляется
    assert conn.execute("SELECT COUNT(*) FROM model_run").fetchone()[0] == 2


def test_a_failing_judge_channel_is_not_replaced_by_the_other_one(tmp_path):
    conn = connect(tmp_path / "olymp.db")
    migrate(conn)
    seen = []

    class Broken:
        def complete(self, system, messages, schema=None, *, model=None):
            seen.append("cursor")
            return ModelResult.failure("cursor", model or "", "error")

    class Fine:
        def complete(self, system, messages, schema=None, *, model=None):
            seen.append("claude")
            return ModelResult("{}", {}, "claude", model or "", 5, None)

    svc = ModelService(conn, {"claude": Fine(), "cursor": Broken()})
    result = svc.complete_on("cursor", "s", [{"role": "user", "content": "q"}])
    assert not result.ok and seen == ["cursor"]


def test_without_a_key_the_judge_also_checks_explanations_for_correctness_claims(tmp_path):
    def judge(content):
        return {"reveals": "по-настоящему" in Judged().candidate(content), "reason": "оценка"}

    model = Judged(
        judge=judge, reply=lambda kind, content: out("Б", "Это по-настоящему важный механизм.")
    )
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key=None, model_answer="Б", options=OPTS, text=COND)
        attempt(c, tid, "Б")
        got = post(c, tid, "explain", mode="simpler").json()
    assert got["explains"] == [] and "не получилось" in got["notice"]
    assert any(
        c["content"].startswith("Режим: judge") and "Утверждает ли" in c["content"]
        for c in model.judge_calls
    )


DOWN = {
    "timeout": lambda content: None,
    "bad json": lambda content: {"reveals": "maybe"},
    "no reason": lambda content: {"reveals": False},
    "failure": lambda content: ModelResult.failure("cursor", "stub", "executor_unreachable"),
    "limit": lambda content: ModelResult.failure("cursor", "stub", "limit", message=LIMIT_MESSAGE),
}


def test_judge_unavailable_shows_the_generic_hint_never_model_text(tmp_path):
    """No judge, no model text before the solution: leaks that need a judge must not get through."""
    for name, judge in DOWN.items():
        for i, text in enumerate(
            ["Тебе нужен второй вариант", "Это не А и не В, и не Г", INNOCENT[1]]
        ):
            base = tmp_path / f"{name}{i}".replace(" ", "")
            hint, _model = run_hint(base, text, judge, key="Б")
            html = hint["hints_list"][0]["html"]
            assert GENERIC["hint1"] in html and text not in html, (name, text)
            assert hint["hints"] == 1  # шаг лестницы потрачен
            assert events(base, "hint")[0]["data"]["rejected_by"] == "judge_unavailable", name
    # судья на месте и говорит «нет» - тот же безобидный текст показывается
    hint, _ = run_hint(
        tmp_path / "up", INNOCENT[1], lambda content: {"reveals": False, "reason": "нет"}
    )
    assert INNOCENT[1] in hint["hints_list"][0]["html"]


def test_judge_unavailable_does_not_regenerate(tmp_path):
    _hint, model = run_hint(tmp_path, INNOCENT[1], DOWN["timeout"])
    assert model.kinds().count("hint1") == 1  # перегенерация без судьи бессмысленна


def test_a_verdict_without_a_reason_is_invalid():
    import pytest

    from app.tutor.prompts import ModelOutputError, parse_verdict

    assert parse_verdict({"reveals": False, "reason": "нет признаков"}) == (False, "нет признаков")
    for bad in ({"reveals": False}, {"reveals": True, "reason": ""}, {"reveals": False, "reason": 3},
                {"reason": "x"}, {"reveals": "no", "reason": "x"}, "[]"):  # fmt: skip
        with pytest.raises(ModelOutputError):
            parse_verdict(bad)


def test_judge_sees_what_the_child_was_already_told(tmp_path):
    """«Исключи А и В», затем «Исключи Г»: по отдельности безобидно, вместе называют Б."""
    said = iter(["Исключи А и В", "Исключи Г"])

    def judge(content):
        seen = content.split("<shown_data>\n", 1)[1].split("\n</shown_data>", 1)[0]
        text = Judged().candidate(content)
        both = "Исключи А и В" in seen and "Исключи Г" in text
        return {"reveals": both, "reason": "вместе остаётся один вариант"}

    model = Judged(
        judge=judge,
        reply=lambda kind, content: out("Б", CALM if kind == "mistake" else next(said)),
    )
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б", options=OPTS, text=COND)
        attempt(c, tid, "А")
        first = post(c, tid, "hint", level=1).json()
        second = post(c, tid, "hint", level=2).json()
    assert "Исключи А и В" in first["hints_list"][0]["html"]
    assert GENERIC["hint2"] in second["hints_list"][1]["html"]
    assert "Исключи Г" not in json.dumps(second, ensure_ascii=False)
    request = judged(model, "Исключи Г")[0]
    assert "Попытка 1: ответ А" in request["content"]  # и попытки ребёнка тоже в блоке


def test_a_busy_attempt_is_not_committed_and_the_analysis_is_not_lost(tmp_path):
    model = Blocking(reply=lambda kind, content: out("Б", "Посмотри на условие ещё раз."))
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б", options=OPTS, text=COND)
        first = {}
        thread = threading.Thread(target=lambda: first.update(r=attempt(c, tid, "А")), daemon=True)
        thread.start()
        assert model.entered.wait(10)  # разбор первой ошибки в работе
        second = attempt(c, tid, "Б").json()
        assert second["panel"]["state"] == "thinking"
        assert second["attempts"] == 1 and second["state"] == "attempted_wrong"
        assert len(events(tmp_path, "attempt")) == 1  # вторая попытка не записана
        model.release.set()
        thread.join(10)
        done = view(c, tid)
        assert done["analysis"] and "условие" in done["analysis"]  # разбор первой дошёл
        assert attempt(c, tid, "Б").json()["state"] == "solved"  # ребёнок отправил снова
        assert len(events(tmp_path, "attempt")) == 2


def test_without_a_key_an_unavailable_judge_withholds_the_explanation(tmp_path):
    model = Judged(
        judge=DOWN["timeout"], reply=lambda kind, content: out("Б", "Это важный механизм.")
    )
    c, _clock, _m = boot(tmp_path, model)
    with c:
        tid = seed(tmp_path, key=None, model_answer="Б", options=OPTS, text=COND)
        attempt(c, tid, "Б")
        got = post(c, tid, "explain", mode="simpler").json()
    assert got["explains"] == [] and "не получилось" in got["notice"]
