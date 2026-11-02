"""Publication gates must inspect every official answer and a catalogued topic."""

import pytest
from fastapi.testclient import TestClient

from app.ingest.store import open_db
from tests.bank_helpers import (
    PARENT,
    StubModel,
    answer_by_number,
    build_app,
    reply,
    row,
    task_ids,
    text_pdf,
    upload,
)
from tests.test_pipeline_imported import blocked_pass, child_ids, feed, mk, run_action, sql, start


@pytest.mark.parametrize("topic", [None, "invented.topic"])
def test_uncatalogued_topic_stays_with_parent(tmp_path, topic):
    client, model, (tid,) = start(tmp_path, [mk(1, topic=topic)], {"1": "Б"}, keys={"1": "Б"})
    with client:
        assert row(tmp_path, tid)["auto_published"] == 0
        assert child_ids(client) == []
        assert "тем" in feed(client)[tid]["hint"].lower()
        assert model.calls_of("task_answer") == []


@pytest.mark.parametrize("answer", ["Б", "В"])
def test_conflicting_answers_inside_one_key_file_stay_with_parent(tmp_path, answer):
    model = StubModel(split=reply(mk(1, topic="b10")), answer=answer_by_number({"1": answer}))
    with TestClient(build_app(tmp_path, model, auto_publish=True)) as client:
        upload(client, text_pdf([["Ключи", "1. Б"], ["1. В"]]), kind="key")
        upload(client, text_pdf([["1. вопрос"]]))
        (tid,) = task_ids(tmp_path)
        assert row(tmp_path, tid)["auto_published"] == 0
        assert child_ids(client) == []
        assert "разные" in feed(client)[tid]["hint"]
        assert model.calls_of("task_answer") == []


def test_repeated_equivalent_answers_are_unambiguous(tmp_path):
    model = StubModel(split=reply(mk(1, topic="b10")), answer=answer_by_number({"1": "Б"}))
    with TestClient(build_app(tmp_path, model, auto_publish=True)) as client:
        upload(client, text_pdf([["Ключи", "1. Б"], ["01. Б"]]), kind="key")
        upload(client, text_pdf([["1. вопрос"]]))
        (tid,) = task_ids(tmp_path)
        assert row(tmp_path, tid)["auto_published"] == 1
        assert child_ids(client) == [tid]


def test_an_unparsed_duplicate_key_is_not_ignored(tmp_path):
    model = StubModel(split=reply(mk(1)), answer=answer_by_number({"1": "Б"}))
    with TestClient(build_app(tmp_path, model, auto_publish=True)) as client:
        upload(client, text_pdf([["Ключи", "1. Б", "1. ???"]]), kind="key")
        upload(client, text_pdf([["1. вопрос"]]))
        (tid,) = task_ids(tmp_path)
        assert child_ids(client) == []
        assert "не все строки" in feed(client)[tid]["hint"]
        assert model.calls_of("task_answer") == []


def test_an_unverified_page_candidate_cannot_auto_publish(tmp_path):
    options = [
        {"label": str(i), "text": word}
        for i, word in enumerate(["ядро", "рибосома", "вакуоль", "мембрана", "хлоропласт"], 1)
    ]
    task = mk(1, type="multiple", options=options)
    task["answer_text"] = "135"
    client, model, (tid,) = start(tmp_path, [task], {"1": "1, 3, 5"})
    with client:
        assert row(tmp_path, tid)["answer_key"] is None
        assert row(tmp_path, tid)["auto_published"] == 0
        assert child_ids(client) == []
        assert feed(client)[tid]["key_unverified"] is True
        assert model.calls_of("task_answer") == []


@pytest.mark.parametrize("change", ["topic", "key"])
def test_publication_rechecks_topic_and_every_key_after_model_call(tmp_path, change):
    def mutate(client, ids):
        if change == "topic":
            sql(tmp_path, "UPDATE task_item SET topic=NULL WHERE id=?", (ids[0],))
        else:
            sql(tmp_path, "UPDATE key_entry SET answer='Б' || char(10) || 'В'")

    client, (tid,) = blocked_pass(tmp_path, [mk(1)], {"1": "Б"}, {"1": "Б"}, mutate)
    with client:
        assert child_ids(client) == []
        assert row(tmp_path, tid)["auto_published"] == 0
        assert ("тем" if change == "topic" else "разные") in feed(client)[tid]["hint"]


@pytest.mark.parametrize("missing_file", [False, True])
def test_existing_key_files_are_rechecked_before_manual_publication(tmp_path, missing_file):
    model = StubModel(split=reply(mk(1)), answer=answer_by_number({"1": "Б"}))
    with TestClient(build_app(tmp_path, model, auto_publish=False)) as client:
        upload(client, text_pdf([["Ключи", "1. Б", "1. В"]]), kind="key")
        upload(client, text_pdf([["1. вопрос"]]))
        (tid,) = task_ids(tmp_path)
        # The deployed importer used to keep only the first answer for each number.
        sql(tmp_path, "UPDATE key_entry SET answer='Б'")
        if missing_file:
            ctx = client.app.state.ctx
            conn = open_db(ctx.db_path)
            try:
                name = conn.execute("SELECT file FROM material WHERE kind='key'").fetchone()[0]
            finally:
                conn.close()
            ctx.material_path(name).unlink()
        run_action(client)
        assert child_ids(client) == []
        assert row(tmp_path, tid)["auto_published"] == 0
        expected = "исходный файл" if missing_file else "разные"
        assert expected in feed(client)[tid]["hint"]
        assert model.calls_of("task_answer") == []


@pytest.mark.parametrize("marker", ["Правильный ответ:", "Ответ:", "Верный вариант —", ""])
def test_textual_answer_in_condition_blocks_publication(tmp_path, marker):
    task = mk(1, f"Какая структура синтезирует белок? {marker} рибосома.")
    client, model, (tid,) = start(tmp_path, [task], {"1": "Б"}, keys={"1": "Б"})
    with client:
        assert child_ids(client) == []
        assert "текст ответа" in feed(client)[tid]["hint"]
        assert model.calls_of("task_answer") == []


@pytest.mark.parametrize("kind,answer", [("single", "Д"), ("match", "1-А")])
def test_official_answer_must_fit_the_whole_task(tmp_path, kind, answer):
    options = None
    if kind == "match":
        options = {
            "left": [{"label": "1", "text": "ядро"}, {"label": "2", "text": "рибосома"}],
            "right": [{"label": "А", "text": "ДНК"}, {"label": "Б", "text": "белок"}],
        }
    task = mk(1, type=kind, **({"options": options} if options else {}))
    client, model, (tid,) = start(tmp_path, [task], {"1": answer}, keys={"1": answer})
    with client:
        assert child_ids(client) == []
        assert "ключ" in feed(client)[tid]["hint"]
        assert model.calls_of("task_answer") == []


@pytest.mark.parametrize("warning", [False, True])
def test_unresolved_or_invalid_merged_structure_stays_with_parent(tmp_path, warning):
    model = StubModel(split=reply(mk(1)), answer=answer_by_number({"1": "Б"}))
    with TestClient(build_app(tmp_path, model, auto_publish=False)) as client:
        upload(client, text_pdf([["Ключи", "1. Б"]]), kind="key")
        upload(client, text_pdf([["1. вопрос"]]))
        (tid,) = task_ids(tmp_path)
        if warning:
            sql(tmp_path, "UPDATE task_item SET review_note='условие собрано из двух порций'")
        else:
            sql(
                tmp_path,
                "UPDATE task_item SET options=json_insert(options,'$[#]', "
                "json_object('label','A','text','другой вариант'))",
            )
        run_action(client)
        assert child_ids(client) == []
        assert "структур" in feed(client)[tid]["hint"]
        assert model.calls_of("task_answer") == []


def test_reparse_cannot_reuse_model_answer_for_replacement_task(tmp_path):
    def mutate(client, ids):
        material = row(tmp_path, ids[0])["material_id"]
        client.app.state.ctx.model.split = reply(mk(1, "Новое условие после повторного разбора"))
        response = client.post(f"/parent/materials/{material}/reparse", headers=PARENT)
        assert response.status_code == 202, response.text
        assert task_ids(tmp_path) == ids  # SQLite reused the deleted row ID.

    client, (tid,) = blocked_pass(tmp_path, [mk(1)], {"1": "Б"}, {"1": "Б"}, mutate)
    with client:
        assert row(tmp_path, tid)["text"] == "Новое условие после повторного разбора"
        assert row(tmp_path, tid)["model_answer"] is None
        assert row(tmp_path, tid)["auto_published"] == 0
        assert child_ids(client) == []
