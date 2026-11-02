"""Строгая проверка ответа и разбор ответа со страницы задания (K5e1, #2134).

Один проверяющий на всех: правило разбора (`fit_page`), нормализатор через модель
(`page_normalizer`), будущий #2135 и решатели зовут `check_answer` / `build_value`.

Контракт кандидата (см. `app.ingest.keys.candidate`):
    value       нормализованный ответ (str | list | dict);
    source      "key_file" | "page" | "page_model";
    reason      "legacy_form" | "extended_form" | "model_normalized" | None (key_file);
    revision    ревизия задания на момент чтения (кандидат привязан к содержимому, не к ревизии);
    unverified  True - кандидат нельзя считать official и нельзя выдавать за ключ без явного
                подтверждения родителя.
`unverified` = False только у key_file и у формы со страницы, которую принимал прежний
прежний разбор (`legacy_form`). Новые формы (`extended_form`) и все `page_model` помечены
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

RULES_VERSION = 3  # растёт при смене правил разбора, схемы или текста запроса к модели

# Коды причин отказа.
EMPTY = "empty"
OPEN = "open_type"
NO_OPTIONS = "no_options"
UNPARSED = "unparsed"  # ответ разобран не целиком
FOREIGN_LABEL = "foreign_label"  # обозначение не из вариантов нужной стороны
CONFLICT = "conflict"  # повтор обозначения, разные значения одного, лишние ответы на single
PARTIAL = "partial_match"  # в соответствии покрыта не вся левая сторона
BAD_TYPE = "bad_type"
NEEDS_UNIT = "needs_unit_context"

# Единицы, которые разрешено отбросить у числа. Значение НЕ пересчитывается: «5 см» и «5 мм» дают 5.
# Список консервативный; расширять только по замеру реальных ответов.
UNITS = frozenset(["г", "мг", "кг", "мл", "л", "см", "мм", "м", "%", "шт"])
AMBIGUOUS = "ambiguous"  # после числа ещё числа или второе значение
# Что видит родитель, когда ответ со страницы не стал кандидатом: одна таблица кодов (Fit.reason).
# OPEN здесь нет: у открытого задания ответ со страницы и не ожидается.
REASON_TEXT = {
    EMPTY: "На странице у задания нет напечатанного ответа.",
    NO_OPTIONS: "У задания нет вариантов, к которым можно привязать ответ со страницы.",
    BAD_TYPE: "Ответ на странице не подходит к типу задания.",
    UNPARSED: "Ответ на странице не удалось разобрать целиком.",
    FOREIGN_LABEL: "В ответе на странице есть обозначение, которого нет среди вариантов задания.",
    CONFLICT: (
        "Ответ на странице неоднозначен: обозначение повторено или указано с разными "
        "значениями, либо ответов больше, чем допускает задание."
    ),
    PARTIAL: "Ответ на странице покрывает не все пары задания.",
    NEEDS_UNIT: "Ответ на странице записан с единицей или словом («12 лет»): число без них не взято.",
    AMBIGUOUS: "После числа на странице стоит ещё число или значение: непонятно, какое верно.",
}


def refusal_text(reason: str | None) -> str | None:
    """Русская фраза для родителя по коду отказа; None, если код не называет причину."""
    return REASON_TEXT.get(reason) if reason else None


_NUMBER_HEAD = re.compile(r"([+-]?\d+(?:[.,]\d+)?)\s*(.*)", re.DOTALL)
# Знаки-оформление вокруг обозначений («(Б)», «1) 3)», ««А»») отбрасывались и раньше.
_SEPARATORS = r"[\s,;.()\[\]«»\"']+"
_PLAIN = re.compile(r"[0-9A-Za-zА-Яа-яЁё\s,;.()\[\]«»\"']*")
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
        found = [label_key(x) for x in labels or []]
        if task_type == "multiple" and len(set(found)) != len(found):
            return None, CONFLICT
        found = sorted(set(found))
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


# Only a standalone, fully specified score annotation can be ignored. Other
# parenthesized text must reach the parser so it cannot hide an answer.
_MATCH_SCORE = re.compile(
    r"\s*\(\s*(?:по\s+)?\d+(?:[.,]\d+)?\s*балл(?:а|ов|у)?\s*\)", re.IGNORECASE
)


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
        score = _MATCH_SCORE.match(text, pos)
        if score:
            pos = score.end()
            separator = re.match(r"\s*[;,]?", text[pos:])
            pos += separator.end() if separator else 0
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


# Баллы в конце - отдельное слово: целое число, «балл/балла/баллов», затем конец строки. Перед
# числом начало строки или разделитель; «2 баллаВ», «15 баллов» как «1» + «5 баллов» не считаются.
_POINTS = re.compile(  # «… 10 баллов», «… (2 балла)»
    r"(?:^|[\s;,]+)\d+(?:[.,]\d+)?\s*балл(?:а|ов)?\s*$|"
    r"(?<=\S)[\s;,]*\(\s*\d+(?:[.,]\d+)?\s*балл(?:а|ов)?\s*\)\s*$",
    re.IGNORECASE,
)
_TRAILING_PARENS = re.compile(r"(?<=\S)((?:\s*\([^()]*\))+)\s*$")
_GROUP = re.compile(r"\(([^()]*)\)")
# Скобки после списка, которые можно отбросить: только баллы и «в любом порядке». Всё остальное
# («или 2», «кроме 2») меняет ответ: такой ответ отказывается как неоднозначный.
_ALLOWED_REMARK = re.compile(
    r"\s*(?:\d+(?:[.,]\d+)?\s*балл(?:а|ов)?|в\s+любом\s+порядке|любой\s+порядок)\s*",
    re.IGNORECASE,
)


def _letters(text: str, labels: set[str], task_type: str) -> tuple[list[str] | None, str | None]:
    """(обозначения, код отказа). Слитный набор («АВГ», «135») делится на знаки, только если
    каждый знак - обозначение (для цифр: все обозначения однозначные цифры) и без повторов."""
    out: list[str] = []
    single_digits = all(len(x) == 1 and x.isdigit() for x in labels)
    for token in (t for t in re.split(_SEPARATORS, text) if t):
        key = label_key(token)
        fused = task_type == "multiple" and (token.isalpha() or (token.isdigit() and single_digits))
        if key in labels:
            out.append(key)
        elif fused:
            parts = [label_key(c) for c in token]
            if not all(p in labels for p in parts):
                return None, None  # не набор обозначений: возможно, тексты вариантов (_names)
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
    ranges = []
    text = _expand_ranges(raw, order, expanded=ranges)
    if text is None:
        return None, UNPARSED
    if task_type == "multiple":  # «9, 9 (9 балл)»: пояснения после списка не обозначения
        tail = _TRAILING_PARENS.search(text)
        if tail and re.search(r"[0-9A-Za-zА-Яа-яЁё]", re.sub(r"\([^()]*\)", " ", text)):
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
    if task_type == "multiple" and ranges:
        ranged = [label for labels in ranges for label in labels]
        if any(
            ranged.count(label) > 1 or found.count(label) - ranged.count(label) > 1
            for label in set(found)
        ):
            return None, CONFLICT
        found = list(dict.fromkeys(found))
    return build_value(task_type, options, labels=found)


def _decimal_points_look_like_a_label(tail: str, options) -> bool:
    """«1, 3,5 балла»: после разделителя списка стоит «3,5» - баллы 3,5 или обозначение 3 и «5»?
    Только такая запись неоднозначна. «1, 3 2,5 балла» и «(2,5 балла)» - обычный хвост с баллами."""
    found = re.match(r"\s*[;,]\s*(\d+)[.,]\d+\s*балл", tail, re.IGNORECASE)
    if not found or not isinstance(options, list):
        return False
    return label_key(found.group(1)) in set(_texts(options))


def fit_page(raw, task_type: str, options) -> Fit:
    """Ответ со страницы, приведённый к заданию, строго. extended: прежний разбор такого не принимал."""
    if task_type not in ("single", "multiple", "match", "numeric"):
        return Fit(None, OPEN)
    if raw is None or not str(raw).strip():
        return Fit(None, EMPTY)
    text = str(raw)
    if task_type != "numeric":  # баллы за ответ в конце строки - не часть ответа
        tail = _POINTS.search(text)
        if tail and _decimal_points_look_like_a_label(tail.group(0), options):
            return Fit(None, AMBIGUOUS)
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


def content_hash(task) -> str:
    """Что видит модель и проверяет валидатор: текст ответа, тип и ОБОЗНАЧЕНИЯ вариантов (их
    тексты не входят). Ревизия тоже не входит: пересчёт рисунка поднимает её сам и не должен
    стирать оплаченный итог."""
    options = json.loads(task["options"]) if task["options"] else None

    def labels(items):
        return [label_key(o.get("label", "")) for o in items or [] if isinstance(o, dict)]

    if isinstance(options, dict):
        options = {"left": labels(options.get("left")), "right": labels(options.get("right"))}
    elif isinstance(options, list):
        options = labels(options)
    payload = [str(task["answer_text"] or ""), task["type"], options]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def stored_candidate(conn, task):
    """Значение, которое модель-нормализатор записала для ЭТОГО содержимого (текст ответа, тип,
    варианты) и ЭТОЙ версии правил; иначе None. Значение проверяется заново: в базе оно не доверенное."""
    row = conn.execute(
        "SELECT content_hash, value, rules_version FROM page_answer WHERE task_item_id = ?",
        (task["id"],),
    ).fetchone()
    if row is None or row[1] is None:
        return None
    if (row[0], row[2]) != (content_hash(task), RULES_VERSION):
        return None  # устаревший итог - как будто его нет (needs_model запросит заново)
    value = json.loads(row[1])
    options = json.loads(task["options"]) if task["options"] else None
    return None if check_answer(value, task["type"], options) else value
