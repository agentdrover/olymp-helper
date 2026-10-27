"""Карта заданий ребёнка (OH-K1a): 14 блоков ЦПМК, статус каждого задания, поиск и фильтры.

Статус берётся из прогона разбора по ТЕКУЩИМ ревизии и версии ключа (state.active_run без `now`):
прогон, начатый до правки родителя, не считается. «Верно с 1-й попытки» - то же, что в
outcomes.outcome_label: solved, подсказок нет, попытка первая. Только опубликованные задания;
ключей, ответов модели и заметок здесь нет."""

from app import exam
from app.ingest.publish import child_view, is_published
from app.search.tasks import search_published_tasks
from app.tutor import state
from app.tutor.outcomes import outcome_label

NO_TOPIC = "none"
GENERATED = "generated"
STATUSES = {
    "new": "не решал",
    "ok": "верно",
    "model": "совпало с моделью · не проверено",
    "modelhint": "совпало с моделью, с подсказкой · не проверено",
    "hint": "с подсказкой",
    "wrong": "ошибся",
    "shown": "ответ показан",
}


def theme_id(topic) -> str:
    """Адрес темы в запросе: порядковый номер из трёх цифр (код ребёнку не показываем)."""
    return f"{topic['position']:03d}"


def task_status(task, run) -> str:
    """Ключ из STATUSES. run - прогон по текущей ревизии или None."""
    if run is None or run["state"] == "new" and not run["attempts"] and not run["hints"]:
        return "new"
    if run["state"] == "answer_shown":
        return "shown"
    if run["state"] == "hint3":
        return "hint"  # без ключа ярлык строки - «решение модели по подсказке 3» (см. status_label)
    if task["answer_key"] is None and state.is_terminal(run):
        # без подтверждённого ключа слово «верно» не используется (SPEC 1.2, 1.3 2в):
        # результат - ответ модели, не проверен, с подсказкой или без
        return "modelhint" if run["hints"] else "model"
    if run["state"] == "solved" and not run["hints"]:
        return "ok"
    if run["hints"] or run["state"] == "hint3":
        return "hint"
    if run["attempts"]:
        return "wrong"
    return "new"


def unverified_hint3(task, run) -> bool:
    """Решение по подсказке 3 без подтверждённого ключа: это решение модели, не «совпало»."""
    return run is not None and run["state"] == "hint3" and task["answer_key"] is None


def status_label(task, run, status: str) -> str:
    if unverified_hint3(task, run):
        return outcome_label(False, "hint3", run["attempts"], 3)
    return STATUSES[status]


def _first_try(run) -> bool:
    return (
        run is not None and run["state"] == "solved" and run["hints"] == 0 and run["attempts"] == 1
    )


def _block_of(code, parents) -> str | None:
    return code if code is None or parents.get(code) is None else parents[code]


def _rows(conn):
    """Опубликованные задания с прогоном по текущей ревизии, статусом и блоком."""
    topics = {r["code"]: r for r in conn.execute("SELECT * FROM topic")}
    parents = {code: row["parent_code"] for code, row in topics.items()}
    locked = exam.running_task_ids(conn)  # идёт олимпиада: статусов её заданий ребёнок не видит
    runs = {}
    for run in conn.execute("SELECT * FROM tutor_run ORDER BY id"):
        if run["task_item_id"] in locked:
            continue
        runs[run["task_item_id"]] = run  # последний прогон каждого задания
    materials = {m["id"]: m for m in conn.execute("SELECT * FROM material")}
    out = []
    for task in conn.execute("SELECT * FROM task_item ORDER BY id"):
        if not is_published(task):
            continue
        run = runs.get(task["id"])
        if run is not None and (run["revision"], run["key_version"]) != (
            task["revision"],
            task["key_version"],
        ):
            run = None
        code = task["topic"] if task["topic"] in topics else None
        out.append(
            {
                "task": task,
                "material": materials.get(task["material_id"]),
                "run": run,
                "status": task_status(task, run),
                "code": code,
                "block": _block_of(code, parents),
                "topic_title": topics[code]["title"] if code else None,
                "block_title": topics[_block_of(code, parents)]["title"] if code else None,
            }
        )
    return topics, out


def _summary(rows) -> dict:
    counts = {"ok": 0, "hint": 0, "wrong": 0, "model": 0}
    done = first = unverified = 0
    for r in rows:
        unverified += unverified_hint3(r["task"], r["run"])
        key = {"shown": "wrong", "modelhint": "model"}.get(r["status"], r["status"])
        if key in counts:
            counts[key] += 1
        # «ответ показан» - не «разобрано» (SPEC 1.1)
        if r["run"] is not None and state.is_terminal(r["run"]) and r["status"] != "shown":
            done += 1
        first += _first_try(r["run"])
    total = len(rows)
    keyed_done = done - counts["model"] - unverified  # совпавшие с моделью в долю не входят
    share = round(100 * first / keyed_done) if keyed_done else None
    return {
        "total": total,
        "done": done,
        "first_try": first,
        "model": counts["model"],
        "hint3_model": unverified,
        "first_share": share,
        "bar": {k: round(100 * v / total) if total else 0 for k, v in counts.items()},
    }


def _first_open(rows) -> int | None:
    for r in rows:
        if r["run"] is None or not state.is_terminal(r["run"]):
            return r["task"]["id"]
    return None


def build_blocks(conn) -> list[dict]:
    topics, rows = _rows(conn)
    blocks = []
    heads = [t for t in topics.values() if t["parent_code"] is None]
    for head in sorted(heads, key=lambda t: t["position"]):
        mine = [r for r in rows if r["block"] == head["code"]]
        blocks.append(
            {
                "code": head["code"],
                "pos": theme_id(head),
                "title": head["title"],
                **_summary(mine),
                "next_id": _first_open(mine),
            }
        )
    free = [r for r in rows if r["block"] is None]
    blocks.append(
        {
            "code": NO_TOPIC,
            "pos": NO_TOPIC,
            "title": "Без темы",
            **_summary(free),
            "next_id": _first_open(free),
        }
    )
    return blocks


def _source_ok(row, source: str) -> bool:
    material = row["material"]
    if source == GENERATED:
        return material is None
    if source:
        return material is not None and str(material["year"]) == source
    return True


def _text_hits(conn, q: str) -> set[int]:
    return {hit["id"] for hit in search_published_tasks(conn, q, limit=10_000)}


def filter_tasks(conn, *, q="", theme="", type="", source="", variant="", status="") -> dict:
    """Поиск и фильтры. Пустые параметры не сужают выборку."""
    topics, rows = _rows(conn)
    q = q.strip()
    pick = next((t["code"] for t in topics.values() if theme_id(t) == theme), None)
    hits = _text_hits(conn, q) if q else set()
    out = []
    for r in rows:
        task = r["task"]
        if q:
            titles = [t for t in (r["topic_title"], r["block_title"]) if t]
            by_title = any(q.lower() in t.lower() for t in titles)
            if not (task["id"] in hits or task["source_number"] == q or by_title):
                continue
        if theme == NO_TOPIC and r["block"] is not None:
            continue
        if theme and theme != NO_TOPIC and (pick is None or pick not in (r["code"], r["block"])):
            continue
        if type and task["type"] != type:
            continue
        if not _source_ok(r, source):
            continue
        if variant and (task["variant"] or "") != variant:
            continue
        if status and r["status"] != status:
            continue
        view = child_view(task, r["material"])
        out.append(
            {
                "id": task["id"],
                "number": task["source_number"],
                "source": view["source"],
                "type": task["type"],
                "topic": r["topic_title"],
                "status": r["status"],
                "status_label": status_label(task, r["run"], r["status"]),
                "snippet": " ".join(task["text"].split())[:140],
            }
        )
    years = sorted({r["material"]["year"] for r in rows if r["material"] and r["material"]["year"]})
    variants = sorted({r["task"]["variant"] for r in rows if r["task"]["variant"]})
    has_generated = any(r["material"] is None for r in rows)
    return {
        "tasks": out,
        "years": years,
        "variants": variants,
        "has_generated": has_generated,
        "topics": [
            {**t, "pos": theme_id(t)} for t in sorted(topics.values(), key=lambda t: t["position"])
        ],
    }
