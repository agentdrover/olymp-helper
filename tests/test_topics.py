"""T-G1: topic catalog, the model's topic proposal at import, and the parent's review of it."""

import json

from fastapi.testclient import TestClient

from app.db import connect, migrate
from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    answer_by_number,
    build_app,
    make_task,
    reply,
    row,
    task_ids,
    text_pdf,
    upload,
)

IN_CATALOG = "cyt.cell"
OUT_OF_CATALOG = "astro.stars"


def run(tmp_path, topics):
    """Import one task per entry of `topics` (the topic code the stub model proposes)."""
    numbers = list(range(1, len(topics) + 1))
    tasks = [make_task(n, f"Вопрос номер {n}", topic=t) for n, t in zip(numbers, topics)]
    model = StubModel(split=reply(*tasks), answer=answer_by_number({str(n): "Б" for n in numbers}))
    c = TestClient(build_app(tmp_path, model))
    c.__enter__()
    upload(c, text_pdf([[f"{n}. вопрос" for n in numbers]]))
    return c, model, task_ids(tmp_path)


def child_json(c):
    out = [c.get("/api/tasks", headers=CHILD).text, c.get("/tasks", headers=CHILD).text]
    for t in json.loads(out[0])["tasks"]:
        out.append(c.get(f"/api/tasks/{t['id']}", headers=CHILD).text)
        out.append(c.get(f"/tasks/{t['id']}", headers=CHILD).text)
    return out


def test_a_proposed_topic_comes_from_the_catalog_and_waits_for_the_parent(tmp_path):
    c, model, (tid,) = run(tmp_path, [IN_CATALOG])
    with c:
        # the proposal is stored with the draft; the catalog was offered to the model
        assert row(tmp_path, tid)["topic"] == IN_CATALOG
        system = model.calls_of("tasks_split")[0].system
        assert IN_CATALOG in system and "Клетка" in system

        # the parent sees the topic on the review card, with its title and the choices
        card = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()["card"]
        assert card["topic"] == IN_CATALOG and card["topic_title"]
        assert IN_CATALOG in [t["code"] for t in card["topics"]]

        # the child sees nothing yet: not the task, not the topic
        assert c.get("/api/tasks", headers=CHILD).json()["tasks"] == []
        assert c.get(f"/api/tasks/{tid}", headers=CHILD).status_code == 404

        # the parent edits the topic, then confirms the task
        r = c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"topic": "gen.heredity"})
        assert r.status_code == 200, r.text
        assert row(tmp_path, tid)["topic"] == "gen.heredity"
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200

        # published now; the child's views never carry the topic
        pages = child_json(c)
        assert len(json.loads(pages[0])["tasks"]) == 1
        for text in pages:
            assert "topic" not in text and "gen.heredity" not in text and IN_CATALOG not in text


def test_a_topic_outside_the_catalog_is_rejected(tmp_path):
    c, _model, (tid,) = run(tmp_path, [OUT_OF_CATALOG])
    with c:
        assert row(tmp_path, tid)["topic"] is None  # «не размечено»
        card = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()["card"]
        assert card["topic"] is None and card["topic_title"] is None
        # the parent cannot set a code outside the catalog either
        r = c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"topic": OUT_OF_CATALOG})
        assert r.status_code == 422
        assert row(tmp_path, tid)["topic"] is None
        # an empty value clears the topic
        c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"topic": "bot.root"})
        c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"topic": ""})
        assert row(tmp_path, tid)["topic"] is None


def test_a_missing_topic_stays_unmarked(tmp_path):
    c, _model, (tid,) = run(tmp_path, [None])
    with c:
        assert row(tmp_path, tid)["topic"] is None


def test_the_catalog_migration(tmp_path):
    conn = connect(tmp_path / "olymp.db")
    migrate(conn)
    rows = conn.execute("SELECT code, title, parent_code FROM topic").fetchall()
    codes = {r[0] for r in rows}
    sections = {r[0] for r in rows if r[2] is None}
    assert {"bot", "zoo", "hum", "cyt", "gen", "eco", "evo"} <= sections
    assert IN_CATALOG in codes and "gen.heredity" in codes
    assert all(r[1].strip() for r in rows)
    assert all(r[2] is None or r[2] in sections for r in rows)
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO topic (code, title, parent_code) VALUES ('x.y', 'T', 'nope')")
    conn.close()


def test_a_topic_change_with_the_unchanged_text_makes_no_revision(tmp_path):
    c, _model, (tid,) = run(tmp_path, [IN_CATALOG])
    with c:
        c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        before = row(tmp_path, tid)
        assert before["import_status"] == "checked" and before["model_answer"] is not None
        assert len(json.loads(c.get("/api/tasks", headers=CHILD).text)["tasks"]) == 1

        # the payload the review card sends: the text as it is, a new topic
        payload = {"text": before["text"], "manual_condition": False, "topic": "gen.heredity"}
        r = c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json=payload)
        assert r.status_code == 200, r.text
        after = row(tmp_path, tid)
        assert after["topic"] == "gen.heredity"
        assert after["revision"] == before["revision"]
        assert after["import_status"] == "checked"
        assert after["model_answer"] == before["model_answer"]
        assert len(json.loads(c.get("/api/tasks", headers=CHILD).text)["tasks"]) == 1


def test_a_real_text_change_still_makes_a_revision(tmp_path):
    c, _model, (tid,) = run(tmp_path, [IN_CATALOG])
    with c:
        c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        before = row(tmp_path, tid)
        payload = {"text": "Совсем другое условие", "topic": "gen.heredity"}
        assert c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json=payload).status_code == 200
        after = row(tmp_path, tid)
        assert after["revision"] == before["revision"] + 1
        assert after["import_status"] == "draft" and after["model_answer"] is None
        assert c.get("/api/tasks", headers=CHILD).json()["tasks"] == []
