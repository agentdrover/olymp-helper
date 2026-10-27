from fastapi.testclient import TestClient

from app.ingest.names import clean_original_name, material_label
from tests.bank_helpers import PARENT, build_app, db, text_pdf, upload


def _names(tmp_path):
    conn = db(tmp_path)
    try:
        return [
            r["original_name"]
            for r in conn.execute("SELECT original_name FROM material ORDER BY id")
        ]
    finally:
        conn.close()


def test_the_original_name_is_kept_safely(tmp_path):
    assert clean_original_name("a/b\\c/Пасечник 5 кл.pdf") == "Пасечник 5 кл.pdf"
    assert clean_original_name("x\x00y\x1b\u202ez.pdf") == "xyz.pdf"
    assert clean_original_name("e\u0301.pdf") == "\u00e9.pdf"
    assert clean_original_name("e\u200d\u0301.pdf") == "\u00e9.pdf"
    assert clean_original_name("a" * 300) == "a" * 120
    assert clean_original_name("  ") is None and clean_original_name(None) is None
    assert clean_original_name("dir/") is None

    with TestClient(build_app(tmp_path)) as c:
        pdf1, pdf2, pdf3 = (text_pdf([[t]]) for t in ("one", "two", "three"))
        assert upload(c, pdf1, kind="textbook", name="Пасечник 5 кл.pdf").status_code == 202
        evil = "../../etc/<script>alert(1)</script>\x07" + "z" * 300 + ".pdf"
        assert upload(c, pdf2, kind="textbook", name="C:\\dir\\" + evil).status_code == 202
        assert (
            upload(c, pdf3, kind="textbook", name="<img src=x onerror=alert(1)>.pdf").status_code
            == 202
        )
        names = _names(tmp_path)
        assert names[0] == "Пасечник 5 кл.pdf"
        assert len(names[1]) == 120 and "/" not in names[1] and "\x07" not in names[1]
        html = c.get("/parent/materials", headers=PARENT).text
        assert "Пасечник 5 кл.pdf" in html
        assert "<img src=x" not in html and "&lt;img src=x" in html

        # duplicate under another name keeps the first name
        assert upload(c, pdf1, kind="textbook", name="other.pdf").json()["duplicate"] is True
        assert _names(tmp_path)[0] == "Пасечник 5 кл.pdf"

    files = sorted(p.name for p in tmp_path.rglob("*.pdf"))
    assert files and all(len(f) == 68 and set(f[:-4]) <= set("0123456789abcdef") for f in files)


def test_citations_show_the_name_not_the_hash(tmp_path):
    assert material_label({"original_name": "Пасечник 5 кл.pdf"}, 7) == "Пасечник 5 кл, стр. 7"
    assert material_label({"original_name": "Учебник.PDF"}, 2) == "Учебник, стр. 2"
    assert material_label({"original_name": None}, 3) == "Учебник, стр. 3"

    from app.db import SCHEMA_VERSION, _migration_files, _statements, connect, migrate

    conn = connect(tmp_path / "old.db")
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for version, path in _migration_files():
        if version >= 13:  # 0013 is the migration under test
            break
        for stmt in _statements(path.read_text(encoding="utf-8")):
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    conn.execute(
        "INSERT INTO material (file, sha256, kind, pages_total) VALUES ('materials/abc.pdf','abc','textbook',1)"
    )
    assert migrate(conn) == SCHEMA_VERSION
    conn.row_factory = __import__("sqlite3").Row
    m = conn.execute("SELECT * FROM material").fetchone()
    assert m["original_name"] is None
    label = material_label(m, 4)
    assert label == "Учебник, стр. 4" and "abc" not in label
