"""Проверка родителем: карточки, лента, подтверждение, правки, ревизии (SPEC-1.0 1.1-1.3а)."""

import json

from app.ingest.answers import display, key_json, key_value, normalize_answer
from app.ingest.keys import candidate
from app.ingest.model_answer import compute_model_answer, model_value
from app.ingest.publish import leak_notice, publication
from app.ingest.schema import InvalidStructure, check_options
from app.ingest.store import (
    atomic,
    figure_of,
    get_task,
    new_revision,
    options_of,
    save_snapshot,
    update_task,
)
from app.ingest.topics import load_catalog
from app.tutor.state import discrepancies

LABELS = {
    "no_key": "ключа нет",
    "discrepancy": "расхождение",
    "needs_figure": "нужен рисунок",
    "open_exclude": "открытый — исключить",
    "child_dispute": "от ребёнка",
    "model_missing": "ответ модели не получен — повторить",
    "confirm": "проверить",
}


class Refusal(Exception):
    """Действие отклонено; message читает родитель."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message = message
        self.status = status


def _task(conn, task_id: int):
    task = get_task(conn, task_id)
    if task is None:
        raise Refusal("Задание не найдено.", 404)
    return task


# ------------------------------------------------------------------------- ключ


def key_info(conn, task):
    """(значение, источник, это кандидат из загруженного ключа). Кандидат ещё не ключ."""
    if task["answer_key"] is not None:
        return key_value(task["answer_key"]), task["key_source"], False
    cand = candidate(conn, task)
    if cand:
        return cand["value"], "official", True
    return None, None, False


def uncovered_lefts(task, key) -> list[str]:
    """Номера левых элементов match, для которых в ключе нет ответа; у других типов пусто.
    Ключ без пары для рисунка нельзя публиковать: ребёнок получил бы задание без проверки."""
    options = options_of(task)
    if task["type"] != "match" or not isinstance(options, dict) or key is None:
        return []
    named = key if isinstance(key, dict) else {}
    return [
        str(x.get("label", "")) for x in options.get("left") or [] if not named.get(x.get("label"))
    ]


UNCOVERED_KEY = "В ключе нет ответа для рисунков: {}. Выберите ответ для каждого рисунка."


def _labels_of(task) -> set[str]:
    options = options_of(task)
    if isinstance(options, list):
        return {normalize_answer(o["label"], "single") for o in options} - {None}
    return set()


def _parse_answer(task, raw) -> object:
    value = normalize_answer(raw, task["type"])
    if value is None:
        what = "число" if task["type"] == "numeric" else "обозначение варианта"
        raise Refusal(f"Не удалось понять ответ «{raw}». Укажите {what}.")
    labels = _labels_of(task)
    named = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    if labels and any(v not in labels for v in named):
        raise Refusal(f"В задании нет варианта «{display(value)}».")
    return value


def set_key(ctx, conn, task_id: int, value, source: str) -> dict:
    """Сменить ключ. У проверенного задания это правка: новая ревизия, «проверено» снято.
    Подтверждённый рисунок проходит защиту от текста ответа заново: решение было для другого ключа;
    то же для рисунков других заданий материала: новый ключ не должен быть напечатан в их вырезках."""
    task = _task(conn, task_id)
    if key_value(task["answer_key"]) == value and task["key_source"] == source:
        return {"revision": task["revision"], "key_version": task["key_version"]}
    # Чтение PDF не зависит от правки: делается до блокировки записи.
    from app.ingest.figure_service import prepare_crop_lines

    prepared = prepare_crop_lines(ctx, conn, task["material_id"])
    with atomic(conn):
        task = _task(conn, task_id)
        if key_value(task["answer_key"]) == value and task["key_source"] == source:
            return {"revision": task["revision"], "key_version": task["key_version"]}
        cols = {
            "answer_key": key_json(value, task["type"]),
            "key_source": source,
            "key_version": task["key_version"] + 1,
        }
        if task["import_status"] == "checked":
            new_revision(conn, task_id, **cols)
        else:
            update_task(conn, task_id, **cols)
            save_snapshot(conn, task_id)
        # figure_service импортирует review
        from app.ingest.figure_service import recheck_material_figures

        recheck_material_figures(ctx, conn, task_id, prepared)
        task = get_task(conn, task_id)
        return {"revision": task["revision"], "key_version": task["key_version"]}


def set_parent_key(ctx, conn, task_id: int, raw, keep_candidate: bool = False) -> dict:
    task = _task(conn, task_id)
    if task["excluded"]:
        raise Refusal("Задание исключено.")
    value = _parse_answer(task, raw)
    # keep_candidate: лента шлёт ответ карточки; если ключа ещё нет и ответ равен кандидату из файла,
    # это не правка родителя - источник не меняется, кандидата поднимет подтверждение. Явный
    # POST /key без флага по-прежнему ставит ключ родителя.
    if keep_candidate and task["answer_key"] is None:
        cand = candidate(conn, task)
        if cand and cand["value"] == value:
            return {"revision": task["revision"], "key_version": task["key_version"]}
    return set_key(ctx, conn, task_id, value, "parent")


# ------------------------------------------------------------------------ правки


def edit_task(
    ctx,
    conn,
    task_id: int,
    *,
    text=None,
    options=None,
    teacher_note=None,
    manual_condition=False,
    topic=None,
) -> dict:
    prepared = None
    if options is not None:  # чтение PDF для проверки рисунков - до блокировки записи
        from app.ingest.figure_service import prepare_crop_lines

        prepared = prepare_crop_lines(ctx, conn, _task(conn, task_id)["material_id"])
    with atomic(conn):
        task = _task(conn, task_id)
        if task["excluded"]:
            raise Refusal("Задание исключено.")
        cols: dict = {}
        if text is not None:
            if not str(text).strip():
                raise Refusal("Условие не может быть пустым.")
            if str(text).strip() != task["text"]:  # то же условие - не правка, ревизии нет
                cols["text"] = str(text).strip()
        if options is not None:
            try:
                checked_options = check_options(task["type"], options)
            except InvalidStructure as exc:
                raise Refusal(str(exc)) from exc
            if checked_options != options_of(task):
                cols["options"] = checked_options
        figure = figure_of(task)
        if teacher_note is not None:
            if figure is None:
                raise Refusal("У задания нет рисунка: пояснение не нужно.")
            figure["teacher_note"] = str(teacher_note)
            cols["figure"] = figure
        if manual_condition:
            if task["type"] == "open":
                raise Refusal("Открытое задание вне v1: его можно только исключить.")
            if task["figure"] is not None or task["unsupported_reason"] is not None:
                cols.update(figure=None, unsupported_reason=None)  # иначе условие не менялось
            if task["import_status"] == "unsupported":
                cols["import_status"] = "draft"
        if topic is not None:
            code = str(topic).strip()
            if code and code not in load_catalog(conn):
                raise Refusal("Такой темы нет в каталоге.")
            if not cols:  # тема - метка для родителя: условие не менялось, ревизия остаётся
                update_task(conn, task_id, topic=code or None)
                return {"revision": task["revision"]}
            cols["topic"] = code or None
        if not cols:
            raise Refusal("Нечего менять.")
        new_revision(conn, task_id, **cols)
        if options is not None:  # варианты задают кандидата ключа, а он - защиту рисунков соседей
            from app.ingest.figure_service import recheck_material_figures

            recheck_material_figures(ctx, conn, task_id, prepared)
        return {"revision": _task(conn, task_id)["revision"]}  # проверка могла создать новую


def exclude_task(conn, task_id: int) -> None:
    _task(conn, task_id)
    update_task(conn, task_id, excluded=1)


# ----------------------------------------------------------------------- споры


def _open_dispute(conn, task) -> int | None:
    row = conn.execute(
        "SELECT MAX(id) FROM event WHERE type = 'disputed_key' AND task_item_id = ?", (task["id"],)
    ).fetchone()
    latest = row[0] or 0
    return latest if latest > task["dispute_cleared_event_id"] else None


def _clear_dispute(conn, task) -> None:
    latest = _open_dispute(conn, task)
    if latest:
        update_task(conn, task["id"], dispute_cleared_event_id=latest)


# ------------------------------------------------------------------ подтверждение


def confirm(ctx, conn, task_id: int, answer=None) -> None:
    """Условие верно (+ ответ подтверждён). Проверка импорта и подтверждение ответа - два
    разных шага одной карточки; источник ключа сохраняется: official только у загруженного."""
    task = _task(conn, task_id)
    if task["excluded"]:
        raise Refusal("Задание исключено.")
    if task["import_status"] == "unsupported":
        raise Refusal(
            task["unsupported_reason"]
            or "Задание не поддерживается: подтвердите рисунок или впишите условие."
        )
    blocked = leak_notice(task)
    if blocked:
        raise Refusal(blocked)
    figure = figure_of(task)
    if figure is not None and not figure.get("confirmed"):
        raise Refusal("Сначала подтвердите рисунок.")
    if figure is not None and not figure.get("no_answer_ack"):
        raise Refusal("Отметьте: в рисунке и в описании нет ответа (шаг 1 карточки рисунка).")
    has_answer = answer is not None and str(answer).strip() != ""
    if has_answer:
        value = _parse_answer(task, answer)
        if value != key_value(task["answer_key"]):
            set_key(ctx, conn, task_id, value, "parent")
    elif task["answer_key"] is None:
        cand = candidate(conn, task)
        if cand:
            set_key(ctx, conn, task_id, cand["value"], "official")
    task = _task(conn, task_id)
    if task["import_status"] == "unsupported":  # новый ключ вернул рисунок на проверку
        raise Refusal(task["unsupported_reason"])
    if figure_of(task) is not None:
        missing = uncovered_lefts(task, key_value(task["answer_key"]))
        if missing:
            raise Refusal(UNCOVERED_KEY.format(", ".join(missing)))
    update_task(conn, task_id, import_status="checked")
    _clear_dispute(conn, task)
    save_snapshot(conn, task_id)
    task = get_task(conn, task_id)
    if figure_of(task) is None and task["model_answer"] is None:
        compute_model_answer(ctx, conn, task_id)


def resolve(ctx, conn, task_id: int, choice: str, answer=None) -> None:
    """Расхождение ключа с моделью: «ключ прав» или «мой ответ …»."""
    task = _task(conn, task_id)
    seen = model_value(task)
    if choice == "key":
        confirm(ctx, conn, task_id)
    elif choice == "mine":
        if answer is None or not str(answer).strip():
            raise Refusal("Укажите свой ответ.")
        confirm(ctx, conn, task_id, answer=answer)
    else:
        raise Refusal("Выберите «ключ прав» или «мой ответ».")
    if seen is not None:
        update_task(conn, task_id, discrepancy_ack=json.dumps(seen, ensure_ascii=False))
    from app.tutor.state import resolve_discrepancies  # T06: расхождения из разбора

    resolve_discrepancies(conn, task_id)


def retry_model(ctx, conn, task_id: int) -> bool:
    task = _task(conn, task_id)
    if figure_of(task) is not None:
        raise Refusal("Для задания с рисунком ответ модели не вычисляется.")
    if task["import_status"] != "checked":
        raise Refusal(
            "Условие этой ревизии ещё не подтверждено: сначала подтвердите его, "
            "затем повторяйте запрос к модели.",
            409,
        )
    if key_info(conn, task)[0] is not None:
        raise Refusal("У задания есть ключ: ответ модели не нужен.", 409)
    if task["model_answer"] is not None:
        raise Refusal(
            "Ответ модели уже получен. Чтобы пересчитать, исправьте задание: это новая ревизия.",
            409,
        )
    return compute_model_answer(ctx, conn, task_id)


# ------------------------------------------------------------------------ карточки


def _discrepancy(task, key, model) -> bool:
    if key is None or model is None or key == model:
        return False
    return task["discrepancy_ack"] != json.dumps(model, ensure_ascii=False)


def build_card(ctx, conn, task, *, deep: bool = False) -> dict | None:
    """Карточка ленты или None, если по заданию родителю нечего делать.

    Только чтение: ответ модели считается лишь после подтверждения условия (POST)."""
    if task["excluded"]:
        return None
    figure = figure_of(task)
    status = task["import_status"]
    key, source, _ = key_info(conn, task)
    model = model_value(task)
    kind, hint = None, ""
    if status == "checked" and _open_dispute(conn, task):
        kind, hint = "child_dispute", "Ребёнок считает ключ спорным. Проверьте ответ."
    elif status == "unsupported":
        if task["type"] == "open":
            kind = "open_exclude"
            hint = task["unsupported_reason"] or "Открытое задание вне v1: исключите его."
        else:
            kind = "needs_figure"
            hint = task["unsupported_reason"] or (
                "Нужен рисунок: подтвердите найденный или поправьте рамку и alt, либо впишите "
                "самодостаточное условие."
            )
    elif status == "checked" and figure is None and key is None and task["model_answer"] is None:
        kind, hint = "model_missing", "Ответ модели не получен. Повторите запрос."
    elif key is None and (figure is not None or status == "draft"):
        kind = "no_key"
        hint = (
            "Для задания с рисунком нужен ключ или ваш ответ: без него оно не публикуется."
            if figure is not None
            else "Ключа нет: впишите ответ (источник — вы) или подтвердите без ключа — "
            "тогда эталоном будет ответ модели."
        )
    elif _discrepancy(task, key, model):
        kind, hint = "discrepancy", "Ответ ключа и ответ модели расходятся."
    elif status == "draft":
        kind, hint = "confirm", "Проверьте, что условие распознано верно."
    if kind is None:
        return None
    # Две разные страницы: текст проверяется по page_from..page_to, рисунок - на своей.
    first, last = task["page_from"], task["page_to"] or task["page_from"]
    pages = list(range(first, last + 1)) if first else []
    figure_page = figure["page"] if figure else None
    text_page = task["text_page"] or first  # страница сохранённого условия
    shown = figure_page if kind == "needs_figure" and figure_page else text_page
    material = task["material_id"]
    agrees = None if key is None or model is None else key == model
    catalog = load_catalog(conn)
    topic = task["topic"] if task["topic"] in catalog else None
    button = None
    if kind == "confirm":
        button = (
            f"Верно, ответ {display(key)} — подтвердить и дальше"
            if key is not None
            else "Верно — подтвердить и дальше"
        )
    card = {
        "task_id": task["id"],
        "kind": kind,
        "label": LABELS[kind],
        "hint": hint,
        "note": task["review_note"],
        "parent_note": task["parent_note"],
        "warning": leak_notice(task),
        "button": button,
        "field": "answer" if kind == "no_key" else None,
        "retry": kind == "model_missing",
        "number": task["source_number"],
        "type": task["type"],
        "text": task["text"],
        "topic": topic,
        "topic_title": catalog[topic]["title"] if topic else None,
        "topics": [{"code": t["code"], "title": t["title"]} for t in catalog.values()],
        "options": options_of(task),
        "material_id": task["material_id"],
        "page": text_page,
        "pages": pages,
        "figure_page": figure_page,
        "page_url": f"/materials/{material}/pages/{shown}.png" if shown else None,
        "page_urls": [{"page": p, "url": f"/materials/{material}/pages/{p}.png"} for p in pages],
        "key": None if key is None else display(key),
        "key_source": source,
        "model_answer": None if model is None else display(model),
        "model_explanation": json.loads(task["model_answer"]).get("explanation")
        if task["model_answer"]
        else None,
        "model_agrees": agrees,
        "figure": figure,
        "revision": task["revision"],
    }
    if kind == "needs_figure" and figure is not None:
        from app.ingest.figure_card import card_extras  # figure_service импортирует review

        card.update(card_extras(ctx, conn, task, figure, figure_page, deep))
    return card


def card_for(ctx, conn, task_id: int) -> dict | None:
    """Карточка одного задания; в очереди PDF не открывается, здесь открывается."""
    return build_card(ctx, conn, _task(conn, task_id), deep=True)


def task_detail(ctx, conn, task_id: int) -> dict:
    task = _task(conn, task_id)
    cand = candidate(conn, task)
    card = build_card(ctx, conn, task, deep=True)
    task = get_task(conn, task_id)
    return {
        "task_id": task["id"],
        "number": task["source_number"],
        "status": task["import_status"],
        "excluded": bool(task["excluded"]),
        "revision": task["revision"],
        "key_version": task["key_version"],
        "text": task["text"],
        "topic": task["topic"],
        "options": options_of(task),
        "key": None if not task["answer_key"] else display(key_value(task["answer_key"])),
        "key_source": task["key_source"],
        "key_candidate": None
        if cand is None
        else {
            "answer": cand["answer"],
            "material_id": cand["material_id"],
            "source": cand["source"],
        },
        "answer_text": task["answer_text"],
        "parent_note": task["parent_note"],
        "model_answer": None
        if not task["model_answer"]
        else json.loads(task["model_answer"])["answer"],
        "figure": figure_of(task),
        "model_discrepancies": discrepancies(conn, task_id),
        "publication": publication(task),
        "card": card,
    }


# --------------------------------------------------------------------------- лента


def queue(ctx, conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM task_item WHERE excluded = 0 ORDER BY id").fetchall()
    return [c for r in rows if (c := build_card(ctx, conn, r))]


def open_feed(ctx, conn) -> dict:
    cards = queue(ctx, conn)
    ctx.feed.order = [c["task_id"] for c in cards]
    return {"total": len(cards), "cards": cards}


def next_card(ctx, conn, current_id: int) -> tuple[dict | None, str | None]:
    """Следующая карточка ленты после current_id и счётчик «N из M»."""
    if current_id not in ctx.feed.order:
        open_feed(ctx, conn)
    order = ctx.feed.order
    start = order.index(current_id) + 1 if current_id in order else 0
    for index in range(start, len(order)):
        card = card_for(ctx, conn, order[index])
        if card:
            return card, f"{index + 1} из {len(order)}"
    return None, None
