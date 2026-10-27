"""OH-T08: the notice for the child, deletion of data, retention (AC3, AC4)."""

import re
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


def ack(c, next_url, **kw):
    """What the notice page does: open it (token in cookie and form), then confirm."""
    page = c.get("/notice", params={"next": next_url}, headers=CHILD)
    token = re.search(r'name="token" value="([^"]+)"', page.text).group(1)
    return c.post(
        "/notice/ack", data={"next": next_url, "token": token}, follow_redirects=False, **kw
    )


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
        # every child door is shut until then: list, JSON, search, figure
        assert c.get("/tasks", headers=CHILD, follow_redirects=False).status_code == 303
        for url in (
            "/api/tasks",
            f"/api/tasks/{tid}",
            "/api/search?q=a",
            f"/tasks/{tid}/figure.png",
        ):
            r = c.get(url, headers=CHILD)
            assert r.status_code == 403, url
            assert r.json()["detail"]["error"] == "notice_required", url
        # a parent is not the child
        assert c.get(f"/tasks/{tid}", headers=PARENT, follow_redirects=False).status_code == 200
        assert c.get("/api/tasks", headers=PARENT).status_code == 200
        # a confirmation without the page's token, or with a wrong one, records nothing
        c.cookies.clear()
        r = c.post("/notice/ack", data={"next": "/tasks"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/notice")
        c.get("/notice")
        r = c.post(
            "/notice/ack", data={"next": "/tasks", "token": "forged"}, follow_redirects=False
        )
        assert r.headers["location"].startswith("/notice")
        # a form posted from another site is refused even with a valid token
        r = ack(c, "/tasks", headers={"Origin": "https://evil.example"})
        assert r.headers["location"].startswith("/notice")
        r = ack(c, "/tasks", headers={"Referer": "https://evil.example/x"})
        assert r.headers["location"].startswith("/notice")
        assert (
            db(tmp_path).execute("SELECT child_notice_seen_at FROM settings").fetchone()[0] is None
        )
        # confirming from this site records the fact and goes on to the question
        r = ack(c, f"/tasks/{tid}", headers={"Origin": "http://testserver"})
        assert r.status_code == 303 and r.headers["location"] == f"/tasks/{tid}"
        seen = db(tmp_path).execute("SELECT child_notice_seen_at FROM settings").fetchone()[0]
        assert seen and datetime.fromisoformat(seen)
        assert c.get(f"/tasks/{tid}", headers=CHILD).status_code == 200
        assert c.get("/api/tasks", headers=CHILD).status_code == 200
        # the token is used up; a second confirmation keeps the first moment
        ack(c, "/tasks")
        assert (
            db(tmp_path).execute("SELECT child_notice_seen_at FROM settings").fetchone()[0] == seen
        )
        # the redirect target stays on this site
        for evil in ("https://evil.example/", "//evil.example", "/\\evil.example"):
            r = ack(c, evil)
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
        # a draft edited before anyone checked it (revision 2) goes away with the file; one the
        # child has already attempted stays
        for text in ("был подтверждён", "есть попытка"):
            conn.execute(
                "INSERT INTO task_item (material_id, type, text, import_status, revision) "
                "VALUES (?, 'single', ?, 'draft', ?)",
                (mid, text, 2 if "подтв" in text else 1),
            )
        edited, attempted = draft + 1, draft + 2
        conn.execute(
            "INSERT INTO task_revision (task_item_id, revision, key_version, snapshot, "
            "created_at) VALUES (?, 1, 1, '{}', 'x')",
            (edited,),
        )
        sess = conn.execute(
            "INSERT INTO study_session (started_at, last_activity_at) VALUES ('x', 'x')"
        ).lastrowid
        conn.execute(
            "INSERT INTO event (session_id, type, task_item_id, created_at) "
            "VALUES (?, 'attempt', ?, 'x')",
            (sess, attempted),
        )
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
        assert "2 неподтверждённых" in page.text and "2 подтверждённых задания" in page.text
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
            conn.execute(
                "SELECT COUNT(*) FROM task_item WHERE id IN (?, ?)", (draft, edited)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM task_item WHERE id = ?", (attempted,)).fetchone()[0]
            == 1
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


def test_deleting_a_material_drops_a_draft_edited_before_checking(tmp_path):
    model = StubModel(split=reply(make_task(1, "Вопрос")), answer=answer_by_number({1: "А"}))
    pdf = text_pdf([["1. вопрос"]])
    with TestClient(build_app(tmp_path, model)) as c:
        upload(c, pdf)
        tid = task_ids(tmp_path)[0]
        r = c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"text": "Новый текст"})
        assert r.status_code == 200, r.text
        got = (
            db(tmp_path)
            .execute("SELECT revision, import_status FROM task_item WHERE id = ?", (tid,))
            .fetchone()
        )
        assert got["revision"] > 1 and got["import_status"] != "checked"
        mid = db(tmp_path).execute("SELECT id FROM material").fetchone()[0]
        page = c.get(f"/parent/materials/{mid}/delete", headers=PARENT)
        assert "1 неподтверждённое" in page.text and "останется" not in page.text
        r = c.post(
            f"/parent/materials/{mid}/delete",
            headers=PARENT,
            data={"confirm": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert db(tmp_path).execute("SELECT COUNT(*) FROM task_item").fetchone()[0] == 0
        assert db(tmp_path).execute("SELECT COUNT(*) FROM material").fetchone()[0] == 0
        again = upload(c, pdf)
        assert again.status_code == 202 and again.json()["duplicate"] is False


def test_retention_cleanup_keeps_session_summaries(tmp_path):
    build_app(tmp_path, StubModel())
    conn = db(tmp_path)
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=RETENTION_DAYS, seconds=1)).isoformat()
    edge = (now - timedelta(days=RETENTION_DAYS - 1)).isoformat()
    sid = conn.execute(
        "INSERT INTO study_session (started_at, last_activity_at, ended_at, end_reason, summary) "
        "VALUES (?, ?, ?, 'explicit', '{\"v\": 2, \"solved\": 4}')",
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
    assert summary == '{"v": 2, "solved": 4}'
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


def test_start_clears_page_images_cached_under_the_old_names(tmp_path):
    app = build_app(tmp_path, StubModel())
    cache = tmp_path / "cache" / "pages"
    cache.mkdir(parents=True)
    legacy = cache / ("a" * 64 + ".png")
    current = cache / ("b" * 64 + "-" + "c" * 64 + ".png")
    legacy.write_bytes(b"old")
    current.write_bytes(b"new")
    with TestClient(app):
        pass
    assert not legacy.exists() and current.exists()
