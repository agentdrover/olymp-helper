"""Импорт материала: текст по страницам, разбиение на задания моделью, запись (SPEC-1.0 1.7)."""

import json
import logging
import re
import threading

import pymupdf

from app.ingest.extract import chunk_pages, extract_pages
from app.ingest.figures import PageIndex, crop_leaks, suggest_regions
from app.ingest.keys import parse_key_text
from app.ingest.leaks import answer_units, crop_units, is_drawing
from app.ingest.prompt import SYSTEM_PROMPT, TASKS_SCHEMA, build_chunk_message
from app.ingest.schema import (
    NUMBER,
    InvalidStructure,
    _label,
    validate_section_at_end,
    validate_task,
    validate_tasks,
)
from app.ingest.store import open_db, save_snapshot
from app.ingest.topics import catalog_prompt, load_catalog
from app.search import index_pages

log = logging.getLogger("app.ingest")

FIGURE_WORDS = re.compile(
    r"рисун|рис\.|схем|таблиц|график|диаграмм|изображ|фотограф", re.IGNORECASE
)
NO_TEXT = "В PDF нет текстового слоя (скан): сканы и распознавание картинок вне v1."


class ImportFailure(Exception):
    """Понятная причина, по которой импорт не удался; текст читает родитель."""


class ImportAborted(Exception):
    """Импорт прерван (таймаут): результат уже не записывается."""


def _check(cancel: threading.Event):
    if cancel.is_set():
        raise ImportAborted


META = ("section", "variant", "stage")


def merge_labels(state: dict, labels: dict) -> dict:
    """Единственное правило слияния меток (раздел, вариант, этап): для заданий и для
    section_at_end. Другой раздел - новое состояние (не названные variant и stage сбрасываются);
    тот же раздел или раздел не назван - названное заменяет, не названное наследуется."""
    if labels.get("section") and labels["section"] != state.get("section"):
        return {name: labels.get(name) for name in META}
    return {name: labels.get(name) or state.get(name) for name in META}


COMBINED_NOTE = "условие собрано из двух порций"


def _norm_text(text: str) -> str:
    return " ".join(text.split())


OPTIONS_NOTE = "условие собрано из двух порций; проверьте варианты"


def _contains(a: str, b: str) -> bool:
    a, b = _norm_text(a).casefold(), _norm_text(b).casefold()
    return a in b or b in a


def _merge_option_list(old: list, new: list) -> tuple[list, bool]:
    """Объединение по обозначению; для одного обозначения побеждает более длинный текст.
    Второй результат - расхождение: обозначение есть только с одной стороны или тексты
    одного обозначения не содержат друг друга."""
    merged = {o["label"]: dict(o) for o in old}
    differs = False
    for option in new:
        label = option["label"]
        if label not in merged:
            merged[label] = dict(option)
            differs = True
            continue
        mine = merged[label]
        if not _contains(mine["text"], option["text"]):
            differs = True
        if len(_norm_text(option["text"])) > len(_norm_text(mine["text"])):
            merged[label] = dict(option)
    differs = differs or {o["label"] for o in old} != {o["label"] for o in new}
    return list(merged.values()), differs


def merge_options(old, new) -> tuple[object, bool]:
    if old is None or new is None:
        return (old if new is None else new), False
    if isinstance(old, dict) and isinstance(new, dict):
        out, differs = {}, False
        for side in ("left", "right"):
            out[side], d = _merge_option_list(old.get(side, []), new.get(side, []))
            differs = differs or d
        return out, differs
    if isinstance(old, list) and isinstance(new, list):
        return _merge_option_list(old, new)
    return old, True


def _text_page(task: dict) -> int:
    return task.get("text_page") or task["pages"][0]


def _figure_pages(task: dict) -> list[int]:
    """Страницы порций, где модель отметила рисунок."""
    if "figure_pages" in task:
        return task["figure_pages"]
    return list(task["pages"]) if task["refers_to_figure"] else []


def merge_repeat(old: dict, new: dict) -> dict:
    """Повтор (раздел, номер) в другой порции, поля объединяются отдельно: условие - более
    длинное (по нормализованной длине), варианты - по обозначению, страницы - объединение,
    признак рисунка - ИЛИ. Расхождение условий или вариантов - пометка родителю."""
    a, b = _norm_text(old["text"]), _norm_text(new["text"])
    keep = new if len(b) > len(a) else old
    merged = dict(keep)
    merged["text_page"] = _text_page(keep)  # факт: где начинается оставленное условие
    merged["pages"] = sorted(set(old["pages"]) | set(new["pages"]))
    merged["refers_to_figure"] = old["refers_to_figure"] or new["refers_to_figure"]
    merged["figure_pages"] = sorted(set(_figure_pages(old)) | set(_figure_pages(new)))
    # Ответ со страницы: из порций берётся более полный.
    merged["answer_text"] = max(
        old.get("answer_text"), new.get("answer_text"), key=lambda t: len(t or "")
    )
    if old["type"] != new["type"]:
        # Тип и варианты - одна пара: смешивать варианты разных типов нельзя.
        merged["options"], options_differ = keep["options"], True
    else:
        merged["options"], options_differ = merge_options(old["options"], new["options"])
    note = old.get("review_note") or new.get("review_note")
    if not _contains(a, b):
        note = COMBINED_NOTE
    if options_differ:
        note = OPTIONS_NOTE
    merged["review_note"] = note
    merged["topic"] = keep.get("topic") or old.get("topic") or new.get("topic")
    return merged


RETRY_NOTE = "Предыдущий ответ отклонён: {error} Верни ответ строго по схеме."
MAX_SKIPPED_NOTE = 1000


def _reason(exc: InvalidStructure) -> str:
    return str(exc).partition("): ")[2].rstrip(".") or str(exc)


def _span(numbers: list[int]) -> str:
    first, last = min(numbers), max(numbers)
    return f"стр. {first}" if first == last else f"стр. {first}–{last}"


def _ask(ctx, system: str, message: str, cancel: threading.Event):
    _check(cancel)
    result = ctx.model.complete(system, [{"role": "user", "content": message}], TASKS_SCHEMA)
    _check(cancel)
    if not getattr(result, "ok", False):
        raise ImportFailure("Модель не ответила при разборе страниц. Повторите загрузку позже.")
    return result.json


def _validate_chunk(payload, numbers: set[int], catalog) -> tuple[list[dict], dict | None]:
    return validate_tasks(payload, numbers, catalog), validate_section_at_end(payload)


def _labels_only(raw) -> dict:
    """Отклонённое задание всё равно переключает раздел, вариант и этап: каждое поле отдельно,
    негодное поле игнорируется."""
    out: dict = {"labels_only": True}
    for name, limit in (("section", 120), ("variant", 20), ("stage", 60)):
        try:
            out[name] = _label(raw.get(name) if isinstance(raw, dict) else None, "", name, limit)
        except InvalidStructure:
            out[name] = None
    return out


def _section_at_end(payload) -> dict | None:
    try:
        return validate_section_at_end(payload)
    except InvalidStructure:  # заголовок раздела не разобран: состояние не меняется
        return None


def _salvage(payload, numbers: set[int], catalog, skipped: list[str]):
    """Ответ не прошёл проверку дважды: годные задания берутся поштучно, негодные - в отчёт.
    Страницы в отчёте - только порции: страницам из ответа модели веры нет."""
    chunk_span = _span(sorted(numbers))
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        try:
            validate_tasks(payload, numbers, catalog)
        except InvalidStructure as exc:
            skipped.append(f"{chunk_span} ({_reason(exc)})")
        return [], _section_at_end(payload)
    good = []
    for i, raw in enumerate(payload["tasks"]):
        try:
            good.append(validate_task(raw, i, numbers, catalog))
        except InvalidStructure as exc:
            good.append(_labels_only(raw))
            where = chunk_span
            number = str(raw.get("number", "")).strip() if isinstance(raw, dict) else ""
            if NUMBER.fullmatch(number):  # номер из ответа модели - только если он годный
                where = f"задание {number} на {where}"
            skipped.append(f"{where} ({_reason(exc)})")
    return good, _section_at_end(payload)


def _chunk_tasks(ctx, system, chunk, state, catalog, cancel, skipped):
    """(задания порции, section_at_end). Неверная структура: одна повторная просьба с текстом
    ошибки, затем поштучный разбор; порция не валит импорт."""
    numbers = {n for n, _ in chunk}
    message = build_chunk_message(chunk, state)
    payload = _ask(ctx, system, message, cancel)
    try:
        return _validate_chunk(payload, numbers, catalog)
    except InvalidStructure as exc:
        note = RETRY_NOTE.format(error=exc.safe)
    payload = _ask(ctx, system, f"{message}\n\n{note}", cancel)
    try:
        return _validate_chunk(payload, numbers, catalog)
    except InvalidStructure:
        return _salvage(payload, numbers, catalog, skipped)


def skipped_note(skipped: list[str]) -> str | None:
    if not skipped:
        return None
    text = "Не разобраны: " + "; ".join(skipped)
    if len(text) > MAX_SKIPPED_NOTE:
        text = text[: MAX_SKIPPED_NOTE - 1].rstrip() + "…"
    return text


def split_tasks(ctx, pages, cancel: threading.Event, skipped: list[str] | None = None, report=None):
    """Задания из всех порций. Номера повторяются по разделам: ключ дедупликации - (раздел,
    номер); состояние раздела (раздел, вариант, этап) переходит между порциями. Не разобранное
    добавляется в skipped. report(порция, всего порций, принято заданий) - ход разбора."""
    skipped = [] if skipped is None else skipped
    seen: dict[tuple, dict] = {}
    state = dict.fromkeys(META)  # состояние на конец предыдущей порции
    conn = open_db(ctx.db_path)
    try:
        catalog = load_catalog(conn)
    finally:
        conn.close()
    system = SYSTEM_PROMPT + "\n" + catalog_prompt(catalog)
    chunks = chunk_pages(pages)
    for index, chunk in enumerate(chunks, start=1):
        if report is not None:
            report(index, len(chunks), len(seen))
        parsed, at_end = _chunk_tasks(ctx, system, chunk, state, catalog, cancel, skipped)
        for task in parsed:
            state = merge_labels(state, task)
            if task.get("labels_only"):
                continue
            task.update(state)
            key = (task["section"], task["number"])
            seen[key] = merge_repeat(seen[key], task) if key in seen else task
        if at_end:  # заголовок после последнего задания важнее состояния последнего задания
            state = merge_labels(state, at_end)
        if report is not None:
            report(index, len(chunks), len(seen))
    return list(seen.values())


DRAWING_REASON = (
    "Задание на рисование вне v1: ответ рисуют, а не вводят. Элементы ответа остались "
    "в заметке; задание можно исключить."
)
SCORED_REASON = (
    "Развёрнутый ответ с баллами вне v1: его проверяет родитель. Эталон остался в заметке; "
    "задание можно исключить."
)
OPEN_REASON = "Открытое задание вне v1: его можно исключить."
FIGURE_REASON = (
    "Условие зависит от рисунка, схемы или таблицы: подтвердите рисунок или впишите условие."
)


def _classify(task: dict) -> tuple[str | None, bool]:
    """(причина неподдерживаемости или None, нужен ли рисунок для подтверждения)."""
    if task["type"] == "open":
        if is_drawing(task["text"]):
            return DRAWING_REASON, False
        scored = task["points"] or "балл" in (task.get("answer_text") or "").lower()
        return (SCORED_REASON if scored else OPEN_REASON), False
    if task["refers_to_figure"] or FIGURE_WORDS.search(task["text"]):
        return FIGURE_REASON, True
    return None, False


AMBIGUOUS_REASON = (
    "Начало условия повторяется в сборнике — обведите рисунок вручную: автоматически "
    "его границы определить нельзя."
)
UNKNOWN_REASON = (
    "Начало условия не найдено на страницах — обведите рисунок вручную: автоматически "
    "его границы определить нельзя."
)


def _figure_record(
    doc, material_id: int, task: dict, located, stop=None, others: list[str] | None = None
) -> dict:
    """located - итог PageIndex.locate: ('known', [место]), ('ambiguous', ...) или ('unknown', []);
    stop - ближайшее по месту начало любого другого условия; others - тексты ответов других
    заданий, которых в вырезке быть не должно."""
    kind, places = located
    page = (_figure_pages(task) or task["pages"])[0]
    regions = []
    if kind == "known" and doc is not None:
        first, start_y = places[0]
        regions = suggest_regions(doc, first, start_y, max(max(task["pages"]), first), stop)
    leak = []
    if regions:  # ответ или варианты, уже напечатанные в предложенной области
        units = crop_units(task.get("answer_text"), [], task["options"], task["type"])
        leak = crop_leaks(doc, regions, [*units, *(others or [])])
    return {
        "material_id": material_id,
        "page": regions[0]["page"] if regions else page,
        "rect": regions[0]["rect"] if regions else None,
        "regions": regions,
        "ambiguous": kind == "ambiguous",
        "anchor_unknown": kind == "unknown",
        "leak": leak,
        "suggested": bool(regions),
        "confirmed": False,
        "alt": f"Рисунок к заданию {task['number']}, страница {page}",
        "teacher_note": "",
        "no_answer_ack": False,
        "teacher_note_ack": False,
        "leak_ack": False,
        "file": None,
    }


_VARIANT_IN_SECTION = re.compile(r"вариант\s*№?\s*(\d+)", re.IGNORECASE)


def _task_variant(task: dict, material) -> str | None:
    if task["variant"]:
        return task["variant"]
    found = _VARIANT_IN_SECTION.search(task["section"] or "")
    return found.group(1) if found else material["variant"]


def _positions(index: PageIndex, tasks: list[dict]) -> list:
    """Итог PageIndex.locate для каждого задания."""
    return [index.locate(task["text"]) for task in tasks]


def _stops(located: list) -> list:
    """Все известные начала условий в материале: и однозначные, и повторяющиеся (все их места)."""
    return [place for _, places in located for place in places]


def _following(located: list, stops: list, index: int):
    """Начало ближайшего ПО МЕСТУ другого условия (страница, y) строго ниже якоря задания, в
    порядке страниц, а не в порядке модели. Ниже ничего нет - None: границы снизу нет."""
    kind, places = located[index]
    if kind != "known":
        return None
    later = [p for p in stops if p > places[0]]
    return min(later) if later else None


def _foreign(task: dict) -> list[str]:
    """Тексты ответа задания, которые не должны оказаться в рисунке соседнего."""
    return answer_units(task.get("answer_text"))


def _write_tasks(conn, material, tasks, pdf: bytes) -> int:
    doc = pymupdf.open("pdf", pdf)
    try:
        located = _positions(PageIndex(doc), tasks)
        stops = _stops(located)
        for index, task in enumerate(tasks):
            reason, wants_figure = _classify(task)
            figure = None
            if wants_figure:
                others = [u for i, t in enumerate(tasks) if i != index for u in _foreign(t)]
                figure = _figure_record(
                    doc,
                    material["id"],
                    task,
                    located[index],
                    _following(located, stops, index),
                    others,
                )
                if figure["ambiguous"]:
                    reason = AMBIGUOUS_REASON
                elif figure["anchor_unknown"]:
                    reason = UNKNOWN_REASON
            # У задания без проверки ответа нет кандидата ключа: ответ со страницы - заметка.
            note = task.get("review_note")
            answer_text = task.get("answer_text")
            parent_note = None
            if reason and not wants_figure:
                parent_note, answer_text = answer_text, None
            cur = conn.execute(
                "INSERT INTO task_item (material_id, page_from, page_to, variant, source_number, "
                "type, text, options, points, topic, import_status, figure, unsupported_reason, "
                "section, stage, review_note, text_page, answer_text, parent_note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    material["id"],
                    min(task["pages"]),
                    max(task["pages"]),
                    _task_variant(task, material),
                    task["number"],
                    task["type"],
                    task["text"],
                    None
                    if task["options"] is None
                    else json.dumps(task["options"], ensure_ascii=False),
                    task["points"],
                    task["topic"],
                    "unsupported" if reason else "draft",
                    None if figure is None else json.dumps(figure, ensure_ascii=False),
                    reason,
                    task["section"],
                    task["stage"] or material["stage"],
                    note,
                    _text_page(task),
                    answer_text,
                    parent_note,
                ),
            )
            save_snapshot(conn, cur.lastrowid)
    finally:
        doc.close()
    return len(tasks)


def _finish(
    conn, material_id: int, run_id: str, pages_total: int, write, after=None, note=None
) -> bool:
    """Записать результат и status=done одной транзакцией, если запуск run_id ещё running.
    after выполняется в той же транзакции после status=done: его отказ откатывает всё."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        owner = conn.execute(
            "SELECT import_status, run_id FROM material WHERE id = ?", (material_id,)
        ).fetchone()
        if (owner[0], owner[1]) != ("running", run_id):
            conn.execute("ROLLBACK")
            return False
        write()
        conn.execute(
            "UPDATE material SET import_status = 'done', import_error = ?, pages_total = ? "
            "WHERE id = ?",
            (note, pages_total, material_id),
        )
        if after is not None:
            after()
        conn.execute("COMMIT")
        return True
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def fail_material(conn, material_id: int, run_id: str, message: str) -> None:
    """Пометить failed только свой запуск: чужой (новый) run_id не трогается."""
    conn.execute(
        "UPDATE material SET import_status = 'failed', import_error = ? "
        "WHERE id = ? AND run_id = ? AND import_status IN ('queued', 'running')",
        (message, material_id, run_id),
    )


def run_import(ctx, material_id: int, cancel: threading.Event, run_id: str) -> None:
    conn = open_db(ctx.db_path)
    try:
        conn.execute(
            "UPDATE material SET import_status = 'running' "
            "WHERE id = ? AND run_id = ? AND import_status = 'queued'",
            (material_id, run_id),
        )
        material = conn.execute("SELECT * FROM material WHERE id = ?", (material_id,)).fetchone()
        if (
            material is None
            or material["import_status"] != "running"
            or material["run_id"] != run_id
        ):
            return
        try:
            pdf = ctx.material_path(material["file"]).read_bytes()
            pages = extract_pages(pdf)
            if not any(text for _, text in pages):
                raise ImportFailure(NO_TEXT)
            _check(cancel)
            if material["kind"] == "textbook":
                _finish(
                    conn,
                    material_id,
                    run_id,
                    len(pages),
                    lambda: index_pages(conn, material_id, pages),
                )
            elif material["kind"] == "key":
                entries = parse_key_text(pages)
                if not entries:
                    raise ImportFailure("В файле не найдено строк ключа вида «12. Б».")

                from app.ingest import figure_service as figs

                before: dict = {}

                def write_key():
                    before.update(figs.candidate_snapshot(conn))
                    conn.execute("DELETE FROM key_entry WHERE material_id = ?", (material_id,))
                    conn.executemany(
                        "INSERT INTO key_entry (material_id, number, answer) VALUES (?, ?, ?)",
                        [(material_id, n, a) for n, a in entries.items()],
                    )

                def recheck():
                    # Кандидаты ключа изменились у заданий любого материала: решения о рисунках
                    # были приняты для прежних ключей. Пока транзакция не закрыта, ребёнок не
                    # увидит ни новый ключ, ни прежний рисунок.
                    changed = figs.changed_materials(before, figs.candidate_snapshot(conn))
                    figs.recheck_materials_figures(ctx, conn, changed)

                _finish(conn, material_id, run_id, len(pages), write_key, recheck)
            else:
                skipped: list[str] = []
                tasks = split_tasks(
                    ctx,
                    pages,
                    cancel,
                    skipped,
                    lambda chunk, total, accepted: ctx.progress.update(
                        material_id, run_id, chunk, total, accepted
                    ),
                )
                note = skipped_note(skipped)
                if not tasks and not note:
                    raise ImportFailure("Модель не нашла в файле ни одного задания.")
                _finish(
                    conn,
                    material_id,
                    run_id,
                    len(pages),
                    lambda: _write_tasks(conn, material, tasks, pdf),
                    note=note,
                )
        except ImportAborted:
            log.info("import %s aborted", material_id)
        except ImportFailure as exc:
            fail_material(conn, material_id, run_id, str(exc))
        except Exception:
            log.exception("import %s failed", material_id)
            fail_material(
                conn, material_id, run_id, "Не удалось обработать PDF. Проверьте файл и повторите."
            )
    finally:
        ctx.progress.clear(material_id, run_id)
        conn.close()
