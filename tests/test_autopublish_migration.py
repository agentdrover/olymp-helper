"""Upgrade the deployed schema without changing existing tasks or key candidates."""

from app import db


def test_existing_main_database_upgrades_without_losing_data(tmp_path, monkeypatch):
    files = db._migration_files()
    monkeypatch.setattr(db, "_migration_files", lambda: [f for f in files if f[0] <= 16])
    conn = db.connect(tmp_path / "olymp.db")
    try:
        assert db.migrate(conn) == 16
        conn.execute(
            "INSERT INTO material(id,file,sha256,kind) VALUES (1,'old.pdf','hash','tasks')"
        )
        conn.execute(
            "INSERT INTO task_item(id,material_id,type,text,topic) "
            "VALUES (1,1,'single','Сохранённое условие','b10')"
        )
        conn.execute("INSERT INTO key_entry(material_id,number,answer) VALUES (1,'1','Б')")
        before = conn.execute("SELECT * FROM task_item").fetchone()
        settings = conn.execute("SELECT * FROM settings").fetchall()
        monkeypatch.setattr(db, "_migration_files", lambda: [f for f in files if f[0] <= 17])
        assert db.migrate(conn) == 17
        assert conn.execute("SELECT * FROM task_item").fetchone() == (*before, 0, None)
        monkeypatch.setattr(db, "_migration_files", lambda: files)  # later migrations stack on top
        latest = db.migrate(conn)
        assert latest == db.SCHEMA_VERSION >= 17
        assert conn.execute("SELECT * FROM task_item").fetchone() == (
            *before,
            0,
            None,
            0,
            *[None] * 6,
        )
        assert conn.execute("SELECT * FROM settings").fetchall() == settings
        assert conn.execute("SELECT number,answer FROM key_entry").fetchall() == [("1", "Б")]
        assert db.migrate(conn) == latest
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
