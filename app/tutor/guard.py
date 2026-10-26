"""Защита вывода модели ребёнку (SPEC-1.0 1.2, 1.3 правило 3, 1.5).

1. Разметка: всё, что похоже на HTML, вырезается, остальное экранируется; из Markdown
   разрешены только абзацы, списки, **жирный**, *курсив* и `код`. Ссылки и картинки - в текст.
2. Верный вариант: подсказка или разбор, которые его называют, заменяются общей подсказкой.
   Фильтр по обозначениям - дополнительная мера: главная защита - сервер не отдаёт ответ.
3. teacher_note: его фразы в выводе модели ребёнку не показываются никогда.
"""

import html
import json
import re

from app.ingest.answers import normalize_answer

GENERIC = {
    "hint1": "Перечитай условие. О чём именно спрашивают и что в нём главное?",
    "hint2": "Вспомни, что ты знаешь по этой теме, и проверь каждый вариант: "
    "что в нём подходит к условию, а что нет и почему.",
    "mistake": "Посмотри ещё раз, почему этот вариант мог показаться подходящим, "
    "и сверь его с условием по словам.",
    "explain": "Объяснение сейчас недоступно: вернись к условию и разбору выше.",
}

_BLOCK = re.compile(
    r"<\s*(script|style|iframe|object|embed|noscript)\b.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_TAG = re.compile(r"</?[A-Za-z!?][^>]*>?")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", re.DOTALL)
_CODE = re.compile(r"`([^`\n]+)`")
_LIST = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")


def strip_markup(text: str) -> str:
    text = _BLOCK.sub("", text or "")
    text = _TAG.sub("", text)
    text = _IMAGE.sub(lambda m: m.group(1), text)
    return _LINK.sub(lambda m: m.group(1), text)


def _inline(line: str) -> str:
    line = html.escape(line, quote=True)
    line = _CODE.sub(r"<code>\1</code>", line)
    line = _BOLD.sub(r"<strong>\1</strong>", line)
    return _ITALIC.sub(r"<em>\1</em>", line)


def render_markdown(text: str) -> str:
    """Безопасный HTML из Markdown модели."""
    blocks, para, items = [], [], []

    def flush():
        if para:
            blocks.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()
        if items:
            blocks.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in strip_markup(text).splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        match = _LIST.match(line)
        if match:
            if para:
                flush()
            items.append(match.group(1))
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    return "".join(blocks)


def plain(text: str) -> str:
    """Текст без разметки - для проверки утечек."""
    text = strip_markup(text)
    return re.sub(r"[*_`#>]+", "", text)


# ----------------------------------------------------------------------- утечки

_TOKEN = re.compile(r"(?<![\w])(?:[A-ZА-ЯЁ]|\d{1,2})(?![\w])")
_AFTER_WORD = re.compile(
    r"(?:вариант\w*|ответ\w*|пункт\w*|цифр\w*|№|номер\w*)\W{0,3}([A-Za-zА-Яа-яЁё])(?![\w])",
    re.IGNORECASE,
)
_CUE = re.compile(
    r"(?<![Нн]е)(?<![Нн]е )(?:верн\w*|правильн\w*|подходит|подходящ\w*|ответ\w*|решени\w*|"
    r"выбира\w*|выбери\w*|единственн\w*)",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])")


def _label(raw: str) -> str | None:
    return normalize_answer(raw, "single")


def _mentions(text: str) -> list[tuple[str, int, int]]:
    found = []
    for m in _TOKEN.finditer(text):
        label = _label(m.group(0))
        if label:
            found.append((label, m.start(), m.end()))
    for m in _AFTER_WORD.finditer(text):
        label = _label(m.group(1))
        if label:
            found.append((label, m.start(1), m.end(1)))
    return found


def _option_texts(options) -> dict[str, str]:
    items = []
    if isinstance(options, list):
        items = options
    elif isinstance(options, dict):
        items = [o for side in options.values() for o in side]
    return {_label(o["label"]) or o["label"]: " ".join(o["text"].lower().split()) for o in items}


def _flat(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    return []


def _norm(text: str) -> str:
    return re.sub(r"[\s\-–—:]+", " ", text.lower()).strip()


def note_segments(note: str | None) -> list[str]:
    parts = re.split(r"[.;,\n]+", note or "")
    return [_norm(p) for p in parts if len(_norm(p)) >= 5]


def leaks_note(text: str, note: str | None) -> bool:
    body = _norm(text)
    return any(seg in body for seg in note_segments(note))


def names_answer(text: str, task_type: str, options, reference) -> bool:
    """Текст называет вариант верным или содержит эталонный ответ."""
    body = plain(text)
    mentions = _mentions(body)
    texts = _option_texts(options)
    lowered = " ".join(body.lower().split())
    # 1. любое обозначение рядом со словами «верный», «ответ», «подходит» (не «неверный»)
    for _, start, end in mentions:
        if _CUE.search(body[max(0, start - 30) : end + 30]):
            return True
    if reference is None:
        return False
    # 2. эталонный ответ
    if task_type == "numeric":
        want = normalize_answer(reference, "numeric")
        return any(normalize_answer(n, "numeric") == want for n in _NUMBER.findall(body))
    if task_type == "match" and isinstance(reference, dict):
        for left, right in reference.items():
            pair = re.compile(
                rf"(?<![\w]){re.escape(left)}\W{{0,8}}{re.escape(right)}(?![\w])", re.IGNORECASE
            )
            if pair.search(body):
                return True
        return False
    named = set(_flat(reference))
    if any(label in named for label, _, _ in mentions):
        return True
    # 3. текст верного варианта
    return any(
        len(texts.get(label, "")) >= 4 and texts[label] in lowered
        for label in named
        if label in texts
    )


def finish(
    text: str,
    kind: str,
    task_type: str,
    options,
    reference,
    note: str | None,
    *,
    reveal: bool = False,
) -> tuple[str, bool]:
    """(безопасный HTML, заменено ли общей подсказкой). reveal=True - решение уже открыто:
    ответ называть можно, пояснение к рисунку - нет."""
    body = plain(text)
    bad = leaks_note(body, note) or (
        not reveal and names_answer(body, task_type, options, reference)
    )
    if bad or not body.strip():
        return render_markdown(GENERIC.get(kind, GENERIC["explain"])), True
    return render_markdown(text), False


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
