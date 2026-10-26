"""OH-T08: the notice for the child, deletion of data, retention (AC3, AC4)."""

import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.privacy import RETENTION_DAYS, cleanup_retention
from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    answer_by_number,
    build_app,
    db,
    make_task,
    reply,
    task_ids,
    text_pdf,
    upload,
)


def published(tmp_path, **kw):
    model = StubModel(
        split=reply(make_task(1, "Вопрос номер 1")), answer=answer_by_number({1: "А"})
    )
    app = build_app(tmp_path, model, **kw)
    c = TestClient(app)
    c.__enter__()
    upload(c, text_pdf([["1. вопрос"]]))
    tid = task_ids(tmp_path)[0]
    r = c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={"answer": "А"})
    assert r.status_code == 200, r.text
    return c, tid


def test_ac3(tmp_path):
    c, tid = published(tmp_path, notice=False)
    with c:
        # before the first question the child gets the notice, not the question
        r = c.get(f"/tasks/{tid}", headers=CHILD, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/notice")
        page = c.get(r.headers["location"], headers=CHILD)
        assert page.status_code == 200
        assert "внешней модели" in page.text and "Голос распознаётся здесь" in page.text
        assert (
            db(tmp_path).execute("SELECT child_notice_seen_at FROM settings").fetchone()[0] is None
        )
        # the list of tasks is not a question: no notice there
        assert c.get("/tasks", headers=CHILD).status_code == 200
        # a parent opening the same page is not the child
        assert c.get(f"/tasks/{tid}", headers=PARENT, follow_redirects=False).status_code == 200
        # confirming records the fact and goes on to the question
        ack = c.post("/notice/ack", data={"next": f"/tasks/{tid}"}, follow_redirects=False)
        assert ack.status_code == 303 and ack.headers["location"] == f"/tasks/{tid}"
        seen = db(tmp_path).execute("SELECT child_notice_seen_at FROM settings").fetchone()[0]
        assert seen and datetime.fromisoformat(seen)
        assert c.get(f"/tasks/{tid}", headers=CHILD).status_code == 200
        # it is shown once: a second confirmation keeps the first moment
        c.post("/notice/ack", data={"next": "/tasks"})
        assert (
            db(tmp_path).execute("SELECT child_notice_seen_at FROM settings").fetchone()[0] == seen
        )
        # the redirect target stays on this site
        for evil in ("https://evil.example/", "//evil.example", "/\\evil.example"):
            r = c.post("/notice/ack", data={"next": evil}, follow_redirects=False)
            assert r.headers["location"] == "/tasks", evil


def test_ac4(tmp_path):
    c, tid = published(tmp_path)
    with c:
        conn = db(tmp_path)
        mid = conn.execute("SELECT material_id FROM task_item WHERE id = ?", (tid,)).fetchone()[0]
        # one more task of the same material that nobody has confirmed
        conn.execute(
            "INSERT INTO task_item (material_id, type, text, import_status) "
            "VALUES (?, 'single', 'черновик', 'draft')",
            (mid,),
        )
        draft = conn.execute("SELECT MAX(id) FROM task_item").fetchone()[0]
        conn.execute(
            "INSERT INTO material_page_text (material_id, page, text) VALUES (?, 1, 'x')", (mid,)
        ) if False else None
        file = conn.execute("SELECT file FROM material WHERE id = ?", (mid,)).fetchone()[0]
        pdf = tmp_path / file
        assert pdf.exists()
        pages_before = conn.execute(
            "SELECT COUNT(*) FROM material_page WHERE material_id = ?", (mid,)
        ).fetchone()[0]

        page = c.get(f"/parent/materials/{mid}/delete", headers=PARENT)
        assert page.status_code == 200
        assert "1 неподтверждённое" in page.text and "1 подтверждённое" in page.text
        assert pdf.exists()  # looking deletes nothing
        r = c.post(f"/parent/materials/{mid}/delete", headers=PARENT, data={})
        assert r.status_code == 200 and pdf.exists()
        # no CSRF token: refused
        no_token = {"Cookie": PARENT["Cookie"]}
        assert (
            c.post(
                f"/parent/materials/{mid}/delete", headers=no_token, data={"confirm": "1"}
            ).status_code
            == 403
        )
        assert pdf.exists()

        r = c.post(
            f"/parent/materials/{mid}/delete",
            headers=PARENT,
            data={"confirm": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert not pdf.exists()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM material_page WHERE material_id = ?", (mid,)
            ).fetchone()[0]
            == 0
            < pages_before + 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM task_item WHERE id = ?", (draft,)).fetchone()[0] == 0
        )
        # the confirmed task, which the child may have worked on, stays; the file is gone for good
        assert (
            conn.execute("SELECT import_status FROM task_item WHERE id = ?", (tid,)).fetchone()[0]
            == "checked"
        )
        assert c.get(f"/materials/{mid}/pdf", headers=PARENT).status_code == 404
        assert c.get(f"/materials/{mid}/pages/1.png", headers=PARENT).status_code == 404
        assert c.get(f"/tasks/{tid}", headers=CHILD).status_code == 200
        assert (
            c.post(
                "/parent/materials/9999/delete", headers=PARENT, data={"confirm": "1"}
            ).status_code
            == 404
        )


def test_deleting_a_material_without_confirmed_tasks_removes_its_row(tmp_path):
    model = StubModel(split=reply(make_task(1, "Вопрос")), answer=answer_by_number({1: "А"}))
    with TestClient(build_app(tmp_path, model)) as c:
        upload(c, text_pdf([["1. вопрос"]]))
        mid = db(tmp_path).execute("SELECT id FROM material").fetchone()[0]
        r = c.post(
            f"/parent/materials/{mid}/delete",
            headers=PARENT,
            data={"confirm": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert db(tmp_path).execute("SELECT COUNT(*) FROM material").fetchone()[0] == 0
        assert db(tmp_path).execute("SELECT COUNT(*) FROM task_item").fetchone()[0] == 0
        # the same file can be uploaded again afterwards
        again = upload(c, text_pdf([["1. вопрос"]]))
        assert again.status_code == 202 and again.json()["duplicate"] is False


def test_retention_cleanup_keeps_session_summaries(tmp_path):
    build_app(tmp_path, StubModel())
    conn = db(tmp_path)
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=RETENTION_DAYS, seconds=1)).isoformat()
    edge = (now - timedelta(days=RETENTION_DAYS - 1)).isoformat()
    sid = conn.execute(
        "INSERT INTO study_session (started_at, last_activity_at, ended_at, end_reason, summary) "
        "VALUES (?, ?, ?, 'explicit', '{\"solved\": 4}')",
        (old, old, old),
    ).lastrowid
    for type_, at in [
        ("chat", old),
        ("voice", old),
        ("chat", edge),
        ("attempt", old),
        ("hint", old),
        ("shown_to_parent", old),
        ("parent_opened_chat", old),
    ]:
        conn.execute(
            "INSERT INTO event (session_id, type, data, created_at) VALUES (?, ?, NULL, ?)",
            (sid, type_, at),
        )
    removed = cleanup_retention(conn, now)
    assert removed == 2
    kinds = sorted(r[0] for r in conn.execute("SELECT type FROM event"))
    assert kinds == sorted(["chat", "attempt", "hint", "shown_to_parent", "parent_opened_chat"])
    summary = conn.execute("SELECT summary FROM study_session WHERE id = ?", (sid,)).fetchone()[0]
    assert summary == '{"solved": 4}'
    assert cleanup_retention(conn, now) == 0
    conn.close()


def test_retention_runs_when_the_app_starts(tmp_path):
    app = build_app(tmp_path, StubModel())
    conn = db(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=RETENTION_DAYS + 5)).isoformat()
    sid = conn.execute(
        "INSERT INTO study_session (started_at, last_activity_at) VALUES (?, ?)", (old, old)
    ).lastrowid
    conn.execute(
        "INSERT INTO event (session_id, type, data, created_at) VALUES (?, 'chat', NULL, ?)",
        (sid, old),
    )
    conn.close()
    with TestClient(app):
        pass
    assert db(tmp_path).execute("SELECT COUNT(*) FROM event").fetchone()[0] == 0
    assert (
        sqlite3.connect(tmp_path / "olymp.db")
        .execute("SELECT COUNT(*) FROM study_session")
        .fetchone()[0]
        == 1
    )
