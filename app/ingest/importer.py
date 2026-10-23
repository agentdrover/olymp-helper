"""Импорт материала: текст по страницам, разбиение на задания моделью, запись (SPEC-1.0 1.7)."""

import json
import logging
import re
import threading

import pymupdf

from app.ingest.extract import chunk_pages, extract_pages
from app.ingest.figures import suggest_rect
from app.ingest.keys import parse_key_text
from app.ingest.prompt import SYSTEM_PROMPT, TASKS_SCHEMA, build_chunk_message
from app.ingest.schema import InvalidStructure, validate_section_at_end, validate_tasks
from app.ingest.store import open_db, save_snapshot
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
    merged["pages"] = sorted(set(old["pages"]) | set(new["pages"]))
    merged["refers_to_figure"] = old["refers_to_figure"] or new["refers_to_figure"]
    merged["figure_pages"] = sorted(set(_figure_pages(old)) | set(_figure_pages(new)))
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
    return merged


def split_tasks(ctx, pages, cancel: threading.Event) -> list[dict]:
    """Задания из всех порций. Номера повторяются по разделам: ключ дедупликации - (раздел,
    номер); состояние раздела (раздел, вариант, этап) переходит между порциями."""
    seen: dict[tuple, dict] = {}
    state = dict.fromkeys(META)  # состояние на конец предыдущей порции
    for chunk in chunk_pages(pages):
        _check(cancel)
        message = build_chunk_message(chunk, state)
        result = ctx.model.complete(
            SYSTEM_PROMPT, [{"role": "user", "content": message}], TASKS_SCHEMA
        )
        _check(cancel)
        if not getattr(result, "ok", False):
            raise ImportFailure("Модель не ответила при разборе страниц. Повторите загрузку позже.")
        try:
            parsed = validate_tasks(result.json, {n for n, _ in chunk})
            at_end = validate_section_at_end(result.json)
        except InvalidStructure as exc:
            raise ImportFailure(str(exc)) from exc
        for task in parsed:
            state = merge_labels(state, task)
            task.update(state)
            key = (task["section"], task["number"])
            seen[key] = merge_repeat(seen[key], task) if key in seen else task
        if at_end:  # заголовок после последнего задания важнее состояния последнего задания
            state = merge_labels(state, at_end)
    return list(seen.values())


def _unsupported_reason(task: dict) -> str | None:
    if task["type"] == "open":
        return "Открытое задание вне v1: его можно исключить."
    if task["refers_to_figure"] or FIGURE_WORDS.search(task["text"]):
        return "Условие зависит от рисунка, схемы или таблицы: подтвердите рисунок или впишите условие."
    return None


def _figure_record(doc, material_id: int, task: dict) -> dict:
    page = (_figure_pages(task) or task["pages"])[0]
    rect = suggest_rect(doc, page, task["text"]) if doc is not None else None
    return {
        "material_id": material_id,
        "page": page,
        "rect": rect,
        "suggested": rect is not None,
        "confirmed": False,
        "alt": f"Рисунок к заданию {task['number']}, страница {page}",
        "teacher_note": "",
        "no_answer_ack": False,
        "teacher_note_ack": False,
        "file": None,
    }


_VARIANT_IN_SECTION = re.compile(r"вариант\s*№?\s*(\d+)", re.IGNORECASE)


def _task_variant(task: dict, material) -> str | None:
    if task["variant"]:
        return task["variant"]
    found = _VARIANT_IN_SECTION.search(task["section"] or "")
    return found.group(1) if found else material["variant"]


def _write_tasks(conn, material, tasks, pdf: bytes) -> int:
    doc = pymupdf.open("pdf", pdf)
    try:
        for task in tasks:
            reason = _unsupported_reason(task)
            figure = None
            if reason and task["type"] != "open":
                figure = _figure_record(doc, material["id"], task)
            cur = conn.execute(
                "INSERT INTO task_item (material_id, page_from, page_to, variant, source_number, "
                "type, text, options, points, topic, import_status, figure, unsupported_reason, "
                "section, stage, review_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    task.get("review_note"),
                ),
            )
            save_snapshot(conn, cur.lastrowid)
    finally:
        doc.close()
    return len(tasks)


def _finish(conn, material_id: int, run_id: str, pages_total: int, write) -> bool:
    """Записать результат и status=done одной транзакцией, если запуск run_id ещё running."""
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
            "UPDATE material SET import_status = 'done', import_error = NULL, pages_total = ? "
            "WHERE id = ?",
            (pages_total, material_id),
        )
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

                def write_key():
                    conn.execute("DELETE FROM key_entry WHERE material_id = ?", (material_id,))
                    conn.executemany(
                        "INSERT INTO key_entry (material_id, number, answer) VALUES (?, ?, ?)",
                        [(material_id, n, a) for n, a in entries.items()],
                    )

                _finish(conn, material_id, run_id, len(pages), write_key)
            else:
                tasks = split_tasks(ctx, pages, cancel)
                if not tasks:
                    raise ImportFailure("Модель не нашла в файле ни одного задания.")
                _finish(
                    conn,
                    material_id,
                    run_id,
                    len(pages),
                    lambda: _write_tasks(conn, material, tasks, pdf),
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
        conn.close()
