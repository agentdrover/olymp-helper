"""Счётчик дней до этапа олимпиады (SPEC 1.3а): ребёнок видит его, только если родитель включил.

Экран «Сегодня» (OH-T06) подключает шаблон `_countdown.html` и передаёт ему результат
`exam_countdown(conn, today)` под именем `countdown`. Выключен или даты нет - возвращается None,
и шаблон ничего не выводит.
"""

import sqlite3
from datetime import date

MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]  # fmt: skip


def exam_countdown(conn: sqlite3.Connection, today: date) -> dict | None:
    row = conn.execute(
        "SELECT show_exam_countdown, stage_date FROM settings WHERE id = 1"
    ).fetchone()
    if row is None or not row[0] or not row[1]:
        return None
    stage = date.fromisoformat(row[1])
    days = (stage - today).days
    if days < 0:  # этап прошёл: считать нечего
        return None
    return {"days": days, "date": row[1], "date_label": f"{stage.day} {MONTHS[stage.month - 1]}"}
