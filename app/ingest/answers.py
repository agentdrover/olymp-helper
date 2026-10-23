"""Ответы и ключи: нормализация и сравнение (SPEC-1.0 1.2 - сравнение нормализованных ответов)."""

import json
import re

_LATIN = str.maketrans("ABCEHKMOPTX", "АВСЕНКМОРТХ")
_TOKEN = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_PAIR = re.compile(r"([0-9A-Za-zА-Яа-яЁё]+)\s*[-–—:=>→]*\s*([A-Za-zА-Яа-яЁё0-9]+)")


def _label(raw: str) -> str:
    return raw.upper().translate(_LATIN)


def normalize_answer(raw, task_type: str):
    """single -> 'Б'; multiple -> ['А', 'В']; match -> {'1': 'А'}; непонятное -> None."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
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


def _pairs(text: str) -> dict:
    out = {}
    for left, right in _PAIR.findall(text.replace(",", " ").replace(";", " ")):
        out[_label(left)] = _label(right)
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
