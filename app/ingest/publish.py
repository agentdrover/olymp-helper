"""Что видит ребёнок: условия публикации и режим подсказок (SPEC-1.0 1.1, 1.2)."""

import json
import re

from app.ingest.answers import key_value
from app.ingest.leaks import condition_leaks
from app.ingest.store import figure_of, options_of
from app.ingest.tiles import stored_view

PUBLISHED_STATUS = "checked"


def has_key(task) -> bool:
    return task["answer_key"] is not None


def leak_of(task) -> list[str]:
    """Части ответа со страницы, оставшиеся в условии: пока они там, задание не публикуется."""
    return condition_leaks(task["text"], task["answer_text"], task["type"])


def leak_notice(task) -> str | None:
    found = leak_of(task)
    if not found:
        return None
    return (
        "В условии остался текст ответа: «" + "», «".join(found) + "». "
        "Поправьте условие: без правки задание не публикуется."
    )


def is_published(task) -> bool:
    """checked, не исключено, в условии нет текста ответа; рисунок - только с подтверждённым ключом и подтверждённым рисунком;
    без рисунка нужен ключ или model_answer."""
    if task["import_status"] != PUBLISHED_STATUS or task["excluded"] or leak_of(task):
        return False
    figure = figure_of(task)
    if figure is not None:
        return bool(figure.get("confirmed")) and has_key(task)
    return has_key(task) or task["model_answer"] is not None


# ------------------------------------------------------------ teacher_note sufficiency

_DIGITS = re.compile(r"(?<![\w])\d{1,2}(?![\w])")
_LETTER = re.compile(r"(?<![\w])[A-ZА-ЯЁ](?![\w])")
_RANGE = re.compile(r"(\d{1,2})\s*[–—-]\s*(\d{1,2})")


def designations(options, extended: bool = False) -> set[str]:
    """Цифры и одиночные заглавные буквы в тексте вариантов (то, что обозначено на рисунке)."""
    texts: list[str] = []
    if isinstance(options, list):
        texts = [o.get("text", "") for o in options]
    elif isinstance(options, dict):
        texts = [o.get("text", "") for side in options.values() for o in side]
    found: set[str] = set()
    for text in texts:
        found.update(_DIGITS.findall(text))
        found.update(_LETTER.findall(text))
    # extended: только для рисунков, подтверждённых после #2110 (отметка items_v2); у записей,
    # опубликованных раньше, правило режима не меняется.
    if extended and isinstance(options, dict):  # номер левого элемента match - обозначение
        for item in options.get("left") or []:
            label = str(item.get("label", "")).strip()
            if _DIGITS.fullmatch(label) or _LETTER.fullmatch(label):
                found.add(label)
    return found


def ordered_designations(options, extended: bool = True) -> list[str]:
    """Обозначения рисунка по порядку: сначала цифры, затем буквы."""
    return sorted(
        designations(options, extended),
        key=lambda s: (not s.isdigit(), int(s) if s.isdigit() else 0, s),
    )


ASSIST_CAPTIONS = {
    "hints": (
        "С подсказками",
        "Ребёнок решает с подсказками помощника, как обычное задание.",
    ),
    "key_only": (
        "Только проверка по ключу",
        (
            "Подсказок нет: две проверки, затем «Показать ответ». Чтобы включить подсказки, "
            "опишите все рисунки в шаге 3 и подтвердите описание."
        ),
    ),
}


def assist_caption(mode: str | None) -> dict:
    """Подпись режима для родителя; сам режим считает assist_mode."""
    label, hint = ASSIST_CAPTIONS.get(mode or "key_only", ASSIST_CAPTIONS["key_only"])
    return {"assist": mode, "label": label, "hint": hint}


def named_in_note(note: str) -> set[str]:
    named = set(_DIGITS.findall(note)) | set(_LETTER.findall(note))
    for lo, hi in _RANGE.findall(note):
        if 0 < int(lo) <= int(hi) <= 99:
            named.update(str(n) for n in range(int(lo), int(hi) + 1))
    return named


def missing_designations(options, note: str, extended: bool = False) -> list[str]:
    missing = designations(options, extended) - named_in_note(note or "")
    return sorted(missing, key=lambda s: (not s.isdigit(), int(s) if s.isdigit() else 0, s))


def assist_mode(task) -> str | None:
    """'hints' | 'key_only'. Для рисунка подсказки включены, только если teacher_note достаточна
    и родитель отметил «в пояснении названо, что обозначено»."""
    figure = figure_of(task)
    if figure is None:
        return "hints"
    missing = missing_designations(
        options_of(task), figure.get("teacher_note", ""), bool(figure.get("items_v2"))
    )
    if missing or not figure.get("teacher_note_ack"):
        return "key_only"
    return "hints"


def parent_notice(task) -> str | None:
    if assist_mode(task) != "key_only" or figure_of(task) is None:
        return None
    figure = figure_of(task)
    missing = missing_designations(
        options_of(task), figure.get("teacher_note", ""), bool(figure.get("items_v2"))
    )
    parts = []
    if missing:
        parts.append("в пояснении к рисунку не названо: " + ", ".join(missing))
    if not figure.get("teacher_note_ack"):
        parts.append("нет отметки «в пояснении названо, что обозначено»")
    return "К заданию не будет подсказок, только проверка по ключу: " + "; ".join(parts) + "."


def publication(task) -> dict:
    return {
        "published": is_published(task),
        "assist": assist_mode(task),
        "parent_notice": parent_notice(task),
        "blocked": leak_notice(task),
    }


def child_view(task, material) -> dict:
    """Единственное представление задания для ребёнка: без ключа, model_answer и teacher_note."""
    figure = figure_of(task)
    view = {
        "id": task["id"],
        "number": task["source_number"],
        "type": task["type"],
        "text": task["text"],
        "options": options_of(task),
        "assist": assist_mode(task),
        "source": _source_label(task, material),
        "figure": None,
    }
    if figure is not None:
        view["figure"] = {"url": f"/tasks/{task['id']}/figure.png", "alt": figure.get("alt", "")}
        view["figure"]["tiles"] = [
            {
                "url": f"/tasks/{task['id']}/figure/{tile['id']}.png",
                "n": number,
                "label": tile.get("label"),
            }
            for number, tile in enumerate(stored_view(figure), 1)
        ]
    return view


def source_caption(material, page) -> str:
    """Подпись источника рисунка: одна функция для предпросмотра и детских маршрутов.

    Известный год - «сборник 2022/23»; иначе имя загруженного файла (без .pdf); иначе «Сборник».
    Номер страницы дописывается, если он известен. Хеш файла в подпись не попадает."""
    year = material["year"]
    name = material["original_name"] if "original_name" in material.keys() else None  # noqa: SIM118
    if year:
        base = f"сборник {year}/{str(year + 1)[-2:]}"
    elif name:
        base = name[:-4].strip() if name.lower().endswith(".pdf") and len(name) > 4 else name
    else:
        base = "Сборник"
    return base + (f", стр. {page}" if page else "")


def _source_label(task, material) -> str:
    if material is None:  # сгенерированное задание: сборника и страницы нет
        return "составлено помощником, проверено родителем"
    return source_caption(material, task["page_from"])


def key_display_value(task):
    return key_value(task["answer_key"])


__all__ = ["child_view", "has_key", "is_published", "json", "publication", "source_caption"]
