"""K5e1 (#2134): ответы со страниц становятся кандидатами ключа - строго и без автоподтверждения."""

import json

import pytest
from fastapi.testclient import TestClient

from app.ingest.keys import page_candidate
from app.ingest.page_answer import build_value, check_answer, fit_page
from app.model.types import ModelResult
from tests.bank_helpers import (
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
