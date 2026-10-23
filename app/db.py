"""SQLite (WAL) и миграции. Версия схемы = номер последней применённой миграции."""

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _migration_files() -> list[tuple[int, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _NAME.match(path.name)
        if match is None:
            raise RuntimeError(f"bad migration file name: {path.name}")
        found.append((int(match.group(1)), path))
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migration numbers must be 1..N without gaps: {versions}")
    return found


SCHEMA_VERSION = len(_migration_files())


def connect(path: str | Path, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=check_same_thread)
    conn.execute("PRAGMA foreign_keys = ON")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if mode.lower() != "wal":
        raise RuntimeError(f"cannot enable WAL, journal_mode={mode}")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Применить недостающие миграции; повторный запуск ничего не меняет."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for version, path in _migration_files():
        if version in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript коммитит неявно, поэтому выполняем по одному оператору.
            for stmt in _statements(path.read_text(encoding="utf-8")):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return schema_version(conn)


def _statements(script: str):
    buf = ""
    for line in script.splitlines(keepends=True):
        if line.lstrip().startswith("--"):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            yield buf.strip()
            buf = ""
    if buf.strip():
        raise RuntimeError("migration ends with an incomplete statement")
