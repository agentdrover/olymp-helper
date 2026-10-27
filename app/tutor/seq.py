"""Версия панели задания (OH-T06b): хранимый монотонный счётчик на задание.

`bump` вызывает КАЖДЫЙ писатель, меняющий то, что ребёнок видит на экране задания, внутри своей
транзакции. Ответ панели несёт `current` из той же транзакции чтения, что и данные экрана
(`read_snapshot`), поэтому HTML ответа и его версия всегда соответствуют друг другу. Версия не
выводится из других таблиц: удаление строк её не уменьшает, правка без новых строк её растит.
"""

from contextlib import contextmanager


def bump(conn, task_id: int | None) -> None:
    """+1 к версии панели задания; в транзакции вызывающего. Без задания - ничего."""
    if task_id is None:
        return
    conn.execute(
        "INSERT INTO panel_seq (task_id, seq) VALUES (?, 1) "
        "ON CONFLICT (task_id) DO UPDATE SET seq = seq + 1",
        (task_id,),
    )


def current(conn, task_id: int) -> int:
    row = conn.execute("SELECT seq FROM panel_seq WHERE task_id = ?", (task_id,)).fetchone()
    return 0 if row is None else row[0]


@contextmanager
def read_snapshot(conn):
    """Одна транзакция чтения: данные экрана и версия видят одно состояние базы. Внутри уже
    открытой транзакции владелец - вызывающий."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")
