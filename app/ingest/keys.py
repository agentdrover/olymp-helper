"""Загруженный ключ ответов: разбор и привязка к заданию (SPEC-1.0 1.2)."""

import json
import re
import sqlite3

from app.ingest.answers import display, normalize_answer
from app.ingest.page_answer import fit_page, refusal_text, stored_candidate

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


def page_candidate(task) -> dict | None:
    """Ответ, напечатанный на странице самого задания (answer_text), приведённый к заданию.

    Форма, которую принимал прежний разбор, - как раньше (unverified=False). Новая форма
    (reason extended_form) помечена unverified=True: official она не становится."""
    options = json.loads(task["options"]) if task["options"] else None
    fit = fit_page(task["answer_text"], task["type"], options)
    if fit.value is None:
        return None
    return {
        "answer": display(fit.value),
        "value": fit.value,
        "material_id": task["material_id"],
        "source": "page",
        "reason": "extended_form" if fit.extended else "legacy_form",
        "revision": task["revision"],
        "unverified": fit.extended,
    }


def model_candidate(conn: sqlite3.Connection, task) -> dict | None:
    """Ответ со страницы, приведённый моделью-нормализатором и прошедший строгую проверку.
    Всегда unverified: чтение карточки модель не вызывает, здесь только сохранённое."""
    value = stored_candidate(conn, task)
    if value is None:
        return None
    return {
        "answer": display(value),
        "value": value,
        "material_id": task["material_id"],
        "source": "page_model",
        "reason": "model_normalized",
        "revision": task["revision"],
        "unverified": True,
    }


def page_refusal(conn: sqlite3.Connection, task) -> str | None:
    """Почему ответ, напечатанный на странице, не стал кандидатом: фраза для родителя.
    None, если ключ или кандидат есть либо разбирать нечего (ответа на странице нет, открытое)."""
    if task["answer_key"] is not None or task["type"] == "open":
        return None
    if not str(task["answer_text"] or "").strip() or candidate(conn, task) is not None:
        return None
    options = json.loads(task["options"]) if task["options"] else None
    return refusal_text(fit_page(task["answer_text"], task["type"], options).reason)


def candidate(conn: sqlite3.Connection, task) -> dict | None:
    """Кандидат ключа: загруженный ключ, затем ответ со страницы (правило), затем модель.

    Кандидат не ключ: official он становится только после подтверждения родителем, а при
    unverified=True - только явного (см. review.confirm). Контракт полей - в page_answer."""
    return _uploaded_candidate(conn, task) or page_candidate(task) or model_candidate(conn, task)


def _uploaded_candidate(conn: sqlite3.Connection, task) -> dict | None:
    """Ответ из загруженного ключа ТОГО ЖЕ сборника, варианта и номера; иначе None."""
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
            return {
                "answer": display(value),
                "value": value,
                "material_id": material_id,
                "source": "key_file",
                "reason": None,
                "revision": task["revision"],
                "unverified": False,
            }
    return None
