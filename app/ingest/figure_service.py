"""Подтверждение рисунка родителем: рамка, alt, пометки, PNG (SPEC-1.0 1.1)."""

import bisect
import contextlib
import json

import pymupdf

from app.ingest import figures, tiles
from app.ingest.answers import key_value
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


def figure_regions(doc, figure: dict, *, rect=None, rect_px=None, scale=None, page=None):
    """Области рисунка с учётом ручной рамки (правит область ОДНОЙ страницы, остальные остаются).
    Общая для подтверждения и предпросмотра: родитель видит ровно ту вырезку, что будет сохранена."""
    page_no = figure["page"]
    regions = figures.regions_of(figure)
    pages = {r["page"] for r in regions} | {page_no}
    if rect_px is not None and page is None:
        raise Refusal("Для нарисованной рамки нужна страница, на которой она нарисована.")
    if rect is not None or rect_px is not None:
        target = page if page is not None else page_no
        if target not in pages:
            raise Refusal(
                f"Рамка нарисована на странице {target}, а рисунок задания на "
                f"страницах {', '.join(map(str, sorted(pages)))}.",
                409,
            )
        frame = _rect_from(figure, doc[target - 1], rect, rect_px, scale)
        return sorted(
            [r for r in regions if r["page"] != target] + [{"page": target, "rect": frame}],
            key=lambda r: r["page"],
        )
    if not regions:
        frame = _rect_from(figure, doc[page_no - 1], None, None, None)
        regions = [{"page": page_no, "rect": frame}]
    return regions


def edited_page(figure: dict, *, rect=None, rect_px=None, page=None, scale=None) -> int | None:
    """Страница, чью рамку правят в этом запросе; None - рамку не трогали."""
    if rect is None and rect_px is None:
        return None
    return page if page is not None else figure["page"]


def tile_plan(doc, task, figure: dict, regions, edited: int | None, state=None):
    """(план плиток, номера левых элементов): предпросмотр и подтверждение берут границы,
    состав и порядок плиток отсюда и только отсюда."""
    plan = tiles.plan_tiles(doc, figure, regions, edited_page=edited, state=state)
    lefts = tiles.left_labels(options_of(task), task["type"])
    if state is None:
        tiles.bind_defaults(plan, lefts)
    return plan, lefts


def _tile_state(raw) -> list[dict] | None:
    """Плитки из запроса: [{id, label}] в порядке показа; None - карточка их не присылала."""
    if raw is None:
        return None
    state = [
        {"id": str(t["id"]), "label": (str(t["label"]) if t.get("label") else None)} for t in raw
    ]
    if len({t["id"] for t in state}) != len(state):
        raise Refusal("Одна плитка указана дважды.")
    return state


def _tile_record(tile: dict, old_file) -> dict:
    return {
        "id": tile["id"],
        "page": tile["page"],
        "rect": tile["rect"],
        "file": old_file,
        "label": tile.get("label"),
    }


def preview_tile_png(
    ctx,
    conn,
    task_id: int,
    tile_id: str,
    *,
    rect=None,
    rect_px=None,
    scale=None,
    page=None,
    state=None,
) -> bytes:
    """PNG одной плитки по текущей рамке: тот же план, что и при подтверждении."""
    task = get_task(conn, task_id)
    figure = figure_of(task) if task is not None else None
    if figure is None or task["excluded"]:
        raise Refusal("У этого задания нет рисунка.", 404)
    material = conn.execute(
        "SELECT file FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    source = ctx.material_path(material["file"]) if material else None
    if source is None or not source.exists():
        raise Refusal("Файл материала удалён: плитку уже не показать.", 409)
    with contextlib.closing(pymupdf.open(str(source))) as doc:
        regions = figure_regions(doc, figure, rect=rect, rect_px=rect_px, scale=scale, page=page)
        edited = edited_page(figure, rect=rect, rect_px=rect_px, page=page)
        plan, _lefts = tile_plan(doc, task, figure, regions, edited, _tile_state(state))
        found = next((t for t in plan["tiles"] if t["id"] == tile_id), None)
        if found is None:
            raise Refusal("Такой плитки нет.", 404)
        return tiles.render_tile_png(doc, found)


def preview_png(
    ctx, conn, task_id: int, *, rect=None, rect_px=None, scale=None, page=None
) -> bytes:
    """Вырезка по текущей рамке для родителя: только чтение, ничего не сохраняется и не
    публикуется. У неподтверждённого рисунка PNG ещё нет, поэтому он рендерится из PDF."""
    task = get_task(conn, task_id)
    figure = figure_of(task) if task is not None else None
    if figure is None or task["excluded"]:
        raise Refusal("У этого задания нет рисунка.", 404)
    material = conn.execute(
        "SELECT file FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    source = ctx.material_path(material["file"]) if material else None
    if source is None or not source.exists():
        raise Refusal("Файл материала удалён: вырезку уже не показать.", 409)
    with contextlib.closing(pymupdf.open(str(source))) as doc:
        regions = figure_regions(doc, figure, rect=rect, rect_px=rect_px, scale=scale, page=page)
        return figures.render_regions_png(doc, regions)


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
    tile_state=None,
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
    source = ctx.material_path(material["file"])
    if not source.exists():
        raise Refusal("Файл материала удалён: рамку рисунка уже не поправить.", 409)
    doc = pymupdf.open(str(source))
    try:
        regions = figure_regions(doc, figure, rect=rect, rect_px=rect_px, scale=scale, page=page)
        problem = figures.alt_problem(alt or "", options_of(task), _correct_labels(conn, task))
        if problem:
            raise Refusal(problem)
        if not no_answer_ack:
            raise Refusal(NO_ANSWER_REFUSAL)
        before_files = tiles.figure_files(figure)
        before_tiles = {t["id"]: t.get("file") for t in tiles.stored_tiles(figure) or []}
        state = _tile_state(tile_state)
        edited = edited_page(figure, rect=rect, rect_px=rect_px, page=page)
        plan, lefts = tile_plan(doc, task, figure, regions, edited, state)
        shown_ids = {t["id"] for t in plan["tiles"]}
        if state is None:  # клиент плиток не показывал: сохраняется только то, что видел родитель
            plan, shown_ids = {"tiles": [], "order": []}, set()
        elif {t["id"] for t in state} != shown_ids:
            raise Refusal(
                "Набор плиток изменился после предпросмотра: обновите карточку и проверьте "
                "плитки ещё раз.",
                409,
            )
        bad_binding = tiles.check_labels(plan, lefts)
        if bad_binding:
            raise Refusal(bad_binding)
        shown = tiles.ordered(plan)
        units = leak_units(conn, task)
        lines = figures.region_lines(doc, regions)
        found = figures.leaks_in_lines(lines, units)
        checks = tiles.check_tiles(doc, shown, units)
        for check in checks.values():  # каждая плитка отдельно: найденное в плитке тоже утечка
            found += [x for x in check["leak"] if x not in found]
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
            raster_only=not lines,
            items_v2=True,
            alt=alt.strip(),
            no_answer_ack=True,
            teacher_note_ack=bool(teacher_note_ack),
            suggested=figure.get("suggested", False) and rect is None and rect_px is None,
            tiles_v=tiles.TILES_VERSION,
            tiles=[_tile_record(t, before_tiles.get(t["id"])) for t in plan["tiles"]],
            tile_order=plan["order"],
        )
        if teacher_note is not None:
            figure["teacher_note"] = teacher_note
        readable = (
            all(c["readable"] for c in checks.values())
            if shown
            else figures.regions_readable(doc, regions)
        )
        if not readable:
            figure["confirmed"] = False
            revision = new_revision(
                conn,
                task_id,
                figure=figure,
                import_status="unsupported",
                unsupported_reason=UNREADABLE,
            )
            keep = set(tiles.figure_files(figure))
            for stale in before_files:  # плитки, которых в новой записи нет, больше не нужны
                if stale not in keep:
                    ctx.material_path(stale).unlink(missing_ok=True)
            return {"status": "unsupported", "reason": UNREADABLE, "revision": revision}
        revision = task["revision"] + 1
        relative = f"figures/{task_id}-r{revision}.png"
        target = ctx.material_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(figures.render_regions_png(doc, regions))
        figure.update(confirmed=True, file=relative)
        for tile in figure["tiles"]:
            tile["file"] = tiles.tile_file_name(task_id, revision, tile["id"])
            tile_target = ctx.material_path(tile["file"])
            tile_target.parent.mkdir(parents=True, exist_ok=True)
            tile_target.write_bytes(
                tiles.render_tile_png(doc, next(t for t in plan["tiles"] if t["id"] == tile["id"]))
            )
        new_revision(conn, task_id, figure=figure, import_status="draft", unsupported_reason=None)
        keep = set(tiles.figure_files(figure))
        for stale in before_files:
            if stale not in keep:
                ctx.material_path(stale).unlink(missing_ok=True)
        return {
            "status": "draft",
            "revision": revision,
            "raster_only": not lines,
            "tiles_raster_only": [i for i, c in checks.items() if c["raster_only"]],
        }
    finally:
        doc.close()


LEAK_REASON = (
    "После смены ключа в вырезке рисунка оказался напечатан текст ответа. Подтвердите рисунок "
    "заново: сдвиньте рамку или явно отметьте, что так и надо."
)


def prepare_crop_lines(ctx, conn, material_id: int | None) -> dict:
    """{задание: (области, строки текста в них)} подтверждённых рисунков материала.

    Читает только PDF и не зависит от правки ключа или вариантов, поэтому выполняется ДО
    транзакции: под блокировкой записи остаётся одно сравнение. Если области задания успели
    измениться, проверка читает их заново. У сгенерированного задания материала нет: {}."""
    if material_id is None:
        return {}
    tasks = [
        (t, figures.regions_of(f))
        for t in conn.execute("SELECT * FROM task_item WHERE material_id = ?", (material_id,))
        if (f := figure_of(t)) and f.get("confirmed")
    ]
    tasks = [(t, regions) for t, regions in tasks if regions]
    if not tasks:
        return {}
    if material_file_gone(ctx, conn, material_id):
        return {}  # строк вырезок для сверки нет
    with contextlib.closing(_open_material(ctx, conn, material_id)) as doc:
        return {t["id"]: (regions, figures.region_lines(doc, regions)) for t, regions in tasks}


def material_file_gone(ctx, conn, material_id: int) -> bool:
    """Файл материала удалён родителем: вырезки подтверждённых рисунков остаются как есть, а сверять
    их с PDF не с чем."""
    material = conn.execute("SELECT file FROM material WHERE id = ?", (material_id,)).fetchone()
    return material is None or not ctx.material_path(material["file"]).exists()


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
        # файл удалён родителем: текст рамок читать неоткуда, проверка alt идёт без него
        self.gone = material_file_gone(ctx, conn, material_id)
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
        if regions and shared.gone:
            found = list(figure.get("leak") or [])  # вырезку не перечитать: прежнее найденное
        elif regions:
            found = shared.leaks(task, regions)
            for tile in tiles.stored_tiles(figure) or []:  # плитки проверяются каждая отдельно
                part = shared.leaks(task, [{"page": tile["page"], "rect": tile["rect"]}])
                found += [x for x in part if x not in found]
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
    if task is None or task["material_id"] is None:  # сгенерированное: материала и рисунков нет
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
