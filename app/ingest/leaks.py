"""Текст ответа в том, что видит ребёнок: условие и вырезка рисунка (SPEC-1.0 1.1, 1.2).

Сравнение идёт по нормализованным целым словам: регистр, «ё», знаки и пробелы не важны.
Одно обозначение или один знак - не текст ответа: его закрывает проверка меток.
"""

import re

from app.ingest.answers import normalize_answer

_WORD = re.compile(r"[0-9a-zа-я]+")
# «1 Стебель», «10 баллов»: число и слова при нём - самостоятельная часть ответа.
_ATOM = re.compile(r"\d+(?:[.,]\d+)?\s+[^\W\d_]+(?:\s+[^\W\d_]+)*")
_PIECES = re.compile(r"[\n;]+")
ELEMENTS = re.compile(r"элементы\s+ответа", re.IGNORECASE)


def norm(text: str) -> str:
    return " ".join(_WORD.findall(str(text).lower().replace("ё", "е")))


def significant(unit: str) -> bool:
    return len(unit.replace(" ", "")) >= 2


def contains_phrase(haystack: str, phrase: str) -> bool:
    return f" {phrase} " in f" {haystack} "


def answer_units(text: str | None) -> list[str]:
    """Что считать текстом ответа: весь текст, его строки и пары «число + слова»."""
    if not text:
        return []
    candidates = [text, *_PIECES.split(text), *_ATOM.findall(text)]
    units: list[str] = []
    for item in candidates:
        unit = norm(item)
        if significant(unit) and unit not in units:
            units.append(unit)
    return units


_MARK = r"(?:ответ[а-я]*|=|→|->)\s*[:\-–—]?\s*"
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])")


def short_tokens(answer_text: str | None) -> list[str]:
    """Однознаковые слова ответа («Б», «7»): сами по себе они есть в любом тексте."""
    out: list[str] = []
    for word in norm(answer_text or "").split():
        if len(word) == 1 and word not in out:
            out.append(word)
    return out


def _stands_as_answer(condition: str, token: str) -> bool:
    """Короткий ответ стоит как ответ: после «Ответ», «=», «→»; один в ячейке таблицы;
    один в последней строке."""
    whole = rf"(?<![\w]){re.escape(token)}(?![\w])"
    if re.search(_MARK + whole, condition.lower().replace("ё", "е")):
        return True
    lines = [line.strip() for line in condition.splitlines() if line.strip()]
    if not lines:
        return False
    cells = [cell for line in lines if "|" in line for cell in line.split("|")]
    return any(norm(cell) == token for cell in cells) or norm(lines[-1]) == token


def _numbers(text: str) -> set[str]:
    return {n for raw in _NUMBER.findall(text) if (n := normalize_answer(raw, "numeric"))}


def condition_leaks(condition: str, answer_text: str | None, task_type: str | None = None):
    """Части ответа со страницы, оставшиеся в условии.

    Длинные части ищутся целыми словами где угодно. Короткие («Б», «7») по контексту: после
    маркера ответа, одни в ячейке или в последней строке. У задания типа numeric число-ответ
    запрещено в условии в любом месте."""
    text = norm(condition)
    found = [u for u in answer_units(answer_text) if contains_phrase(text, u)]
    for token in short_tokens(answer_text):
        if token not in found and _stands_as_answer(condition, token):
            found.append(token)
    if task_type == "numeric":
        for number in sorted(_numbers(answer_text or "") & _numbers(condition)):
            if number not in found:
                found.append(number)
    return found


def crop_units(answer_text: str | None, keys: list[str], options) -> list[str]:
    """Тексты, которых не должно быть в вырезке: ответ со страницы, ключ и тексты вариантов.
    Вариант из одних цифр и знаков - обозначение на рисунке, а не ответ: он не берётся."""
    out = list(answer_units(answer_text))
    for token in short_tokens(answer_text):
        if token not in out:
            out.append(token)
    for key in keys:
        unit = norm(key)
        if unit and unit not in out:
            out.append(unit)
    texts: list[str] = []
    if isinstance(options, list):
        texts = [o.get("text", "") for o in options]
    elif isinstance(options, dict):
        texts = [o.get("text", "") for side in options.values() for o in side]
    for text in texts:
        unit = norm(text)
        if significant(unit) and re.search(r"[a-zа-я]", unit) and unit not in out:
            out.append(unit)
    return out


def split_elements(condition: str) -> tuple[str, str | None]:
    """«Элементы ответа» не входят в условие: (условие, отрезанная часть или None)."""
    found = ELEMENTS.search(condition)
    if not found:
        return condition, None
    return condition[: found.start()].strip(), condition[found.start() :].strip()


DRAWING = re.compile(r"нарису|начерти|изобразите\s+(?:схем|рисун)|зарисуй|вычертит", re.IGNORECASE)


def is_drawing(condition: str) -> bool:
    return bool(DRAWING.search(condition))
