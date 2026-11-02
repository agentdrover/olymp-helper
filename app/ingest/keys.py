"""Загруженный ключ ответов: разбор и привязка к заданию (SPEC-1.0 1.2)."""

import json
import logging
import re
import sqlite3

from app.ingest.answers import display, normalize_answer
from app.ingest.extract import extract_pages
from app.ingest.page_answer import fit_page, refusal_text, stored_candidate

_LINE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})?)\s*[.)\-–—:]\s*(.+?)\s*$")
_LEADING_ZEROS = re.compile(r"^0+(?=\d)")
log = logging.getLogger("app.ingest")


def normalize_number(number: str) -> str:
    return _LEADING_ZEROS.sub("", number.strip())


def parse_key_text(pages: list[tuple[int, str]]) -> dict[str, str]:
    """{номер: исходные ответы через перевод строки}; повторный номер не теряет ответ."""
    out: dict[str, list[str]] = {}
    for _, text in pages:
        for line in text.splitlines():
            m = _LINE.match(line)
            if m:
                out.setdefault(normalize_number(m.group(1)), []).append(m.group(2))
    return {number: "\n".join(answers) for number, answers in out.items()}


def _file_values(raw: str, task_type: str) -> list:
    """Различные нормализованные ответы; None сохраняет факт неразобранной строки."""
    found: list = []
    for line in raw.splitlines():
        value = normalize_answer(line, task_type)
        if value not in found:
            found.append(value)
    return found or [None]


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


def _uploaded_rows(conn: sqlite3.Connection, task) -> list:
    """Строки загруженных ключей ТОГО ЖЕ сборника, варианта и номера, новые первыми."""
    number = normalize_number(str(task["source_number"] or ""))
    if not number:
        return []
    return conn.execute(
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


def _uploaded_candidate(conn: sqlite3.Connection, task) -> dict | None:
    """Ответ из загруженного ключа ТОГО ЖЕ сборника, варианта и номера; иначе None."""
    for material_id, raw in _uploaded_rows(conn, task):
        values = _file_values(raw, task["type"])
        if len(values) == 1 and (value := values[0]) is not None:
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


class KeySourceUnavailable(Exception):
    """Исходный файл ключа не удалось перепроверить; его однозначность неизвестна."""


def _source_answers(ctx, conn, material_id: int, cache: dict) -> dict[str, str]:
    """Файл неизменяем после загрузки; читать и разбирать его один раз за обход черновиков.
    Старые записи key_entry могли потерять повторные номера, поэтому одного индекса мало."""
    if material_id not in cache:
        material = conn.execute("SELECT file FROM material WHERE id=?", (material_id,)).fetchone()
        try:
            cache[material_id] = parse_key_text(
                extract_pages(ctx.material_path(material[0]).read_bytes())
            )
        except Exception:
            log.exception("official key source %s could not be rechecked", material_id)
            cache[material_id] = None
    if cache[material_id] is None:
        raise KeySourceUnavailable(material_id)
    return cache[material_id]


def official_values(ctx, conn: sqlite3.Connection, task, cache: dict) -> list:
    """Все различные официальные ответы на задание: из каждого файла ключа и со страницы.
    Больше одного - ключ неоднозначен, None - строка ключа не разобрана. Непроверенный
    кандидат со страницы не является официальным ответом."""
    found: list = []
    for material_id, raw in _uploaded_rows(conn, task):
        original = _source_answers(ctx, conn, material_id, cache)
        number = normalize_number(str(task["source_number"] or ""))
        if number not in original:
            raise KeySourceUnavailable(material_id)
        for text in (raw, original[number]):
            for value in _file_values(text, task["type"]):
                if value not in found:
                    found.append(value)
    page = page_candidate(task)
    if page is not None and not page["unverified"] and page["value"] not in found:
        found.append(page["value"])
    return found
