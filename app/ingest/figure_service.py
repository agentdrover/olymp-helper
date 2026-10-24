"""Подтверждение рисунка родителем: рамка, alt, пометки, PNG (SPEC-1.0 1.1)."""

import pymupdf

from app.ingest import figures
from app.ingest.answers import key_value
from app.ingest.figures import crop_leaks
from app.ingest.keys import candidate
from app.ingest.leaks import crop_units
from app.ingest.review import Refusal
from app.ingest.store import (
    figure_of,
    get_task,
    new_revision,
    options_of,
    save_snapshot,
    update_task,
)

NO_ANSWER_REFUSAL = (
    "Отметьте: в рисунке и в описании нет ответа (подписи, легенды, соседний текст)."
)
UNREADABLE = (
    "Подписи на рисунке мельче читаемого размера при показе шириной 560 px: "
    "задание остаётся неподдерживаемым."
)


def _correct_labels(conn, task) -> list[str]:
    value = key_value(task["answer_key"])
    if value is None:
        cand = candidate(conn, task)
        value = cand["value"] if cand else None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return []


def leak_units(conn, task) -> list[str]:
    """Что не должно быть напечатано в вырезке: ответ со страницы, ключ, тексты вариантов."""
    keys = []
    value = key_value(task["answer_key"])
    cand = candidate(conn, task)
    for item in (value, cand["value"] if cand else None):
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, list):
            keys.extend(item)
        elif isinstance(item, dict):
            keys.extend(item.values())
    return crop_units(task["answer_text"], keys, options_of(task))


def _rect_from(figure: dict, page, rect, rect_px, scale):
    if rect_px is not None:
        if len(rect_px) != 4 or not scale or scale <= 0:
            raise Refusal("Рамка задана неверно: нужны четыре числа и масштаб рендера.")
        stored = figures.px_to_stored(page, rect_px, float(scale))
    elif rect is not None:
        if len(rect) != 4:
            raise Refusal("Рамка задана неверно: нужны четыре числа.")
        stored = [float(v) for v in rect]
    elif figure.get("rect"):
        stored = list(figure["rect"])
    else:
        raise Refusal("Рисунок не найден автоматически: нарисуйте рамку на странице.")
    if stored[2] - stored[0] < 5 or stored[3] - stored[1] < 5:
        raise Refusal("Рамка слишком мала.")
    return stored


def confirm_figure(
    ctx,
    conn,
    task_id: int,
    *,
    rect=None,
    rect_px=None,
    scale=None,
    page=None,
    alt="",
    teacher_note=None,
    no_answer_ack=False,
    teacher_note_ack=False,
    leak_ack=False,
) -> dict:
    task = get_task(conn, task_id)
    if task is None:
        raise Refusal("Задание не найдено.", 404)
    figure = figure_of(task)
    if figure is None or task["excluded"]:
        raise Refusal("У этого задания нет рисунка для подтверждения.")
    material = conn.execute(
        "SELECT file FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    doc = pymupdf.open(str(ctx.material_path(material["file"])))
    try:
        page_no = figure["page"]
        regions = [dict(r) for r in figure.get("regions") or []]
        pages = {r["page"] for r in regions} | {page_no}
        if rect_px is not None and page is None:
            raise Refusal("Для нарисованной рамки нужна страница, на которой она нарисована.")
        manual = rect is not None or rect_px is not None
        if manual:  # ручная рамка правит область ОДНОЙ страницы, остальные остаются
            target = page if page is not None else page_no
            if target not in pages:
                raise Refusal(
                    f"Рамка нарисована на странице {target}, а рисунок задания на "
                    f"страницах {', '.join(map(str, sorted(pages)))}.",
                    409,
                )
            frame = _rect_from(figure, doc[target - 1], rect, rect_px, scale)
            regions = sorted(
                [r for r in regions if r["page"] != target] + [{"page": target, "rect": frame}],
                key=lambda r: r["page"],
            )
        elif not regions:
            frame = _rect_from(figure, doc[page_no - 1], None, None, None)
            regions = [{"page": page_no, "rect": frame}]
        problem = figures.alt_problem(alt or "", options_of(task), _correct_labels(conn, task))
        if problem:
            raise Refusal(problem)
        if not no_answer_ack:
            raise Refusal(NO_ANSWER_REFUSAL)
        found = crop_leaks(doc, regions, leak_units(conn, task))
        if found and not leak_ack:
            raise Refusal(
                "В вырезке напечатан текст ответа или вариантов: «" + "», «".join(found) + "». "
                "Сдвиньте рамку или подтвердите явно, что так и надо."
            )
        figure.update(
            rect=regions[0]["rect"],
            regions=regions,
            leak=found,
            leak_ack=bool(found and leak_ack),
            alt=alt.strip(),
            no_answer_ack=True,
            teacher_note_ack=bool(teacher_note_ack),
            suggested=figure.get("suggested", False) and rect is None and rect_px is None,
        )
        if teacher_note is not None:
            figure["teacher_note"] = teacher_note
        if not figures.regions_readable(doc, regions):
            figure["confirmed"] = False
            revision = new_revision(
                conn,
                task_id,
                figure=figure,
                import_status="unsupported",
                unsupported_reason=UNREADABLE,
            )
            return {"status": "unsupported", "reason": UNREADABLE, "revision": revision}
        revision = task["revision"] + 1
        relative = f"figures/{task_id}-r{revision}.png"
        target = ctx.material_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(figures.render_regions_png(doc, regions))
        old = figure.get("file")
        figure.update(confirmed=True, file=relative)
        new_revision(conn, task_id, figure=figure, import_status="draft", unsupported_reason=None)
        if old and old != relative:
            ctx.material_path(old).unlink(missing_ok=True)
        return {"status": "draft", "revision": revision}
    finally:
        doc.close()


LEAK_REASON = (
    "После смены ключа в вырезке рисунка оказался напечатан текст ответа. Подтвердите рисунок "
    "заново: сдвиньте рамку или явно отметьте, что так и надо."
)


def recheck_figure(ctx, conn, task_id: int) -> None:
    """Ключ задания изменился: прежнее решение о тексте в вырезке относилось к другому ключу.

    Решение (leak_ack) сбрасывается, защита заново проходит по сохранённым областям; при
    тексте ответа в вырезке рисунок снимается с подтверждения до нового явного решения."""
    task = get_task(conn, task_id)
    figure = figure_of(task) if task is not None else None
    if figure is None or not figure.get("confirmed") or not figure.get("regions"):
        return
    material = conn.execute(
        "SELECT file FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    doc = pymupdf.open(str(ctx.material_path(material["file"])))
    try:
        found = crop_leaks(doc, figure["regions"], leak_units(conn, task))
    finally:
        doc.close()
    if not found and not figure.get("leak_ack") and not figure.get("leak"):
        return
    figure.update(leak=found, leak_ack=False)
    if found:
        figure["confirmed"] = False
        new_revision(
            conn,
            task_id,
            figure=figure,
            import_status="unsupported",
            unsupported_reason=LEAK_REASON,
        )
    else:
        update_task(conn, task_id, figure=figure)
        save_snapshot(conn, task_id)
