"""model_answer: ответ модели на задание без рисунка (SPEC-1.0 1.2). Хранится как «не проверен»."""

import json

from app.ingest.answers import display, normalize_answer
from app.ingest.keys import candidate
from app.ingest.prompt import ANSWER_SCHEMA, build_task_request
from app.ingest.store import figure_of, get_task, save_snapshot


def compute_model_answer(ctx, conn, task_id: int) -> bool:
    """Вычислить и записать model_answer для текущей ревизии; False - ответ не получен.

    Для задания с рисунком не вычисляется никогда: модель картинку не видит.
    """
    return compute_model_answer_why(ctx, conn, task_id)[0]


def compute_model_answer_why(
    ctx, conn, task_id: int, *, keyed: bool = False
) -> tuple[bool, str | None]:
    """(записан ли ответ, причина отказа). keyed=True - автопубликация (K5b): ответ нужен и
    заданию с ключом, чтобы сравнить его с ключом до того, как задание увидит ребёнок."""
    task = get_task(conn, task_id)
    if task is None or figure_of(task) is not None or task["type"] == "open":
        return False, "figure_or_open"
    if not keyed and (task["answer_key"] is not None or candidate(conn, task) is not None):
        return False, "has_key"  # ключ (в том числе загруженный) есть: ответ модели не нужен
    system, messages = build_task_request(task)
    result = ctx.model.complete(system, messages, ANSWER_SCHEMA)
    payload = getattr(result, "json", None)
    if not getattr(result, "ok", False) or not isinstance(payload, dict):
        return False, getattr(result, "error_kind", None) or "error"
    value = normalize_answer(payload.get("proposed_answer"), task["type"])
    if value is None:
        return False, "unparsed"
    record = {
        "answer": display(value),
        "value": value,
        "explanation": str(payload.get("explanation", "")),
        "uncertainty": payload.get("uncertainty")
        if payload.get("uncertainty") in ("low", "high")
        else "high",
        "unverified": True,
    }
    # Запись ответа и снимок ревизии - одна транзакция: между ними не может появиться новая
    # ревизия. Пишем только в пустое поле и только если ревизия не изменилась за время запроса;
    # заполненный ответ не заменяется (смена эталона - новая ревизия).
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE task_item SET model_answer = ? WHERE id = ? AND revision = ? "
            "AND model_answer IS NULL",
            (json.dumps(record, ensure_ascii=False), task_id, task["revision"]),
        )
        if cur.rowcount == 1:
            save_snapshot(conn, task_id)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return cur.rowcount == 1, None


def model_value(task):
    """Нормализованный ответ модели или None."""
    return json.loads(task["model_answer"])["value"] if task["model_answer"] else None


__all__ = ["compute_model_answer", "compute_model_answer_why", "model_value"]
