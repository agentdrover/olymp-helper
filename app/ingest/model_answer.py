"""model_answer: ответ модели на задание без рисунка (SPEC-1.0 1.2). Хранится как «не проверен»."""

import json

from app.ingest.answers import display, normalize_answer
from app.ingest.keys import candidate
from app.ingest.prompt import ANSWER_SCHEMA, build_task_request
from app.ingest.store import figure_of, get_task, save_snapshot
from app.tutor.seq import bump


def compute_model_answer(ctx, conn, task_id: int) -> bool:
    """Вычислить и записать model_answer для текущей ревизии; False - ответ не получен.

    Для задания с рисунком не вычисляется никогда: модель картинку не видит.
    """
    task = get_task(conn, task_id)
    if task is None or figure_of(task) is not None or task["type"] == "open":
        return False
    if task["answer_key"] is not None or candidate(conn, task) is not None:
        return False  # ключ (в том числе загруженный) есть: ответ модели не нужен
    system, messages = build_task_request(task)
    result = ctx.model.complete(system, messages, ANSWER_SCHEMA)
    payload = getattr(result, "json", None)
    if not getattr(result, "ok", False) or not isinstance(payload, dict):
        return False
    value = normalize_answer(payload.get("proposed_answer"), task["type"])
    if value is None:
        return False
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
            bump(conn)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return cur.rowcount == 1


def model_value(task):
    """Нормализованный ответ модели или None."""
    return json.loads(task["model_answer"])["value"] if task["model_answer"] else None


__all__ = ["compute_model_answer", "model_value"]
