"""Поиск по учебникам для чата: только учебники, не удалённые, с фрагментом вокруг совпадения.

Ребёнок получает из этого поиска текстовую цитату с файлом и номером страницы; страницы как
картинки и сам PDF ему не отдаются (SPEC-1.0 1.3 правило 3).
"""

import re
import sqlite3

from app.search.index import _match_expression
from app.search.normalize import normalize_tokens

MAX_FRAGMENTS = 4
FRAGMENT_CHARS = 400
_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")


def _window(text: str, tokens: set[str], chars: int) -> str:
    """Отрезок страницы вокруг первого слова запроса, по границам слов."""
    flat = " ".join(text.split())
    start = 0
    for m in _WORD.finditer(flat):
        if normalize_tokens(m.group(0))[0] in tokens:
            start = max(0, m.start() - 80)
            break
    if start:
        start = flat.find(" ", start) + 1 or start  # не с середины слова
    piece = flat[start : start + chars]
    if start + chars < len(flat):
        piece = piece.rsplit(" ", 1)[0] if " " in piece else piece
    return ("…" if start else "") + piece + ("…" if start + chars < len(flat) else "")


def search_textbooks(
    conn: sqlite3.Connection, query: str, limit: int = MAX_FRAGMENTS, chars: int = FRAGMENT_CHARS
) -> list[dict]:
    """[{id, material_id, page, file, quote}], лучшие первыми; пустой запрос - пустая выдача.
    id - серверный номер строки индекса: только на такие id модель может ссылаться."""
    tokens = list(dict.fromkeys(t for t in normalize_tokens(query) if t))
    if not tokens:
        return []
    rows = conn.execute(
        "SELECT p.rowid, p.material_id, p.page, t.text, m.file FROM material_page p "
        "JOIN material_page_text t ON t.material_id = p.material_id AND t.page = p.page "
        "JOIN material m ON m.id = p.material_id "
        "WHERE material_page MATCH ? AND m.kind = 'textbook' AND m.deleted_at IS NULL "
        "ORDER BY bm25(material_page) LIMIT ?",
        (_match_expression(tokens), limit),
    ).fetchall()
    wanted = set(tokens)
    return [
        {
            "id": r[0],
            "material_id": r[1],
            "page": r[2],
            "file": str(r[4]).rsplit("/", 1)[-1],
            "quote": _window(r[3], wanted, chars),
        }
        for r in rows
    ]
