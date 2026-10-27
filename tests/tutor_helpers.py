"""Builders for the tutor (T06) tests: tasks seeded straight into the bank, a scripted model,
a movable clock. No real model is ever called."""

import itertools
import json
import threading
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.ingest.answers import key_json
from app.model.types import ModelResult
from tests.bank_helpers import CHILD, OPTS4, PARENT, build_app, db, quad_png

__all__ = [
    "CHILD",
    "OPTS4",
    "PARENT",
    "Blocking",
    "Clock",
    "Scripted",
    "attempt",
    "boot",
    "events",
    "out",
    "post",
    "seed",
    "view",
]

_counter = itertools.count(1)
START = datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def out(proposed="Б", explanation="Разбор.", uncertainty="low"):
    return {"proposed_answer": proposed, "explanation": explanation, "uncertainty": uncertainty}


class Scripted:
    """Replaces only the model. reply(kind, content) -> payload | None | ModelResult;
    kind is the mode named in the request (hint1, hint2, mistake, solution, explain_*)."""

    def __init__(self, reply=None, default=None, judge=None):
        self.reply = reply
        self.default = default if default is not None else out()
        self.calls = []
        self.judge = judge  # None: the judge says "no leak"; a callable may return None = down
        self.judge_calls = []

    def complete_on(self, channel, system, messages, schema=None, session_id=None):
        content = messages[-1]["content"]
        self.judge_calls.append({"channel": channel, "content": content, "system": system})
        if self.judge is None:
            payload = {"reveals": False, "reason": "нет признаков ответа"}
        else:
            payload = self.judge(content)
        if isinstance(payload, ModelResult):
            return payload
        if payload is None:
            return ModelResult.failure(channel, "stub", "error")
        return ModelResult(None, payload, channel, "stub", 1, None)

    def candidate(self, content):
        return content.split("<candidate_data>\n", 1)[1].rsplit("\n</candidate_data>", 1)[0]

    def complete(self, system, messages, schema=None, session_id=None):
        content = messages[-1]["content"]
        kind = next((k for k in MODES if f"Режим: {k}\n" in content), "?")
        self.calls.append({"kind": kind, "system": system, "content": content, "schema": schema})
        payload = self.reply(kind, content) if callable(self.reply) else self.default
        if isinstance(payload, ModelResult):
            return payload
        if payload is None:
            return ModelResult.failure("claude", "stub", "error")
        return ModelResult(None, payload, "claude", "stub", 1, None)

    def kinds(self):
        return [c["kind"] for c in self.calls]


MODES = (
    "hint1", "hint2", "mistake", "solution", "judge", "chat", "formulate",
    "explain_simpler", "explain_table", "explain_example", "explain_olympiad", "explain_check",
)  # fmt: skip


class Judged(Scripted):
    """Scripted with an explicit judge: Judged(judge=callable, reply=...)."""


class Blocking(Scripted):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, *a, **kw):
        self.entered.set()
        self.release.wait(20)
        return super().complete(*a, **kw)


def boot(tmp_path, model=None, clock=None, notice=True):
    """(client, clock, model); the caller uses it as a context manager."""
    clock = clock or Clock()
    model = model if model is not None else Scripted()
    app = build_app(tmp_path, model, clock=clock, notice=notice)
    client = TestClient(app)
    client.__enter__()
    return client, clock, model


def seed(
    tmp_path,
    *,
    type="single",
    options=None,
    key="Б",
    model_answer=None,
    model_explanation="Решение модели с импорта.",
    figure=None,
    text="Что изучает цитология?",
    number=None,
):
    """A published task. key=None means no confirmed key (model_answer is then the reference)."""
    conn = db(tmp_path)
    n = next(_counter)
    try:
        material = conn.execute("SELECT id FROM material WHERE sha256 = 'seed'").fetchone()
        if material is None:
            conn.execute(
                "INSERT INTO material (file, sha256, kind, year, import_status) "
                "VALUES ('m.pdf', 'seed', 'tasks', 2022, 'done')"
            )
            material = conn.execute("SELECT id FROM material WHERE sha256 = 'seed'").fetchone()
        if options is None and type in ("single", "multiple"):
            options = OPTS4
        if options is None and type == "match":
            options = {
                "left": [{"label": "1", "text": "корень"}, {"label": "2", "text": "лист"}],
                "right": [
                    {"label": "А", "text": "всасывание"},
                    {"label": "Б", "text": "фотосинтез"},
                ],
            }
        value = key if key is not None else None
        cols = {
            "material_id": material["id"],
            "source_number": number or str(n),
            "type": type,
            "text": text,
            "options": None if options is None else json.dumps(options, ensure_ascii=False),
            "import_status": "checked",
            "page_from": 3,
            "answer_key": None,
            "key_source": None,
            "model_answer": None,
            "figure": None,
        }
        if value is not None:
            from app.ingest.answers import normalize_answer

            cols["answer_key"] = key_json(normalize_answer(value, type), type)
            cols["key_source"] = "parent"
        if model_answer is not None:
            from app.ingest.answers import display, normalize_answer

            norm = normalize_answer(model_answer, type)
            cols["model_answer"] = json.dumps(
                {
                    "answer": display(norm),
                    "value": norm,
                    "explanation": model_explanation,
                    "uncertainty": "low",
                    "unverified": True,
                },
                ensure_ascii=False,
            )
        if figure:
            name = f"figures/seed-{n}.png"
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(quad_png())
            cols["figure"] = json.dumps(
                {
                    "file": name,
                    "confirmed": figure.get("confirmed", True),
                    "alt": figure.get(
                        "alt", "Продольный разрез цветка, цифрами 1-6 обозначены части"
                    ),
                    "teacher_note": figure.get(
                        "teacher_note",
                        "1 - тычинка, 2 - пестик, 3 - лепесток, 4 - чашелистик, 5 - завязь, 6 - рыльце",
                    ),
                    "teacher_note_ack": figure.get("ack", True),
                    "page": 3,
                },
                ensure_ascii=False,
            )
        names = ", ".join(cols)
        conn.execute(
            f"INSERT INTO task_item ({names}) VALUES ({', '.join('?' * len(cols))})",
            tuple(cols.values()),
        )
        return conn.execute("SELECT MAX(id) FROM task_item").fetchone()[0]
    finally:
        conn.close()


def post(c, tid, path, query="", **body):
    return c.post(f"/tasks/{tid}/{path}{query}", json=body, headers=CHILD)


def attempt(c, tid, answer, confidence=None):
    return post(c, tid, "attempt", answer=answer, confidence=confidence)


def view(c, tid):
    r = c.get(f"/api/tasks/{tid}/tutor", headers=CHILD)
    assert r.status_code == 200, r.text
    return r.json()


def events(tmp_path, type=None):
    conn = db(tmp_path)
    try:
        sql = "SELECT * FROM event" + (" WHERE type = ?" if type else "") + " ORDER BY id"
        rows = conn.execute(sql, (type,) if type else ()).fetchall()
        return [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None} for r in rows]
    finally:
        conn.close()
