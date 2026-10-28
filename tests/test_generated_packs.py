"""T-G2: a pack of similar questions on the parent's request lands in the review queue."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import SCHEMA_VERSION, _migration_files, _statements, connect, migrate
from app.model.types import ModelResult
from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    build_app,
    db,
    make_task,
    reply,
    text_pdf,
    upload,
)

TOPIC = "b10.cell"
OPTS = [
    {"label": c, "text": t} for c, t in zip("АБВГ", ("ядро", "рибосома", "вакуоль", "пластида"))
]
BANK = [
    "Какой органоид клетки отвечает за синтез белков в цитоплазме?",
    "Какая структура клетки содержит хромосомы и управляет её жизнью?",
]
DRAFT_ONLY = "Секретный неподтверждённый черновик про митохондрии клетки"


def good(text, key="Б", **extra):
    item = {
        "text": text,
        "type": "single",
        "options": OPTS,
        "answer_key": key,
        "explanation": "потому что так",
    }
    item.update(extra)
    return item


NEW = [
    good("Какой органоид содержит хлорофилл и участвует в фотосинтезе у растений?", "Г"),
    good("Где в клетке эукариот хранится основная часть наследственной информации?", "А"),
    good("Какой из органоидов запасает воду и растворённые вещества в растительной клетке?", "В"),
]
REPEAT = good("Какой органоид клетки отвечает за синтез белков в цитоплазме клетки?")
INVALID = good("Вопрос с ключом на несуществующий вариант в этой клетке", key="Ж")


class PackModel:
    """Imports tasks for the bank setup; answers the pack request with `pack` (None = refusal)."""

    def __init__(self, bank=(), pack=None):
        self.stub = StubModel(split=reply(*bank))
        self.pack = pack
        self.pack_calls = []
        self.during = None  # runs while the "model" is answering: a concurrent writer

    def complete(self, system, messages, schema=None, session_id=None):
        if (schema or {}).get("title") != "question_pack":
            return self.stub.complete(system, messages, schema, session_id)
        self.pack_calls.append((system, messages, schema))
        if self.during:
            self.during()
        if self.pack is None:
            return ModelResult.failure("claude", "stub", "executor_unreachable")
        return ModelResult(None, self.pack, "claude", "stub", 1, None)


def make_bank(tmp_path, pack, texts=BANK, draft=DRAFT_ONLY, topic=TOPIC):
    """App whose bank holds `texts` confirmed tasks of `topic` and one unconfirmed task."""
    tasks = [make_task(n, t, topic=topic) for n, t in enumerate(texts, 1)]
    if draft:
        tasks.append(make_task(len(texts) + 1, draft, topic=topic))
    model = PackModel(tasks, pack)
    c = TestClient(build_app(tmp_path, model))
    c.__enter__()
    upload(c, text_pdf([[f"{n}. вопрос" for n in range(1, len(tasks) + 1)]]))
    ids = [r["id"] for r in db(tmp_path).execute("SELECT id FROM task_item ORDER BY id")]
    for tid in ids[: len(texts)]:
        r = c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={"answer": "Б"})
        assert r.status_code == 200, r.text
    return c, model


def request_pack(c, count=5, kind="single", topic=TOPIC):
    return c.post(
        "/parent/generate",
        headers=PARENT,
        data={"topic": topic, "kind": kind, "count": str(count)},
        follow_redirects=False,
    )


def generated(tmp_path):
    conn = db(tmp_path)
    try:
        return conn.execute("SELECT * FROM task_item WHERE key_source = 'generated'").fetchall()
    finally:
        conn.close()


def test_a_pack_lands_in_the_review_queue_without_repeats(tmp_path):
    c, _ = make_bank(tmp_path, {"questions": [*NEW[:2], REPEAT, INVALID, NEW[2]]})
    with c:
        r = request_pack(c, 5)
        assert r.status_code == 303, r.text
        pack_url = r.headers["location"]

        rows = generated(tmp_path)
        assert len(rows) == 3
        assert {row["topic"] for row in rows} == {TOPIC}
        assert len({row["pack_id"] for row in rows}) == 1 and rows[0]["pack_id"] is not None
        assert {row["import_status"] for row in rows} == {"draft"}
        assert {row["material_id"] for row in rows} == {None}
        assert [json.loads(row["answer_key"])["value"] for row in rows] == ["Г", "А", "В"]

        # they are in the parent review queue ...
        queue = {
            card["task_id"]
            for card in c.get("/parent/review/queue", headers=PARENT).json()["cards"]
        }
        assert {row["id"] for row in rows} <= queue
        # ... and the child cannot reach them until the parent confirms
        listed = {t["id"] for t in c.get("/api/tasks", headers=CHILD).json()["tasks"]}
        assert listed.isdisjoint(row["id"] for row in rows)
        for row in rows:
            assert c.get(f"/api/tasks/{row['id']}", headers=CHILD).status_code == 404
            assert c.get(f"/tasks/{row['id']}", headers=CHILD).status_code == 404

        # the pack page names both rejected items with a reason
        page = c.get(pack_url, headers=PARENT)
        assert page.status_code == 200
        assert "Принято в ленту проверки: <b>3</b>" in page.text
        assert "повтор: уже есть в банке заданий" in page.text
        assert "несуществующий вариант" in page.text
        pack = db(tmp_path).execute("SELECT * FROM question_pack").fetchone()
        assert (pack["status"], pack["accepted_count"], pack["requested_count"]) == ("done", 3, 5)
        assert (
            pack["topic"] == TOPIC and pack["task_type"] == "single" and pack["channel"] == "claude"
        )
        assert [x["number"] for x in json.loads(pack["rejected"])] == [3, 4]


def test_unconfirmed_bank_tasks_are_never_sent_as_examples(tmp_path):
    c, model = make_bank(tmp_path, {"questions": []})
    with c:
        request_pack(c, 3)
        ((system, messages, _),) = model.pack_calls
        sent = system + messages[0]["content"]
        assert BANK[0] in sent and BANK[1] in sent
        assert "Секретный" not in sent and "митохондрии" not in sent
        assert "ВсОШ" in system and "реальные биологические термины" in system
        assert "Цитология" in sent or "Клетка" in sent


def test_at_most_five_confirmed_examples_are_sent(tmp_path):
    texts = [f"Подтверждённый вопрос номер {n} про строение клетки" for n in range(1, 8)]
    c, model = make_bank(tmp_path, {"questions": []}, texts=texts, draft=None)
    with c:
        request_pack(c, 3)
        content = model.pack_calls[0][1][0]["content"]
        assert sum(t in content for t in texts) == 5


def test_a_count_above_twenty_is_refused(tmp_path):
    c, model = make_bank(tmp_path, {"questions": NEW})
    with c:
        for bad in (21, 0, -1):
            r = request_pack(c, bad)
            assert r.status_code == 422
        assert request_pack(c, "много").status_code == 422
        assert model.pack_calls == []
        assert db(tmp_path).execute("SELECT COUNT(*) FROM question_pack").fetchone()[0] == 0
        assert generated(tmp_path) == []
        assert request_pack(c, 20).status_code == 303


def test_a_refused_or_unreachable_model_fails_the_pack_without_rows(tmp_path):
    c, _ = make_bank(tmp_path, None)
    with c:
        r = request_pack(c, 4)
        assert r.status_code == 303
        pack = db(tmp_path).execute("SELECT * FROM question_pack").fetchone()
        assert pack["status"] == "failed" and pack["accepted_count"] == 0
        assert "Модель не ответила" in pack["error"]
        assert generated(tmp_path) == []
        assert "Пачка не создана" in c.get(r.headers["location"], headers=PARENT).text


def test_a_broken_answer_fails_the_pack_without_rows(tmp_path):
    c, _ = make_bank(tmp_path, {"questions": "не список"})
    with c:
        request_pack(c, 2)
        pack = db(tmp_path).execute("SELECT * FROM question_pack").fetchone()
        assert pack["status"] == "failed" and "структур" in pack["error"]
        assert generated(tmp_path) == []


def test_repeats_inside_one_pack_and_across_packs_are_rejected(tmp_path):
    c, _ = make_bank(tmp_path, {"questions": [NEW[0], NEW[0], NEW[1]]})
    with c:
        request_pack(c, 3)
        assert len(generated(tmp_path)) == 2
        request_pack(c, 3)  # the same answer again: all three are repeats now
        assert len(generated(tmp_path)) == 2
        rejected = json.loads(
            db(tmp_path).execute("SELECT rejected FROM question_pack WHERE id = 2").fetchone()[0]
        )
        assert len(rejected) == 3


def test_invalid_items_are_rejected_with_reasons(tmp_path):
    bad = [
        good("Условие про рисунок: что показано на рисунке клетки здесь?"),
        good("Вопрос другого типа для проверки типа ответа", type="multiple"),
        good("Вопрос без нормальных вариантов ответа в клетке", options=[OPTS[0]]),
        good("Вопрос без ключа ответа в клетке эукариот", key=""),
        good("Ответ Б назван прямо: правильный ответ рибосома рибосома рибосома", key="Б"),
        "не объект",
    ]
    c, _ = make_bank(tmp_path, {"questions": bad})
    with c:
        request_pack(c, 6)
        assert generated(tmp_path) == []
        rejected = json.loads(
            db(tmp_path).execute("SELECT rejected FROM question_pack").fetchone()[0]
        )
        assert len(rejected) == 6 and all(x["reason"] for x in rejected)
        reasons = " | ".join(x["reason"] for x in rejected)
        assert "рисунок" in reasons and "тип" in reasons and "нет ключа" in reasons


def test_other_kinds_are_checked_against_their_own_rules(tmp_path):
    match = {
        "text": "Установите соответствие между органоидом и его функцией в клетке",
        "type": "match",
        "options": {
            "left": [{"label": "1", "text": "рибосома"}, {"label": "2", "text": "ядро"}],
            "right": [
                {"label": "А", "text": "синтез белка"},
                {"label": "Б", "text": "хранение ДНК"},
            ],
        },
        "answer_key": "1-А; 2-Б",
    }
    half = {
        **match,
        "text": "Другое соответствие между структурой и её ролью в клетке",
        "answer_key": "1-А",
    }
    c, _ = make_bank(tmp_path, {"questions": [match, half]})
    with c:
        request_pack(c, 2, kind="match")
        rows = generated(tmp_path)
        assert [r["type"] for r in rows] == ["match"]
        assert json.loads(rows[0]["answer_key"])["value"] == {"1": "А", "2": "Б"}


def test_a_generated_task_has_no_material_and_still_works_after_confirmation(tmp_path):
    c, _ = make_bank(tmp_path, {"questions": NEW})
    with c:
        request_pack(c, 3)
        rows = generated(tmp_path)
        tid = rows[0]["id"]
        # parent side: card page, fragment, detail, edit, feed
        assert c.get(f"/parent/review/{tid}", headers=PARENT).status_code == 200
        assert c.get(f"/parent/review/{tid}?fragment=1", headers=PARENT).status_code == 200
        detail = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()
        assert detail["key_source"] == "generated" and detail["card"]["kind"] == "confirm"
        assert c.get("/parent/review", headers=PARENT).status_code == 200
        edit = c.post(
            f"/parent/tasks/{tid}/edit",
            headers=PARENT,
            json={"text": rows[0]["text"] + " Уточнение."},
        )
        assert edit.status_code == 200, edit.text
        assert c.get("/parent/materials", headers=PARENT).status_code == 200
        # confirmed -> the child sees it, with a source line that needs no material
        r = c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        assert r.status_code == 200, r.text
        view = c.get(f"/api/tasks/{tid}", headers=CHILD)
        assert view.status_code == 200 and "помощником" in view.json()["source"]
        assert c.get(f"/tasks/{tid}", headers=CHILD).status_code == 200
        assert tid in [t["id"] for t in c.get("/api/tasks", headers=CHILD).json()["tasks"]]
        assert c.get("/api/search?q=хлорофилл", headers=CHILD).status_code == 200
        # a sibling that was not confirmed stays hidden
        assert c.get(f"/api/tasks/{rows[1]['id']}", headers=CHILD).status_code == 404


def test_the_form_names_the_external_model_and_offers_the_catalog(tmp_path):
    c, _ = make_bank(tmp_path, {"questions": []})
    with c:
        page = c.get("/parent/generate", headers=PARENT)
        assert page.status_code == 200
        assert "внешней модели" in page.text and 'name="count"' in page.text
        assert "Цитология" in page.text and "Клетка: строение" in page.text
        assert "/parent/generate" in c.get("/parent/materials", headers=PARENT).text
        assert "/parent/generate" in c.get("/parent/review", headers=PARENT).text
        assert c.get("/parent/generate", headers=CHILD, follow_redirects=False).status_code in (
            401,
            403,
            303,
        )
        bad = c.post(
            "/parent/generate",
            headers=PARENT,
            data={"topic": "zzz", "kind": "single", "count": "3"},
        )
        assert bad.status_code == 422 and "каталоге" in bad.text


def test_the_migration_upgrades_a_v9_database_keeping_the_data(tmp_path):
    conn = connect(tmp_path / "olymp.db")
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for version, path in _migration_files()[:9]:
        for stmt in _statements(path.read_text(encoding="utf-8")):
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    conn.execute("PRAGMA user_version = 9")
    conn.execute(
        "INSERT INTO material(file, sha256, kind, import_status) VALUES ('a.pdf', 'h', 'tasks', 'done')"
    )
    conn.execute(
        "INSERT INTO task_item(material_id, source_number, type, text, topic, import_status, "
        "answer_key, key_source, answer_text, parent_note) VALUES "
        "(1, '7', 'single', 'q', 'b10', 'checked', '{\"type\":\"single\",\"value\":\"Б\"}', 'parent', 'ans', 'note')"
    )
    conn.execute("INSERT INTO task_revision VALUES (1, 1, 1, '{}', 't')")
    before = conn.execute("SELECT * FROM task_item").fetchall()

    assert migrate(conn) == SCHEMA_VERSION >= 10  # later migrations (0011_auth, ...) stack on top
    after = conn.execute("SELECT * FROM task_item").fetchall()
    assert [tuple(r) for r in after] == [
        (*before[0], None, None)
    ]  # every old column kept; pack_id, key_candidate_source empty
    assert conn.execute("SELECT COUNT(*) FROM task_revision").fetchone()[0] == 1
    conn.execute(
        "INSERT INTO question_pack(created_at, topic, task_type, requested_count) VALUES ('t', 'b10', 'single', 3)"
    )
    conn.execute(  # a task without material, from the pack
        "INSERT INTO task_item(material_id, type, text, answer_key, key_source, pack_id) "
        "VALUES (NULL, 'single', 'g', '{\"type\":\"single\",\"value\":\"А\"}', 'generated', 1)"
    )
    conn.execute(  # two material-less tasks do not collide on the unique (material, section, number)
        "INSERT INTO task_item(material_id, type, text, source_number) VALUES (NULL, 'single', 'h', 'Г1.1')"
    )
    conn.execute(
        "INSERT INTO task_item(material_id, type, text, source_number) VALUES (NULL, 'single', 'i', 'Г1.1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_item(material_id, type, text, key_source) VALUES (1, 'single', 'x', 'model')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO question_pack(created_at, topic, task_type, requested_count) VALUES ('t', 'b10', 'single', 21)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_item(material_id, type, text, pack_id) VALUES (NULL, 'single', 'x', 99)"
        )
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrate(conn) == SCHEMA_VERSION  # a second run changes nothing


def test_more_questions_than_requested_are_not_all_accepted(tmp_path):
    c, _ = make_bank(tmp_path, {"questions": NEW})
    with c:
        request_pack(c, 2)
        assert len(generated(tmp_path)) == 2
        rejected = json.loads(
            db(tmp_path).execute("SELECT rejected FROM question_pack").fetchone()[0]
        )
        assert [x["reason"] for x in rejected] == ["сверх запрошенного числа"]


def test_a_duplicate_that_appears_during_the_model_call_is_rejected_at_write(tmp_path):
    c, model = make_bank(tmp_path, {"questions": [NEW[0], NEW[1]]})
    with c:

        def other_pack_lands_first():
            conn = db(tmp_path)
            conn.execute(
                "INSERT INTO task_item(material_id, type, text, topic) VALUES (NULL, 'single', ?, ?)",
                (NEW[0]["text"], TOPIC),
            )
            conn.close()

        model.during = other_pack_lands_first
        request_pack(c, 2)
        assert [r["text"] for r in generated(tmp_path)] == [NEW[1]["text"]]
        rejected = json.loads(
            db(tmp_path).execute("SELECT rejected FROM question_pack").fetchone()[0]
        )
        assert [x["reason"] for x in rejected] == ["повтор: уже есть в банке заданий"]


def test_option_labels_that_collide_after_normalization_are_rejected(tmp_path):
    def opts(*labels):
        return [{"label": lb, "text": f"вариант {i}"} for i, lb in enumerate(labels)]

    colliding = [
        good("Латинская и кириллическая А в одном вопросе про клетку", options=opts("A", "А", "В")),
        good("Регистр меток в одном вопросе про клетку эукариот", options=opts("б", "Б", "В")),
        good("Пробелы в метках одного вопроса про клетку растений", options=opts("В ", "В", "Г")),
    ]
    match = {
        "text": "Соответствие с совпадающими метками правого набора клетки",
        "type": "match",
        "options": {
            "left": [{"label": "1", "text": "ядро"}, {"label": "2", "text": "рибосома"}],
            "right": [{"label": "A", "text": "x"}, {"label": "А", "text": "y"}],
        },
        "answer_key": "1-А; 2-А",
    }
    c, _ = make_bank(tmp_path, {"questions": [*colliding, match]})
    with c:
        request_pack(c, 4)
        assert generated(tmp_path) == []
        rejected = json.loads(
            db(tmp_path).execute("SELECT rejected FROM question_pack").fetchone()[0]
        )
        assert len(rejected) == 4
        assert all("совпадают" in x["reason"] for x in rejected[:3])
        request_pack(c, 1, kind="match")
        assert generated(tmp_path) == []


def test_true_false_needs_exactly_the_pair_yes_no(tmp_path):
    def tf(text, a, b):
        return good(text, "А", options=[{"label": "А", "text": a}, {"label": "Б", "text": b}])

    c, _ = make_bank(
        tmp_path,
        {
            "questions": [
                tf("Утверждение: ядро есть у всех клеток без исключения всегда", "Да", "Нет"),
                tf(
                    "Утверждение: рибосомы синтезируют белки в клетке любого организма",
                    " верно",
                    "НЕВЕРНО ",
                ),
                good(
                    "Утверждение: хлоропласты есть только у растений и водорослей, верно ли",
                    "А",
                    options=[
                        {"label": "А", "text": "Верно"},
                        {"label": "Б", "text": "Неверно"},
                        {"label": "В", "text": "Верно"},
                    ],
                ),
                tf(
                    "Утверждение: вакуоль встречается в растительных клетках чаще всего",
                    "Верно",
                    "Верно",
                ),
            ]
        },
    )
    with c:
        request_pack(c, 3, kind="truefalse")
        assert [r["text"][:21] for r in generated(tmp_path)] == ["Утверждение: рибосомы"]
