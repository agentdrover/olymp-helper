"""Нормализация русского текста: одинаковая для индекса и запроса (SPEC-1.0 1.7)."""

import re

import snowballstemmer

_STEMMER = snowballstemmer.stemmer("russian")
_TOKEN = re.compile(r"[0-9a-zа-я]+")


def normalize_tokens(text: str) -> list[str]:
    """Нижний регистр, ё -> е, разбор на слова, стемминг Snowball."""
    words = _TOKEN.findall(text.lower().replace("ё", "е"))
    return [_STEMMER.stemWord(w) for w in words]
