"""Автопубликация заданий сборника с официальным ключом (SPEC-1.0 1.2, решение владельца 27.10).

Задание без рисунка, у которого ключ взят из официального источника (страница задания или
загруженный файл ключа), а ответ модели совпал с ключом, публикуется без проверки родителем.
Публикация идёт тем же путём, что и подтверждение родителя (review.confirm), с пометкой
`auto_published`. Не прошедшее условие оставляет задание в ленте родителя с причиной
(`auto_hold`). Рисунок всегда идёт к родителю: модель картинку не видит.
"""

import logging

from app.ingest import review
from app.ingest.answers import key_value
from app.ingest.keys import official_values
from app.ingest.leaks import condition_leaks
from app.ingest.model_answer import compute_model_answer_why, model_value
from app.ingest.publish import leak_notice
from app.ingest.store import atomic, figure_of, get_task, options_of, update_task
from app.tutor.guard import prefilter_reason

log = logging.getLogger("app.ingest")

HOLD_AMBIGUOUS = "Автопубликация остановлена: официальных ответов несколько и они разные."
HOLD_LEAK = "Автопубликация остановлена: в условии остался текст ответа."
HOLD_LIMIT = "Автопубликация остановлена: дневной лимит запросов к модели исчерпан."
HOLD_NO_MODEL = "Автопубликация остановлена: ответ модели не получен."
HOLD_DISAGREE = "Автопубликация остановлена: ответ модели расходится с ключом."
HOLD_KEY_MISMATCH = "Автопубликация остановлена: ключ в задании не совпадает с официальным."
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


def _blank(options):
    """Те же варианты без текста: остаются только обозначения (правило «обозначение верного»)."""
    if isinstance(options, list):
        return [{**o, "text": ""} for o in options]
    if isinstance(options, dict):
        return {side: [{**o, "text": ""} for o in items] for side, items in options.items()}
    return options


def _pieces(task) -> list[str]:
    """Всё, что ребёнок читает до ответа: условие и текст каждого варианта."""
    options = options_of(task)
    items = options if isinstance(options, list) else []
    if isinstance(options, dict):
        items = [o for side in options.values() for o in side]
    return [task["text"], *[o.get("text", "") for o in items]]


def _child_leak(task, key) -> bool:
    """Ответ в содержимом, видимом ребёнку: условие и варианты. Сильнейшая из готовых проверок
    (фильтр T06: обозначение верного варианта в контексте обозначения, число эталона); сбой
    проверки считается утечкой."""
    options = _blank(options_of(task))
    try:
        return any(
            prefilter_reason(piece, task["type"], options, key) is not None
            for piece in _pieces(task)
        ) or any(
            condition_leaks(piece, task["answer_text"], task["type"]) for piece in _pieces(task)
        )
    except Exception:
        log.exception("leak check failed for task %s", task["id"])
        return True


def _check(conn, task, *, revision=None, need_model: bool) -> str | None:
    """None - все условия выполнены; "" - задание не для автопубликации (молча); иначе причина
    остановки. Читает ТЕКУЩУЮ строку: вызывается и до запроса к модели, и перед подтверждением."""
    if task is None or not _eligible(task):
        return ""
    if revision is not None and task["revision"] != revision:
        return ""  # задание правили: новую ревизию оценит следующий проход
    values = official_values(conn, task)
    if not values:
        return ""  # официального ключа нет: обычная проверка родителем
    if len(values) > 1:
        return HOLD_AMBIGUOUS
    value = values[0]
    stored = key_value(task["answer_key"]) if task["answer_key"] is not None else value
    if stored != value:
        return HOLD_KEY_MISMATCH  # confirm опубликовал бы не тот ключ, что в источнике
    if leak_notice(task) or _child_leak(task, value):
        return HOLD_LEAK
    if need_model:
        if task["model_answer"] is None:
            return HOLD_NO_MODEL
        if model_value(task) != value:
            return HOLD_DISAGREE
    return None


def _hold(conn, task, reason: str) -> str:
    if task["auto_hold"] != reason:
        update_task(conn, task["id"], auto_hold=reason)
    return reason


def try_publish(ctx, conn, task_id: int, state: dict) -> str | None:
    """None - опубликовано или задание не для автопубликации; иначе причина остановки."""
    task = get_task(conn, task_id)
    reason = _check(conn, task, need_model=False)
    if reason is not None:
        return _hold(conn, task, reason) if reason else None
    revision = task["revision"]
    if task["model_answer"] is None:
        if state.get("limit"):
            return _hold(conn, task, HOLD_LIMIT)
        _ok, why = compute_model_answer_why(ctx, conn, task_id, keyed=True)
        if why == "limit":
            state["limit"] = True
            return _hold(conn, task, HOLD_LIMIT)
    # Проверка и публикация - одна запись: за время запроса к модели задание могли править,
    # родитель мог вписать ключ, мог появиться второй файл ключа.
    try:
        with atomic(conn):
            task = get_task(conn, task_id)
            reason = _check(conn, task, revision=revision, need_model=True)
            if reason is not None:
                return _hold(conn, task, reason) if reason else None
            review.confirm(ctx, conn, task_id)
            update_task(conn, task_id, auto_published=1, auto_hold=None)
    except review.Refusal as exc:
        return _hold(conn, get_task(conn, task_id), HOLD_REFUSED + exc.message)
    return None


def publish_pending(
    ctx, conn, material_id: int | None = None, *, force: bool = False, report=None
) -> int:
    """Пройти черновики (одного материала или все); вернуть число опубликованных.
    Сбой одного задания не прерывает остальные и не роняет импорт. force - явное действие
    родителя: работает и при выключенной автопубликации. report(checked, total, published,
    limit) вызывается после каждого задания."""
    if not force and not getattr(ctx, "auto_publish", False):
        return 0
    sql = "SELECT id FROM task_item WHERE import_status = 'draft' AND excluded = 0"
    args: tuple = ()
    if material_id is not None:
        sql += " AND material_id = ?"
        args = (material_id,)
    ids = [r[0] for r in conn.execute(sql + " ORDER BY id", args).fetchall()]
    state: dict = {}
    published = 0
    for index, task_id in enumerate(ids, 1):
        try:
            try_publish(ctx, conn, task_id, state)
        except Exception:
            log.exception("auto publish of task %s failed", task_id)
        if get_task(conn, task_id)["auto_published"]:
            published += 1
        if report is not None:
            report(index, len(ids), published, bool(state.get("limit")))
    return published
