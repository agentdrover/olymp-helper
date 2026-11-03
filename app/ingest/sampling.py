"""Выборка прошедших конвейер для родителя (OH-K5c, решение владельца 27.10).

Из заданий, опубликованных без родителя, родителю показывается каждое 5-е. Пока родитель
проверил подряд 20 заданий выборки и не нашёл ни одной ошибки, - каждое 10-е; найденная ошибка
возвращает выборку к каждому 5-му. Решение «в выборке» принимается при записи задания и хранится
в `sample_state`: позже оно не пересчитывается, поэтому смена шага не двигает уже отобранные.
"""

import sqlite3

EVERY = 5
EVERY_CLEAN = 10
CLEAN_AFTER = 20

# Ворота конвейера и их подписи для журнала. Ворота называет pipeline.Stop.
GATES = {
    "topic": "тема не из каталога",
    "no_critic": "нет критика другого семейства",
    "unavailable": "критик, редактор или судья недоступны",
    "size": "запрос к модели слишком велик",
    "limit": "исчерпан дневной лимит вопросов к помощнику",
    "remarks": "замечания критика не сняты",
    "disagree": "критик решил иначе, чем ключ",
    "editor": "редактор не смог исправить",
    "duplicate": "повтор вопроса из банка",
    "leak": "в условии очевидный признак ответа",
    "judge": "судья нашёл подсказку ответа",
    "other": "другая причина",
}


def clean_streak(conn: sqlite3.Connection) -> int:
    """Сколько заданий выборки родитель проверил подряд без найденной ошибки."""
    return conn.execute(
        "SELECT COUNT(*) FROM task_item WHERE sample_state = 'ok' AND sample_review_no > "
        "COALESCE((SELECT MAX(sample_review_no) FROM task_item WHERE sample_state = 'error'), 0)"
    ).fetchone()[0]


def every(conn: sqlite3.Connection) -> int:
    return EVERY_CLEAN if clean_streak(conn) >= CLEAN_AFTER else EVERY


def new_sample_state(conn: sqlite3.Connection) -> str | None:
    """'pending', если очередное прошедшее задание идёт в выборку, иначе None. Считаются
    прошедшие после последнего отобранного: так шаг 5 -> 10 и обратно не теряет и не дублирует."""
    waited = conn.execute(
        "SELECT COUNT(*) FROM task_item WHERE pipeline_outcome = 'passed' AND id > "
        "COALESCE((SELECT MAX(id) FROM task_item WHERE sample_state IS NOT NULL), 0)"
    ).fetchone()[0]
    return "pending" if waited + 1 >= every(conn) else None
