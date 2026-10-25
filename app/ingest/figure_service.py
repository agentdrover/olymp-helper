"""Подтверждение рисунка родителем: рамка, alt, пометки, PNG (SPEC-1.0 1.1)."""

import bisect
import contextlib
import json

import pymupdf

from app.ingest import figures
from app.ingest.answers import key_value
from app.ingest.figures import crop_leaks
from app.ingest.keys import candidate
from app.ingest.leaks import answer_units, crop_units, norm, significant
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


def _labels_from(task, cand) -> list[str]:
    value = key_value(task["answer_key"])
    if value is None:
        value = cand["value"] if cand else None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return []


def _correct_labels(conn, task) -> list[str]:
    return _labels_from(task, candidate(conn, task))


def _keys_from(task, cand) -> list[str]:
    keys = []
    value = key_value(task["answer_key"])
    for item in (value, cand["value"] if cand else None):
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, list):
            keys.extend(item)
        elif isinstance(item, dict):  # сопоставление: ответ - пара, а не обозначение стороны
            keys.extend(f"{k} {v}" for k, v in item.items())
    return keys


def _keys_of(conn, task) -> list[str]:
    return _keys_from(task, candidate(conn, task))


def _foreign_units(task, keys) -> list[str]:
    """Что из ответа и ключа задания не должно стоять в вырезке ЧУЖОГО задания."""
    texts = [*answer_units(task["answer_text"]), *map(norm, keys)]
    return [t for t in texts if significant(t)]


def leak_units(conn, task) -> list[str]:
    """Что не должно быть напечатано в вырезке: ответ со страницы, ключ и тексты вариантов
    этого задания, а также ответы и ключи ВСЕХ других заданий материала: рамка задела
    соседнее задание."""
    units = crop_units(task["answer_text"], _keys_of(conn, task), options_of(task), task["type"])
    others = conn.execute(
        "SELECT * FROM task_item WHERE material_id = ? AND id != ?",
        (task["material_id"], task["id"]),
    ).fetchall()
    for other in others:
        for text in _foreign_units(other, _keys_of(conn, other)):
            if text not in units:
                units.append(text)
    return units


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
        regions = figures.regions_of(figure)
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


def prepare_crop_lines(ctx, conn, material_id: int) -> dict:
    """{задание: (области, строки текста в них)} подтверждённых рисунков материала.

    Читает только PDF и не зависит от правки ключа или вариантов, поэтому выполняется ДО
    транзакции: под блокировкой записи остаётся одно сравнение. Если области задания успели
    измениться, проверка читает их заново."""
    tasks = [
        (t, figures.regions_of(f))
        for t in conn.execute("SELECT * FROM task_item WHERE material_id = ?", (material_id,))
        if (f := figure_of(t)) and f.get("confirmed")
    ]
    tasks = [(t, regions) for t, regions in tasks if regions]
    if not tasks:
        return {}
    with contextlib.closing(_open_material(ctx, conn, material_id)) as doc:
        return {t["id"]: (regions, figures.region_lines(doc, regions)) for t, regions in tasks}


def _open_material(ctx, conn, material_id: int):
    material = conn.execute("SELECT file FROM material WHERE id = ?", (material_id,)).fetchone()
    return pymupdf.open(str(ctx.material_path(material["file"])))


class _LeakIndex:
    """Ответы и ключи всех заданий материала, собранные один раз: общий для рисунков индекс.

    Текст рамки сопоставляется с ним без перебора всех ответов: ответ внутри строки находится
    по словесным отрезкам строки, строка внутри ответа - одним поиском подстроки по сплошному
    тексту ответов."""

    def __init__(self, foreign: dict[int, list[str]]):
        # Ответы самого задания входят в его собственные юниты (crop_units), поэтому отдельно
        # исключать их из общего индекса не нужно.
        self.owners = dict.fromkeys(t for texts in foreign.values() for t in texts)
        self.texts = list(self.owners)
        self.order = {t: i for i, t in enumerate(self.texts)}
        self.blob = "\n".join(f" {t} " for t in self.texts)
        self.starts, at = [], 0
        for text in self.texts:
            self.starts.append(at)
            at += len(text) + 3
        self.longest = max((len(t.split()) for t in self.texts), default=0)

    def matches(self, lines: list[str]) -> list[str]:
        """Чужие тексты, которые есть в строках рамки (правило crop_leaks), по порядку заданий."""
        hits: set[str] = set()
        for line in lines:
            words = line.split()
            for i in range(len(words)):
                for j in range(i + 1, min(len(words), i + self.longest) + 1):
                    gram = " ".join(words[i:j])
                    if gram in self.owners:
                        hits.add(gram)
            if significant(line):
                needle = f" {line} "
                pos = self.blob.find(needle)
                while pos != -1:
                    hits.add(self.texts[bisect.bisect_right(self.starts, pos) - 1])
                    pos = self.blob.find(needle, pos + 1)
        return sorted(hits, key=self.order.__getitem__)


def _build_leak_index(foreign: dict[int, list[str]]) -> _LeakIndex:
    return _LeakIndex(foreign)


class _Shared:
    """Входы проверки рисунков одного материала: кандидаты, ключи, ответы и подготовленный
    текст рамок считаются один раз, PDF открывается не чаще одного раза."""

    def __init__(self, ctx, conn, material_id: int, prepared: dict | None = None):
        self.ctx, self.conn, self.material_id = ctx, conn, material_id
        self.prepared = prepared or {}
        self.tasks = {
            t["id"]: t
            for t in conn.execute("SELECT * FROM task_item WHERE material_id = ?", (material_id,))
        }
        cands = {i: candidate(conn, t) for i, t in self.tasks.items()}
        self.keys = {i: _keys_from(t, cands[i]) for i, t in self.tasks.items()}
        self.labels = {i: _labels_from(t, cands[i]) for i, t in self.tasks.items()}
        self.foreign = {i: _foreign_units(t, self.keys[i]) for i, t in self.tasks.items()}
        self.index = _build_leak_index(self.foreign)
        self._doc = None

    def leaks(self, task, regions) -> list[str]:
        """Тексты из защиты (свои и чужие ответы, ключи, варианты), напечатанные в рамках."""
        lines = self.lines(task, regions)
        own = crop_units(task["answer_text"], self.keys[task["id"]], options_of(task), task["type"])
        found = figures.leaks_in_lines(lines, own)
        mine = set(map(norm, own))
        return found + [t for t in self.index.matches(lines) if t not in mine]

    def lines(self, task, regions) -> list[str]:
        hit = self.prepared.get(task["id"])
        if hit is not None and hit[0] == regions:
            return hit[1]
        if self._doc is None:
            self._doc = _open_material(self.ctx, self.conn, self.material_id)
        return figures.region_lines(self._doc, regions)

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    def confirmed(self) -> list[int]:
        return [i for i, t in self.tasks.items() if (figure_of(t) or {}).get("confirmed")]


def recheck_figure(
    ctx, conn, task_id: int, *, keep_ack: bool = False, shared: _Shared | None = None
) -> None:
    """Ключ задания изменился: прежние решения о рисунке относились к другому ключу.

    Защита от текста ответа в вырезке и проверка описания (alt) проходят заново с новым
    ключом; решение leak_ack сбрасывается. При нарушении рисунок снимается с подтверждения
    и задание возвращается на карточку рисунка. keep_ack: проверка из-за ключа ДРУГОГО задания;
    если найденное не изменилось, прежнее решение родителя остаётся. shared - входы материала,
    посчитанные один раз для всех его рисунков."""
    task = get_task(conn, task_id)
    figure = figure_of(task) if task is not None else None
    if figure is None or not figure.get("confirmed"):
        return
    own = shared is None
    if own:
        shared = _Shared(ctx, conn, task["material_id"])
    try:
        regions = figures.regions_of(figure)
        found = []
        if regions:
            found = shared.leaks(task, regions)
        alt_bad = figures.alt_problem(
            figure.get("alt", ""), options_of(task), shared.labels[task_id]
        )
    finally:
        if own:
            shared.close()
    if keep_ack and not alt_bad and sorted(found) == sorted(figure.get("leak") or []):
        return
    if not found and not alt_bad and not figure.get("leak_ack") and not figure.get("leak"):
        return
    figure.update(leak=found, leak_ack=False)
    if found or alt_bad:
        figure["confirmed"] = False
        reason = " ".join(x for x in (LEAK_REASON if found else None, alt_bad) if x)
        new_revision(
            conn, task_id, figure=figure, import_status="unsupported", unsupported_reason=reason
        )
    else:
        update_task(conn, task_id, figure=figure)
        save_snapshot(conn, task_id)


def recheck_material_figures(ctx, conn, task_id: int, prepared: dict | None = None) -> None:
    """Ключ или варианты задания изменились: они входят в защиту от утечки (leak_units) у каждого
    другого задания материала, поэтому уже подтверждённые рисунки соседей проходят проверку заново.
    prepared - prepare_crop_lines, выполненный до транзакции."""
    task = get_task(conn, task_id)
    if task is None:
        return
    shared = _Shared(ctx, conn, task["material_id"], prepared)
    try:
        recheck_figure(ctx, conn, task_id, shared=shared)
        for other in shared.confirmed():
            if other != task_id:
                recheck_figure(ctx, conn, other, keep_ack=True, shared=shared)
    finally:
        shared.close()


def recheck_materials_figures(ctx, conn, material_ids) -> None:
    """Проверить подтверждённые рисунки всех заданий материалов: у каждого задания защита от
    утечки включает ответы остальных заданий материала. Решение родителя об ответе в вырезке
    остаётся, если найденное не изменилось."""
    for material_id in sorted(material_ids):
        shared = _Shared(ctx, conn, material_id)
        try:
            for task_id in shared.confirmed():
                recheck_figure(ctx, conn, task_id, keep_ack=True, shared=shared)
        finally:
            shared.close()


def candidate_snapshot(conn) -> dict[int, tuple[int, str]]:
    """{задание: (материал, кандидат ключа)} у материалов, где есть подтверждённый рисунок:
    только их вырезки зависят от кандидатов."""
    rows = conn.execute("SELECT * FROM task_item WHERE figure IS NOT NULL").fetchall()
    materials = {r["material_id"] for r in rows if (figure_of(r) or {}).get("confirmed")}
    out = {}
    for material_id in materials:
        for task in conn.execute("SELECT * FROM task_item WHERE material_id = ?", (material_id,)):
            cand = candidate(conn, task)
            out[task["id"]] = (material_id, json.dumps(cand["value"] if cand else None))
    return out


def changed_materials(before: dict, after: dict) -> set[int]:
    """Материалы, где кандидат ключа хотя бы одного задания изменился."""
    return {m for t, (m, value) in after.items() if before.get(t, (m, None))[1] != value}
