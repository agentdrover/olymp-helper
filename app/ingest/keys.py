"""Загруженный ключ ответов: разбор и привязка к заданию (SPEC-1.0 1.2)."""

import re
import sqlite3

from app.ingest.answers import display, normalize_answer

_LINE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})?)\s*[.)\-–—:]\s*(.+?)\s*$")
_LEADING_ZEROS = re.compile(r"^0+(?=\d)")


def normalize_number(number: str) -> str:
    return _LEADING_ZEROS.sub("", number.strip())


def parse_key_text(pages: list[tuple[int, str]]) -> dict[str, str]:
    """{номер: ответ} из строк вида «12. Б»; первое вхождение номера выигрывает."""
    out: dict[str, str] = {}
    for _, text in pages:
        for line in text.splitlines():
            m = _LINE.match(line)
            if m:
                out.setdefault(normalize_number(m.group(1)), m.group(2))
    return out


def candidate(conn: sqlite3.Connection, task) -> dict | None:
    """Ответ из загруженного ключа ТОГО ЖЕ сборника, варианта и номера; иначе None.

    Кандидат не ключ: official он становится только после подтверждения родителем.
    """
    number = normalize_number(str(task["source_number"] or ""))
    if not number:
        return None
    rows = conn.execute(
        "SELECT k.material_id, k.answer FROM key_entry k "
        "JOIN material km ON km.id = k.material_id "
        "JOIN material tm ON tm.id = ? "
        "WHERE km.kind = 'key' AND km.import_status = 'done' AND k.number = ? "
        "AND COALESCE(km.variant, '') = COALESCE(?, COALESCE(tm.variant, '')) "
        "AND COALESCE(km.year, 0) = COALESCE(tm.year, 0) "
        "AND COALESCE(km.stage, '') = COALESCE(?, COALESCE(tm.stage, '')) "
        "AND COALESCE(km.olympiad, '') = COALESCE(tm.olympiad, '') "
        "ORDER BY km.id DESC",
        (task["material_id"], number, task["variant"], task["stage"]),
    ).fetchall()
    for material_id, raw in rows:
        value = normalize_answer(raw, task["type"])
        if value is not None:
            return {"answer": display(value), "value": value, "material_id": material_id}
    return None
