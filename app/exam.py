"""Имитация олимпиады (OH-K3a): формат, допуск заданий, сборка, прохождение, сдача, баллы.

Формат этапа вводит родитель; официальных чисел здесь нет. Пока вариант идёт, помощь по его
заданиям закрыта (`task_locked`), а ответы и признаки верности скрыты. Всё состояние - в базе:
срок считается по серверным часам, закрытие по времени происходит при любом обращении
(`expire_due`), поэтому переживает перезапуск.
"""

import json
import random
import sqlite3
from datetime import UTC, datetime, timedelta

from app.ingest.answers import display, key_value, label_key
from app.ingest.publish import is_published
from app.ingest.store import atomic, figure_of, options_of
from app.tutor import seq
from app.tutor.check import BadAnswer, check_answer, parse_answer
from app.tutor.seq import bump

TYPES = ("single", "multiple", "match", "numeric")
TYPE_LABELS = {
    "single": "один ответ",
    "multiple": "несколько ответов",
    "match": "соответствие",
    "numeric": "число",
}
STAGES = {
    "school": "школьный",
    "municipal": "муниципальный",
    "regional": "региональный",
    "final": "заключительный",
}
GRADES = tuple(range(5, 12))
KEY_SOURCES = ("official", "parent")
RUNNING_MESSAGE = "Сейчас идёт олимпиада: помощь, подсказки и проверка закрыты до сдачи."
NOT_SET = "Формат не задан — попроси родителя."
NOT_ADMITTED = "не войдут: нет подтверждённого ключа"


class ExamError(Exception):
    """Действие отклонено; status и code идут в ответ, message читает ребёнок."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _t(now) -> datetime:
    """Момент времени: datetime или функция часов (читается внутри транзакции, после замка)."""
    return now() if callable(now) else now


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _num(value: float):
    """Баллы без хвоста «.0» для показа."""
    value = round(float(value), 6)
    return int(value) if value == int(value) else value


# ------------------------------------------------------------------------- формат


def get_format(conn) -> dict | None:
    row = conn.execute("SELECT * FROM exam_format WHERE id = 1").fetchone()
    if row is None:
        return None
    return {
        "stage": row["stage"],
        "stage_label": STAGES[row["stage"]],
        "grade": row["grade"],
        "minutes": row["minutes"],
        "counts": {t: row[f"count_{t}"] for t in TYPES},
        "points": {t: _num(row[f"points_{t}"]) for t in TYPES},
        "material_ids": json.loads(row["material_ids"]),
        "total": sum(row[f"count_{t}"] for t in TYPES),
    }


def save_format(
    conn, now: datetime, *, stage, grade, minutes, counts, points, material_ids
) -> None:
    """Проверенный формат; ValueError с понятным текстом, если числа не складываются."""
    if stage not in STAGES:
        raise ValueError("Выбери этап.")
    if grade not in GRADES:
        raise ValueError("Класс — от 5 до 11.")
    if not 1 <= minutes <= 1440:
        raise ValueError("Время — от 1 до 1440 минут.")
    for t in TYPES:
        if not 0 <= counts[t] <= 200:
            raise ValueError("Число заданий каждого типа — от 0 до 200.")
        if not 0 <= points[t] <= 1000:
            raise ValueError("Баллы за задание — от 0 до 1000.")
        if counts[t] and points[t] <= 0:
            raise ValueError(
                "Если заданий типа нет в формате, поставь 0 заданий; иначе баллы больше 0."
            )
    if not sum(counts.values()):
        raise ValueError("В формате должно быть хотя бы одно задание.")
    known = {r[0] for r in conn.execute("SELECT id FROM material WHERE kind = 'tasks'")}
    ids = sorted({m for m in material_ids if m in known})
    cols = {"stage": stage, "grade": grade, "minutes": minutes, "updated_at": iso(now)}
    cols |= {f"count_{t}": counts[t] for t in TYPES} | {f"points_{t}": points[t] for t in TYPES}
    cols["material_ids"] = json.dumps(ids)
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
    conn.execute(
        f"INSERT INTO exam_format (id, {names}) VALUES (1, {marks}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}",
        tuple(cols.values()),
    )


# ------------------------------------------------------------------------- допуск


def admitted(task) -> bool:
    """Одно правило допуска: опубликовано, тип из четырёх, непустой ключ родителя или официальный.
    Ответ модели, generated и кандидаты загруженного ключа не допускаются."""
    return (
        is_published(task)
        and task["type"] in TYPES
        and bool(task["answer_key"])
        and key_value(task["answer_key"]) not in (None, "", [], {})
        and task["key_source"] in KEY_SOURCES
    )


def groups(conn) -> list[dict]:
    """Материалы-сборники и их группы (раздел, вариант, этап) с числом допущенных заданий."""
    out = []
    for material in conn.execute("SELECT * FROM material WHERE kind = 'tasks' ORDER BY id"):
        found: dict = {}
        for task in conn.execute(
            "SELECT * FROM task_item WHERE material_id = ? ORDER BY id", (material["id"],)
        ):
            key = (task["section"], task["variant"], task["stage"])
            entry = found.setdefault(key, {"admitted": 0, "skipped": []})
            if admitted(task):
                entry["admitted"] += 1
            else:
                entry["skipped"].append(_label(task))
        for (section, variant, stage), counts in found.items():
            out.append({
                "material_id": material["id"], "material": material["file"],
                "section": section, "variant": variant, "stage": stage, **counts,
            })  # fmt: skip
    return out


def _group_tasks(conn, material_id: int, section, variant, stage) -> list:
    return conn.execute(
        "SELECT * FROM task_item WHERE material_id = ? AND COALESCE(section, '') = ? "
        "AND COALESCE(variant, '') = ? AND COALESCE(stage, '') = ? ORDER BY id",
        (material_id, section or "", variant or "", stage or ""),
    ).fetchall()


def _label(task) -> str:
    return task["source_number"] or str(task["id"])


def stock(conn, fmt: dict) -> dict:
    """По каждому типу: сколько нужно по формату и сколько допущено в выбранных материалах."""
    have = dict.fromkeys(TYPES, 0)
    for mid in fmt["material_ids"]:
        for task in conn.execute("SELECT * FROM task_item WHERE material_id = ?", (mid,)):
            if admitted(task):
                have[task["type"]] += 1
    return {
        t: {"label": TYPE_LABELS[t], "need": fmt["counts"][t], "have": have[t]}
        for t in TYPES
        if fmt["counts"][t]
    }


# ------------------------------------------------------------------------- сборка


def _snapshot_item(task) -> dict:
    figure = figure_of(task)
    return {
        "text": task["text"],
        "options": options_of(task),
        "figure": {"alt": figure.get("alt", "")} if figure else None,
        "number": task["source_number"],
        "revision": task["revision"],
        "key_version": task["key_version"],
        "key_source": task["key_source"],
        "key": key_value(task["answer_key"]),
    }


def _create(conn, now, *, mode, title, fmt, picked, reduced, read_figure=None) -> int:
    snap = {
        "stage": fmt["stage"], "stage_label": fmt["stage_label"], "grade": fmt["grade"],
        "minutes": fmt["minutes"], "points": fmt["points"], "reduced": reduced,
    }  # fmt: skip
    maximum = sum(fmt["points"][t["type"]] for t in picked)
    deadline = now + timedelta(minutes=fmt["minutes"])
    try:
        cur = conn.execute(
            "INSERT INTO exam_attempt (mode, title, started_at, deadline_at, snapshot, max_score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mode, title, iso(now), iso(deadline), json.dumps(snap, ensure_ascii=False), maximum),
        )
    except sqlite3.IntegrityError as exc:  # соседний запрос успел стартовать раньше
        raise ExamError(409, "already_running", "Вариант уже идёт.") from exc
    attempt_id = cur.lastrowid
    for position, task in enumerate(picked, 1):
        figure = figure_of(task)
        png = read_figure(figure["file"]) if figure and read_figure and figure.get("file") else None
        conn.execute(
            "INSERT INTO exam_item (attempt_id, position, task_item_id, task_type, points, "
            "snapshot, figure_png) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, position, task["id"], task["type"], fmt["points"][task["type"]],
             json.dumps(_snapshot_item(task), ensure_ascii=False), png),
        )  # fmt: skip
    _open_view(conn, attempt_id, 1, now)
    bump(conn)  # задания варианта закрылись для панели: версия растёт в той же транзакции
    return attempt_id


def start(
    conn,
    now: datetime,
    *,
    mode: str,
    material_id=None,
    section=None,
    variant=None,
    stage=None,
    accept_reduced: bool = False,
    rng=None,
    read_figure=None,
) -> int:
    """Новое прохождение или уже идущее (повторный старт срок не сбрасывает)."""
    active(
        conn, _t(now)
    )  # просроченное закрывается отдельной транзакцией: отказ сборки её не откатит
    with atomic(conn):
        now = _t(now)
        running = active(conn, now)
        if running is not None:
            return running["id"]
        fmt = get_format(conn)
        if fmt is None:
            raise ExamError(409, "no_format", NOT_SET)
        if mode == "real":
            return _start_real(conn, now, fmt, material_id, section, variant, stage, read_figure)
        if mode == "format":
            return _start_format(
                conn, now, fmt, accept_reduced, rng or random.Random(), read_figure
            )
        raise ExamError(422, "bad_mode", "Выбери способ сборки.")


def _start_real(conn, now, fmt, material_id, section, variant, stage, read_figure) -> int:
    material = conn.execute(
        "SELECT * FROM material WHERE id = ? AND kind = 'tasks'", (material_id,)
    ).fetchone()
    if material is None:
        raise ExamError(404, "no_material", "Такого сборника нет.")
    tasks = _group_tasks(conn, material_id, section, variant, stage)
    picked = [t for t in tasks if admitted(t)]
    skipped = [_label(t) for t in tasks if not admitted(t)]
    if not picked:
        raise ExamError(422, "empty", "В этой группе нет заданий с подтверждённым ключом.",
                        skipped=skipped, note=NOT_ADMITTED)  # fmt: skip
    parts = [p for p in (section, variant, stage) if p]
    title = material["file"] + (" · " + " · ".join(parts) if parts else "")
    return _create(
        conn,
        now,
        mode="real",
        title=title,
        fmt=fmt,
        picked=picked,
        reduced=False,
        read_figure=read_figure,
    )


def _start_format(conn, now, fmt, accept_reduced: bool, rng, read_figure) -> int:
    pool = {t: [] for t in TYPES}
    for mid in fmt["material_ids"]:
        for task in conn.execute(
            "SELECT * FROM task_item WHERE material_id = ? ORDER BY id", (mid,)
        ):
            if admitted(task):
                pool[task["type"]].append(task)
    shortage = {}
    for t in TYPES:
        need, have = fmt["counts"][t], len(pool[t])
        if have < need:
            shortage[t] = {"label": TYPE_LABELS[t], "need": need, "have": have}
    taken = {t: min(fmt["counts"][t], len(pool[t])) for t in TYPES}
    if not sum(taken.values()):
        raise ExamError(422, "empty", "В выбранных материалах нет заданий с подтверждённым ключом.",
                        shortage=shortage)  # fmt: skip
    if shortage and not accept_reduced:
        raise ExamError(409, "shortage", "Заданий не хватает: можно начать меньший вариант.",
                        shortage=shortage)  # fmt: skip
    picked = []
    for t in TYPES:
        picked += sorted(rng.sample(pool[t], taken[t]), key=lambda x: x["id"])
    title = f"По формату: {fmt['stage_label']} этап, {fmt['grade']} класс"
    return _create(conn, now, mode="format", title=title, fmt=fmt, picked=picked,
                   reduced=bool(shortage), read_figure=read_figure)  # fmt: skip


# ------------------------------------------------------------------ прохождение


def _open_view(conn, attempt_id: int, position: int, at: datetime) -> None:
    """Открытие задания закрывает предыдущий открытый интервал (в том числе из другой вкладки)."""
    open_row = conn.execute(
        "SELECT id, position FROM exam_view WHERE attempt_id = ? AND closed_at IS NULL",
        (attempt_id,),
    ).fetchone()
    if open_row is not None:
        if open_row["position"] == position:
            return
        conn.execute("UPDATE exam_view SET closed_at = ? WHERE id = ?", (iso(at), open_row["id"]))
    conn.execute(
        "INSERT INTO exam_view (attempt_id, position, opened_at) VALUES (?, ?, ?)",
        (attempt_id, position, iso(at)),
    )


def _close_views(conn, attempt_id: int, at: datetime) -> None:
    conn.execute(
        "UPDATE exam_view SET closed_at = ? WHERE attempt_id = ? AND closed_at IS NULL",
        (iso(at), attempt_id),
    )


def _score(conn, attempt_id: int) -> tuple[float, int]:
    total, correct = 0.0, 0
    for item in conn.execute("SELECT * FROM exam_item WHERE attempt_id = ?", (attempt_id,)):
        snap = json.loads(item["snapshot"])
        given = None if item["answer"] is None else json.loads(item["answer"])
        ok = given is not None and check_answer(item["task_type"], snap["key"], given)
        earned = item["points"] if ok else 0
        conn.execute(
            "UPDATE exam_item SET is_correct = ?, earned = ? WHERE attempt_id = ? AND position = ?",
            (1 if ok else 0, earned, attempt_id, item["position"]),
        )
        total += earned
        correct += 1 if ok else 0
    return round(total, 6), correct


def _finish(conn, attempt, reason: str, at: datetime) -> None:
    """Закрыть прохождение: интервалы закрыты на момент at, баллы посчитаны по снимку."""
    _close_views(conn, attempt["id"], at)
    score, correct = _score(conn, attempt["id"])
    conn.execute(
        "UPDATE exam_attempt SET status = 'submitted', end_reason = ?, submitted_at = ?, "
        "score = ?, correct_count = ? WHERE id = ? AND status = 'running'",
        (reason, iso(at), score, correct, attempt["id"]),
    )
    bump(conn)  # задания варианта открылись для панели


def expire_due(conn, now: datetime) -> None:
    """Закрыть по времени все прохождения, у которых срок вышел."""
    due = conn.execute(
        "SELECT * FROM exam_attempt WHERE status = 'running' AND deadline_at <= ?", (iso(now),)
    ).fetchall()
    if not due:
        return
    with atomic(conn):
        for attempt in conn.execute(
            "SELECT * FROM exam_attempt WHERE status = 'running' AND deadline_at <= ?",
            (iso(now),),
        ).fetchall():
            _finish(conn, attempt, "time", _parse(attempt["deadline_at"]))


def active(conn, now: datetime):
    """Идущее прохождение (после закрытия просроченных) или None. Вызывается всеми маршрутами,
    которым нужна активность варианта."""
    expire_due(conn, now)
    return conn.execute("SELECT * FROM exam_attempt WHERE status = 'running'").fetchone()


def locked_task_ids(conn, now: datetime) -> frozenset[int]:
    attempt = active(conn, now)
    if attempt is None:
        return frozenset()
    return frozenset(
        r[0]
        for r in conn.execute(
            "SELECT task_item_id FROM exam_item WHERE attempt_id = ? AND task_item_id IS NOT NULL",
            (attempt["id"],),
        )
    )


def task_running(conn, task_id: int) -> bool:
    """Только чтение, без закрытия просроченных (годится внутри снимка чтения): задание входит в
    идущее прохождение. Просроченное, но не закрытое считается идущим - строго безопаснее."""
    return (
        conn.execute(
            "SELECT 1 FROM exam_item i JOIN exam_attempt a ON a.id = i.attempt_id "
            "WHERE a.status = 'running' AND i.task_item_id = ?",
            (task_id,),
        ).fetchone()
        is not None
    )


def task_locked(conn, task_id: int, now: datetime) -> bool:
    """«Идёт олимпиада» для задания: единственная проверка всех маршрутов задания."""
    return task_id in locked_task_ids(conn, now)


def _item(conn, attempt_id: int, position: int):
    item = conn.execute(
        "SELECT * FROM exam_item WHERE attempt_id = ? AND position = ?", (attempt_id, position)
    ).fetchone()
    if item is None:
        raise ExamError(404, "no_item", "Такого задания в варианте нет.")
    return item


def _mutate(conn, now, attempt_id: int, work):
    """Сохранение атомарно со сроком: время читается после получения замка записи; при времени
    >= срока изменение отклоняется, вариант закрывается по времени. Запрос от другого
    (завершённого) прохождения отклоняется."""
    with atomic(conn):
        t = _t(now)
        attempt = conn.execute("SELECT * FROM exam_attempt WHERE status = 'running'").fetchone()
        if attempt is None or attempt["id"] != attempt_id:
            raise ExamError(
                409, "not_running", "Этот вариант уже не идёт: он сдан или время вышло."
            )
        if t >= _parse(attempt["deadline_at"]):
            _finish(conn, attempt, "time", _parse(attempt["deadline_at"]))
            return "timeout"
        work(attempt, t)
    return "ok"


def _refuse_timeout() -> ExamError:
    return ExamError(409, "time_up", "Время вышло: вариант сдан.")


def open_item(conn, now, attempt_id: int, position: int) -> None:
    def work(attempt, t):
        _item(conn, attempt["id"], position)
        _open_view(conn, attempt["id"], position, t)

    if _mutate(conn, now, attempt_id, work) == "timeout":
        raise _refuse_timeout()


def save_answer(conn, now, attempt_id: int, position: int, raw) -> None:
    def work(attempt, t):
        item = _item(conn, attempt["id"], position)
        snap = json.loads(item["snapshot"])
        if raw is None or raw == [] or raw == {} or (isinstance(raw, str) and not raw.strip()):
            value = None
        else:
            try:
                value = parse_answer(item["task_type"], snap["options"], raw)
            except BadAnswer as exc:
                raise ExamError(422, "bad_answer", str(exc)) from exc
        conn.execute(
            "UPDATE exam_item SET answer = ? WHERE attempt_id = ? AND position = ?",
            (None if value is None else json.dumps(value, ensure_ascii=False),
             attempt["id"], position),
        )  # fmt: skip

    if _mutate(conn, now, attempt_id, work) == "timeout":
        raise _refuse_timeout()


def set_mark(conn, now, attempt_id: int, position: int, marked: bool) -> None:
    def work(attempt, t):
        _item(conn, attempt["id"], position)
        conn.execute(
            "UPDATE exam_item SET marked = ? WHERE attempt_id = ? AND position = ?",
            (1 if marked else 0, attempt["id"], position),
        )

    if _mutate(conn, now, attempt_id, work) == "timeout":
        raise _refuse_timeout()


def submit(conn, now, attempt_id: int, confirm: bool = True) -> dict:
    """Сдача идемпотентна: повторная возвращает тот же итог, ответы после сдачи не меняются."""
    with atomic(conn):
        now = _t(now)
        attempt = conn.execute("SELECT * FROM exam_attempt WHERE id = ?", (attempt_id,)).fetchone()
        if attempt is None:
            raise ExamError(404, "no_attempt", "Такого прохождения нет.")
        if attempt["status"] == "running":
            deadline = _parse(attempt["deadline_at"])
            if now < deadline and not confirm:
                raise ExamError(422, "confirm_required", "Подтверди сдачу варианта.")
            if now >= deadline:
                _finish(conn, attempt, "time", deadline)
            else:
                _finish(conn, attempt, "manual", now)
    return result(conn, attempt_id, now)


# ----------------------------------------------------------------------- показ


def _view_seconds(conn, attempt_id: int) -> dict[int, float]:
    out: dict[int, float] = {}
    for row in conn.execute(
        "SELECT position, opened_at, closed_at FROM exam_view WHERE attempt_id = ?", (attempt_id,)
    ):
        if row["closed_at"] is None:
            continue
        span = (_parse(row["closed_at"]) - _parse(row["opened_at"])).total_seconds()
        out[row["position"]] = out.get(row["position"], 0.0) + max(span, 0.0)
    return out


def _current_position(conn, attempt_id: int) -> int:
    row = conn.execute(
        "SELECT position FROM exam_view WHERE attempt_id = ? ORDER BY id DESC LIMIT 1",
        (attempt_id,),
    ).fetchone()
    return row[0] if row else 1


def _with_keys(options):
    """Обозначение варианта и его ключ в виде, в котором его сравнивает проверка ответа
    (label_key): экран сверяет выбор и сохранённый ответ по ключу, а не по сырой метке."""
    if isinstance(options, list):
        return [{**o, "token": label_key(o["label"])} for o in options]
    if isinstance(options, dict):
        return {side: _with_keys(rows) for side, rows in options.items()}
    return options


def running_view(conn, attempt, now=None) -> dict:
    """Что видит ребёнок, пока вариант идёт: условия, свои ответы и отметки. Ни ключа, ни
    признака верности."""
    snap = json.loads(attempt["snapshot"])
    items = []
    for item in conn.execute(
        "SELECT * FROM exam_item WHERE attempt_id = ? ORDER BY position", (attempt["id"],)
    ):
        s = json.loads(item["snapshot"])
        figure = s["figure"]
        items.append({
            "position": item["position"],
            "type": item["task_type"],
            "number": s["number"],
            "text": s["text"],
            "options": _with_keys(s["options"]),
            "figure": None if figure is None else {
                "url": f"/exam/figure/{item['position']}.png", "alt": figure["alt"],
            },
            "answer": None if item["answer"] is None else json.loads(item["answer"]),
            "marked": bool(item["marked"]),
        })  # fmt: skip
    return {
        "id": attempt["id"],
        "status": "running",
        "server_now": iso(_t(now)) if now is not None else None,
        "title": attempt["title"],
        "stage": snap["stage_label"],
        "grade": snap["grade"],
        "started_at": attempt["started_at"],
        "deadline_at": attempt["deadline_at"],
        "reduced": snap["reduced"],
        "current": _current_position(conn, attempt["id"]),
        "items": items,
    }


def _overlaps_now(conn, attempt_id: int) -> bool:
    """Только чтение (годится внутри снимка): задания этого прохождения входят в идущее."""
    return (
        conn.execute(
            "SELECT 1 FROM exam_item mine JOIN exam_item live ON live.task_item_id = mine.task_item_id "
            "JOIN exam_attempt a ON a.id = live.attempt_id "
            "WHERE mine.attempt_id = ? AND a.status = 'running' AND mine.task_item_id IS NOT NULL "
            "LIMIT 1",
            (attempt_id,),
        ).fetchone()
        is not None
    )


def _overlaps_running(conn, attempt_id: int, now: datetime) -> bool:
    """Предварительная проверка: закрывает просроченные и смотрит, скрыты ли результаты. Данные
    читаются в снимке, где проверка повторяется: между ними вариант мог стартовать."""
    expire_due(conn, now)
    return _overlaps_now(conn, attempt_id)


def result(conn, attempt_id: int, now: datetime) -> dict:
    """Итог сданного прохождения; идущее не показывается."""
    attempt = conn.execute("SELECT * FROM exam_attempt WHERE id = ?", (attempt_id,)).fetchone()
    if attempt is None:
        raise ExamError(404, "no_attempt", "Такого прохождения нет.")
    if attempt["status"] == "running":
        raise ExamError(409, "exam_running", RUNNING_MESSAGE)
    if _overlaps_running(conn, attempt_id, now):
        raise ExamError(409, "exam_running", RUNNING_MESSAGE)
    with seq.read_snapshot(conn):  # проверка и данные - из одного состояния базы
        if _overlaps_now(conn, attempt_id):
            raise ExamError(409, "exam_running", RUNNING_MESSAGE)
        return _result_data(conn, attempt_id)


def _result_data(conn, attempt_id: int) -> dict:
    attempt = conn.execute("SELECT * FROM exam_attempt WHERE id = ?", (attempt_id,)).fetchone()
    snap = json.loads(attempt["snapshot"])
    seconds = _view_seconds(conn, attempt_id)
    items = []
    for item in conn.execute(
        "SELECT * FROM exam_item WHERE attempt_id = ? ORDER BY position", (attempt_id,)
    ):
        given = None if item["answer"] is None else json.loads(item["answer"])
        s = json.loads(item["snapshot"])
        entry = {
            "position": item["position"],
            "type": item["task_type"],
            "number": s["number"],
            "text": s["text"],
            "answer": None if given is None else display(given),
            "answered": given is not None,
            "correct": bool(item["is_correct"]),
            "points": _num(item["earned"] or 0),
            "max_points": _num(item["points"]),
            "seconds": round(seconds.get(item["position"], 0.0)),
            "review_url": None,
        }
        if not entry["correct"] and item["task_item_id"] is not None:
            entry["review_url"] = f"/tasks/{item['task_item_id']}"
        items.append(entry)
    return {
        "id": attempt["id"],
        "status": "submitted",
        "end_reason": attempt["end_reason"],
        "title": attempt["title"],
        "stage": snap["stage_label"],
        "grade": snap["grade"],
        "reduced": snap["reduced"],
        "started_at": attempt["started_at"],
        "submitted_at": attempt["submitted_at"],
        "score": _num(attempt["score"]),
        "max_score": _num(attempt["max_score"]),
        "correct": attempt["correct_count"],
        "total": len(items),
        "items": items,
    }


def figure_png(conn, attempt_id: int, position: int) -> bytes | None:
    row = conn.execute(
        "SELECT figure_png FROM exam_item WHERE attempt_id = ? AND position = ?",
        (attempt_id, position),
    ).fetchone()
    return row[0] if row else None


def history(conn, now: datetime) -> list[dict]:
    expire_due(conn, now)
    with seq.read_snapshot(conn):  # скрытие и данные строк - из одного состояния базы
        return [
            {
                "id": r["id"], "title": r["title"], "submitted_at": r["submitted_at"],
                "score": _num(r["score"]), "max_score": _num(r["max_score"]),
            }
            for r in conn.execute(
                "SELECT * FROM exam_attempt WHERE status = 'submitted' ORDER BY id DESC LIMIT 20"
            ).fetchall()
            if not _overlaps_now(conn, r["id"])
        ]  # fmt: skip
