"""Строгая проверка ответа и разбор ответа со страницы задания (K5e1, #2134).

Один проверяющий на всех: правило разбора (`fit_page`), нормализатор через модель
(`page_normalizer`), будущий #2135 и решатели зовут `check_answer` / `build_value`.

Контракт кандидата (см. `app.ingest.keys.candidate`):
    value       нормализованный ответ (str | list | dict);
    source      "key_file" | "page" | "page_model";
    reason      "legacy_form" | "extended_form" | "model_normalized" | None (key_file);
    revision    ревизия задания, для которой кандидат получен;
    unverified  True - кандидат нельзя считать official и нельзя выдавать за ключ без явного
                подтверждения родителя.
`unverified` = False только у key_file и у формы со страницы, которую принимал прежний
`fit_page_answer` (`legacy_form`). Новые формы (`extended_form`) и все `page_model` помечены
`unverified` = True: #2103 (`official_values` / `_eligible`) и #2135 обязаны их исключать.

Принятое правило: ничего не угадывается. Что не разобрано целиком, содержит чужое
обозначение, неполно или противоречиво - отказ с коротким кодом причины.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from app.ingest.answers import (
    _PAIR,
    _expand_ranges,
    _fit_legacy,
    _label_for,
    _texts,
    label_key,
    normalize_answer,
)

RULES_VERSION = 1  # растёт при смене правил разбора, схемы или текста запроса к модели

# Коды причин отказа.
EMPTY = "empty"
OPEN = "open_type"
NO_OPTIONS = "no_options"
UNPARSED = "unparsed"  # ответ разобран не целиком
FOREIGN_LABEL = "foreign_label"  # обозначение не из вариантов нужной стороны
CONFLICT = "conflict"  # противоречивые повторы
PARTIAL = "partial_match"  # в соответствии покрыта не вся левая сторона
BAD_TYPE = "bad_type"
NEEDS_UNIT = "needs_unit_context"

# Единицы, которые разрешено отбросить у числа. Значение НЕ пересчитывается: «5 см» и «5 мм» дают 5.
# Список консервативный; расширять только по замеру реальных ответов.
UNITS = frozenset(["г", "мг", "кг", "мл", "л", "см", "мм", "м", "%", "шт"])
AMBIGUOUS = "ambiguous"  # после числа ещё числа или второе значение
_NUMBER_HEAD = re.compile(r"([+-]?\d+(?:[.,]\d+)?)\s*(.*)", re.DOTALL)
_PLAIN = re.compile(r"[0-9A-Za-zА-Яа-яЁё\s,;.]*")
_ITEM = re.compile(
    r"\s*(?P<a>\d{1,3}|[A-Za-zА-Яа-яЁё])(?P<gap>\s*[-–—:=>→.)]*\s*)(?P<b>\d{1,3}|[A-Za-zА-Яа-яЁё])"
    r"(?![0-9A-Za-zА-Яа-яЁё])\s*[;,]?"
)


@dataclass(frozen=True)
class Fit:
    value: object  # None - не кандидат
    reason: str | None  # код отказа, если value is None
    extended: bool = False  # форма сверх принимавшихся раньше


def _sides(options):
    """(обозначения левой, обозначения правой) для match."""
    if not isinstance(options, dict):
        return None
    left, right = options.get("left"), options.get("right")
    if not isinstance(left, list) or not isinstance(right, list):
        return None
    return set(_texts(left)), set(_texts(right))


def check_answer(value, task_type: str, options) -> str | None:
    """Код отказа или None, если value - полный, непротиворечивый ответ этого задания."""
    if task_type not in ("single", "multiple", "match", "numeric"):
        return OPEN
    if value is None or value == "" or value == [] or value == {}:
        return EMPTY
    if task_type == "numeric":
        ok = isinstance(value, str) and normalize_answer(value, "numeric") == value
        return None if ok else BAD_TYPE
    if task_type == "match":
        sides = _sides(options)
        if sides is None:
            return NO_OPTIONS
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            return BAD_TYPE
        left, right = sides
        keys = set(value)
        if not keys <= left or not set(value.values()) <= right:
            return FOREIGN_LABEL
        return None if keys == left else PARTIAL
    if not isinstance(options, list):
        return NO_OPTIONS
    labels = set(_texts(options))
    if task_type == "single":
        if not isinstance(value, str):
            return BAD_TYPE
        return None if value in labels else FOREIGN_LABEL
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return BAD_TYPE
    if len(set(value)) != len(value):
        return CONFLICT
    return None if set(value) <= labels else FOREIGN_LABEL


def build_value(task_type: str, options, *, labels=None, pairs=None, number=None):
    """(значение, код отказа) из разобранных частей. Повторы сверяются ДО сворачивания в dict."""
    if task_type == "numeric":
        value = normalize_answer(number, "numeric") if number is not None else None
    elif task_type == "match":
        folded: dict[str, str] = {}
        for left, right in pairs or []:
            a, b = label_key(left), label_key(right)
            if folded.setdefault(a, b) != b:
                return None, CONFLICT
        value = folded or None
    else:
        found = sorted({label_key(x) for x in labels or []})
        if len(found) > 1 and task_type == "single":
            return None, CONFLICT
        value = (found[0] if task_type == "single" else found) if found else None
    reason = check_answer(value, task_type, options)
    return (None, reason) if reason else (value, None)


def _numeric(text: str) -> tuple[object, str | None]:
    head = _NUMBER_HEAD.fullmatch(text.replace(" ", " ").strip())
    if not head:
        return None, UNPARSED
    rest = head.group(2).strip().rstrip(".").lower()
    if rest and rest not in UNITS:
        if re.search(r"[^\W\d_]|[%°^×·*/]", rest):
            return None, NEEDS_UNIT
        return None, AMBIGUOUS if re.search(r"[\d;]", rest) else UNPARSED
    value = normalize_answer(head.group(1), "numeric")
    return (value, None) if value is not None else (None, UNPARSED)


def _covered(text: str, spans: list[tuple[int, int]]) -> bool:
    """Ничего, кроме пробелов, между найденными кусками и вокруг них."""
    pos = 0
    for start, end in spans:
        if text[pos:start].strip():
            return False
        pos = end
    return not text[pos:].strip()


def _pair_tokens(text: str) -> list[tuple[str, str]] | None:
    items, pos = [], 0
    while text[pos:].strip():
        m = _ITEM.match(text, pos)
        if not m or m.end() == pos:
            items = None
            break
        a, b = m.group("a"), m.group("b")
        if not m.group("gap").strip() and a.isdigit() == b.isdigit():
            items = None  # «12», «АБ»: где кончается левая часть, не сказано
            break
        items.append((a, b))
        pos = m.end()
    if items:
        return items
    flat = text.replace(",", " ").replace(";", " ")
    found = list(_PAIR.finditer(flat))
    if found and _covered(flat, [m.span() for m in found]):
        return [(m.group(1), m.group(2)) for m in found]
    return None


def _fit_match(raw: str, options) -> tuple[object, str | None]:
    if _sides(options) is None:
        return None, NO_OPTIONS
    tokens = _pair_tokens(raw)
    if not tokens:
        return None, UNPARSED
    pairs = []
    for a, b in tokens:
        la, rb = _label_for(a, options["left"]), _label_for(b, options["right"])
        if la is None or rb is None:
            return None, FOREIGN_LABEL
        pairs.append((la, rb))
    return build_value("match", options, pairs=pairs)


_POINTS = re.compile(r"(?<=\S)[\s;,]+\d+\s+балл\w*\s*$", re.IGNORECASE)  # «… 10 баллов»
_TRAILING_PARENS = re.compile(r"(?<=\S)((?:\s*\([^()]*\))+)\s*$")
_GROUP = re.compile(r"\(([^()]*)\)")
# Скобки после списка, которые можно отбросить: только баллы и «в любом порядке». Всё остальное
# («или 2», «кроме 2») меняет ответ: такой ответ отказывается как неоднозначный.
_ALLOWED_REMARK = re.compile(
    r"\s*(?:[^()]*\bбалл\w*[^()]*|в\s+любом\s+порядке|любой\s+порядок)\s*", re.IGNORECASE
)


def _letters(text: str, labels: set[str], task_type: str) -> tuple[list[str] | None, str | None]:
    """(обозначения, код отказа). Слитный набор («АВГ», «135») делится на знаки, только если
    каждый знак - обозначение (для цифр: все обозначения однозначные цифры) и без повторов."""
    out: list[str] = []
    single_digits = all(len(x) == 1 and x.isdigit() for x in labels)
    for token in (t for t in re.split(r"[\s,;.]+", text) if t):
        key = label_key(token)
        fused = task_type == "multiple" and (token.isalpha() or (token.isdigit() and single_digits))
        if key in labels:
            out.append(key)
        elif fused:
            parts = [label_key(c) for c in token]
            if not all(p in labels for p in parts):
                return None, FOREIGN_LABEL
            if len(set(parts)) != len(parts):
                return None, CONFLICT
            out.extend(parts)
        else:
            return None, None
    return out, None


def _names(text: str, options) -> list[str] | None:
    """Целые куски через запятую/«;»: обозначение или текст варианта."""
    found = [_label_for(p, options) for p in re.split(r"[,;\n]+", text) if p.strip()]
    return None if not found or None in found else found


def _fit_choice(raw: str, task_type: str, options) -> tuple[object, str | None]:
    if not isinstance(options, list):
        return None, NO_OPTIONS
    order = [label_key(o["label"]) for o in options]
    text = _expand_ranges(raw, order)
    if text is None:
        return None, UNPARSED
    if task_type == "multiple":  # «9, 9 (9 балл)»: пояснения после списка не обозначения
        tail = _TRAILING_PARENS.search(text)
        if tail:
            if not all(_ALLOWED_REMARK.fullmatch(g) for g in _GROUP.findall(tail.group(1))):
                return None, AMBIGUOUS
            text = text[: tail.start()]
    plain = _PLAIN.fullmatch(text) is not None
    found, why = _letters(text, set(order), task_type) if plain else (None, None)
    if why:
        return None, why
    if found is None:
        found = _names(text, options)
    if found is None:
        return None, FOREIGN_LABEL if plain else UNPARSED
    return build_value(task_type, options, labels=found)


def fit_page(raw, task_type: str, options) -> Fit:
    """Ответ со страницы, приведённый к заданию, строго. extended: прежний разбор такого не принимал."""
    if task_type not in ("single", "multiple", "match", "numeric"):
        return Fit(None, OPEN)
    if raw is None or not str(raw).strip():
        return Fit(None, EMPTY)
    text = str(raw)
    if task_type != "numeric":  # баллы за ответ в конце строки - не часть ответа
        text = _POINTS.sub("", text)
    if task_type == "numeric":
        value, reason = _numeric(text)
    elif task_type == "match":
        value, reason = _fit_match(text, options)
    else:
        value, reason = _fit_choice(text, task_type, options)
    if value is None:
        return Fit(None, reason or UNPARSED)
    return Fit(value, None, extended=_fit_legacy(raw, task_type, options) != value)


def raw_hash(raw) -> str:
    return hashlib.sha256(str(raw or "").encode()).hexdigest()


def stored_candidate(conn, task):
    """Значение, которое модель-нормализатор записала для ЭТОЙ ревизии и ЭТОГО текста ответа;
    иначе None. Значение проверяется заново: в базе оно не доверенное."""
    row = conn.execute(
        "SELECT revision, raw_hash, value, rules_version FROM page_answer WHERE task_item_id = ?",
        (task["id"],),
    ).fetchone()
    if row is None or row[2] is None:
        return None
    if (row[0], row[1], row[3]) != (task["revision"], raw_hash(task["answer_text"]), RULES_VERSION):
        return None  # устаревший итог - как будто его нет (needs_model запросит заново)
    value = json.loads(row[2])
    options = json.loads(task["options"]) if task["options"] else None
    return None if check_answer(value, task["type"], options) else value
