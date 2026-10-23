import json

from fastapi.testclient import TestClient

from app.db import connect, migrate
from app.ingest.attempts import attempt_ref, describe_attempt
from app.sessions import SessionService
from tests.bank_helpers import (
    CHILD,
    PARENT,
    TEST_PARENT_KEY,
    StubModel,
    answer_by_number,
    build_app,
    db,
    key_pdf,
    make_task,
    parent_cookie,
    reply,
    row,
    task_ids,
    text_pdf,
    upload,
)


def child_ids(c):
    return [t["id"] for t in c.get("/api/tasks", headers=CHILD).json()["tasks"]]


def start(tmp_path, numbers, answers, keys=None, variant="1"):
    tasks = [make_task(n, f"Вопрос номер {n}") for n in numbers]
    model = StubModel(split=reply(*tasks), answer=answer_by_number(answers))
    app = build_app(tmp_path, model)
    c = TestClient(app)
    c.__enter__()
    if keys:
        upload(c, key_pdf(keys), kind="key", variant=variant, name="key.pdf")
    upload(c, text_pdf([[f"{n}. вопрос" for n in numbers]]), variant=variant)
    return c, model, task_ids(tmp_path)


def post(c, url, **body):
    r = c.post(url, headers=PARENT, json=body)
    assert r.status_code == 200, (url, r.text)
    return r.json()


def attempt(tmp_path, tid, answer, correct):
    """What T06 will do when the child answers: an attempt event that refers to a revision."""
    conn = connect(tmp_path / "olymp.db")
    migrate(conn)
    svc = SessionService(conn)
    sid = svc.open_session()
    eid = svc.record_event(
        sid, "attempt", tid, {**attempt_ref(conn, tid), "answer": answer, "correct": correct}
    )
    event = next(e for e in svc.list_events(sid) if e["id"] == eid)
    conn.close()
    return sid, event


def test_ac15(tmp_path):
    answers = {"1": "Б", "2": "В", "3": "А"}
    c, _model, (a, b, third) = start(tmp_path, [1, 2, 3], answers, keys={"1": "Б"})
    with c:
        # B: the parent writes the answer in beforehand; C: types it in the confirm action.
        post(c, f"/parent/tasks/{b}/key", answer="В")
        q = c.get("/parent/review/queue", headers=PARENT).json()
        assert q["total"] == 3 and [x["task_id"] for x in q["cards"]] == [a, b, third]
        assert q["cards"][0]["kind"] == "confirm" and q["cards"][0]["model_answer"] is None
        assert _model.calls_of("task_answer") == []  # opening the feed never runs the model
        assert q["cards"][0]["button"] == "Верно, ответ Б — подтвердить и дальше"

        first = post(c, f"/parent/tasks/{a}/confirm")
        assert first["counter"] == "2 из 3" and first["next"]["task_id"] == b
        second = post(c, f"/parent/tasks/{b}/confirm")
        assert second["counter"] == "3 из 3" and second["next"]["task_id"] == third
        last = post(c, f"/parent/tasks/{third}/confirm", answer="А")
        assert last["next"] is None

    ra, rb, rc = row(tmp_path, a), row(tmp_path, b), row(tmp_path, third)
    assert ra["import_status"] == rb["import_status"] == rc["import_status"] == "checked"
    assert ra["key_source"] == "official" and json.loads(ra["answer_key"])["value"] == "Б"
    assert rb["key_source"] == "parent" and json.loads(rb["answer_key"])["value"] == "В"
    assert rc["key_source"] == "parent" and json.loads(rc["answer_key"])["value"] == "А"


def test_ac16(tmp_path):
    answers = {"1": "А", "2": "Б", "3": None}
    c, _model, ids = start(tmp_path, [1, 2, 3], answers)
    with c:
        for tid in ids:
            post(c, f"/parent/tasks/{tid}/confirm")
        rows = [row(tmp_path, t) for t in ids]
        assert [
            json.loads(r["model_answer"])["answer"] if r["model_answer"] else None for r in rows
        ] == ["А", "Б", None]
        card = c.get(f"/parent/tasks/{ids[2]}", headers=PARENT).json()["card"]
        assert card["kind"] == "model_missing"
        assert card["label"] == "ответ модели не получен — повторить" and card["retry"] is True
        # no key and no model_answer: not published
        assert child_ids(c) == ids[:2]

        # the retry works once the model answers
        c.app.state.model.answer = answer_by_number({"3": "В"})
        post(c, f"/parent/tasks/{ids[2]}/retry-model")
        assert json.loads(row(tmp_path, ids[2])["model_answer"])["answer"] == "В"
        assert child_ids(c) == ids


def test_ac17(tmp_path):
    c, model, (tid,) = start(tmp_path, [1], {"1": "Б"})
    with c:
        post(c, f"/parent/tasks/{tid}/confirm")
        assert child_ids(c) == [tid]
        _sid, old = attempt(tmp_path, tid, "А", False)
        old_text = row(tmp_path, tid)["text"]
        assert old["data"]["revision"] == 1
        answer_calls = len(model.calls_of("task_answer"))

        r = post(c, f"/parent/tasks/{tid}/edit", text="Исправленное условие номер 1")
        assert r["revision"] == 2
        got = row(tmp_path, tid)
        assert got["revision"] == 2 and got["import_status"] == "draft"
        assert got["model_answer"] is None and child_ids(c) == []

        post(c, f"/parent/tasks/{tid}/confirm")
        got = row(tmp_path, tid)
        assert got["import_status"] == "checked" and got["model_answer"] is not None
        assert len(model.calls_of("task_answer")) > answer_calls  # recomputed
        assert "Исправленное" in model.calls_of("task_answer")[-1].messages[-1]["content"]

        # the old attempt still points at revision 1 and shows the text it was made on
        conn = db(tmp_path)
        info = describe_attempt(conn, old)
        assert info["revision"] == 1 and info["text"] == old_text
        assert info["key_corrected_after_attempt"] is False


def test_ac18(tmp_path):
    c, _model, (tid,) = start(tmp_path, [1], {"1": "Б"}, keys={"1": "Б"})
    with c:
        post(c, f"/parent/tasks/{tid}/confirm")
        _, old = attempt(tmp_path, tid, "Б", True)
        before = row(tmp_path, tid)["key_version"]
        r = post(c, f"/parent/tasks/{tid}/key", answer="Г")
        assert r["revision"] == 2 and r["key_version"] == before + 1
        got = row(tmp_path, tid)
        assert json.loads(got["answer_key"])["value"] == "Г" and got["key_source"] == "parent"
        assert got["import_status"] == "draft"  # edit unchecks until re-confirmed

        conn = db(tmp_path)
        info = describe_attempt(conn, old)
        assert info["graded_by_key"] == "Б" and info["revision"] == 1
        assert info["key_corrected_after_attempt"] is True
        assert info["note"] == "ключ исправлен после попытки"

        post(c, f"/parent/tasks/{tid}/confirm")
        _, fresh = attempt(tmp_path, tid, "Г", True)
        assert fresh["data"]["revision"] == 2 and fresh["data"]["key_version"] == before + 1
        new_info = describe_attempt(db(tmp_path), fresh)
        assert new_info["graded_by_key"] == "Г" and new_info["key_corrected_after_attempt"] is False


def test_ac19(tmp_path):
    answers = {"1": "В", "2": "Г", "3": "Г"}
    c, _model, (no_key, keep, mine) = start(tmp_path, [1, 2, 3], answers, keys={"2": "Б", "3": "Б"})
    with c:
        # No key: the parent writes the answer; the source is "parent".
        card = c.get(f"/parent/tasks/{no_key}", headers=PARENT).json()["card"]
        assert card["kind"] == "no_key" and card["field"] == "answer"
        post(c, f"/parent/tasks/{no_key}/confirm", answer="В")
        got = row(tmp_path, no_key)
        assert got["key_source"] == "parent" and json.loads(got["answer_key"])["value"] == "В"

        # Discrepancy: key Б against the model's Г. The model runs only after the condition is
        # confirmed, so the discrepancy card exists from then on.
        for tid in (keep, mine):
            card = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()["card"]
            assert card["kind"] == "confirm" and card["model_answer"] is None
            post(c, f"/parent/tasks/{tid}/confirm")
            card = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()["card"]
            assert card["kind"] == "discrepancy"
            assert card["key"] == "Б" and card["model_answer"] == "Г"
        post(c, f"/parent/tasks/{keep}/resolve", choice="key")
        post(c, f"/parent/tasks/{mine}/resolve", choice="mine", answer="Г")
        kept, replaced = row(tmp_path, keep), row(tmp_path, mine)
        assert json.loads(kept["answer_key"])["value"] == "Б" and kept["key_source"] == "official"
        assert json.loads(replaced["answer_key"])["value"] == "Г"
        assert replaced["key_source"] == "parent"
        for tid in (keep, mine):
            assert row(tmp_path, tid)["import_status"] == "checked"
            d = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()
            assert d["card"] is None  # the discrepancy is closed
        queue = c.get("/parent/review/queue", headers=PARENT).json()
        assert queue["cards"] == []


def test_ac20(tmp_path):
    c, _model, (tid,) = start(tmp_path, [1], {"1": "Б"})
    with c:
        post(c, f"/parent/tasks/{tid}/confirm")
        assert c.get("/parent/review/queue", headers=PARENT).json()["cards"] == []

        conn = connect(tmp_path / "olymp.db")
        svc = SessionService(conn)
        sid = svc.open_session()
        svc.record_event(sid, "disputed_key", tid, {"note": "по-моему, ключ неверный"})
        conn.close()

        cards = c.get("/parent/review/queue", headers=PARENT).json()["cards"]
        assert [x["task_id"] for x in cards] == [tid]
        assert cards[0]["kind"] == "child_dispute" and cards[0]["label"] == "от ребёнка"

        post(c, f"/parent/tasks/{tid}/confirm")  # "key is right"
        assert c.get("/parent/review/queue", headers=PARENT).json()["cards"] == []


def test_feed_page_binds_the_review_keys(tmp_path):
    c, _model, _ids = start(tmp_path, [1], {"1": "Б"})
    with c:
        page = c.get("/parent/review", headers=PARENT)
        assert page.status_code == 200
        js = c.get("/static/feed.js").text
        for needle in ('"Enter"', '"ArrowRight"', '"KeyE"'):
            assert needle in js
        assert c.get("/parent/review", headers=CHILD).status_code == 403


def test_an_excluded_task_leaves_the_queue_and_the_child_list(tmp_path):
    c, _model, (a, b) = start(tmp_path, [1, 2], {"1": "Б", "2": "В"})
    with c:
        post(c, f"/parent/tasks/{a}/confirm")
        assert child_ids(c) == [a]
        r = post(c, f"/parent/tasks/{a}/exclude")
        assert child_ids(c) == []
        assert r["next"]["task_id"] == b
        ids = [x["task_id"] for x in c.get("/parent/review/queue", headers=PARENT).json()["cards"]]
        assert ids == [b]
        assert c.post(f"/parent/tasks/{a}/confirm", headers=PARENT, json={}).status_code == 422


def test_changing_only_the_key_source_makes_a_revision(tmp_path):
    c, _model, (tid,) = start(tmp_path, [1], {"1": "Б"}, keys={"1": "Б"})
    with c:
        post(c, f"/parent/tasks/{tid}/confirm")
        got = row(tmp_path, tid)
        assert got["key_source"] == "official"
        r = post(c, f"/parent/tasks/{tid}/key", answer="Б")  # same answer, now the parent's own
        got = row(tmp_path, tid)
        assert got["key_source"] == "parent" and got["import_status"] == "draft"
        assert r["revision"] == 2 and r["key_version"] == got["key_version"]


def test_get_never_runs_the_model_and_confirm_runs_it_once(tmp_path):
    c, model, (tid,) = start(tmp_path, [1], {"1": "Б"})
    with c:
        for url in (
            "/parent/review",
            "/parent/review/queue",
            f"/parent/review/{tid}",
            f"/parent/review/{tid}?fragment=1",
            f"/parent/tasks/{tid}",
        ):
            assert c.get(url, headers=PARENT).status_code == 200, url
        assert model.calls_of("task_answer") == []
        assert row(tmp_path, tid)["model_answer"] is None
        post(c, f"/parent/tasks/{tid}/confirm")
        assert len(model.calls_of("task_answer")) == 1
        # a failing model is not retried by reloading pages
        c.app.state.model.answer = None
        before = len(model.calls_of("task_answer"))
        for _ in range(3):
            c.get("/parent/review", headers=PARENT)
            c.get(f"/parent/tasks/{tid}", headers=PARENT)
        assert len(model.calls_of("task_answer")) == before


def test_retry_cannot_replace_a_model_answer_and_revisions_keep_the_old_one(tmp_path):
    c, _model, (tid,) = start(tmp_path, [1], {"1": "Б"})
    with c:
        post(c, f"/parent/tasks/{tid}/confirm")
        before = row(tmp_path, tid)
        assert json.loads(before["model_answer"])["answer"] == "Б"
        c.app.state.model.answer = answer_by_number({"1": "Г"})
        r = c.post(f"/parent/tasks/{tid}/retry-model", headers=PARENT)
        assert r.status_code == 409
        after = row(tmp_path, tid)
        assert after["model_answer"] == before["model_answer"] and after["revision"] == 1
        assert after["import_status"] == "checked"

        # a new revision keeps the old model answer in its snapshot
        post(c, f"/parent/tasks/{tid}/edit", text="Другое условие")
        conn = db(tmp_path)
        snap = conn.execute(
            "SELECT snapshot FROM task_revision WHERE task_item_id = ? AND revision = 1", (tid,)
        ).fetchone()[0]
        assert json.loads(snap)["model_answer"]["answer"] == "Б"
        assert row(tmp_path, tid)["model_answer"] is None
        post(c, f"/parent/tasks/{tid}/confirm")
        assert json.loads(row(tmp_path, tid)["model_answer"])["answer"] == "Г"
        snap = conn.execute(
            "SELECT snapshot FROM task_revision WHERE task_item_id = ? AND revision = 2", (tid,)
        ).fetchone()[0]
        assert json.loads(snap)["model_answer"]["answer"] == "Г"


def test_a_forged_role_cookie_gets_nothing(tmp_path, monkeypatch, caplog):
    url = "/parent/materials"
    app = build_app(tmp_path, StubModel())
    with TestClient(app) as c:
        assert c.get(url, headers={"Cookie": "olymp_role=parent"}).status_code == 403
        assert c.get(url, headers={"Cookie": "olymp_parent=" + "0" * 64}).status_code == 403
        assert c.get(url, headers={"Cookie": "olymp_parent=parent"}).status_code == 403
        other = parent_cookie("another-key")
        assert c.get(url, headers={"Cookie": "olymp_parent=" + other}).status_code == 403
        assert c.get(url, headers=PARENT).status_code == 200
        assert c.get("/api/tasks").status_code == 200  # the child's side needs no key

        # the login form: wrong key refused; right key sets an HttpOnly, SameSite=Strict cookie
        assert c.get("/parent/login").status_code == 200
        assert c.post("/parent/login", data={"key": "wrong"}).status_code == 403
        ok = c.post("/parent/login", data={"key": TEST_PARENT_KEY}, follow_redirects=False)
        assert ok.status_code == 303
        cookie = ok.headers["set-cookie"]
        assert "olymp_parent=" + parent_cookie(TEST_PARENT_KEY) in cookie
        assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
        assert c.get(url).status_code == 200  # the browser now carries the cookie
        c.cookies.clear()

    # No key configured: nobody is a parent, every parent route is refused (fail closed).
    monkeypatch.delenv("OLYMP_PARENT_KEY")
    routes = [
        (m.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if path != "/parent/login" and path.startswith(("/parent", "/materials"))
        for m in item
    ]
    assert len(routes) >= 15
    with TestClient(app) as c, caplog.at_level("WARNING"):
        for method, path in routes:
            concrete = path.replace("{task_id}", "1").replace("{material_id}", "1")
            concrete = concrete.replace("{page}", "1")
            r = c.request(method, concrete, headers=PARENT, json={} if method == "POST" else None)
            assert r.status_code == 403, (method, path, r.status_code)
        assert c.post("/parent/login", data={"key": TEST_PARENT_KEY}).status_code == 403
    assert "OLYMP_PARENT_KEY" in caplog.text


def test_model_answer_and_its_snapshot_are_written_in_one_transaction(tmp_path):
    import sqlite3

    from app.ingest import model_answer
    from app.ingest.store import open_db

    c, _model, (tid,) = start(tmp_path, [1], {"1": "Б"})
    with c:
        conn = open_db(tmp_path / "olymp.db")
        conn.execute("UPDATE task_item SET import_status = 'checked' WHERE id = ?", (tid,))
        real = model_answer.save_snapshot
        seen = {}

        def hook(conn_, task_id):
            # Between the UPDATE and the snapshot nobody else may write (no revision can slip in).
            other = open_db(tmp_path / "olymp.db")
            other.execute("PRAGMA busy_timeout = 100")
            try:
                other.execute(
                    "UPDATE task_item SET revision = revision + 1 WHERE id = ?", (task_id,)
                )
                seen["raced"] = True
            except sqlite3.OperationalError:
                seen["raced"] = False
            finally:
                other.close()
            real(conn_, task_id)

        model_answer.save_snapshot = hook
        try:
            assert model_answer.compute_model_answer(c.app.state.ctx, conn, tid) is True
        finally:
            model_answer.save_snapshot = real
        assert seen["raced"] is False
        snap = conn.execute(
            "SELECT snapshot FROM task_revision WHERE task_item_id = ? AND revision = 1", (tid,)
        ).fetchone()[0]
        assert json.loads(snap)["model_answer"]["answer"] == "Б"
        assert row(tmp_path, tid)["model_answer"] is not None
        conn.close()


def test_retry_needs_the_condition_confirmed_for_the_current_revision(tmp_path):
    c, _model, (tid,) = start(tmp_path, [1], {"1": None})  # the model never answers
    with c:
        r = c.post(f"/parent/tasks/{tid}/retry-model", headers=PARENT)
        assert r.status_code == 409  # draft: condition not confirmed
        assert row(tmp_path, tid)["model_answer"] is None

        post(c, f"/parent/tasks/{tid}/confirm")  # checked, model_answer missing
        c.app.state.model.answer = answer_by_number({"1": "Б"})
        post(c, f"/parent/tasks/{tid}/edit", text="Другое условие")  # unchecks
        r = c.post(f"/parent/tasks/{tid}/retry-model", headers=PARENT)
        assert r.status_code == 409 and row(tmp_path, tid)["model_answer"] is None

        c.app.state.model.answer = None
        post(c, f"/parent/tasks/{tid}/confirm")  # confirmed again; model still silent
        c.app.state.model.answer = answer_by_number({"1": "Б"})
        r = c.post(f"/parent/tasks/{tid}/retry-model", headers=PARENT)
        assert r.status_code == 200 and row(tmp_path, tid)["model_answer"] is not None


def test_confirm_returns_the_card_that_still_needs_the_parent(tmp_path):
    # a single task whose model stays silent
    c, _model, (only,) = start(tmp_path, [1], {"1": None})
    with c:
        c.get("/parent/review/queue", headers=PARENT)
        r = post(c, f"/parent/tasks/{only}/confirm")
        assert r["next"] and r["next"]["task_id"] == only
        assert r["next"]["kind"] == "model_missing" and r["counter"] == "1 из 1"

    # several: a discrepancy on the second task keeps the feed on it
    tmp2 = tmp_path / "second"
    c, _model, (a, b, third) = start(
        tmp2, [1, 2, 3], {"1": "А", "2": "Г", "3": None}, keys={"2": "Б"}
    )
    with c:
        c.get("/parent/review/queue", headers=PARENT)
        r = post(c, f"/parent/tasks/{a}/confirm", answer="А")
        assert r["next"]["task_id"] == b and r["counter"] == "2 из 3"
        r = post(c, f"/parent/tasks/{b}/confirm")  # key Б, model Г
        assert r["next"]["task_id"] == b and r["next"]["kind"] == "discrepancy"
        assert r["counter"] == "2 из 3"
        r = post(c, f"/parent/tasks/{b}/resolve", choice="key")
        assert r["next"]["task_id"] == third
        r = post(c, f"/parent/tasks/{third}/confirm")  # model silent for the third
        assert r["next"]["task_id"] == third and r["next"]["kind"] == "model_missing"
        # skipping a card does move on
        r = post(c, f"/parent/tasks/{third}/skip")
        assert r["next"] is None or r["next"]["task_id"] != third


def test_resolve_keeps_the_task_on_screen_when_the_new_model_answer_reopens_the_conflict(tmp_path):
    c, _model, (tid,) = start(tmp_path, [1], {"1": "Г"}, keys={"1": "Б"})
    with c:
        c.get("/parent/review/queue", headers=PARENT)
        r = post(c, f"/parent/tasks/{tid}/confirm")
        assert r["next"]["kind"] == "discrepancy"  # key Б, model Г
        c.app.state.model.answer = answer_by_number({"1": "Д"})  # the model changes its mind
        r = post(c, f"/parent/tasks/{tid}/resolve", choice="mine", answer="А")
        assert r["next"]["task_id"] == tid and r["next"]["kind"] == "discrepancy"
        assert r["next"]["key"] == "А" and r["next"]["model_answer"] == "Д"


def test_a_late_failure_of_an_old_run_does_not_touch_the_current_run(tmp_path):
    from app.db import connect, migrate
    from app.ingest.importer import fail_material

    conn = connect(tmp_path / "olymp.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO material (file, sha256, kind, import_status, run_id) "
        "VALUES ('f', 'h', 'tasks', 'running', 'run-b')"
    )
    fail_material(conn, 1, "run-a", "поздний сбой старого запуска")
    assert conn.execute("SELECT import_status, import_error FROM material").fetchone() == (
        "running",
        None,
    )
    fail_material(conn, 1, "run-b", "сбой текущего")
    assert conn.execute("SELECT import_status FROM material").fetchone()[0] == "failed"
