from fastapi.testclient import TestClient

from app.db import connect, migrate
from app.search import index_pages, normalize_tokens, search
from tests.bank_helpers import CHILD, build_app

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
        body = c.get("/api/search", params={"q": "ёж"}, headers=CHILD).json()
        assert [r["page"] for r in body["results"]] == [2]
        assert c.get("/api/search", params={"q": '"'}, headers=CHILD).json()["results"] == []
