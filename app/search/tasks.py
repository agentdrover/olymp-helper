"""Поиск ребёнка: только проверенные и опубликованные задания, только их условие."""

import json
import sqlite3

from app.ingest.publish import is_published
from app.search.index import MAX_RESULTS, SNIPPET_CHARS
from app.search.normalize import normalize_tokens


def search_published_tasks(
    conn: sqlite3.Connection, query: str, limit: int = MAX_RESULTS
) -> list[dict]:
    """Условие и варианты опубликованных заданий; ключей, ответов модели и страниц
    материалов здесь нет и быть не может."""
    tokens = set(normalize_tokens(query))
    if not tokens:
        return []
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    out = []
    for task in cur.execute("SELECT * FROM task_item ORDER BY id").fetchall():
        if not is_published(task):
            continue
        options = json.loads(task["options"]) if task["options"] else None
        texts = [task["text"]]
        if isinstance(options, list):
            texts += [o["text"] for o in options]
        haystack = set(normalize_tokens(" ".join(texts)))
        if tokens & haystack:
            out.append(
                {
                    "id": task["id"],
                    "task_id": task["id"],
                    "number": task["source_number"],
                    "snippet": " ".join(task["text"].split())[:SNIPPET_CHARS],
                }
            )
        if len(out) >= limit:
            break
    return out
