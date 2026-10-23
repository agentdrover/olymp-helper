"""Вычистка секретов из любого текста, который выходит за границу процесса."""

import re

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bkey_[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bcrsr_[A-Za-z0-9_\-]{12,}"),
]
MASK = "[скрыто]"


def redact(text: str, secrets=()) -> str:
    """Заменить известные значения секретов и типовые шаблоны токенов."""
    for secret in sorted((s for s in secrets if s and len(s) >= 4), key=len, reverse=True):
        text = text.replace(secret, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(MASK, text)
    return text
