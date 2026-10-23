"""Проверка структуры ответа модели при разбиении страниц на задания."""

import re

TYPES = ("single", "multiple", "match", "open")
# Номер задания: цифры, необязательные «.цифры» и необязательная буква; ничего другого.
NUMBER = re.compile(r"^\d{1,4}(\.\d{1,3})?[A-Za-zА-Яа-я]?$")


class InvalidStructure(Exception):
    """Ответ модели не соответствует схеме; текст читает родитель."""


def _fail(where: str, what: str):
    raise InvalidStructure(f"Модель вернула неверную структуру ({where}): {what}.")


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
            _fail(where, f"у варианта {label} нет текста")
        if label in labels:
            _fail(where, f"обозначение {label} повторяется")
        labels.add(label)
        out.append({"label": label.strip(), "text": text.strip()})
    return out


def _options(kind: str, value, where: str):
    if kind == "open":
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


def check_options(kind: str, value):
    """Варианты, введённые родителем: те же правила, что и для ответа модели."""
    return _options(kind, value, "варианты")


def validate_tasks(payload, pages: set[int]) -> list[dict]:
    """Список заданий из ответа модели; pages - номера страниц порции."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        _fail("корень", "ожидался объект с полем tasks")
    out = []
    for i, raw in enumerate(payload["tasks"]):
        where = f"задание {i + 1}"
        if not isinstance(raw, dict):
            _fail(where, "ожидался объект")
        kind = raw.get("type")
        if kind not in TYPES:
            _fail(where, f"неизвестный тип {kind!r}")
        number = raw.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            number = str(number)
        if not isinstance(number, str) or not NUMBER.fullmatch(number.strip()):
            _fail(where, "номер задания должен быть числом, например 12, 5.2 или 3а")
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            _fail(where, "пустое условие")
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
        out.append(
            {
                "number": number.strip(),
                "type": kind,
                "text": text.strip(),
                "options": _options(kind, raw.get("options"), where),
                "pages": sorted(set(task_pages)),
                "refers_to_figure": bool(raw.get("refers_to_figure", False)),
                "points": points,
                "topic": raw.get("topic") if isinstance(raw.get("topic"), str) else None,
                "section": _label(raw.get("section"), where, "раздел", 120),
                "variant": _label(raw.get("variant"), where, "вариант", 20),
                "stage": _label(raw.get("stage"), where, "этап", 60),
            }
        )
    return out
