from fastapi.testclient import TestClient

from app.db import connect, migrate
from app.search import index_pages, normalize_tokens, search
from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    answer_by_number,
    build_app,
    make_task,
    reply,
    task_ids,
    text_pdf,
    upload,
)

PAGES = [
    (1, "Строение клетки. Клетка — основная единица живого."),
    (2, "Ёж — млекопитающее; питается насекомыми."),
    (3, "Фотосинтез идёт в хлоропластах листа."),
]


def test_ac4(tmp_path):
    conn = connect(tmp_path / "olymp.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO material (id, file, sha256, kind) VALUES (1, 'b.pdf', 'h', 'textbook')"
    )
    index_pages(conn, 1, PAGES)

    assert normalize_tokens("Ёж, ЕЖ! Клетки?") == normalize_tokens("еж еж клетка")

    pages = lambda q: [r["page"] for r in search(conn, q)]
    assert 1 in pages("клетки")  # word forms: клетки -> клетка
    assert pages("ёж") == pages("еж") == [2]  # ё = е both ways
    assert pages("Ежа") == [2]
    # punctuation and FTS syntax do not break the query or inject MATCH operators
    assert pages('клетка"; DROP TABLE material_page; --') == pages("клетка")
    assert pages("((клетка OR NEAR( * ^") == pages("клетка OR NEAR")
    assert pages("клетка:") == [1]
    assert pages("text:клетка") == pages("text клетка")  # no column filters
    # no match and no tokens: an empty list, not an error
    assert pages("квазар") == []
    assert pages("") == [] and pages("?!...") == [] and pages('"') == []
    # the result carries a server id and escaped-ready plain text
    hit = search(conn, "клетки")[0]
    assert (
        hit["material_id"] == 1 and isinstance(hit["id"], int) and "клет" in hit["snippet"].lower()
    )
    conn.close()

    with TestClient(build_app(tmp_path)) as c:
        body = c.get("/api/search", params={"q": "ёж"}, headers=PARENT).json()
        assert [r["page"] for r in body["results"]] == [2]
        assert c.get("/api/search", params={"q": '"'}, headers=PARENT).json()["results"] == []


def test_the_child_searches_only_published_tasks_never_material_pages(tmp_path):
    model = StubModel(
        split=reply(make_task(1, "Какая органелла содержит собственную ДНК?")),
        answer=answer_by_number({"1": "Б"}),
    )
    with TestClient(build_app(tmp_path, model)) as c:
        book = text_pdf([["Ответы к заданиям", "1 — Б", "2 — Г"]])
        assert upload(c, book, kind="textbook", name="book.pdf").status_code == 202
        upload(c, text_pdf([["1. задание"]]))
        tid = task_ids(tmp_path)[0]

        def found(q, headers):
            r = c.get("/api/search", params={"q": q}, headers=headers)
            assert r.status_code == 200
            return r.json()["results"]

        # The answers page is in the materials: only the parent finds it.
        assert found("ответы", CHILD) == []
        hits = found("ответы", PARENT)
        assert len(hits) == 1 and "1 — Б" in hits[0]["snippet"]
        # A draft task is not searchable by the child.
        assert found("органелла", CHILD) == []

        c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        results = found("органелла", CHILD)
        assert [r["task_id"] for r in results] == [tid]
        assert set(results[0]) == {"id", "task_id", "number", "snippet"}
        assert "органелла" in results[0]["snippet"]
        # Never keys, model answers or material text for the child.
        assert found("ответы", CHILD) == []
