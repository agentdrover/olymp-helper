"""Подтверждение рисунка родителем: рамка, alt, пометки, PNG (SPEC-1.0 1.1)."""

import pymupdf

from app.ingest import figures
from app.ingest.answers import key_value
from app.ingest.keys import candidate
from app.ingest.review import Refusal
from app.ingest.store import figure_of, get_task, new_revision, options_of

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
        if rect_px is not None:
            if page is None:
                raise Refusal("Для нарисованной рамки нужна страница, на которой она нарисована.")
            if page != page_no:
                raise Refusal(
                    f"Рамка нарисована на странице {page}, а рисунок задания на странице {page_no}.",
                    409,
                )
        stored = _rect_from(figure, doc[page_no - 1], rect, rect_px, scale)
        problem = figures.alt_problem(alt or "", options_of(task), _correct_labels(conn, task))
        if problem:
            raise Refusal(problem)
        if not no_answer_ack:
            raise Refusal(NO_ANSWER_REFUSAL)
        figure.update(
            rect=stored,
            alt=alt.strip(),
            no_answer_ack=True,
            teacher_note_ack=bool(teacher_note_ack),
            suggested=figure.get("suggested", False) and rect is None and rect_px is None,
        )
        if teacher_note is not None:
            figure["teacher_note"] = teacher_note
        if not figures.labels_readable(doc, page_no, stored):
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
        target.write_bytes(figures.render_figure_png(doc, page_no, stored))
        old = figure.get("file")
        figure.update(confirmed=True, file=relative)
        new_revision(conn, task_id, figure=figure, import_status="draft", unsupported_reason=None)
        if old and old != relative:
            ctx.material_path(old).unlink(missing_ok=True)
        return {"status": "draft", "revision": revision}
    finally:
        doc.close()
