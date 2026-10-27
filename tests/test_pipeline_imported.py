"""K5b: tasks of an imported collection with an OFFICIAL key publish themselves when the
automatic gates pass (SPEC-1.0 1.2, owner decision 27.10); everything else stays with the parent."""

import json

from fastapi.testclient import TestClient

from app.ingest import autopublish
from app.ingest.store import open_db
from app.model.types import ModelResult
from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    answer_by_number,
    build_app,
    key_pdf,
    make_task,
    reply,
    row,
    task_ids,
    text_pdf,
    upload,
)


def child_ids(c):
    return [t["id"] for t in c.get("/api/tasks", headers=CHILD).json()["tasks"]]


def feed(c):
    return {x["task_id"]: x for x in c.get("/parent/review/queue", headers=PARENT).json()["cards"]}


def start(tmp_path, tasks, answers, keys=None, model=None, key_files=()):
    model = model or StubModel(split=reply(*tasks), answer=answer_by_number(answers))
    c = TestClient(build_app(tmp_path, model, auto_publish=True))
    c.__enter__()
    for pairs in ([keys] if keys else []) + list(key_files):
        upload(c, key_pdf(pairs), kind="key", name="key.pdf")
    upload(c, text_pdf([[f"{t['number']}. вопрос" for t in tasks]]))
    return c, model, task_ids(tmp_path)


def test_keyed_tasks_publish_themselves_and_figures_go_to_the_parent(tmp_path):
    tasks = [
        make_task(1, "Вопрос номер 1"),
        make_task(2, "Рассмотрите рисунок", figure=True),
        make_task(3, "Вопрос номер 3"),
    ]
    answers = {"1": "Б", "2": "Б", "3": "В"}
    c, model, (agree, figure, differ) = start(
        tmp_path, tasks, answers, keys={"1": "Б", "2": "Б", "3": "Б"}
    )
    with c:
        won = row(tmp_path, agree)
        assert won["import_status"] == "checked" and won["auto_published"] == 1
        assert won["key_source"] == "official" and json.loads(won["answer_key"])["value"] == "Б"
        assert agree in child_ids(c)

        assert row(tmp_path, figure)["auto_published"] == 0
        assert row(tmp_path, figure)["import_status"] == "unsupported"
        assert figure not in child_ids(c)
        assert [
            x for x in model.calls_of("task_answer") if "№2\n" in x.messages[-1]["content"]
        ] == []

        assert row(tmp_path, differ)["import_status"] == "draft"
        assert differ not in child_ids(c)
        cards = feed(c)
        assert set(cards) == {figure, differ} and agree not in cards
        assert cards[differ]["kind"] == "discrepancy"
        assert "расходится с ключом" in cards[differ]["hint"]
        assert row(tmp_path, differ)["auto_hold"] == autopublish.HOLD_DISAGREE


class LimitModel(StubModel):
    """The daily model limit is spent: every answer request fails with error_kind=limit."""

    def complete(self, system, messages, schema=None, session_id=None):
        if (schema or {}).get("title") == "task_answer":
            self.calls.append(None)
            return ModelResult.failure("claude", "stub", "limit")
        return super().complete(system, messages, schema, session_id)


def test_model_limit_hit_leaves_the_tasks_in_the_feed(tmp_path):
    tasks = [make_task(1), make_task(2)]
    model = LimitModel(split=reply(*tasks))
    c, model, ids = start(tmp_path, tasks, {}, keys={"1": "Б", "2": "А"}, model=model)
    with c:
        assert child_ids(c) == []
        cards = feed(c)
        assert set(cards) == set(ids)
        assert all(x["kind"] == "confirm" for x in cards.values())
        assert all("лимит" in x["hint"] for x in cards.values())
        assert model.calls.count(None) == 1  # the limit stops further requests in this pass

    # the parent's confirm path still works afterwards
    with TestClient(build_app(tmp_path, model, auto_publish=True)) as c2:
        r = c2.post(f"/parent/tasks/{ids[0]}/confirm", headers=PARENT, json={})
        assert r.status_code == 200 and row(tmp_path, ids[0])["auto_published"] == 0
        assert row(tmp_path, ids[0])["auto_hold"] is None


def test_the_parent_sees_auto_published_tasks(tmp_path):
    c, _m, (tid, other) = start(
        tmp_path,
        [make_task(1), make_task(2)],
        {"1": "Б", "2": "В"},
        keys={"1": "Б", "2": "Б"},
    )
    with c:
        detail = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()
        assert (
            detail["auto_published"] is True and detail["auto_note"] == "опубликовано автоматически"
        )
        listed = c.get("/parent/review/auto", headers=PARENT).json()["tasks"]
        assert [t["task_id"] for t in listed] == [tid]
        assert listed[0]["note"] == "опубликовано автоматически" and listed[0]["key"] == "Б"
        # a parent-confirmed task is not marked
        c.post(f"/parent/tasks/{other}/resolve", headers=PARENT, json={"choice": "key"})
        assert row(tmp_path, other)["auto_published"] == 0
        assert [
            t["task_id"] for t in c.get("/parent/review/auto", headers=PARENT).json()["tasks"]
        ] == [tid]

        # an edit is a new revision: the mark and the publication are withdrawn
        c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"text": "Другое условие"})
        again = row(tmp_path, tid)
        assert again["auto_published"] == 0 and again["import_status"] == "draft"
        assert tid not in child_ids(c)


def test_without_an_official_key_nothing_is_published(tmp_path):
    c, model, (tid,) = start(tmp_path, [make_task(1)], {"1": "Б"})
    with c:
        assert row(tmp_path, tid)["import_status"] == "draft" and child_ids(c) == []
        assert model.calls_of("task_answer") == []  # no key: no model call before the parent

        # a model answer alone is never an official key, even when it is already stored
        conn = open_db(tmp_path / "olymp.db")
        conn.execute(
            "UPDATE task_item SET model_answer = ? WHERE id = ?",
            (json.dumps({"answer": "Б", "value": "Б", "explanation": "", "unverified": True}), tid),
        )
        assert autopublish.publish_pending(c.app.state.ctx, conn) == 0
        conn.close()
        assert row(tmp_path, tid)["import_status"] == "draft" and child_ids(c) == []


def test_ambiguous_key_and_leak_stay_with_the_parent(tmp_path):
    leaky = make_task(2, "Ответ: Б. Какой вариант верен?")
    leaky["answer_text"] = "Б"
    c, _m, (ambiguous, leak) = start(
        tmp_path,
        [make_task(1), leaky],
        {"1": "Б", "2": "Б"},
        keys={"1": "Б", "2": "Б"},
        key_files=[{"1": "В"}],
    )
    with c:
        assert row(tmp_path, ambiguous)["auto_hold"] == autopublish.HOLD_AMBIGUOUS
        assert row(tmp_path, leak)["auto_hold"] == autopublish.HOLD_LEAK
        assert child_ids(c) == []
        assert set(feed(c)) == {ambiguous, leak}


def test_a_task_with_a_figure_is_never_auto_published(tmp_path):
    """Even when the figure is confirmed and a model answer is stored next to the key: the model
    cannot see the picture, so only the parent publishes such a task."""
    c, _m, (tid,) = start(
        tmp_path, [make_task(1, "Рассмотрите рисунок", figure=True)], {"1": "Б"}, keys={"1": "Б"}
    )
    with c:
        conn = open_db(tmp_path / "olymp.db")
        figure = json.loads(row(tmp_path, tid)["figure"])
        figure["confirmed"] = True
        conn.execute(
            "UPDATE task_item SET import_status = 'draft', unsupported_reason = NULL, figure = ?, "
            "model_answer = ? WHERE id = ?",
            (
                json.dumps(figure),
                json.dumps({"answer": "Б", "value": "Б", "explanation": "", "unverified": True}),
                tid,
            ),
        )
        assert autopublish.publish_pending(c.app.state.ctx, conn) == 0
        conn.close()
        assert row(tmp_path, tid)["import_status"] == "draft" and child_ids(c) == []
