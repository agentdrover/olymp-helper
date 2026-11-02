"""Причина, по которой ответ со страницы не разобран, доходит до родителя (K5e1, #2134)."""

import json

import pytest

from app.ingest import keys, page_normalizer
from app.ingest.keys import page_refusal
from app.ingest.page_answer import (
    AMBIGUOUS,
    BAD_TYPE,
    CONFLICT,
    EMPTY,
    FOREIGN_LABEL,
    NEEDS_UNIT,
    NO_OPTIONS,
    PARTIAL,
    REASON_TEXT,
    UNPARSED,
    refusal_text,
)
from tests.bank_helpers import PARENT, db
from tests.test_page_answers import LETTERS, MATCH, detail, normalize, set_row, start

UNIT = REASON_TEXT[NEEDS_UNIT]


def test_the_conflict_phrase_names_both_a_repeat_and_different_values():
    text = REASON_TEXT[CONFLICT]
    assert "повторено" in text and "разными значениями" in text and "больше, чем" in text


def test_every_refusal_code_has_one_parent_phrase():
    for code in (EMPTY, NO_OPTIONS, BAD_TYPE, UNPARSED, FOREIGN_LABEL, CONFLICT, PARTIAL):
        assert refusal_text(code) and refusal_text(code) == REASON_TEXT[code]
    assert refusal_text(NEEDS_UNIT) and refusal_text(AMBIGUOUS)
    assert len(set(REASON_TEXT.values())) == len(REASON_TEXT)  # фразы не дублируются
    assert refusal_text(None) is None and refusal_text("open_type") is None


@pytest.mark.parametrize(
    ("raw", "kind", "options", "text"),
    [
        ("12 лет", "numeric", None, REASON_TEXT[NEEDS_UNIT]),
        ("1-А", "match", MATCH, REASON_TEXT[PARTIAL]),
        ("АЯ", "multiple", LETTERS, REASON_TEXT[FOREIGN_LABEL]),
        ("ААВ", "multiple", LETTERS, REASON_TEXT[CONFLICT]),  # повтор обозначения
        ("1-А; 1-Б; 2-Б", "match", MATCH, REASON_TEXT[CONFLICT]),  # разные значения одного
        ("Б Г", "single", LETTERS, REASON_TEXT[CONFLICT]),  # два ответа вместо одного
    ],
)
def test_the_parent_sees_why_the_page_answer_was_not_taken(tmp_path, raw, kind, options, text):
    client, _, tid = start(tmp_path, raw, kind, options)
    with client:
        info = detail(client, tid)
        assert info["key_candidate"] is None and info["key_refusal"] == text
        assert info["card"]["key_refusal"] == text
        assert text in client.get(f"/parent/review/{tid}", headers=PARENT).text


def test_the_reason_is_shown_on_the_figure_card_too(tmp_path):
    client, _, tid = start(tmp_path, "12 лет", "numeric")
    with client:
        set_row(
            tmp_path,
            tid,
            import_status="unsupported",
            figure=json.dumps({"page": 1, "regions": [], "confirmed": False}),
            unsupported_reason="нужен рисунок",
        )
        assert detail(client, tid)["card"]["kind"] == "needs_figure"
        assert UNIT in client.get(f"/parent/review/{tid}", headers=PARENT).text


def _task(conn, tid):
    return conn.execute("SELECT * FROM task_item WHERE id = ?", (tid,)).fetchone()


def test_a_candidate_from_the_model_hides_the_reason(tmp_path):
    client, _, tid = start(
        tmp_path, "двадцать восемь", "numeric", payload={"status": "ok", "number": "28"}
    )
    with client:
        conn = db(tmp_path)
        assert page_refusal(conn, _task(conn, tid)) == REASON_TEXT[UNPARSED]  # правило не читает
        assert normalize(client)["stored"] == 1
        assert page_refusal(conn, _task(conn, tid)) is None
        info = detail(client, tid)
        assert info["key_candidate"]["source"] == "page_model" and info["key_refusal"] is None
        assert info["card"]["key_refusal"] is None
        conn.close()


def test_a_candidate_from_the_key_file_hides_the_reason(tmp_path, monkeypatch):
    client, _, tid = start(tmp_path, "двадцать восемь", "numeric")
    with client:
        conn = db(tmp_path)
        assert page_refusal(conn, _task(conn, tid)) == REASON_TEXT[UNPARSED]
        monkeypatch.setattr(
            keys, "_uploaded_candidate", lambda c, t: {"value": "5", "source": "key_file"}
        )
        assert page_refusal(conn, _task(conn, tid)) is None
        conn.close()


def test_no_reason_when_the_rule_reads_the_answer_there_is_a_key_or_nothing_to_read(tmp_path):
    client, _, tid = start(tmp_path, "Б", "single", LETTERS)
    with client:
        info = detail(client, tid)
        assert info["key_candidate"] is not None and info["key_refusal"] is None
        assert info["card"]["key_refusal"] is None
        conn = db(tmp_path)
        assert page_refusal(conn, _task(conn, tid)) is None
        for cols in ({"answer_text": ""}, {"type": "open", "import_status": "unsupported"}):
            set_row(tmp_path, tid, **cols)
            assert page_refusal(conn, _task(conn, tid)) is None, cols
        set_row(
            tmp_path,
            tid,
            type="numeric",
            answer_text="12 лет",
            answer_key='"5"',
            key_source="parent",
        )
        assert page_refusal(conn, _task(conn, tid)) is None  # ключ уже есть
        conn.close()


def test_a_candidate_from_the_key_file_means_no_model_call(tmp_path, monkeypatch):
    client, model, _ = start(
        tmp_path, "двадцать восемь", "numeric", payload={"status": "ok", "number": "28"}
    )
    with client:
        assert normalize(client)["stored"] == 1  # без кандидата из файла модель зовётся
        assert len(model.norm_calls) == 1
        conn = db(tmp_path)
        conn.execute("DELETE FROM page_answer")
        conn.commit()
        conn.close()
        monkeypatch.setattr(page_normalizer, "_uploaded_candidate", lambda c, t: {"value": "5"})
        counts = normalize(client)
        assert counts["stored"] == 0 and len(model.norm_calls) == 1  # новых вызовов нет


def test_the_refusal_hint_matches_the_controls_of_the_card_kind(tmp_path):
    client, _, tid = start(tmp_path, "12 лет", "numeric")
    with client:
        html = client.get(f"/parent/review/{tid}", headers=PARENT).text
        assert detail(client, tid)["card"]["kind"] == "no_key"
        assert UNIT + " Впишите ответ сами." in html and 'id="answer"' in html
        set_row(tmp_path, tid, import_status="checked")  # ответа модели нет, рисунка нет
        html = client.get(f"/parent/review/{tid}", headers=PARENT).text
        assert detail(client, tid)["card"]["kind"] == "model_missing"
        assert UNIT + " Повторите запрос к модели." in html
        assert "Впишите" not in html and 'id="answer"' not in html
