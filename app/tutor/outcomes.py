"""Итог разбора словами SPEC-1.0 1.3 (2в). Слово «верно» без ключа не используется."""


def _attempts_word(n: int) -> str:
    return "попытки" if n % 10 == 1 and n % 100 != 11 else "попыток"


def outcome_label(keyed: bool, state: str, attempts: int, hints: int) -> str | None:
    if state == "answer_shown":
        return f"ответ показан после {attempts} {_attempts_word(attempts)}"
    if state == "hint3":
        return "решение по подсказке 3" if keyed else "решение модели по подсказке 3, не проверено"
    if state == "solved_unknown":
        return "совпало с ответом модели, не проверено"
    if state == "solved":
        if hints:
            return f"верно после подсказки {hints}"
        return f"верно с {attempts}-й попытки"
    return None
