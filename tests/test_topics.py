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

IN_CATALOG = "b10.cell"
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
        r = c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"topic": "b14.heredity"})
        assert r.status_code == 200, r.text
        assert row(tmp_path, tid)["topic"] == "b14.heredity"
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200

        # published now; the child's JSON never carries the topic code (the list page shows
        # only the catalog title — owner decision 26.10, canvas screen 3)
        pages = child_json(c)
        assert len(json.loads(pages[0])["tasks"]) == 1
        for text in pages:
            assert "topic" not in text and "b14.heredity" not in text and IN_CATALOG not in text


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
        c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"topic": "b05.morph"})
        c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"topic": ""})
        assert row(tmp_path, tid)["topic"] is None


def test_a_missing_topic_stays_unmarked(tmp_path):
    c, _model, (tid,) = run(tmp_path, [None])
    with c:
        assert row(tmp_path, tid)["topic"] is None


OFFICIAL_BLOCKS = [
    ("Биология как наука. Методы научного познания", "5, 6", "7"),
    ("Признаки живых организмов", "5, 6", "7"),
    ("Царство бактерий", "6", "7"),
    ("Царство грибов", "6", "7"),
    ("Царство растений", "7", "7"),
    ("Царство животных", "8", "8"),
    ("Система органического мира", "9", "9"),
    ("Человек", "9", "9"),
    ("Организм и окружающая среда. Экология", "10", "10"),
    ("Цитология", "10", "10"),
    ("Микробиология и биотехнология", "10", "10"),
    ("Многообразие и эволюция живой природы", "11", "11"),
    ("Биология клетки. Биохимия", "11", "11"),
    ("Молекулярная биология. Генетика", "11", "11"),
]


def test_the_catalog_migration(tmp_path):
    conn = connect(tmp_path / "olymp.db")
    migrate(conn)
    rows = conn.execute("SELECT code, title, parent_code FROM topic").fetchall()
    codes = {r[0] for r in rows}
    sections = {r[0] for r in rows if r[2] is None}
    assert IN_CATALOG in codes and "b14.heredity" in codes
    assert all(r[1].strip() for r in rows)
    assert all(r[2] is None or r[2] in sections for r in rows)
    # exactly the 14 official content blocks, in order, with both grade columns
    blocks = conn.execute(
        "SELECT title, school_grade, municipal_grade FROM topic "
        "WHERE parent_code IS NULL ORDER BY position"
    ).fetchall()
    assert [tuple(b) for b in blocks] == OFFICIAL_BLOCKS
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
        payload = {"text": before["text"], "manual_condition": False, "topic": "b14.heredity"}
        r = c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json=payload)
        assert r.status_code == 200, r.text
        after = row(tmp_path, tid)
        assert after["topic"] == "b14.heredity"
        assert after["revision"] == before["revision"]
        assert after["import_status"] == "checked"
        assert after["model_answer"] == before["model_answer"]
        assert len(json.loads(c.get("/api/tasks", headers=CHILD).text)["tasks"]) == 1


def test_a_real_text_change_still_makes_a_revision(tmp_path):
    c, _model, (tid,) = run(tmp_path, [IN_CATALOG])
    with c:
        c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        before = row(tmp_path, tid)
        payload = {"text": "Совсем другое условие", "topic": "b14.heredity"}
        assert c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json=payload).status_code == 200
        after = row(tmp_path, tid)
        assert after["revision"] == before["revision"] + 1
        assert after["import_status"] == "draft" and after["model_answer"] is None
        assert c.get("/api/tasks", headers=CHILD).json()["tasks"] == []


def test_the_ui_payload_with_manual_condition_and_a_new_topic_makes_no_revision(tmp_path):
    c, _model, (tid,) = run(tmp_path, [IN_CATALOG])
    with c:
        c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        before = row(tmp_path, tid)
        assert before["figure"] is None and before["unsupported_reason"] is None
        payload = {"text": before["text"], "manual_condition": True, "topic": "b14.heredity"}
        r = c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json=payload)
        assert r.status_code == 200, r.text
        after = row(tmp_path, tid)
        assert after["topic"] == "b14.heredity"
        assert after["revision"] == before["revision"]
        assert after["import_status"] == "checked" and after["model_answer"] is not None
        assert len(json.loads(c.get("/api/tasks", headers=CHILD).text)["tasks"]) == 1


def test_an_old_free_text_topic_is_cleared_by_the_migration(tmp_path):
    """v8 databases hold the model's free text in task_item.topic; after 0009 it is NULL."""
    from app.db import _migration_files, _statements

    conn = connect(tmp_path / "olymp.db")
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for version, path in _migration_files()[:8]:
        for stmt in _statements(path.read_text(encoding="utf-8")):
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    conn.execute(
        "INSERT INTO material(file, sha256, kind, import_status) "
        "VALUES ('a.pdf', 'h1', 'tasks', 'done')"
    )
    conn.execute("PRAGMA user_version = 8")
    for n, topic in ((1, "cyt.cell"), (2, "клетка и ткани"), (3, None)):
        conn.execute(
            "INSERT INTO task_item(material_id, page_from, page_to, source_number, type, text, "
            "options, points, topic, import_status) VALUES "
            "(1, 1, 1, ?, 'single', 'Вопрос', '[\"а\",\"б\"]', 1, ?, 'draft')",
            (str(n), topic),
        )
    conn.close()

    c = TestClient(build_app(tmp_path, StubModel()))
    with c:
        ids = task_ids(tmp_path)
        assert [row(tmp_path, i)["topic"] for i in ids] == [None, None, None]
        # the card edit with the topic as shown (unmarked) is accepted
        r = c.post(
            f"/parent/tasks/{ids[0]}/edit", headers=PARENT, json={"text": "Вопрос", "topic": ""}
        )
        assert r.status_code == 200, r.text
