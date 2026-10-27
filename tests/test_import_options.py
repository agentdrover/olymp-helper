"""#2106: options as strings from the model, schema shape, and the safe «Разобрать заново»."""

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ingest.answers import normalize_answer
from app.ingest.prompt import SYSTEM_PROMPT, TASKS_SCHEMA
from tests.bank_helpers import PARENT, StubModel, build_app, db, make_task, reply, text_pdf, upload

HTML = {**PARENT, "Accept": "text/html,application/xhtml+xml"}
API = {**PARENT, "Accept": "application/json"}
AVAILABLE = "Есть проверенные задания — разбор заново недоступен"


def _objs(labels, prefix):
    return [{"label": c, "text": f"{prefix} {c}"} for c in labels]


def _stored(tmp_path):
    conn = db(tmp_path)
    try:
        return {r["source_number"]: r for r in conn.execute("SELECT * FROM task_item")}
    finally:
        conn.close()


def _labels(options):
    return [normalize_answer(o["label"], "single") for o in options]


# --------------------------------------------------------------------- options


def test_string_options_are_parsed_or_skipped(tmp_path):
    import json

    tasks = [
        make_task(1, "Первое условие", options=_objs("АБВГ", "об")),
        make_task(
            2,
            "Второе условие",
            options=["А) митохондрия", "Б. ядро", "в) рибосома", "Г) вакуоль"],
        ),
        make_task(
            3,
            "Третье условие",
            "multiple",
            options=["A) лист", "B) корень", "C) стебель"],  # Latin look-alikes
        ),
        make_task(
            4,
            "Четвёртое условие",
            "match",
            options={
                "left": ["1) клетка", "2) ткань"],
                "right": ["А) основа", "Б) группа"],
            },
        ),
        make_task(
            5,
            "Пятое условие",
            "match",
            options={"left": ["А) клетка", "Б) ткань"], "right": ["А) основа", "Б) группа"]},
        ),
        make_task(6, "Шестое условие", options=["митохондрия", "ядро"]),
        make_task(7, "Седьмое условие", options=["А) первый", "A) второй"]),
        make_task(8, "Восьмое условие", options=["А) первый", {"label": "Б", "text": "второй"}]),
    ]
    model = StubModel(split=lambda content: reply(*tasks))
    with TestClient(build_app(tmp_path, model)) as c:
        assert upload(c, text_pdf([["1. вопрос"]])).status_code == 202
        stored = _stored(tmp_path)
        assert set(stored) == {"1", "2", "3", "4", "5", "8"}
        two = json.loads(stored["2"]["options"])
        assert _labels(two) == ["А", "Б", "В", "Г"]
        assert two[1] == {"label": "Б", "text": "ядро"}
        assert _labels(json.loads(stored["3"]["options"])) == ["А", "В", "С"]
        four = json.loads(stored["4"]["options"])
        assert [o["label"] for o in four["left"]] == ["1", "2"]
        assert _labels(four["right"]) == ["А", "Б"]
        five = json.loads(stored["5"]["options"])
        assert _labels(five["left"]) == _labels(five["right"]) == ["А", "Б"]
        conn = db(tmp_path)
        note = conn.execute("SELECT import_error FROM material").fetchone()[0]
        conn.close()
        assert "задание 6" in note and "задание 7" in note
        assert "без обозначения" in note and "совпадают после нормализации" in note


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_schema_describes_option_shape():
    options = TASKS_SCHEMA["properties"]["tasks"]["items"]["properties"]["options"]
    assert options != {}
    shapes = [n for n in _walk(options) if isinstance(n, dict)]
    assert any(
        {"label", "text"} <= set(n.get("properties", {})) and "label" in n.get("required", [])
        for n in shapes
    )
    assert any({"left", "right"} <= set(n.get("properties", {})) for n in shapes)
    for word in ("options", "label", "text", "left", "right", "single", "match", "numeric"):
        assert word in SYSTEM_PROMPT
    assert '"label"' in SYSTEM_PROMPT  # a short example


# --------------------------------------------------------------------- reparse


def _split(count=2):
    return reply(*(make_task(n, f"Условие номер {n}", figure=n == 2) for n in range(1, count + 1)))


def _start(tmp_path, **kw):
    model = StubModel(split=lambda content: _split())
    client = TestClient(build_app(tmp_path, model, **kw))
    client.__enter__()
    return client, model


def _waiting(client):
    client.app.state.imports.wait_idle(10)


def _status(tmp_path):
    conn = db(tmp_path)
    try:
        return tuple(conn.execute("SELECT import_status, import_error FROM material").fetchone())
    finally:
        conn.close()


def _count(tmp_path, table):
    conn = db(tmp_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_reparse_untouched_material_runs_one_import(tmp_path):
    release, entered = threading.Event(), threading.Event()
    state = {"mode": "ok", "calls": 0}

    def split(content):
        state["calls"] += 1
        if state["mode"] == "gate":
            entered.set()
            release.wait(20)
        if state["mode"] == "down":
            return None
        return _split()

    model = StubModel(split=split)
    with TestClient(build_app(tmp_path, model, sync_imports=False)) as c:
        assert upload(c, text_pdf([["1. вопрос"]])).status_code == 202
        _waiting(c)
        assert _count(tmp_path, "task_item") == 2
        conn = db(tmp_path)
        stray = Path(tmp_path / "figures" / "2-r1.png")
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"png")
        conn.execute(
            "UPDATE task_item SET figure = json_set(figure, '$.file', 'figures/2-r1.png') "
            "WHERE source_number = '2'"
        )
        conn.close()
        page = c.get("/parent/materials", headers=HTML).text
        assert "/parent/materials/1/reparse" in page
        calls = state["calls"]

        state["mode"] = "gate"
        first = c.post("/parent/materials/1/reparse", headers=API)
        assert first.status_code == 202, first.text
        assert entered.wait(10)
        assert _count(tmp_path, "task_item") == 0 and not stray.exists()
        assert _status(tmp_path)[0] in ("queued", "running")
        second = c.post("/parent/materials/1/reparse", headers=API)  # double click
        assert second.status_code in (200, 202, 409)
        assert "/parent/materials/1/reparse" not in c.get("/parent/materials", headers=HTML).text
        release.set()
        _waiting(c)
        assert state["calls"] == calls + 1  # exactly one import ran
        assert _status(tmp_path) == ("done", None)
        after = {r[0] for r in db(tmp_path).execute("SELECT id FROM task_item")}
        assert len(after) == 2 and not stray.exists()  # (ids may be reused by SQLite)
        assert _count(tmp_path, "task_revision") == 2

        # failure path: failed with a clear error, the same button retries
        state["mode"] = "down"
        assert c.post("/parent/materials/1/reparse", headers=API).status_code == 202
        _waiting(c)
        status, error = _status(tmp_path)
        assert status == "failed" and error
        assert "/parent/materials/1/reparse" in c.get("/parent/materials", headers=HTML).text
        state["mode"] = "ok"
        assert c.post("/parent/materials/1/reparse", headers=API).status_code == 202
        _waiting(c)
        assert _status(tmp_path) == ("done", None)
        assert _count(tmp_path, "task_item") == 2


def test_reparse_is_atomic(tmp_path):
    client, _ = _start(tmp_path)
    with client as c:
        assert upload(c, text_pdf([["1. вопрос"]])).status_code == 202
        conn = db(tmp_path)
        conn.execute(
            "CREATE TRIGGER no_delete BEFORE DELETE ON task_item "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        conn.close()
        quiet = TestClient(c.app, raise_server_exceptions=False)
        got = quiet.post("/parent/materials/1/reparse", headers=API)
        assert got.status_code >= 400
        assert _status(tmp_path) == ("done", None)
        assert _count(tmp_path, "task_item") == 2


TOUCH = {
    "checked": "UPDATE task_item SET import_status = 'checked' WHERE source_number = '1'",
    "edited": "UPDATE task_item SET revision = 2 WHERE source_number = '1'",
    "excluded": "UPDATE task_item SET excluded = 1 WHERE source_number = '1'",
    "figure_confirmed": (
        "UPDATE task_item SET figure = "
        '\'{"confirmed": true, "file": "figures/1-r2.png"}\' WHERE source_number = \'2\''
    ),
    "parent_key": (
        'UPDATE task_item SET answer_key = \'{"type": "single", "value": "А"}\', '
        "key_source = 'parent', key_version = 2 WHERE source_number = '1'"
    ),
    "child_attempt": [
        "INSERT INTO study_session (started_at, last_activity_at) VALUES ('t', 't')",
        (
            "INSERT INTO event (session_id, type, task_item_id, created_at) "
            "VALUES (1, 'attempt', (SELECT id FROM task_item WHERE source_number = '1'), 't')"
        ),
    ],
    "tutor_run": (
        "INSERT INTO tutor_run (task_item_id, opened_at) "
        "VALUES ((SELECT id FROM task_item WHERE source_number = '2'), 't')"
    ),
}


@pytest.mark.parametrize("kind", sorted(TOUCH))
def test_reparse_refused_when_parent_touched_material(tmp_path, kind):
    client, model = _start(tmp_path)
    with client as c:
        assert upload(c, text_pdf([["1. вопрос"]])).status_code == 202
        conn = db(tmp_path)
        for sql in [TOUCH[kind]] if isinstance(TOUCH[kind], str) else TOUCH[kind]:
            conn.execute(sql)
        conn.close()
        calls = len(model.calls)
        page = c.get("/parent/materials", headers=HTML).text
        assert "/parent/materials/1/reparse" not in page
        assert AVAILABLE in page
        got = c.post("/parent/materials/1/reparse", headers=API)
        assert got.status_code == 409
        assert _count(tmp_path, "task_item") == 2
        assert _status(tmp_path) == ("done", None)
        assert len(model.calls) == calls


def test_unicode_digit_labels_are_skipped_with_a_reason(tmp_path):
    tasks = [
        make_task(1, "Первое условие", options=["١) первый", "٢) второй"]),
        make_task(2, "Второе условие", options=["1) первый", "2) второй"]),
    ]
    model = StubModel(split=lambda content: reply(*tasks))
    with TestClient(build_app(tmp_path, model)) as c:
        assert upload(c, text_pdf([["1. вопрос"]])).status_code == 202
        assert set(_stored(tmp_path)) == {"2"}
        conn = db(tmp_path)
        note = conn.execute("SELECT import_error FROM material").fetchone()[0]
        conn.close()
        assert "задание 1" in note and "без обозначения" in note


def test_reparse_survives_a_file_that_cannot_be_removed(tmp_path, monkeypatch):
    client, _ = _start(tmp_path)
    with client as c:
        assert upload(c, text_pdf([["1. вопрос"]])).status_code == 202
        stray = Path(tmp_path / "figures" / "2-r1.png")
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"png")
        original = Path.unlink

        def deny(self, *args, **kwargs):
            if self.name.endswith(".png"):
                raise PermissionError("denied")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", deny)
        got = c.post("/parent/materials/1/reparse", headers=API)
        assert got.status_code == 202, got.text
        assert _status(tmp_path) == ("done", None)
        assert _count(tmp_path, "task_item") == 2


def test_reparse_resubmits_a_queued_material_without_a_running_job(tmp_path):
    client, model = _start(tmp_path)
    with client as c:
        assert upload(c, text_pdf([["1. вопрос"]])).status_code == 202
        conn = db(tmp_path)
        conn.execute("DELETE FROM task_item")
        conn.execute("UPDATE material SET import_status = 'queued'")  # stuck: no job runs
        conn.close()
        calls = len(model.calls)
        got = c.post("/parent/materials/1/reparse", headers=API)
        assert got.status_code == 202 and got.json()["started"] is True
        assert len(model.calls) == calls + 1
        assert _status(tmp_path) == ("done", None)
