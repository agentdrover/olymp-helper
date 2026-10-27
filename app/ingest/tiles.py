"""Плитки рисунка: отдельные картинки внутри рамки страницы (SPEC-1.0 1.1, #2121).

Рамка страницы остаётся рамкой, плитки - её разбиение: PyMuPDF отдаёт места размещённых
изображений (get_image_info), и они лишь предлагают границы плиток внутри рамки. Общая вырезка
остаётся в неоднозначных случаях: одна картинка, полосы одной картинки, перекрытие.

Запись в task_item.figure: tiles_v=1, tiles=[{id, page, rect, file, label}], tile_order=[id].
Привязка плитки к номеру левого элемента match - label; порядок показа - tile_order. Ключ и
teacher_note ссылаются на метки, а не на позиции, поэтому перестановка их не затрагивает.
"""

import hashlib

import pymupdf

from app.ingest import figures

TILES_VERSION = 1
MIN_TILES = 2  # одна картинка - не плитки
TOUCH_PT = 1.0  # изображения ближе этого - смежные
SAME_SHARE = 0.9  # размещения почти в одном месте - одно изображение (маска и основное)
STRIP_TOLERANCE = 0.03  # полосы одной картинки: сумма площадей равна площади объединения
CAPTION_REACH_PT = 40.0  # дальше этого от изображения текст плитке не принадлежит
OVERLAP_PT2 = 4.0  # пересечение плиток больше этого - неоднозначно


def tile_id(page_no: int, rect) -> str:
    """Id из места изображения: тот же рисунок остаётся с тем же id при пересчёте."""
    key = f"{page_no}:" + ",".join(str(round(v)) for v in rect)
    return "t" + hashlib.sha1(key.encode()).hexdigest()[:8]


def _area(rect: pymupdf.Rect) -> float:
    return 0.0 if rect.is_empty else rect.get_area()


def _overlap(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    inter = a & b
    return 0.0 if inter.is_empty else inter.get_area()


def _placements(page, frame: pymupdf.Rect) -> list[pymupdf.Rect]:
    """Размещённые изображения, чья большая часть лежит в рамке. Маски - часть основного
    изображения и своих плиток не дают."""
    area = abs(page.cropbox.width * page.cropbox.height)
    listed = page.get_images(full=True)
    masks = {item[1] for item in listed if item[1]} - {item[0] for item in listed}
    out = []
    for info in page.get_image_info(xrefs=True):
        rect = pymupdf.Rect(info["bbox"])
        if info.get("xref") in masks:
            continue
        if rect.width < figures.MIN_SIDE_PT or rect.height < figures.MIN_SIDE_PT:
            continue
        if _area(rect) >= figures.SCAN_SHARE * area:
            continue
        if _overlap(rect, frame) < 0.5 * _area(rect):
            continue
        out.append(rect)
    return out


def _same_place(rects: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """Размещения в одном месте (основное и его маска, повторная отрисовка) - одно изображение."""
    out: list[pymupdf.Rect] = []
    for rect in rects:
        for i, other in enumerate(out):
            if _overlap(rect, other) >= SAME_SHARE * max(_area(rect), _area(other)):
                out[i] = other | rect
                break
        else:
            out.append(pymupdf.Rect(rect))
    return out


def _touch(a: pymupdf.Rect, b: pymupdf.Rect) -> bool:
    gap_x = max(b.x0 - a.x1, a.x0 - b.x1)
    gap_y = max(b.y0 - a.y1, a.y0 - b.y1)
    return gap_x <= TOUCH_PT and gap_y <= TOUCH_PT


def _pictures(rects: list[pymupdf.Rect]) -> list[pymupdf.Rect] | None:
    """Картинки из размещений: смежные изображения, вместе образующие прямоугольник, - полосы
    одной картинки. Смежные, но не образующие прямоугольника (или пересекающиеся), - None:
    неоднозначно, остаётся общая вырезка."""
    parent = list(range(len(rects)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(rects):
        for j in range(i + 1, len(rects)):
            if _touch(a, rects[j]):
                parent[root(j)] = root(i)
    groups: dict[int, list[pymupdf.Rect]] = {}
    for i, rect in enumerate(rects):
        groups.setdefault(root(i), []).append(rect)
    pictures = []
    for members in groups.values():
        union = members[0]
        for rect in members[1:]:
            union = union | rect
        if len(members) > 1:
            total = sum(_area(r) for r in members)
            if abs(total - _area(union)) > STRIP_TOLERANCE * _area(union):
                return None
        pictures.append(pymupdf.Rect(union))
    return pictures


def _distance(rect: pymupdf.Rect, point: pymupdf.Point) -> float:
    dx = max(rect.x0 - point.x, 0.0, point.x - rect.x1)
    dy = max(rect.y0 - point.y, 0.0, point.y - rect.y1)
    return (dx * dx + dy * dy) ** 0.5


def _with_captions(page, frame: pymupdf.Rect, pictures: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """Плитка = изображение + ближайший к нему текст в рамке (подпись в его ячейке). Слово
    принадлежит ближайшей картинке; равноудалённое от двух - никому. Если после этого плитки
    пересекаются, остаются границы изображений."""
    grown = [pymupdf.Rect(p) for p in pictures]
    for word in page.get_text("words"):
        box = pymupdf.Rect(word[:4])
        centre = pymupdf.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
        if not frame.contains(centre):
            continue
        ranked = sorted((_distance(p, centre), i) for i, p in enumerate(pictures))
        if ranked[0][0] > CAPTION_REACH_PT:
            continue
        if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < 1.0:
            continue
        grown[ranked[0][1]] |= box
    grown = [g & frame for g in grown]
    for i, a in enumerate(grown):
        if any(_overlap(a, b) > OVERLAP_PT2 for b in grown[i + 1 :]):
            return [p & frame for p in pictures]
    return grown


def _reading_order(items: list[tuple[pymupdf.Rect, pymupdf.Rect]]):
    """Строками сверху вниз, внутри строки слева направо. Порядок берётся по изображениям."""
    ranked = sorted(items, key=lambda item: (item[0].y0, item[0].x0))
    rows: list[list[tuple[pymupdf.Rect, pymupdf.Rect]]] = []
    for item in ranked:
        row = rows[-1] if rows else None
        if row and item[0].y0 < row[0][0].y0 + 0.5 * min(r[0].height for r in row):
            row.append(item)
        else:
            rows.append([item])
    return [item for row in rows for item in sorted(row, key=lambda it: it[0].x0)]


def suggest_page_tiles(page, page_no: int, stored_frame) -> list[dict]:
    """Плитки внутри одной рамки страницы: [] - плиток нет (одна картинка, неоднозначно)."""
    frame = figures.cropbox_relative(page, stored_frame)
    pictures = _pictures(_same_place(_placements(page, frame)))
    if not pictures or len(pictures) < MIN_TILES:
        return []
    grown = _with_captions(page, frame, pictures)
    out = []
    for picture, rect in _reading_order(list(zip(pictures, grown, strict=True))):
        out.append(
            {
                "id": tile_id(page_no, picture),
                "page": page_no,
                "rect": figures.stored_from_page(page, rect),
                "file": None,
                "label": None,
            }
        )
    return out


# ------------------------------------------------------------------------------- план


def stored_tiles(figure: dict) -> list[dict] | None:
    """Сохранённые плитки; None - записи вида до плиток (или ещё не предлагались)."""
    if figure.get("tiles_v") != TILES_VERSION:
        return None
    return [dict(t) for t in figure.get("tiles") or []]


def left_labels(options, kind: str) -> list[str]:
    if kind != "match" or not isinstance(options, dict):
        return []
    return [str(item.get("label", "")) for item in options.get("left") or []]


def plan_tiles(
    doc, figure: dict, regions: list[dict], *, edited_page: int | None = None, state=None
) -> dict:
    """{'tiles': [...], 'order': [id]}: единственный источник состава, границ и порядка плиток
    и для предпросмотра, и для подтверждения.

    Страницы, чью рамку не правили, берут сохранённые плитки; рамку исправленной страницы (и
    записи без плиток) предлагает suggest_page_tiles. state - [{id, label}] в порядке показа,
    присланные карточкой: метки и порядок переживают пересчёт, пока плитка на месте."""
    saved = stored_tiles(figure)
    tiles: list[dict] = []
    for region in sorted(regions, key=lambda r: r["page"]):
        page_no = region["page"]
        if saved is not None and page_no != edited_page:
            tiles.extend(t for t in saved if t["page"] == page_no)
            continue
        page = doc[page_no - 1]
        old = {t["id"]: t for t in saved or [] if t["page"] == page_no}
        for tile in suggest_page_tiles(page, page_no, region["rect"]):
            tile["label"] = (old.get(tile["id"]) or {}).get("label")
            tiles.append(tile)
    by_id = {t["id"]: t for t in tiles}
    order = [i for i in figure.get("tile_order") or [] if i in by_id]
    for item in state or []:
        tile = by_id.get(item.get("id"))
        if tile is not None:
            tile["label"] = item.get("label") or None
    if state:
        order = [i["id"] for i in state if i.get("id") in by_id]
    order += [t["id"] for t in tiles if t["id"] not in order]
    return {"tiles": tiles, "order": list(dict.fromkeys(order))}


def ordered(plan: dict) -> list[dict]:
    by_id = {t["id"]: t for t in plan["tiles"]}
    return [by_id[i] for i in plan["order"]]


def bind_defaults(plan: dict, lefts: list[str]) -> None:
    """Нет ни одной привязки, а плиток ровно столько же, сколько левых элементов: привязка
    по порядку показа, один раз; дальше она хранится явно и позиции не зависит."""
    shown = ordered(plan)
    if lefts and len(shown) == len(lefts) and all(t.get("label") is None for t in shown):
        for tile, label in zip(shown, lefts, strict=True):
            tile["label"] = label


def check_labels(plan: dict, lefts: list[str]) -> str | None:
    """Причина отказа или None: метка - номер левого элемента, не повторяется; при равном
    числе плиток и левых элементов привязана каждая плитка."""
    shown = ordered(plan)
    labels = [t["label"] for t in shown if t.get("label")]
    if any(label not in lefts for label in labels):
        return "Плитка привязана к номеру, которого нет в задании."
    if len(set(labels)) != len(labels):
        return "Два рисунка привязаны к одному номеру."
    if lefts and len(shown) == len(lefts) and len(labels) != len(shown):
        return "Привяжите каждый рисунок к номеру задания."
    return None


# ----------------------------------------------------------------------- проверки


def check_tile(doc, tile: dict, units: list[str]) -> dict:
    """Проверки одной плитки: текстовый слой, утечка, читаемость. Каждая плитка отдельно:
    плитка без текстового слоя предупреждается, даже если у соседней текст есть."""
    region = [{"page": tile["page"], "rect": tile["rect"]}]
    lines = figures.region_lines(doc, region)
    return {
        "id": tile["id"],
        "raster_only": not lines,
        "leak": figures.leaks_in_lines(lines, units),
        "readable": figures.labels_readable(doc, tile["page"], tile["rect"]),
    }


def check_tiles(doc, tiles: list[dict], units: list[str]) -> dict[str, dict]:
    return {t["id"]: check_tile(doc, t, units) for t in tiles}


def render_tile_png(doc, tile: dict) -> bytes:
    return figures.render_figure_png(doc, tile["page"], tile["rect"])


def tile_file_name(task_id: int, revision: int, tile: str) -> str:
    return f"figures/{task_id}-r{revision}-{tile}.png"


def figure_files(figure: dict | None) -> list[str]:
    """Все файлы рисунка в хранилище: общая вырезка и PNG плиток."""
    if not figure:
        return []
    files = [figure.get("file")]
    files += [t.get("file") for t in figure.get("tiles") or []]
    return [f for f in files if f]


def stored_view(figure: dict) -> list[dict]:
    """Плитки для ребёнка и страниц с готовыми файлами: в порядке показа."""
    saved = stored_tiles(figure)
    if not saved:
        return []
    by_id = {t["id"]: t for t in saved}
    order = [i for i in figure.get("tile_order") or [] if i in by_id]
    order += [t["id"] for t in saved if t["id"] not in order]
    return [by_id[i] for i in order if by_id[i].get("file")]
