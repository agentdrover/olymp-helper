"""Постраничный индекс учебников в SQLite FTS5 (SPEC-1.0 1.7)."""

import sqlite3

from app.search.normalize import normalize_tokens

MAX_RESULTS = 20
SNIPPET_CHARS = 240


def index_pages(conn: sqlite3.Connection, material_id: int, pages: list[tuple[int, str]]) -> None:
    """Заново проиндексировать страницы материала; исходный текст хранится рядом для показа."""
    conn.execute("DELETE FROM material_page WHERE material_id = ?", (material_id,))
    conn.execute("DELETE FROM material_page_text WHERE material_id = ?", (material_id,))
    for page, text in pages:
        conn.execute(
            "INSERT INTO material_page (material_id, page, text) VALUES (?, ?, ?)",
            (material_id, page, " ".join(normalize_tokens(text))),
        )
        conn.execute(
            "INSERT INTO material_page_text (material_id, page, text) VALUES (?, ?, ?)",
            (material_id, page, text),
        )


def _match_expression(tokens: list[str]) -> str:
    # Каждый токен - строковый литерал FTS5; ввод пользователя синтаксиса MATCH не несёт.
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def search(conn: sqlite3.Connection, query: str, limit: int = MAX_RESULTS) -> list[dict]:
    """Страницы, подходящие под запрос, лучшие первыми; пустой запрос - пустая выдача."""
    tokens = list(dict.fromkeys(normalize_tokens(query)))
    if not tokens:
        return []
    rows = conn.execute(
        "SELECT p.rowid, p.material_id, p.page, t.text FROM material_page p "
        "JOIN material_page_text t ON t.material_id = p.material_id AND t.page = p.page "
        "WHERE material_page MATCH ? ORDER BY bm25(material_page) LIMIT ?",
        (_match_expression(tokens), limit),
    ).fetchall()
    return [
        {
            "id": r[0],
            "material_id": r[1],
            "page": r[2],
            "snippet": " ".join(r[3].split())[:SNIPPET_CHARS],
        }
        for r in rows
    ]
