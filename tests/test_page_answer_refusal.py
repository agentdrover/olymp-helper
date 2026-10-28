"""Причина, по которой ответ со страницы не разобран, доходит до родителя (K5e1, #2134)."""

import json

import pytest

from app.ingest import page_normalizer
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
        ("ААВ", "multiple", LETTERS, REASON_TEXT[CONFLICT]),
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


def test_no_reason_when_there_is_a_candidate_a_key_or_nothing_to_read(tmp_path):
    client, _, tid = start(tmp_path, "Б", "single", LETTERS)
    with client:
        info = detail(client, tid)
        assert info["key_candidate"] is not None and info["key_refusal"] is None
        assert info["card"]["key_refusal"] is None
        conn = db(tmp_path)
        task = conn.execute("SELECT * FROM task_item WHERE id = ?", (tid,)).fetchone()
        assert page_refusal(conn, task) is None
        for cols in ({"answer_text": ""}, {"type": "open", "import_status": "unsupported"}):
            set_row(tmp_path, tid, **cols)
            task = conn.execute("SELECT * FROM task_item WHERE id = ?", (tid,)).fetchone()
            assert page_refusal(conn, task) is None, cols
        set_row(
            tmp_path,
            tid,
            type="numeric",
            answer_text="12 лет",
            answer_key='"5"',
            key_source="parent",
        )
        task = conn.execute("SELECT * FROM task_item WHERE id = ?", (tid,)).fetchone()
        assert page_refusal(conn, task) is None  # ключ уже есть
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
