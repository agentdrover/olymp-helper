import sqlite3

from app.db import SCHEMA_VERSION, connect, migrate

TABLES = {
    "material",
    "task_item",
    "study_session",
    "event",
    "settings",
    "material_page",
    "model_run",
}
INDEXES = {
    "idx_model_run_day",
    "idx_task_item_material",
    "idx_task_item_status",
    "idx_event_session",
    "idx_event_task_item",
    "idx_study_session_open",
}


def _dump(conn):
    """Schema plus all rows of every ordinary table, as a comparable value."""
    schema = conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    data = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'material_page_%' ORDER BY name"
    ).fetchall():
        data[name] = conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
    return schema, data


def test_ac2(tmp_path):
    path = tmp_path / "olymp.db"
    conn = connect(path)
    migrate(conn)

    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert INDEXES <= idx
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    # FTS5: virtual table of the expected module and tokenizer, and it really matches.
    fts_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='material_page'").fetchone()[0]
    assert "USING fts5" in fts_sql and "unicode61" in fts_sql
    conn.execute(
        "INSERT INTO material_page(material_id, page, text) VALUES (1, 3, 'Клетка и ядро')"
    )
    hit = conn.execute("SELECT page FROM material_page WHERE material_page MATCH 'ядро'").fetchall()
    assert hit == [(3,)]
    conn.execute("DELETE FROM material_page")

    # Constraints are enforced by the schema, not by the application.
    conn.execute(
        "INSERT INTO material(file, sha256, kind, import_status) "
        "VALUES ('a.pdf', 'h1', 'tasks', 'queued')"
    )
    bad = [
        "INSERT INTO material(file, sha256, kind, import_status) VALUES ('b.pdf','h2','video','queued')",
        "INSERT INTO material(file, sha256, kind, import_status) VALUES ('c.pdf','h1','tasks','queued')",
        "INSERT INTO task_item(material_id, type, text) VALUES (999, 'single', 'q')",
        "INSERT INTO task_item(material_id, type, text, import_status) VALUES (1, 'open', 'q', 'draft')",
        "INSERT INTO task_item(material_id, type, text, answer_key) VALUES (1, 'single', 'q', '\"a\"')",
        "INSERT INTO task_item(material_id, subject, type, text) VALUES (1, 'chemistry', 'single', 'q')",
        "INSERT INTO event(session_id, type, created_at) VALUES (999, 'chat', 't')",
        "INSERT INTO settings(id, primary_channel) VALUES (2, 'claude')",
        "UPDATE settings SET daily_run_limit = 0",
    ]
    for sql in bad:
        try:
            conn.execute(sql)
        except sqlite3.IntegrityError:
            continue
        raise AssertionError(f"constraint did not fire: {sql}")
    conn.commit()

    seed = conn.execute(
        "SELECT primary_channel, daily_run_limit, show_exam_countdown FROM settings"
    ).fetchall()
    assert seed == [("claude", 200, 0)]

    before = _dump(conn)
    versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert versions == [(v,) for v in range(1, SCHEMA_VERSION + 1)]
    conn.close()

    conn2 = connect(path)
    migrate(conn2)
    assert _dump(conn2) == before
    assert conn2.execute("SELECT version FROM schema_migrations").fetchall() == [
        (v,) for v in range(1, SCHEMA_VERSION + 1)
    ]
    conn2.close()


def test_ac2_settings_stage_date(tmp_path):
    conn = connect(tmp_path / "olymp.db")
    migrate(conn)
    assert conn.execute("SELECT stage_date FROM settings").fetchone() == (None,)
    conn.execute("UPDATE settings SET stage_date = '2027-02-14', show_exam_countdown = 1")
    conn.close()
    conn = connect(tmp_path / "olymp.db")
    migrate(conn)
    assert conn.execute("SELECT stage_date, show_exam_countdown FROM settings").fetchone() == (
        "2027-02-14",
        1,
    )
    for bad in ("14.02.2027", "2027-02-30", ""):
        try:
            conn.execute("UPDATE settings SET stage_date = ?", (bad,))
        except sqlite3.IntegrityError:
            continue
        raise AssertionError(f"stage_date accepted: {bad!r}")
