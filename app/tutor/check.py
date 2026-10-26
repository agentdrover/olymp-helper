"""Проверка ответа ребёнка без модели: равенство, множество, отображение (SPEC-1.0 1.1)."""

from app.ingest.answers import normalize_answer, same_answer


class BadAnswer(ValueError):
    """Ответ не складывается в ответ на это задание; message читает ребёнок."""


def answer_text(raw) -> str:
    """Ответ из запроса (строка, список, словарь) -> текст для normalize_answer."""
    if isinstance(raw, bool) or raw is None:
        raise BadAnswer("Выбери ответ.")
    if isinstance(raw, int | float):
        return str(raw)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return ", ".join(str(x) for x in raw)
    if isinstance(raw, dict):
        return "; ".join(f"{k}-{v}" for k, v in raw.items())
    raise BadAnswer("Не получилось понять ответ.")


def _labels(options, side=None) -> set[str]:
    items = options.get(side, []) if isinstance(options, dict) else options or []
    return {normalize_answer(o["label"], "single") for o in items} - {None}


def parse_answer(task_type: str, options, raw):
    """Нормализованный ответ; BadAnswer, если варианта нет или ответ не того вида."""
    value = normalize_answer(answer_text(raw), task_type)
    if value is None:
        raise BadAnswer(
            "Не получилось понять ответ: выбери вариант."
            if task_type != "numeric"
            else "Введи число."
        )
    if task_type in ("single", "multiple") and isinstance(options, list):
        named = [value] if isinstance(value, str) else value
        if any(v not in _labels(options) for v in named):
            raise BadAnswer("Такого варианта в задании нет.")
    if task_type == "match" and isinstance(options, dict):
        left, right = _labels(options, "left"), _labels(options, "right")
        if any(k not in left or v not in right for k, v in value.items()):
            raise BadAnswer("Такого варианта в задании нет.")
    return value


def check_answer(task_type: str, reference, given) -> bool:
    """Ответ равен эталону (ключу или, без ключа, model_answer). Модель не участвует."""
    try:
        return same_answer(task_type, reference, answer_text(given))
    except BadAnswer:
        return False
