"""K5b: tasks of an imported collection with an OFFICIAL key publish themselves when the
automatic gates pass (SPEC-1.0 1.2, owner decision 27.10); everything else stays with the parent."""

import json
import sqlite3
import threading
import time

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

# The shared fixture options ("вариант б") read as an answer label to the child-content check.
OPTS = [
    {"label": c, "text": t} for c, t in zip("АБВГ", ["ядро", "рибосома", "вакуоль", "мембрана"])
]


def mk(number, text="Что изучает цитология?", **kw):
    kw.setdefault("options", OPTS)
    return make_task(number, text, **kw)


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
        mk(1, "Вопрос номер 1"),
        mk(2, "Рассмотрите рисунок", figure=True),
        mk(3, "Вопрос номер 3"),
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
    tasks = [mk(1), mk(2)]
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
        [mk(1), mk(2)],
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
    c, model, (tid,) = start(tmp_path, [mk(1)], {"1": "Б"})
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
    leaky = mk(2, "Ответ: Б. Какой вариант верен?")
    leaky["answer_text"] = "Б"
    c, _m, (ambiguous, leak) = start(
        tmp_path,
        [mk(1), leaky],
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
        tmp_path, [mk(1, "Рассмотрите рисунок", figure=True)], {"1": "Б"}, keys={"1": "Б"}
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


def drafts_with_keys(tmp_path, model):
    """Drafts that exist BEFORE auto-publication is shipped: imported with it switched off."""
    tasks = [mk(1), mk(2), mk(3)]
    c = TestClient(build_app(tmp_path, model, auto_publish=False))
    c.__enter__()
    upload(c, key_pdf({"1": "Б", "2": "Б"}), kind="key", name="key.pdf")
    upload(c, text_pdf([[f"{t['number']}. вопрос" for t in tasks]]))
    return c, task_ids(tmp_path)


def run_action(c, **body):
    r = c.post("/parent/review/auto/run", headers=PARENT, json=body)
    assert r.status_code == 200, r.text
    return c.get("/parent/review/auto/status", headers=PARENT).json()


def test_the_parent_action_publishes_existing_drafts_with_keys_once(tmp_path):
    model = StubModel(
        split=reply(mk(1), mk(2), mk(3)),
        answer=answer_by_number({"1": "Б", "2": "В", "3": "Б"}),
    )
    c, (agree, differ, keyless) = drafts_with_keys(tmp_path, model)
    with c:
        assert child_ids(c) == []
        status = run_action(c)
        assert status["state"] == "done"
        assert status["text"] == "проверено 3 из 3, опубликовано 1, оставлено вам 2."
        assert child_ids(c) == [agree] and row(tmp_path, agree)["auto_published"] == 1
        assert row(tmp_path, differ)["import_status"] == "draft"
        assert row(tmp_path, keyless)["import_status"] == "draft"  # no key: parent's path
        calls = len(model.calls_of("task_answer"))
        assert calls == 2  # the keyless task never reaches the model

        again = run_action(c, material_id=row(tmp_path, agree)["material_id"])
        assert again["state"] == "done" and child_ids(c) == [agree]
        assert len(model.calls_of("task_answer")) == calls  # nothing is asked twice
        assert row(tmp_path, agree)["revision"] == 1


def test_the_parent_action_stops_at_the_model_limit_and_says_so(tmp_path):
    model = LimitModel(split=reply(mk(1), mk(2), mk(3)))
    c, _ids = drafts_with_keys(tmp_path, model)
    with c:
        status = run_action(c)
        assert status["state"] == "done" and "лимит" in status["text"]
        assert status["text"].startswith("проверено 3 из 3, опубликовано 0, оставлено вам 3")
        assert model.calls.count(None) == 1 and child_ids(c) == []


# ------------------------------------------------- review round: races, key identity, leaks


class BlockingAnswers(StubModel):
    """The model answer request waits until the test releases it: a window for a race."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, system, messages, schema=None, session_id=None):
        if (schema or {}).get("title") == "task_answer":
            self.entered.set()
            assert self.release.wait(10)
        return super().complete(system, messages, schema, session_id)


def blocked_pass(tmp_path, tasks, answers, keys, mutate):
    """Draft + key, a publish pass in a real thread stuck in the model call, `mutate` in between."""
    model = BlockingAnswers(split=reply(*tasks), answer=answer_by_number(answers))
    c = TestClient(build_app(tmp_path, model, auto_publish=False))
    c.__enter__()
    upload(c, key_pdf(keys), kind="key", name="key.pdf")
    upload(c, text_pdf([[f"{t['number']}. вопрос" for t in tasks]]))
    ids = task_ids(tmp_path)
    ctx = c.app.state.ctx
    conn = open_db(tmp_path / "olymp.db")
    worker = threading.Thread(
        target=autopublish.publish_pending, args=(ctx, conn), kwargs={"force": True}
    )
    worker.start()
    assert model.entered.wait(10)
    mutate(c, ids)
    model.release.set()
    worker.join(10)
    assert not worker.is_alive()
    conn.close()
    return c, ids


def sql(tmp_path, statement, args=()):
    conn = open_db(tmp_path / "olymp.db")
    try:
        conn.execute(statement, args)
    finally:
        conn.close()


def test_a_task_edited_while_the_model_thinks_is_not_published(tmp_path):
    def mutate(c, ids):
        r = c.post(f"/parent/tasks/{ids[0]}/edit", headers=PARENT, json={"text": "Другое условие"})
        assert r.status_code == 200
        # even a model answer for the NEW revision must wait for the next pass
        sql(
            tmp_path,
            "UPDATE task_item SET model_answer = ? WHERE id = ?",
            (json.dumps({"answer": "Б", "value": "Б", "explanation": ""}), ids[0]),
        )

    c, (tid,) = blocked_pass(tmp_path, [mk(1)], {"1": "Б"}, {"1": "Б"}, mutate)
    with c:
        got = row(tmp_path, tid)
        assert got["revision"] == 2 and got["import_status"] == "draft"
        assert got["auto_published"] == 0 and child_ids(c) == []


def test_a_task_the_parent_keyed_while_the_model_thinks_is_not_published(tmp_path):
    def mutate(c, ids):
        r = c.post(f"/parent/tasks/{ids[0]}/key", headers=PARENT, json={"answer": "Б"})
        assert r.status_code == 200

    c, (tid,) = blocked_pass(tmp_path, [mk(1)], {"1": "Б"}, {"1": "Б"}, mutate)
    with c:
        got = row(tmp_path, tid)
        assert got["key_source"] == "parent" and got["auto_published"] == 0
        assert got["import_status"] == "draft" and child_ids(c) == []


def test_a_second_conflicting_official_key_added_meanwhile_stops_publication(tmp_path):
    def mutate(c, ids):
        sql(
            tmp_path,
            "INSERT INTO material(file, sha256, kind, import_status, variant, year, stage, "
            "olympiad) SELECT 'k2.pdf', 'h2', kind, import_status, variant, year, stage, "
            "olympiad FROM material WHERE kind = 'key'",
        )
        sql(
            tmp_path,
            "INSERT INTO key_entry(material_id, number, answer) "
            "SELECT id, '1', 'В' FROM material WHERE sha256 = 'h2'",
        )

    # the model agrees with the NEWER file: only the ambiguity check can stop the task
    c, (tid,) = blocked_pass(tmp_path, [mk(1)], {"1": "В"}, {"1": "Б"}, mutate)
    with c:
        got = row(tmp_path, tid)
        assert got["auto_published"] == 0 and got["auto_hold"] == autopublish.HOLD_AMBIGUOUS
        assert child_ids(c) == []


def test_the_published_key_is_the_one_the_official_source_names(tmp_path):
    model = StubModel(split=reply(mk(1)), answer=answer_by_number({"1": "Б"}))
    c, _m, (tid,) = start(tmp_path, [mk(1)], {"1": "Б"}, model=model)
    with c:
        # a key already filled in (official, but another answer) next to a key file saying Б
        sql(
            tmp_path,
            "UPDATE task_item SET answer_key = ?, key_source = 'official' WHERE id = ?",
            (json.dumps({"type": "single", "value": "А"}), tid),
        )
        upload(c, key_pdf({"1": "Б"}), kind="key", name="key.pdf", year="2022")
        conn = open_db(tmp_path / "olymp.db")
        assert autopublish.publish_pending(c.app.state.ctx, conn, force=True) == 0
        conn.close()
        got = row(tmp_path, tid)
        assert got["import_status"] == "draft" and got["auto_hold"] == autopublish.HOLD_KEY_MISMATCH
        assert json.loads(got["answer_key"])["value"] == "А" and child_ids(c) == []


def test_an_answer_inside_an_option_text_keeps_the_task_with_the_parent(tmp_path):
    leaky = [dict(o) for o in OPTS]
    leaky[1]["text"] = "Ответ: Б"
    c, _m, (tid,) = start(tmp_path, [mk(1, options=leaky)], {"1": "Б"}, keys={"1": "Б"})
    with c:
        got = row(tmp_path, tid)
        assert got["auto_published"] == 0 and got["auto_hold"] == autopublish.HOLD_LEAK
        assert child_ids(c) == [] and tid in feed(c)


def test_the_import_hook_and_the_manual_run_never_work_in_parallel(tmp_path):
    model = BlockingAnswers(split=reply(mk(1)), answer=answer_by_number({"1": "Б"}))
    c = TestClient(build_app(tmp_path, model, auto_publish=False))
    c.__enter__()
    upload(c, key_pdf({"1": "Б"}), kind="key", name="key.pdf")
    upload(c, text_pdf([["1. вопрос"]]))
    (tid,) = task_ids(tmp_path)
    ctx = c.app.state.ctx
    ctx.auto_publish = True
    ctx.autorun.sync = False
    hook_conn = open_db(tmp_path / "olymp.db")
    hook = threading.Thread(target=ctx.autorun.run_inline, args=(ctx, hook_conn, None))
    hook.start()
    assert model.entered.wait(10)
    assert ctx.autorun.start(ctx, None) is True  # background thread, waits for the shared lock
    time.sleep(0.5)  # long enough for an unguarded second run to reach the model
    model.release.set()
    hook.join(10)
    for _ in range(100):
        if ctx.autorun.snapshot()["state"] == "done":
            break
        time.sleep(0.1)
    hook_conn.close()
    with c:
        assert len(model.calls_of("task_answer")) == 1
        got = row(tmp_path, tid)
        assert got["auto_published"] == 1 and got["revision"] == 1 and child_ids(c) == [tid]


def test_checks_and_confirm_are_one_write(tmp_path, monkeypatch):
    """Between the last check and the confirm no other writer can change the task."""
    c, _m, (tid,) = start(tmp_path, [mk(1)], {"1": "Б"}, keys={"1": "Б"})
    with c:
        sql(tmp_path, "UPDATE task_item SET import_status = 'draft', auto_published = 0")
        sql(
            tmp_path,
            "UPDATE task_item SET model_answer = ? WHERE id = ?",
            (json.dumps({"answer": "Б", "value": "Б", "explanation": ""}), tid),
        )
        outcome = {}
        real = autopublish.review.confirm

        def spy(ctx, conn, task_id, answer=None):
            other = open_db(tmp_path / "olymp.db")
            other.execute("PRAGMA busy_timeout = 50")
            try:
                other.execute("UPDATE task_item SET text = 'tampered' WHERE id = ?", (task_id,))
                outcome["writer"] = "got in"
            except sqlite3.OperationalError as exc:
                outcome["writer"] = "blocked" if "locked" in str(exc) else repr(exc)
            finally:
                other.close()
            return real(ctx, conn, task_id, answer)

        monkeypatch.setattr(autopublish.review, "confirm", spy)
        conn = open_db(tmp_path / "olymp.db")
        assert autopublish.try_publish(c.app.state.ctx, conn, tid, {}) is None
        conn.close()
        assert outcome["writer"] == "blocked"
