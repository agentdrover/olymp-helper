"""Экран «Сегодня» ребёнка (экран 2 холста): где остановился, что сделано, к чему вернуться.

Только факты занятия, без имени ребёнка (SPEC-1.0 1.6) и без похвалы (1.3, правило 8). «Стоит
вернуться» (1.3, 2б): сначала «уверен и ошибся», затем остальные ошибки по давности.
"""

import json
from datetime import date, datetime

from app.ingest.publish import is_published
from app.model.ledger import moscow_day
from app.tutor import state


def _label(material) -> str:
    year = material["year"]
    return f"{year}/{str(year + 1)[-2:]}" if year else "без года"


def _flags(conn, run) -> dict:
    sure_wrong = wrong = False
    for event in conn.execute(
        "SELECT data FROM event WHERE type = 'attempt' AND task_item_id = ? ORDER BY id",
        (run["task_item_id"],),
    ):
        data = json.loads(event["data"] or "{}")
        if data.get("run") != run["id"]:
            continue
        missed = data.get("correctness") == "incorrect" or data.get("matched_model") is False
        wrong = wrong or missed
        sure_wrong = sure_wrong or (missed and data.get("confidence") == "sure")
    return {"sure_wrong": sure_wrong, "wrong": wrong}


def _reason(run, flags) -> str | None:
    if flags["sure_wrong"]:
        return "был уверен, но ошибся"
    if flags["wrong"]:
        return "была ошибка"
    if run["state"] == "hint3":
        return run["outcome"]
    if run["state"] in ("solved_unknown",):
        return "ответ модели, не проверен"
    if run["state"] == "answer_shown":
        return run["outcome"]
    return None


def build_today(conn, now: datetime) -> dict:
    day = moscow_day(now)
    tasks = [t for t in conn.execute("SELECT * FROM task_item ORDER BY id") if is_published(t)]
    runs = {}
    for run in conn.execute("SELECT * FROM tutor_run ORDER BY id"):
        runs[run["task_item_id"]] = run  # последний прогон каждого задания
    by_id = {t["id"]: t for t in tasks}

    resume = None
    for run in sorted(runs.values(), key=lambda r: -r["id"]):
        task = by_id.get(run["task_item_id"])
        if task and not state.is_terminal(run) and run["attempts"] > 0:
            resume = {"id": task["id"], "number": task["source_number"], "text": task["text"]}
            break

    solved_today = [
        r for r in runs.values()
        if r["closed_at"] and moscow_day(datetime.fromisoformat(r["closed_at"])) == day
    ]  # fmt: skip
    hints_today = sum(
        1 for e in conn.execute("SELECT created_at FROM event WHERE type = 'hint'")
        if moscow_day(datetime.fromisoformat(e["created_at"])) == day
    )  # fmt: skip

    revisit = []
    for run in sorted(runs.values(), key=lambda r: -r["id"]):
        task = by_id.get(run["task_item_id"])
        if task is None:
            continue
        flags = _flags(conn, run)
        reason = _reason(run, flags)
        if reason:
            revisit.append({
                "id": task["id"], "number": task["source_number"], "text": task["text"],
                "reason": reason, "sure_wrong": flags["sure_wrong"],
            })  # fmt: skip
    revisit.sort(key=lambda r: not r["sure_wrong"])  # устойчиво: давность внутри групп сохранена

    books = []
    for material in conn.execute("SELECT * FROM material WHERE kind = 'tasks' ORDER BY id DESC"):
        mine = [t for t in tasks if t["material_id"] == material["id"]]
        if not mine:
            continue
        done = sum(1 for t in mine if t["id"] in runs and state.is_terminal(runs[t["id"]]))
        books.append({
            "label": _label(material), "total": len(mine), "done": done,
            "keyed": all(t["answer_key"] is not None for t in mine),
        })  # fmt: skip

    settings = conn.execute(
        "SELECT show_exam_countdown, stage_date FROM settings WHERE id = 1"
    ).fetchone()
    countdown = None
    if settings and settings["show_exam_countdown"] and settings["stage_date"]:
        left = (date.fromisoformat(settings["stage_date"]) - date.fromisoformat(day)).days
        countdown = max(left, 0)
    return {
        "resume": resume,
        "solved_today": len(solved_today),
        "hints_today": hints_today,
        "revisit": revisit[:10],
        "books": books,
        "countdown": countdown,
    }
