"""One durable sample counter for publication pipelines and parent outcomes."""

import hashlib
import json

from app.ingest.store import atomic

HOLD_TEXT_SAMPLE = "Задание прошло конвейер и ждёт вашей проверки перед публикацией."

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


def text_digest(task) -> str:
    fields = ("type", "text", "options", "answer_key", "key_source", "key_version")
    return hashlib.sha256(json.dumps([task[field] for field in fields]).encode()).hexdigest()


def state(conn) -> dict:
    row = conn.execute(
        "SELECT passed,clean_streak FROM pipeline_sampling_state WHERE id=1"
    ).fetchone()
    return {"passed": row[0], "clean_streak": row[1], "interval": 10 if row[1] >= 20 else 5}


def reserve(conn, task_id: int, revision: int, digest: str) -> bool:
    """Called inside the publication transaction; a repeated revision never advances it."""
    with atomic(conn):
        old = conn.execute(
            "SELECT chosen,input_digest FROM pipeline_sample WHERE task_item_id=? AND revision=?",
            (task_id, revision),
        ).fetchone()
        if old:
            if old[1] != digest:
                raise ValueError("Revision content changed after sampling")
            return bool(old[0])
        current = state(conn)
        ordinal = current["passed"] + 1
        chosen = ordinal % current["interval"] == 0
        conn.execute("UPDATE pipeline_sampling_state SET passed=? WHERE id=1", (ordinal,))
        conn.execute(
            "INSERT INTO pipeline_sample(task_item_id,revision,input_digest,ordinal,chosen) "
            "VALUES(?,?,?,?,?)",
            (task_id, revision, digest, ordinal, int(chosen)),
        )
        return chosen


def matches_pending(conn, task_id: int, revision: int, digest: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM pipeline_sample WHERE task_item_id=? AND revision=? "
            "AND input_digest=? AND chosen=1 AND outcome IS NULL",
            (task_id, revision, digest),
        ).fetchone()
        is not None
    )


def parent_outcome(conn, task_id: int, revision: int, outcome: str) -> None:
    """Only an explicit confirmation/edit/exclusion resolves a sample; opening it does not."""
    with atomic(conn):
        eligible = (
            "outcome IS NULL" if outcome == "clean" else "(outcome IS NULL OR outcome='clean')"
        )
        updated = conn.execute(
            "UPDATE pipeline_sample SET outcome=? WHERE task_item_id=? AND revision=? "
            f"AND chosen=1 AND {eligible}",
            (outcome, task_id, revision),
        ).rowcount
        if updated:
            if outcome == "clean":
                conn.execute(
                    "UPDATE pipeline_sampling_state SET clean_streak=clean_streak+1 WHERE id=1"
                )
            else:
                conn.execute("UPDATE pipeline_sampling_state SET clean_streak=0 WHERE id=1")
