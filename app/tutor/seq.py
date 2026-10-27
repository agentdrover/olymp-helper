"""Версия панели (OH-T06b): один глобальный монотонный счётчик в базе.

Правило записи: каждый писатель, меняющий то, что ребёнок видит на экране задания, зовёт `bump` в
ТОЙ ЖЕ транзакции, что и сама запись; изменение в памяти (замок `Inflight`) растит версию своей
короткой транзакцией ПОСЛЕ изменения (`bump_now`).
Правило чтения: ответ панели читает версию ПЕРВОЙ, до замка и данных, потом данные в снимке чтения.
Данные новее версии безвредны: в худшем случае ответ отбросят, и следующий опрос всё исправит.
Версия не выводится из других таблиц, поэтому удаление строк её не уменьшает.
"""

from contextlib import contextmanager


def bump(conn) -> None:
    """+1 к версии панели; в транзакции вызывающего."""
    conn.execute("UPDATE panel_seq SET seq = seq + 1 WHERE id = 1")


def bump_now(conn) -> None:
    """Короткая собственная транзакция (изменение в памяти уже сделано). Внутри открытой
    транзакции вызывающего - просто её часть."""
    if conn.in_transaction:
        bump(conn)
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        bump(conn)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def current(conn) -> int:
    return conn.execute("SELECT seq FROM panel_seq WHERE id = 1").fetchone()[0]


@contextmanager
def read_snapshot(conn):
    """Одна транзакция чтения для данных экрана. Внутри уже открытой транзакции владелец - вызывающий."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")
