"""Проверка структуры ответа модели при разбиении страниц на задания."""

import re

from app.ingest.leaks import is_drawing, split_elements
from app.ingest.topics import accept

TYPES = ("single", "multiple", "match", "open", "numeric")
NO_OPTIONS = ("open", "numeric")
MAX_ANSWER_TEXT = 4000
# Номер задания: цифры, необязательные «.цифры» и необязательная буква; ничего другого.
NUMBER = re.compile(r"^\d{1,4}(\.\d{1,3})?[A-Za-zА-Яа-я]?$")


class InvalidStructure(Exception):
    """Ответ модели не соответствует схеме; текст читает родитель. safe - то же без единого
    значения из ответа модели: только он годится для повторной просьбы модели."""

    def __init__(self, message: str, safe: str | None = None):
        super().__init__(message)
        self.safe = safe or message


def _fail(where: str, what: str, safe: str | None = None):
    """safe - фиксированная формулировка категории, если what содержит значение из ответа."""
    raise InvalidStructure(
        f"Модель вернула неверную структуру ({where}): {what}.",
        f"Модель вернула неверную структуру ({where}): {safe or what}.",
    )


def _option_list(value, where: str, minimum: int) -> list[dict]:
    if not isinstance(value, list) or len(value) < minimum:
        _fail(where, f"нужен список из {minimum} или больше вариантов")
    out, labels = [], set()
    for item in value:
        if not isinstance(item, dict):
            _fail(where, "вариант должен быть объектом")
        label, text = item.get("label"), item.get("text")
        if not isinstance(label, str) or not label.strip():
            _fail(where, "у варианта нет обозначения")
        if not isinstance(text, str) or not text.strip():
            _fail(where, f"у варианта {label} нет текста", "у варианта нет текста")
        if label in labels:
            _fail(where, f"обозначение {label} повторяется", "обозначение варианта повторяется")
        labels.add(label)
        out.append({"label": label.strip(), "text": text.strip()})
    return out


def _options(kind: str, value, where: str):
    if kind in NO_OPTIONS:
        return None
    if kind == "match":
        if not isinstance(value, dict):
            _fail(where, "для соответствия нужны списки left и right")
        return {
            "left": _option_list(value.get("left"), where + ".left", 2),
            "right": _option_list(value.get("right"), where + ".right", 2),
        }
    return _option_list(value, where, 2)


def _label(raw, where: str, name: str, limit: int) -> str | None:
    """Необязательная короткая метка (раздел, вариант, этап): одна строка, не длиннее limit."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        _fail(where, f"{name} должен быть строкой")
    text = " ".join(raw.split())
    if len(text) > limit:
        _fail(where, f"{name} длиннее {limit} знаков")
    return text or None


def validate_section_at_end(payload) -> dict | None:
    """Раздел, вариант и этап, действующие в конце порции; None, если модель их не назвала."""
    raw = payload.get("section_at_end") if isinstance(payload, dict) else None
    if raw is None:
        return None
    where = "section_at_end"
    if not isinstance(raw, dict):
        _fail(where, "ожидался объект")
    state = {
        "section": _label(raw.get("section"), where, "раздел", 120),
        "variant": _label(raw.get("variant"), where, "вариант", 20),
        "stage": _label(raw.get("stage"), where, "этап", 60),
    }
    return state if any(state.values()) else None


def check_options(kind: str, value):
    """Варианты, введённые родителем: те же правила, что и для ответа модели."""
    return _options(kind, value, "варианты")


def validate_task(raw, i: int, pages: set[int], catalog=()) -> dict:
    """Одно задание из ответа модели; i - его номер в списке (с нуля)."""
    where = f"задание {i + 1}"
    if not isinstance(raw, dict):
        _fail(where, "ожидался объект")
    kind = raw.get("type")
    if kind not in TYPES:
        _fail(where, f"неизвестный тип {kind!r}", "неизвестный тип задания")
    number = raw.get("number")
    if isinstance(number, int) and not isinstance(number, bool):
        number = str(number)
    if not isinstance(number, str) or not NUMBER.fullmatch(number.strip()):
        _fail(where, "номер задания должен быть числом, например 12, 5.2 или 3а")
    # condition - условие БЕЗ ответов; text - прежнее имя того же поля.
    text = raw.get("condition", raw.get("text"))
    if not isinstance(text, str) or not text.strip():
        _fail(where, "пустое условие")
    answer_text = raw.get("answer_text")
    if answer_text is not None and not isinstance(answer_text, str):
        _fail(where, "answer_text должен быть строкой")
    if answer_text and len(answer_text) > MAX_ANSWER_TEXT:
        _fail(where, f"answer_text длиннее {MAX_ANSWER_TEXT} знаков")
    text, elements = split_elements(text.strip())
    if not text:
        _fail(where, "пустое условие")
    if elements:  # «Элементы ответа» остались в условии: это часть ответа
        answer_text = f"{answer_text}\n{elements}" if answer_text else elements
    if is_drawing(text):  # ответ рисуется: проверить его нельзя, тип - открытое
        kind = "open"
    task_pages = raw.get("pages")
    if (
        not isinstance(task_pages, list)
        or not task_pages
        or not all(isinstance(p, int) and not isinstance(p, bool) for p in task_pages)
    ):
        _fail(where, "нужен непустой список номеров страниц")
    if not set(task_pages) <= pages:
        _fail(where, "указана страница вне переданного текста")
    points = raw.get("points")
    if points is not None and (isinstance(points, bool) or not isinstance(points, int | float)):
        _fail(where, "points должен быть числом")
    return {
        "number": number.strip(),
        "type": kind,
        "text": text,
        "answer_text": (answer_text or "").strip() or None,
        "options": _options(kind, raw.get("options"), where),
        "pages": sorted(set(task_pages)),
        "refers_to_figure": bool(raw.get("refers_to_figure", False)),
        "points": points,
        "topic": accept(raw.get("topic"), catalog),
        "section": _label(raw.get("section"), where, "раздел", 120),
        "variant": _label(raw.get("variant"), where, "вариант", 20),
        "stage": _label(raw.get("stage"), where, "этап", 60),
    }


def validate_tasks(payload, pages: set[int], catalog=()) -> list[dict]:
    """Список заданий из ответа модели; pages - номера страниц порции; catalog - коды тем:
    тема вне каталога отбрасывается (задание остаётся не размеченным)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        _fail("корень", "ожидался объект с полем tasks")
    return [validate_task(raw, i, pages, catalog) for i, raw in enumerate(payload["tasks"])]
