"""Автопубликация заданий сборника с официальным ключом (SPEC-1.0 1.2, решение владельца 27.10).

Задание без рисунка, у которого ключ взят из официального источника (страница задания или
загруженный файл ключа), а ответ модели совпал с ключом, публикуется без проверки родителем.
Публикация идёт тем же путём, что и подтверждение родителя (review.confirm), с пометкой
`auto_published`. Не прошедшее условие оставляет задание в ленте родителя с причиной
(`auto_hold`). Рисунок всегда идёт к родителю: модель картинку не видит.
"""

import logging

from app.ingest import review
from app.ingest.keys import official_values
from app.ingest.model_answer import compute_model_answer_why, model_value
from app.ingest.publish import leak_notice
from app.ingest.store import figure_of, get_task, update_task

log = logging.getLogger("app.ingest")

HOLD_AMBIGUOUS = "Автопубликация остановлена: официальных ответов несколько и они разные."
HOLD_LEAK = "Автопубликация остановлена: в условии остался текст ответа."
HOLD_LIMIT = "Автопубликация остановлена: дневной лимит запросов к модели исчерпан."
HOLD_NO_MODEL = "Автопубликация остановлена: ответ модели не получен."
HOLD_DISAGREE = "Автопубликация остановлена: ответ модели расходится с ключом."
HOLD_REFUSED = "Автопубликация остановлена: "


def _eligible(task) -> bool:
    """Структура разобрана без пропусков, рисунка нет, родитель задание ещё не трогал."""
    return (
        task["import_status"] == "draft"
        and not task["excluded"]
        and task["unsupported_reason"] is None
        and task["type"] != "open"
        and figure_of(task) is None
        and (task["answer_key"] is None or task["key_source"] == "official")
    )


def _hold(conn, task, reason: str) -> str:
    if task["auto_hold"] != reason:
        update_task(conn, task["id"], auto_hold=reason)
    return reason


def try_publish(ctx, conn, task_id: int, state: dict) -> str | None:
    """None - опубликовано или задание не для автопубликации; иначе причина остановки."""
    task = get_task(conn, task_id)
    if task is None or not _eligible(task):
        return None
    values = official_values(conn, task)
    if not values:
        return None  # официального ключа нет: обычная проверка родителем
    if len(values) > 1:
        return _hold(conn, task, HOLD_AMBIGUOUS)
    value = values[0]
    if leak_notice(task):
        return _hold(conn, task, HOLD_LEAK)
    if task["model_answer"] is None:
        if state.get("limit"):
            return _hold(conn, task, HOLD_LIMIT)
        _ok, why = compute_model_answer_why(ctx, conn, task_id, keyed=True)
        if why == "limit":
            state["limit"] = True
            return _hold(conn, task, HOLD_LIMIT)
        task = get_task(conn, task_id)
        if task["model_answer"] is None:
            return _hold(conn, task, HOLD_NO_MODEL)
    if model_value(task) != value:
        return _hold(conn, task, HOLD_DISAGREE)
    try:
        review.confirm(ctx, conn, task_id)
    except review.Refusal as exc:
        return _hold(conn, get_task(conn, task_id), HOLD_REFUSED + exc.message)
    update_task(conn, task_id, auto_published=1, auto_hold=None)
    return None


def publish_pending(ctx, conn, material_id: int | None = None) -> int:
    """Пройти черновики (одного материала или все); вернуть число опубликованных.
    Сбой одного задания не прерывает остальные и не роняет импорт."""
    if not getattr(ctx, "auto_publish", False):
        return 0
    sql = "SELECT id FROM task_item WHERE import_status = 'draft' AND excluded = 0"
    args: tuple = ()
    if material_id is not None:
        sql += " AND material_id = ?"
        args = (material_id,)
    state: dict = {}
    published = 0
    for (task_id,) in conn.execute(sql + " ORDER BY id", args).fetchall():
        try:
            try_publish(ctx, conn, task_id, state)
        except Exception:
            log.exception("auto publish of task %s failed", task_id)
            continue
        if get_task(conn, task_id)["auto_published"]:
            published += 1
    return published
