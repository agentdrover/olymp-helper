"""K5e1 (#2134): ответы со страниц становятся кандидатами ключа - строго и без автоподтверждения."""

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.ingest.keys import page_candidate
from app.ingest.page_answer import build_value, check_answer, fit_page
from app.model.types import ModelResult
from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    build_app,
    db,
    make_task,
    reply,
    task_ids,
    text_pdf,
    upload,
)

DIGITS = [{"label": str(i), "text": f"утверждение {i}"} for i in range(1, 7)]
LETTERS = [{"label": c, "text": f"вариант {c.lower()}"} for c in "АБВГД"]
MATCH = {
    "left": [{"label": "1", "text": "клетка"}, {"label": "2", "text": "ткань"}],
    "right": [{"label": c, "text": f"функция {c.lower()}"} for c in "АБВ"],
}


def fit(raw, kind, options=None):
    return fit_page(raw, kind, options)


def test_confirmed_answer_forms_become_candidates():
    # Буквы без разделителя, через пробел и точку; латиница вместо кириллицы.
    assert fit("АВГ", "multiple", LETTERS).value == ["А", "В", "Г"]
    assert fit("А В", "multiple", LETTERS).value == ["А", "В"]
    assert fit("А.В.Г", "multiple", LETTERS).value == ["А", "В", "Г"]
    assert fit("AB", "multiple", LETTERS).value == ["А", "В"]
    assert fit("B", "single", LETTERS).value == "В"
    # Цифры слитно - только когда все обозначения однозначные цифры; пробелы - как раньше.
    assert fit("135", "multiple", DIGITS).value == ["1", "3", "5"]
    assert fit("135", "multiple", DIGITS).extended is True
    assert fit("1 3 5", "multiple", DIGITS).extended is False
    # Пояснения в скобках после списка не становятся обозначениями.
    assert fit("1, 3, 5 (2,5 балла) (1 балл)", "multiple", DIGITS).value == ["1", "3", "5"]
    # Пары соответствия: слитная запись, разные тире, регистр.
    got = fit("1-А; 2Б; 1 – а", "match", MATCH)
    assert got.value == {"1": "А", "2": "Б"} and got.extended is True
    # Десятичная запятая и разрешённый суффикс единицы; значение не пересчитывается.
    assert fit("2,5", "numeric").value == "2.5" and fit("2,5", "numeric").extended is False
    assert fit("12 см", "numeric").value == "12" and fit("12 см", "numeric").extended is True
    assert fit("5 мм", "numeric").value == "5"


@pytest.mark.parametrize(
    ("raw", "kind", "options", "reason"),
    [
        ("1-А; 1-Б; 2-Б", "match", MATCH, "conflict"),
        ("1-А; 2-Б; 3-Я", "match", MATCH, "foreign_label"),
        ("1-А", "match", MATCH, "partial_match"),
        ("2-Б", "match", MATCH, "partial_match"),
        ("Б Г", "single", LETTERS, "conflict"),
        ("АЯ", "multiple", LETTERS, "foreign_label"),
        ("ААВ", "multiple", LETTERS, "conflict"),
        ("112", "multiple", DIGITS, "conflict"),
        ("789", "multiple", DIGITS, "foreign_label"),
        ("12 лет", "numeric", None, "needs_unit_context"),
        ("9l999 l", "numeric", None, "needs_unit_context"),
        ("2.5; 3.5", "numeric", None, "ambiguous"),
        ("двадцать", "numeric", None, "unparsed"),
        ("", "single", LETTERS, "empty"),
        ("А", "open", None, "open_type"),
    ],
)
def test_refusals_carry_a_reason(raw, kind, options, reason):
    got = fit(raw, kind, options)
    assert got.value is None and got.reason == reason


def test_digit_run_needs_single_digit_labels_and_known_labels():
    wide = [{"label": str(i), "text": "x"} for i in (1, 2, 12)]
    assert fit("112", "multiple", wide).value is None  # «12» - обозначение, но «112» - нет
    for count in (2, 3, 4):  # знаков больше, чем обозначений
        labels = [{"label": str(i), "text": "x"} for i in range(1, count + 1)]
        assert fit("999", "multiple", labels).value is None


def test_strict_validator_is_shared():
    assert check_answer({"1": "А", "2": "Б"}, "match", MATCH) is None
    assert check_answer({"1": "А"}, "match", MATCH) == "partial_match"
    assert check_answer({"1": "Я", "2": "Б"}, "match", MATCH) == "foreign_label"  # правая сторона
    assert build_value("match", MATCH, pairs=[("1", "Я"), ("2", "Б")]) == (None, "foreign_label")
    assert check_answer(["А", "А"], "multiple", LETTERS) == "conflict"
    assert check_answer("Я", "single", LETTERS) == "foreign_label"
    assert build_value("match", MATCH, pairs=[("1", "А"), ("1", "Б"), ("2", "Б")]) == (
        None,
        "conflict",
    )


def test_legacy_forms_keep_their_old_candidate_and_new_ones_are_marked():
    def task(raw, kind, options):
        return {
            "answer_text": raw,
            "type": kind,
            "options": json.dumps(options, ensure_ascii=False),
            "material_id": 1,
            "revision": 1,
        }

    old = page_candidate(task("Б, Г", "multiple", LETTERS))
    assert old["unverified"] is False and old["reason"] == "legacy_form"
    new = page_candidate(task("135", "multiple", DIGITS))
    assert new["unverified"] is True and new["reason"] == "extended_form"


# ------------------------------------------------------------------- модель


class NormalizerModel(StubModel):
    """Подменяет только ответ модели-нормализатора."""

    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.norm_calls = []

    def complete(self, system, messages, schema=None, session_id=None):
        if (schema or {}).get("title") != "page_answer_normalize":
            return super().complete(system, messages, schema, session_id)
        self.norm_calls.append(messages[-1]["content"])
        payload = self.payload(messages[-1]["content"]) if callable(self.payload) else self.payload
        if payload is None:
            return ModelResult.failure("claude", "stub", "error")
        return ModelResult(None, payload, "claude", "stub", 1, None)


def start(tmp_path, answer_text, kind="single", options=None, payload=None):
    task = make_task(1, "Вопрос номер 1", type=kind, options=options)
    model = NormalizerModel(payload or {"status": "unparsable"})
    model.split = reply(task)
    client = TestClient(build_app(tmp_path, model))
    client.__enter__()
    upload(client, text_pdf([["1. вопрос"]]))
    (tid,) = task_ids(tmp_path)
    conn = db(tmp_path)
    conn.execute("UPDATE task_item SET answer_text = ? WHERE id = ?", (answer_text, tid))
    conn.close()
    return client, model, tid


def normalize(client):
    r = client.post("/parent/page-answers/normalize", headers=PARENT, json={})
    assert r.status_code == 200, r.text
    return r.json()["counts"]


def stored(tmp_path, tid):
    conn = db(tmp_path)
    try:
        return conn.execute("SELECT * FROM page_answer WHERE task_item_id = ?", (tid,)).fetchone()
    finally:
        conn.close()


def detail(client, tid):
    return client.get(f"/parent/tasks/{tid}", headers=PARENT).json()


def test_model_normalizer_only_proposes_valid_candidates(tmp_path):
    client, model, tid = start(
        tmp_path, "первый вариант", payload={"status": "ok", "labels": ["А"]}
    )
    with client:
        assert detail(client, tid)["key_candidate"] is None
        assert model.norm_calls == []  # чтение карточки модель не вызывает
        assert normalize(client)["stored"] == 1
        # Модель видела только ответ и обозначения: ни условия задания, ни рисунка.
        sent = model.norm_calls[0]
        assert "первый вариант" in sent and "Вопрос номер 1" not in sent
        cand = detail(client, tid)["key_candidate"]
        assert cand["source"] == "page_model" and cand["unverified"] is True
        row = stored(tmp_path, tid)
        assert (row["source"], json.loads(row["value"]), row["revision"]) == ("page_model", "А", 1)
        # Повтор не платит второй раз.
        assert normalize(client)["stored"] == 0 and len(model.norm_calls) == 1


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"status": "ok", "labels": ["Я"]}, "foreign_label"),  # чужое обозначение
        ({"status": "ok", "labels": ["А", "Б"]}, "conflict"),  # два ответа на single
        ({"status": "ok", "labels": []}, "empty"),
        ({"status": "unparsable"}, "unparsable"),
    ],
)
def test_model_output_that_fails_the_validator_is_not_a_candidate(tmp_path, payload, reason):
    client, model, tid = start(tmp_path, "первый вариант", payload=payload)
    with client:
        normalize(client)
        row = stored(tmp_path, tid)
        assert row["value"] is None and row["reason"] == reason
        assert detail(client, tid)["key_candidate"] is None
        normalize(client)
        assert len(model.norm_calls) == 1  # отказ тоже записан: повтор не платный


def test_model_pairs_conflict_is_found_before_folding(tmp_path):
    pairs = [{"left": "1", "right": "А"}, {"left": "1", "right": "Б"}, {"left": "2", "right": "Б"}]
    client, _, tid = start(
        tmp_path, "см. рисунок", "match", MATCH, {"status": "ok", "pairs": pairs}
    )
    with client:
        normalize(client)
        assert stored(tmp_path, tid)["reason"] == "conflict"


def test_a_failed_model_call_is_not_stored_and_can_be_retried(tmp_path):
    client, model, tid = start(tmp_path, "первый вариант")
    model.payload = lambda _content: None
    with client:
        assert normalize(client)["error"] == 1
        assert stored(tmp_path, tid) is None
        model.payload = {"status": "ok", "labels": ["Б"]}
        assert normalize(client)["stored"] == 1


def test_unverified_candidates_are_never_auto_promoted(tmp_path):
    client, _, tid = start(tmp_path, "первый вариант", payload={"status": "ok", "labels": ["Б"]})
    with client:
        normalize(client)
        card = detail(client, tid)["card"]
        assert card["key"] == "Б" and card["key_unverified"] is True
        assert card["key_source"] != "official" and "модель" in card["key_source"]
        # Подтверждение без явного ответа отклонено, ключ не появился.
        r = client.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        assert r.status_code == 422
        conn = db(tmp_path)
        assert conn.execute("SELECT answer_key FROM task_item").fetchone()[0] is None
        conn.close()
        # Явное подтверждение (кнопка шлёт ответ) делает ключ родительским, не official.
        r = client.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={"answer": "Б"})
        assert r.status_code == 200, r.text
        conn = db(tmp_path)
        assert conn.execute("SELECT key_source FROM task_item").fetchone()[0] == "parent"
        conn.close()


def test_a_new_rule_form_is_not_official_either_but_an_old_one_still_is(tmp_path):
    client, _, tid = start(tmp_path, "135", "multiple", DIGITS)
    with client:
        card = detail(client, tid)["card"]
        assert card["key_unverified"] is True and card["key_source"] != "official"
        assert (
            client.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 422
        )
    client, _, tid = start(tmp_path / "old", "Б, Г", "multiple", LETTERS)
    with client:
        assert detail(client, tid)["card"]["key_source"] == "official"
        assert (
            client.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        )
        conn = db(tmp_path / "old")
        assert conn.execute("SELECT key_source FROM task_item").fetchone()[0] == "official"
        conn.close()


# ------------------------------------------------------------- круг 1 (Codex)


def set_row(tmp_path, tid, **cols):
    conn = db(tmp_path)
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE task_item SET {sets} WHERE id = ?", (*cols.values(), tid))
    conn.close()


def test_only_listed_remarks_after_the_list_are_dropped():
    assert fit("1, 3 (любой порядок)", "multiple", DIGITS).value == ["1", "3"]
    assert fit("1, 3 (в любом порядке) (2 балла)", "multiple", DIGITS).value == ["1", "3"]
    for raw in (
        "1 (или 2)",
        "1, 2 (кроме 2)",
        "1, 3 (2 балла) (или 5)",
        "1, 2 (кроме 2; 1 балл)",  # «балл» внутри не делает пояснение допустимым
        "1 (или 2 за 1 балл)",
        "1, 2 (3)",
    ):
        got = fit(raw, "multiple", DIGITS)
        assert got.value is None and got.reason == "ambiguous", raw


def test_numeric_answers_the_rule_cannot_read_go_to_the_model(tmp_path):
    client, model, tid = start(
        tmp_path,
        "двадцать восемь",
        "numeric",
        payload={"status": "ok", "number": "28"},
    )
    with client:
        assert normalize(client)["stored"] == 1
        assert detail(client, tid)["key_candidate"]["answer"] == "28"
        assert len(model.norm_calls) == 1


def test_an_unverified_candidate_does_not_take_a_task_out_of_the_feed(tmp_path):
    client, model, tid = start(
        tmp_path, "первый вариант", payload={"status": "ok", "labels": ["Б"]}
    )
    with client:
        set_row(tmp_path, tid, import_status="checked")
        assert detail(client, tid)["card"]["kind"] == "model_missing"
        normalize(client)
        card = detail(client, tid)["card"]
        assert card is not None and card["kind"] == "model_missing"
        assert card["key"] == "Б" and card["key_unverified"] is True
        queue = client.get("/parent/review/queue", headers=PARENT).json()
        assert tid in [c["task_id"] for c in queue["cards"]]
        # Ответ модели можно запросить: непроверенный кандидат ключом не считается.
        model.answer = lambda _c: {"proposed_answer": "А", "explanation": "e", "uncertainty": "low"}
        r = client.post(f"/parent/tasks/{tid}/retry-model", headers=PARENT, json={})
        assert r.status_code == 200, r.text
        assert detail(client, tid)["model_answer"] == "А"  # ответ модели действительно записан


def test_the_discrepancy_card_can_be_resolved_on_an_unverified_candidate(tmp_path):
    client, _, tid = start(tmp_path, "первый вариант", payload={"status": "ok", "labels": ["Б"]})
    with client:
        normalize(client)
        seen = json.dumps({"answer": "А", "value": "А", "explanation": "", "unverified": True})
        set_row(tmp_path, tid, model_answer=seen)
        card = detail(client, tid)["card"]
        assert card["kind"] == "discrepancy" and card["button_answer"] == "Б"
        html = client.get(f"/parent/review/{tid}", headers=PARENT).text
        assert 'data-choice="key" data-answer="Б"' in html
        r = client.post(
            f"/parent/tasks/{tid}/resolve",
            headers=PARENT,
            json={"choice": "key", "answer": card["button_answer"]},
        )
        assert r.status_code == 200, r.text


def test_the_source_of_an_unverified_candidate_is_shown_on_every_card_kind(tmp_path):
    client, _, tid = start(tmp_path, "первый вариант", payload={"status": "ok", "labels": ["Б"]})
    with client:
        normalize(client)
        seen = json.dumps({"answer": "А", "value": "А", "explanation": "", "unverified": True})
        for cols in (
            {},  # confirm
            {"model_answer": seen},  # discrepancy
            {
                "import_status": "unsupported",
                "figure": json.dumps({"page": 1, "regions": [], "confirmed": False}),
                "unsupported_reason": "нужен рисунок",
            },  # needs_figure
        ):
            if cols:
                set_row(tmp_path, tid, **cols)
            html = client.get(f"/parent/review/{tid}", headers=PARENT).text
            kind = detail(client, tid)["card"]["kind"]
            assert "через модель, не проверен" in html, kind
            assert "Ключ из загруженного файла" not in html, kind


def test_a_stale_rules_version_makes_the_stored_result_absent(tmp_path):
    client, model, tid = start(
        tmp_path, "первый вариант", payload={"status": "ok", "labels": ["Б"]}
    )
    with client:
        normalize(client)
        conn = db(tmp_path)
        conn.execute("UPDATE page_answer SET rules_version = 0")
        conn.close()
        assert detail(client, tid)["key_candidate"] is None
        assert detail(client, tid)["card"]["key"] is None
        assert normalize(client)["stored"] == 1 and len(model.norm_calls) == 2
        assert detail(client, tid)["key_candidate"]["answer"] == "Б"


def test_a_task_excluded_during_the_batch_is_not_sent_or_stored(tmp_path):
    tasks = [make_task(n, f"Вопрос номер {n}") for n in (1, 2)]
    model = NormalizerModel(None)
    model.split = reply(*tasks)
    client = TestClient(build_app(tmp_path, model))
    with client:
        upload(client, text_pdf([["1. вопрос", "2. вопрос"]]))
        first, second = task_ids(tmp_path)
        for tid in (first, second):
            set_row(tmp_path, tid, answer_text="первый вариант")

        def hook(_content):
            set_row(tmp_path, second, excluded=1)  # родитель исключает соседа во время пакета
            return {"status": "ok", "labels": ["А"]}

        model.payload = hook
        counts = normalize(client)
        assert counts["stored"] == 1 and len(model.norm_calls) == 1
        assert stored(tmp_path, second) is None and stored(tmp_path, first) is not None

        # Исключено во время собственного запроса: ответ не пишется.
        set_row(tmp_path, second, excluded=0)
        conn = db(tmp_path)
        conn.execute("DELETE FROM page_answer")
        conn.close()
        model.payload = lambda _c: (
            set_row(tmp_path, first, excluded=1) or {"status": "ok", "labels": ["А"]}
        )
        normalize(client)
        assert stored(tmp_path, first) is None


def test_the_lock_is_released_when_the_background_connection_fails(tmp_path, monkeypatch):
    import time

    from app.ingest import page_normalizer, web

    client, _model, tid = start(
        tmp_path, "первый вариант", payload={"status": "ok", "labels": ["А"]}
    )
    with client:
        client.app.state.imports.sync = False  # как в работе: пакет идёт фоном

        real = web.open_db

        def boom(path):  # ломается только в фоновом потоке пакета; запрос открывает базу как обычно
            if threading.current_thread().name == "page-answers":
                raise OSError("db is gone")
            return real(path)

        with monkeypatch.context() as patch:
            patch.setattr(web, "open_db", boom)
            r = client.post("/parent/page-answers/normalize", headers=PARENT, json={})
            assert r.json()["started"] is True
            for _ in range(100):
                if not page_normalizer.LOCK.locked():
                    break
                time.sleep(0.05)
            assert not page_normalizer.LOCK.locked()
        r = client.post("/parent/page-answers/normalize", headers=PARENT, json={})
        assert r.json()["started"] is True  # не «уже идёт»
        for _ in range(100):
            if not page_normalizer.LOCK.locked():
                break
            time.sleep(0.05)
        assert stored(tmp_path, tid) is not None


NAMED = [{"label": "1", "text": "клетка"}, {"label": "2", "text": "ткань"}]


def test_forms_main_accepted_stay_accepted_and_legacy():
    """Прежний разбор принимал эти формы: кандидат остаётся как был (не unverified)."""
    for raw, kind, options, want in (
        ("клетка, ткань", "multiple", NAMED, ["1", "2"]),  # тексты вариантов
        ("Ткань", "single", NAMED, "2"),
        ("(Б)", "single", LETTERS, "Б"),
        ("(А), (В)", "multiple", LETTERS, ["А", "В"]),
        ("А) В)", "multiple", LETTERS, ["А", "В"]),
        ("1-А, 2-Б (2 балла)", "match", MATCH, {"1": "А", "2": "Б"}),
    ):
        got = fit(raw, kind, options)
        assert got.value == want and got.extended is False, raw


def test_published_figure_is_withdrawn_when_a_normalised_answer_shows_in_its_frame(tmp_path):
    from tests.test_layout import good_tasks, import_layout

    tasks = good_tasks()
    tasks[2]["answer_text"] = "двадцать восемь"  # «28» напечатано в ячейке страницы 2
    with import_layout(tmp_path, tasks) as client:
        by = {}
        for t in _tasks(tmp_path):  # номера повторяются в разделах: берём первый раздел
            by.setdefault(t["source_number"], t["id"])
        two, three = by["1.2"], by["1.3"]
        cell = [150, 510, 400, 536]
        r = client.post(f"/parent/tasks/{two}/key", headers=PARENT, json={"answer": "Б"})
        assert r.status_code == 200
        r = client.post(
            f"/parent/tasks/{two}/figure",
            headers=PARENT,
            json={
                "alt": "Фотографии животных в таблице",
                "teacher_note": "1 2 3 4 5",
                "no_answer_ack": True,
                "teacher_note_ack": True,
                "rect": cell,
                "page": 2,
            },
        )
        assert r.status_code == 200, r.text
        r = client.post(f"/parent/tasks/{two}/confirm", headers=PARENT, json={})
        assert r.status_code == 200, r.text
        child = {t["id"] for t in client.get("/api/tasks", headers=CHILD).json()["tasks"]}
        assert two in child
        model = NormalizerModel(
            lambda content: (
                {"status": "ok", "number": "28"}
                if "двадцать" in content
                else {"status": "unparsable"}
            )
        )
        model.split = client.model.split
        client.app.state.model = model
        assert normalize(client)["stored"] == 1
        assert detail(client, three)["key_candidate"]["answer"] == "28"
        child = {t["id"] for t in client.get("/api/tasks", headers=CHILD).json()["tasks"]}
        assert two not in child  # публикация снята после пакета


def _tasks(tmp_path):
    conn = db(tmp_path)
    try:
        return conn.execute("SELECT id, source_number FROM task_item").fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------- круг 3 (Opus)


@pytest.mark.parametrize("raw", ["Б 2 баллаВ", "15 баллов", "12 баллов", "Б 2 баллов1", "А2 балла"])
def test_points_are_stripped_only_as_a_separate_trailing_token(raw):
    kind, options = ("multiple", DIGITS) if raw.startswith("12") else ("single", LETTERS)
    assert fit(raw, kind, options).value is None, raw
    assert fit("Б 2 балла", "single", LETTERS).value == "Б"
    assert fit("Б, 2,5 балла", "single", LETTERS).value == "Б"
    assert fit("1, 3 (2 балла)", "multiple", DIGITS).value == ["1", "3"]


@pytest.mark.parametrize(
    ("raw", "options"),
    [
        (
            "АГ БД ВЕ",
            {
                "left": [{"label": c, "text": "x"} for c in "АБВ"],
                "right": [{"label": c, "text": "y"} for c in "ГДЕ"],
            },
        ),
        (
            "14 25 36",
            {
                "left": [{"label": c, "text": "x"} for c in "123"],
                "right": [{"label": c, "text": "y"} for c in "456"],
            },
        ),
    ],
)
def test_a_pair_without_separator_needs_sides_of_different_kind(raw, options):
    got = fit(raw, "match", options)
    assert got.value is None, raw


def test_a_stored_value_is_validated_again_on_read(tmp_path):
    client, _, tid = start(tmp_path, "первый вариант", payload={"status": "ok", "labels": ["Б"]})
    with client:
        normalize(client)
        conn = db(tmp_path)
        conn.execute("UPDATE page_answer SET value = ?", (json.dumps("Я"),))  # чужое обозначение
        conn.close()
        assert detail(client, tid)["key_candidate"] is None


def test_the_cache_is_keyed_to_the_content_not_to_the_revision(tmp_path):
    client, model, tid = start(
        tmp_path, "первый вариант", payload={"status": "ok", "labels": ["Б"]}
    )
    with client:
        normalize(client)
        r = client.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"text": "Новое условие"})
        assert r.status_code == 200 and r.json()["revision"] == 2
        assert detail(client, tid)["key_candidate"]["answer"] == "Б"  # правка условия не стирает
        assert normalize(client)["stored"] == 0 and len(model.norm_calls) == 1
        # Изменился текст ответа: итог относится к другому содержимому.
        set_row(tmp_path, tid, answer_text="второй вариант")
        assert detail(client, tid)["key_candidate"] is None


@pytest.mark.parametrize(
    "cols",
    [
        {"excluded": 1},
        {"revision": 2},
        {"answer_key": json.dumps({"type": "single", "value": "А"}), "key_source": "parent"},
        {"answer_text": "другой текст ответа"},
    ],
    ids=["excluded", "revision", "answer_key", "answer_text"],
)
def test_a_result_is_not_written_when_the_task_changed_during_the_call(tmp_path, cols):
    client, model, tid = start(tmp_path, "первый вариант")
    model.payload = lambda _c: set_row(tmp_path, tid, **cols) or {"status": "ok", "labels": ["А"]}
    with client:
        normalize(client)
        assert len(model.norm_calls) == 1 and stored(tmp_path, tid) is None


def test_a_second_command_is_refused_while_the_first_runs(tmp_path):
    import time

    from app.ingest import page_normalizer

    release, entered = threading.Event(), threading.Event()

    def slow(_content):
        entered.set()
        release.wait(10)
        return {"status": "ok", "labels": ["А"]}

    client, model, tid = start(tmp_path, "первый вариант", payload=slow)
    with client:
        client.app.state.imports.sync = False
        first = client.post("/parent/page-answers/normalize", headers=PARENT, json={})
        assert first.json()["started"] is True and entered.wait(5)
        second = client.post("/parent/page-answers/normalize", headers=PARENT, json={})
        assert second.json()["started"] is False
        release.set()
        for _ in range(100):
            if not page_normalizer.LOCK.locked():
                break
            time.sleep(0.05)
        assert len(model.norm_calls) == 1 and stored(tmp_path, tid) is not None


def _published_figure_layout(tmp_path, *, crash_task=None, candidate_task=None):
    """Раздел из test_layout: у 1.2 опубликован рисунок, в рамке которого напечатано «28»."""
    from tests.test_layout import good_tasks, import_layout

    tasks = good_tasks()
    tasks[2]["answer_text"] = "двадцать восемь"  # «28» напечатано в ячейке страницы 2
    if crash_task is not None:
        tasks[3]["answer_text"] = "что-то совсем непонятное"
    if candidate_task is not None:
        tasks[2]["answer_text"] = None
        tasks[1]["answer_text"] = "второй вариант"
    client = import_layout(tmp_path, tasks)
    by = {}
    for t in _tasks(tmp_path):  # номера повторяются в разделах: берём первый раздел
        by.setdefault(t["source_number"], t["id"])
    two = by["1.2"]
    cell = [150, 510, 400, 536]
    if candidate_task is None:
        assert (
            client.post(
                f"/parent/tasks/{two}/key", headers=PARENT, json={"answer": "Б"}
            ).status_code
            == 200
        )
        r = client.post(
            f"/parent/tasks/{two}/figure",
            headers=PARENT,
            json={
                "alt": "Фотографии животных в таблице",
                "teacher_note": "1 2 3 4 5",
                "no_answer_ack": True,
                "teacher_note_ack": True,
                "rect": cell,
                "page": 2,
            },
        )
        assert r.status_code == 200, r.text
        assert (
            client.post(f"/parent/tasks/{two}/confirm", headers=PARENT, json={}).status_code == 200
        )
        assert two in _child_ids(client)
    return client, by, cell


def _child_ids(client):
    return {t["id"] for t in client.get("/api/tasks", headers=CHILD).json()["tasks"]}


def test_figures_are_rechecked_even_when_the_batch_crashes(tmp_path):
    client, by, _ = _published_figure_layout(tmp_path, crash_task=True)
    with client:

        def reply_by_content(content):
            if "двадцать" in content:
                return {"status": "ok", "number": "28"}
            raise RuntimeError("db is locked")  # пакет падает на следующем задании

        model = NormalizerModel(reply_by_content)
        model.split = client.model.split
        client.app.state.model = model
        with pytest.raises(RuntimeError):
            client.post("/parent/page-answers/normalize", headers=PARENT, json={})
        assert detail(client, by["1.3"])["key_candidate"]["answer"] == "28"  # запись осталась
        assert by["1.2"] not in _child_ids(client)  # а публикация снята в finally


def test_publishing_with_the_preselected_candidate_records_where_it_came_from(tmp_path):
    client, by, cell = _published_figure_layout(tmp_path, candidate_task=True)
    with client:
        model = NormalizerModel(
            lambda content: (
                {"status": "ok", "labels": ["Б"]}
                if "второй" in content
                else {"status": "unparsable"}
            )
        )
        model.split = client.model.split
        client.app.state.model = model
        normalize(client)
        two = by["1.2"]
        assert detail(client, two)["key_candidate"]["unverified"] is True
        # Цепочка ленты: ключ (кандидат выбран заранее) -> рисунок -> публикация.
        r = client.post(
            f"/parent/tasks/{two}/key", headers=PARENT, json={"answer": "Б", "keep_candidate": True}
        )
        assert r.status_code == 200, r.text
        conn = db(tmp_path)
        row = conn.execute(
            "SELECT key_source, key_candidate_source, answer_key FROM task_item WHERE id = ?",
            (two,),
        ).fetchone()
        conn.close()
        assert row["answer_key"] is not None
        assert (row["key_source"], row["key_candidate_source"]) == ("parent", "page_model")
        r = client.post(
            f"/parent/tasks/{two}/figure",
            headers=PARENT,
            json={
                "alt": "Фотографии животных в таблице",
                "teacher_note": "1 2 3 4 5",
                "no_answer_ack": True,
                "teacher_note_ack": True,
                "rect": cell,
                "page": 2,
            },
        )
        assert r.status_code == 200, r.text
        assert (
            client.post(f"/parent/tasks/{two}/confirm", headers=PARENT, json={}).status_code == 200
        )
        assert two in _child_ids(client)


# ------------------------------------------------------------- круг 4


def candidate_source(tmp_path, tid):
    conn = db(tmp_path)
    try:
        return conn.execute(
            "SELECT key_source, key_candidate_source FROM task_item WHERE id = ?", (tid,)
        ).fetchone()
    finally:
        conn.close()


def put_key(client, tid, answer):
    r = client.post(f"/parent/tasks/{tid}/key", headers=PARENT, json={"answer": answer})
    assert r.status_code == 200, r.text


def with_candidate(tmp_path):
    return start(tmp_path, "первый вариант", payload={"status": "ok", "labels": ["Б"]})


def test_the_candidate_source_is_recorded_only_for_an_answer_equal_to_the_candidate(tmp_path):
    client, _, tid = with_candidate(tmp_path)
    with client:
        normalize(client)
        put_key(client, tid, "В")  # свой ответ, не кандидат
        assert tuple(candidate_source(tmp_path, tid)) == ("parent", None)


def test_the_candidate_source_is_not_set_again_when_a_key_is_changed(tmp_path):
    client, _, tid = with_candidate(tmp_path)
    with client:
        normalize(client)
        put_key(client, tid, "В")
        put_key(client, tid, "Б")  # ключ уже есть: совпадение с кандидатом - не подтверждение
        assert tuple(candidate_source(tmp_path, tid)) == ("parent", None)
    client, _, tid = with_candidate(tmp_path / "b")
    with client:
        normalize(client)
        put_key(client, tid, "Б")  # первый ключ равен кандидату: источник записан
        assert candidate_source(tmp_path / "b", tid)["key_candidate_source"] == "page_model"
        put_key(client, tid, "В")  # смена ключа стирает источник
        assert candidate_source(tmp_path / "b", tid)["key_candidate_source"] is None


def test_confirm_with_an_explicit_answer_records_the_candidate_source(tmp_path):
    client, _, tid = with_candidate(tmp_path)
    with client:
        normalize(client)
        r = client.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={"answer": "Б"})
        assert r.status_code == 200, r.text
        assert tuple(candidate_source(tmp_path, tid)) == ("parent", "page_model")


def test_the_cache_follows_labels_and_type_but_not_option_texts(tmp_path):
    client, model, tid = with_candidate(tmp_path)
    with client:
        normalize(client)
        conn = db(tmp_path)
        options = json.loads(conn.execute("SELECT options FROM task_item").fetchone()[0])
        # (ii) изменился только текст варианта: модель не зовётся, кандидат остался.
        renamed = [{**o, "text": o["text"] + " (правка)"} for o in options]
        conn.execute("UPDATE task_item SET options = ?", (json.dumps(renamed, ensure_ascii=False),))
        assert detail(client, tid)["key_candidate"]["answer"] == "Б"
        assert normalize(client)["stored"] == 0 and len(model.norm_calls) == 1
        # (i) изменились обозначения: итог относится к другим вариантам - его нет, модель зовётся.
        relabelled = [{**o, "label": o["label"] + "2"} for o in options]
        conn.execute(
            "UPDATE task_item SET options = ?", (json.dumps(relabelled, ensure_ascii=False),)
        )
        assert detail(client, tid)["key_candidate"] is None
        model.payload = {"status": "ok", "labels": ["Б2"]}
        assert normalize(client)["stored"] == 1 and len(model.norm_calls) == 2
        conn.close()


def test_the_proposal_source_is_shown_by_its_label_on_the_figure_card(tmp_path):
    client, by, _ = _published_figure_layout(tmp_path, candidate_task=True)
    with client:
        model = NormalizerModel(
            lambda content: (
                {"status": "ok", "labels": ["Б"]}
                if "второй" in content
                else {"status": "unparsable"}
            )
        )
        model.split = client.model.split
        client.app.state.model = model
        normalize(client)
        two = by["1.2"]
        put_key(client, two, "Б")
        card = detail(client, two)["card"]
        assert card["kind"] == "needs_figure" and card["key_candidate_source"] == "page_model"
        html = client.get(f"/parent/review/{two}", headers=PARENT).text
        assert "предложен: модель по ответу на странице" in html
        assert "предложен: page_model" not in html


def test_a_decimal_comma_next_to_a_label_before_points_is_ambiguous():
    got = fit("1, 3,5 балла", "multiple", DIGITS)
    assert got.value is None and got.reason == "ambiguous"
    assert fit("1, 3 (5 балла)", "multiple", DIGITS).value == ["1", "3"]
    assert fit("1, 3, 5 баллов", "multiple", DIGITS).value == ["1", "3"]
    assert fit("Б, 2,5 балла", "single", LETTERS).value == "Б"  # «2» - не обозначение


@pytest.mark.parametrize(
    ("raw", "kind", "want"),
    [
        ("1, 3 2,5 балла", "multiple", ["1", "3"]),
        ("1, 3 1,5 балла", "multiple", ["1", "3"]),
        ("1;3 2.5 балла", "multiple", ["1", "3"]),
        ("2 1,5 балла", "single", "2"),
        ("1, 3 (2,5 балла)", "multiple", ["1", "3"]),  # скобки: обозначение 2 не затронуто
        ("1, 3 2 балла", "multiple", ["1", "3"]),
    ],
)
def test_an_ordinary_points_tail_with_a_decimal_is_accepted(raw, kind, want):
    got = fit(raw, kind, DIGITS)
    assert got.value == want, raw


@pytest.mark.parametrize("raw", ["1, 3,5 балла", "1;3,5 балла", "1, 3, 2,5 балла"])
def test_a_decimal_right_after_a_list_separator_is_ambiguous(raw):
    got = fit(raw, "multiple", DIGITS)
    assert got.value is None and got.reason == "ambiguous", raw


def test_editing_option_texts_of_a_match_task_does_not_call_the_model_again(tmp_path):
    pairs = [{"left": "1", "right": "А"}, {"left": "2", "right": "Б"}]
    client, model, tid = start(
        tmp_path, "см. рисунок", "match", MATCH, {"status": "ok", "pairs": pairs}
    )
    with client:
        assert normalize(client)["stored"] == 1
        conn = db(tmp_path)
        options = json.loads(conn.execute("SELECT options FROM task_item").fetchone()[0])
        edited = {
            side: [{**o, "text": o["text"] + " (правка)"} for o in items]
            for side, items in options.items()
        }
        conn.execute("UPDATE task_item SET options = ?", (json.dumps(edited, ensure_ascii=False),))
        conn.close()
        assert detail(client, tid)["key_candidate"]["answer"] == "1-А; 2-Б"
        assert normalize(client)["stored"] == 0 and len(model.norm_calls) == 1


# ------------------------------------------------------------- Hub #2134 RED


@pytest.mark.parametrize(
    ("raw", "options"),
    [("А, А", LETTERS), ("A, А", LETTERS), ("1, 1, 3", DIGITS)],
    ids=["cyrillic", "latin_twin", "digits"],
)
def test_hub2134_separated_duplicate_multiple_labels_are_conflict(raw, options):
    got = fit(raw, "multiple", options)
    assert got.value is None and got.reason == "conflict"


@pytest.mark.parametrize(
    ("raw", "options"),
    [("А, А", LETTERS), ("A, А", LETTERS), ("1, 1, 3", DIGITS)],
    ids=["cyrillic", "latin_twin", "digits"],
)
def test_hub2134_duplicate_page_answer_never_becomes_official(tmp_path, raw, options):
    client, _, tid = start(tmp_path, raw, "multiple", options)
    with client:
        r = client.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        assert r.status_code == 200, r.text
        conn = db(tmp_path)
        row = conn.execute(
            "SELECT answer_key, key_source FROM task_item WHERE id = ?", (tid,)
        ).fetchone()
        conn.close()
        assert row["answer_key"] is None and row["key_source"] is None


@pytest.mark.parametrize(
    ("options", "labels"),
    [(LETTERS, ["А", "А"]), (LETTERS, ["A", "А"]), (DIGITS, ["1", "1", "3"])],
    ids=["cyrillic", "latin_twin", "digits"],
)
def test_hub2134_model_duplicate_multiple_labels_are_conflict(tmp_path, options, labels):
    client, _, tid = start(
        tmp_path,
        "несколько вариантов",
        "multiple",
        options,
        {"status": "ok", "labels": labels},
    )
    with client:
        assert normalize(client)["rejected"] == 1
        row = stored(tmp_path, tid)
        assert row["value"] is None and row["reason"] == "conflict"
        assert detail(client, tid)["key_candidate"] is None


@pytest.mark.parametrize(
    ("options", "labels", "want", "shown"),
    [
        (LETTERS, ["A", "В"], ["А", "В"], "А, В"),
        (DIGITS, ["1", "3"], ["1", "3"], "1, 3"),
    ],
    ids=["latin_twin", "digits"],
)
def test_hub2134_model_unique_multiple_labels_remain_candidates(
    tmp_path, options, labels, want, shown
):
    client, _, tid = start(
        tmp_path,
        "несколько вариантов",
        "multiple",
        options,
        {"status": "ok", "labels": labels},
    )
    with client:
        assert normalize(client)["stored"] == 1
        row = stored(tmp_path, tid)
        assert json.loads(row["value"]) == want and row["reason"] == "model_normalized"
        assert detail(client, tid)["key_candidate"]["answer"] == shown


@pytest.mark.parametrize("raw", ["12 лет", "12 месяцев", "9l999 l"])
def test_hub2134_unknown_numeric_unit_cannot_be_removed_by_model(tmp_path, raw):
    client, _, tid = start(
        tmp_path, raw, "numeric", payload={"status": "ok", "number": "12"}
    )
    with client:
        got = fit(raw, "numeric", None)
        assert got.value is None and got.reason == "needs_unit_context"
        normalize(client)
        row = stored(tmp_path, tid)
        assert row is None or (row["value"] is None and row["reason"] == "needs_unit_context")
        result = detail(client, tid)
        assert result["key_candidate"] is None
        assert result["card"]["key_refusal"] is not None


@pytest.mark.parametrize(
    ("raw", "want"),
    [("0012,50 см", "12.5"), ("5 мм", "5"), ("2,5", "2.5"), ("12%", "12"), ("3 шт.", "3")],
)
def test_hub2134_numeric_units_and_number_normalization_still_work(raw, want):
    got = fit(raw, "numeric", None)
    assert got.value == want and got.reason is None


@pytest.mark.parametrize(
    ("raw", "options"),
    [
        ("1-3, 2, 2", DIGITS),
        ("1-3, 1-3", DIGITS),
        ("1-3, 2-4", DIGITS),
        ("А-В, A, А", LETTERS),
    ],
    ids=["explicit_after_range", "repeated_range", "overlapping_ranges", "latin_twin"],
)
def test_hub2134_duplicates_after_range_expansion_are_conflict(raw, options):
    got = fit(raw, "multiple", options)
    assert got.value is None and got.reason == "conflict"


def test_hub2134_one_explicit_label_may_overlap_a_range():
    options = [{"label": str(i), "text": f"утверждение {i}"} for i in range(1, 8)]
    got = fit("2 … 7, 3", "multiple", options)
    assert got.value == ["2", "3", "4", "5", "6", "7"] and got.reason is None


@pytest.mark.parametrize(
    "raw",
    [
        "1-А; 2-Б (по 0,5 балла)",
        "1-А (0,5 балла); 2-Б (0,5 балла)",
        "1-А; 2-Б (по 1 баллу)",
    ],
)
def test_match_score_annotations_are_ignored_without_losing_pairs(raw):
    got = fit(raw, "match", MATCH)
    assert got.value == {"1": "А", "2": "Б"}, (raw, got)


@pytest.mark.parametrize(
    "raw",
    [
        "1-А (0,5 балла; 3-Я); 2-Б",
        "1 (0,5 балла)-А; 2-Б",
        "1-А (или 1-Б за 0,5 балла); 2-Б",
        "1-А (0,5 балла); 1-Б; 2-Б",
        "1-А (0,5 балла); 2-Я",
    ],
)
def test_match_score_annotations_do_not_hide_unparsed_or_conflicting_data(raw):
    assert fit(raw, "match", MATCH).value is None


@pytest.mark.parametrize("raw", [
    "1-А; (по 0,5 балла); 2-Б",
    "1-А; 2-Б; (по 0,5 балла)",
    "1–А; 2–Б; (по 1,5 балла)",
    "1-А,  (по 0,5 балла), 2-Б",
])
def test_match_score_after_separator_keeps_complete_pairs(raw):
    got = fit(raw, "match", MATCH)
    assert got.value == {"1": "А", "2": "Б"}


@pytest.mark.parametrize("raw", [
    "1-А; (по 0,5 балла; 3-Я); 2-Б",
    "1-А; (по 0,5 балла); 1-Б; 2-Б",
    "1-А; (по 0,5 балла); 2-Я",
    "1; (по 0,5 балла)-А; 2-Б",
])
def test_match_score_after_separator_cannot_hide_invalid_data(raw):
    assert fit(raw, "match", MATCH).value is None
