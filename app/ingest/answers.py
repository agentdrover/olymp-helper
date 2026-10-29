"""Ответы и ключи: нормализация и сравнение (SPEC-1.0 1.2 - сравнение нормализованных ответов)."""

import json
import re

_LATIN = str.maketrans("ABCEHKMOPTX", "АВСЕНКМОРТХ")
_TOKEN = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_NUMBER = re.compile(r"([+-]?)(\d{1,60})(?:\.(\d{0,60}))?")
_PAIR = re.compile(r"([0-9A-Za-zА-Яа-яЁё]+)\s*[-–—:=>→]*\s*([A-Za-zА-Яа-яЁё0-9]+)")


def _label(raw: str) -> str:
    return raw.upper().translate(_LATIN)


def label_key(raw) -> str:
    """Обозначение варианта в виде, в котором его сравнивает проверка ответа: регистр и
    латинские двойники кириллицы не важны. Единственное место, где это решается."""
    return normalize_answer(raw, "single") or _label(str(raw).strip())


def normalize_answer(raw, task_type: str):
    """single -> 'Б'; multiple -> ['А', 'В']; match -> {'1': 'А'}; непонятное -> None."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if task_type == "numeric":
        return _number(text)
    if task_type == "match":
        pairs = _pairs(text)
        return pairs or None
    tokens = [_label(t) for t in _TOKEN.findall(text)]
    if not tokens:
        return None
    if task_type == "single":
        return tokens[0] if len(tokens) == 1 else None
    # multiple: «АВ» тоже набор букв
    letters: list[str] = []
    for tok in tokens:
        letters.extend(list(tok) if tok.isalpha() and len(tok) <= 6 else [tok])
    return sorted(set(letters))


def _number(text: str) -> str | None:
    """Число для точного сравнения, строкой и без округления: запятая = точка, нули в начале
    целой части и в конце дробной, знак плюс и «-0» не важны."""
    found = _NUMBER.fullmatch(text.replace(",", "."))
    if not found:
        return None
    sign, whole, fraction = found.group(1), found.group(2).lstrip("0") or "0", found.group(3) or ""
    fraction = fraction.rstrip("0")
    number = whole + ("." + fraction if fraction else "")
    return "-" + number if sign == "-" and number != "0" else number


def same_answer(task_type: str, key, given) -> bool:
    """Ответ ребёнка равен ключу: сравниваются нормализованные значения, допуска нет."""
    value = normalize_answer(given, task_type)
    return value is not None and value == key


def _pairs(text: str) -> dict:
    """{левое: правое}; противоречивый повтор («1-А; 1-Б») -> {}: ответа нет. Повторы
    сверяются до сворачивания в dict."""
    out: dict[str, str] = {}
    for left, right in _PAIR.findall(text.replace(",", " ").replace(";", " ")):
        if out.setdefault(_label(left), _label(right)) != _label(right):
            return {}
    return out


def display(value) -> str:
    if isinstance(value, list):
        return ", ".join(value)
    if isinstance(value, dict):
        return "; ".join(f"{k}-{v}" for k, v in value.items())
    return "" if value is None else str(value)


def key_json(value, task_type: str) -> str:
    return json.dumps({"type": task_type, "value": value}, ensure_ascii=False)


def key_value(answer_key: str | None):
    return None if not answer_key else json.loads(answer_key)["value"]


def _texts(options) -> dict[str, str]:
    """{метка: нормализованный текст варианта}."""
    return {
        normalize_answer(o["label"], "single"): _label(" ".join(o["text"].split()))
        for o in options or []
        if normalize_answer(o["label"], "single")
    }


def _label_for(token: str, options) -> str | None:
    """Обозначение варианта по обозначению или по тексту варианта; иначе None."""
    texts = _texts(options)
    word = _label(token.strip())
    if word in texts:
        return word
    found = [label for label, text in texts.items() if text == word]
    return found[0] if len(found) == 1 else None


_END = r"(?:№\s*)?([0-9]+|[A-Za-zА-Яа-яЁё])"
_RANGE = re.compile(rf"(?<![\w]){_END}\s*(?:…|\.{{2,}}|[-–—])\s*{_END}(?![\w])")


_DASHES = re.compile(r"…|\.\.|[-–—]")


def _expand_ranges(raw: str, order: list[str], *, expanded=None) -> str | None:
    """«№ 2 … № 7», «2-7», «2...7» -> «2, 3, 4, 5, 6, 7»: все обозначения между концами по порядку
    вариантов. Если конец - не обозначение задания или диапазон идёт назад, None."""
    failed = False

    def expand(found):
        nonlocal failed
        a, b = _label(found.group(1)), _label(found.group(2))
        if a not in order or b not in order or order.index(a) > order.index(b):
            failed = True
            return ""
        labels = order[order.index(a) : order.index(b) + 1]
        if expanded is not None:
            expanded.append(labels)
        return ", ".join(labels)

    out = _RANGE.sub(expand, raw)
    return None if failed or _DASHES.search(out) else out  # a range left unread is ambiguous


def _fit_legacy(raw, task_type: str, options):
    """Прежний разбор. Нужен только чтобы отличить формы, которые принимались раньше (их
    кандидат остаётся как был), от новых (они unverified). Значение для ключа он не даёт."""
    if task_type == "open":
        return None
    if task_type == "numeric":
        return normalize_answer(raw, "numeric")
    if raw is None or not str(raw).strip():
        return None
    if task_type == "match":
        return _fit_pairs(str(raw), options)
    if not isinstance(options, list):
        return None
    labels = set(_texts(options))
    raw = _expand_ranges(str(raw), [normalize_answer(o["label"], "single") for o in options])
    if raw is None:
        return None
    value = normalize_answer(raw, task_type)
    named = [value] if isinstance(value, str) else value or []
    if value is not None and named and all(v in labels for v in named):
        return value
    pieces = [p for p in re.split(r"[,;\n]+", str(raw)) if p.strip()]
    found = [_label_for(p, options) for p in pieces]
    if not found or None in found or (task_type == "single" and len(found) != 1):
        return None
    return found[0] if task_type == "single" else sorted(set(found))


def _fit_pairs(raw: str, options):
    if not isinstance(options, dict):
        return None
    left, right = options.get("left", []), options.get("right", [])
    pairs: dict[str, str] = {}
    for key, value in _PAIR.findall(raw.replace(",", " ").replace(";", " ")):
        a, b = _label_for(key, left), _label_for(value, right)
        if a is not None and b is not None:
            pairs.setdefault(a, b)
    return pairs if pairs and set(pairs) == set(_texts(left)) else None
