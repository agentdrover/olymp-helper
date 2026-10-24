"""Рисунок задания: автопоиск, рамка, PNG, проверка подтверждения (SPEC-1.0 1.1).

Рамка хранится в пунктах PDF в системе НЕповёрнутой страницы относительно MediaBox: то есть
в системе, где PyMuPDF отдаёт координаты изображений и текста, плюс смещение начала CropBox.
Рамка из пикселей рендера переводится: / масштаб, матрица снятия поворота, смещение CropBox.
"""

import re

import pymupdf

from app.ingest.leaks import contains_phrase, norm, significant

SCALE = 2.0  # PNG рисунка рендерится в двойном разрешении
DISPLAY_WIDTH_PX = 560  # SPEC 1.1: рисунок показывается не уже 560 px
MIN_LABEL_PX = 10.0  # подписи мельче этого размера при показе - рисунок нечитаем
SCAN_SHARE = 0.9  # изображение на такую долю страницы - скан, а не рисунок
MIN_SIDE_PT = 20.0


# ----------------------------------------------------------------------- координаты


def _crop_origin(page) -> pymupdf.Point:
    return page.cropbox_position


def px_to_stored(page, rect_px, scale: float) -> list[float]:
    """Пиксели рендера (повёрнутая страница) -> пункты неповёрнутой страницы с CropBox."""
    rect = pymupdf.Rect(*[v / scale for v in rect_px]) * page.derotation_matrix
    rect.normalize()
    origin = _crop_origin(page)
    return [rect.x0 + origin.x, rect.y0 + origin.y, rect.x1 + origin.x, rect.y1 + origin.y]


def cropbox_relative(page, stored) -> pymupdf.Rect:
    origin = _crop_origin(page)
    return pymupdf.Rect(
        stored[0] - origin.x, stored[1] - origin.y, stored[2] - origin.x, stored[3] - origin.y
    )


def stored_to_clip(page, stored) -> pymupdf.Rect:
    """Рамка из хранилища -> область в системе показанной (повёрнутой) страницы."""
    rect = cropbox_relative(page, stored) * page.rotation_matrix
    rect.normalize()
    return rect


# ------------------------------------------------------------------------ автопоиск


def _anchor(page, text: str):
    for needle in (text[:30], text[:15]):
        needle = " ".join(needle.split())
        if len(needle) >= 6:
            hits = page.search_for(needle)
            if hits:
                return hits[0]
    return None


def _image_boxes(page) -> list[pymupdf.Rect]:
    """Растровые изображения страницы без мелочи и без скана на всю страницу."""
    area = abs(page.cropbox.width * page.cropbox.height)
    boxes = []
    for info in page.get_image_info():
        rect = pymupdf.Rect(info["bbox"])
        if rect.width < MIN_SIDE_PT or rect.height < MIN_SIDE_PT:
            continue
        if rect.width * rect.height >= SCAN_SHARE * area:
            continue
        boxes.append(rect)
    return boxes


def _stored(page, rect: pymupdf.Rect) -> list[float]:
    origin = _crop_origin(page)
    return [rect.x0 + origin.x, rect.y0 + origin.y, rect.x1 + origin.x, rect.y1 + origin.y]


def _nearest(boxes, anchor):
    if anchor is None:
        return min(boxes, key=lambda r: (r.y0, r.x0))

    def gap(r):
        vertical = max(0.0, r.y0 - anchor.y1, anchor.y0 - r.y1)
        return (vertical, abs((r.y0 + r.y1) / 2 - (anchor.y0 + anchor.y1) / 2))

    return min(boxes, key=gap)


def suggest_regions(
    doc: pymupdf.Document,
    first_page: int,
    task_text: str,
    last_page: int,
    next_page: int | None = None,
    next_text: str | None = None,
) -> list[dict]:
    """Области рисунка: все растровые изображения между якорем этого задания и якорем
    следующего, по одной области (объединению) на страницу. Задание на нескольких страницах
    даёт несколько областей. Если между якорями изображений нет, берётся ближайшее к якорю.

    Скан (изображение почти на всю страницу) и векторная схема (изображений нет) не
    предлагаются: рамку рисует родитель.
    """
    anchor = _anchor(doc[first_page - 1], task_text)
    end, stop = max(last_page, first_page), None
    if next_page is not None and next_text and first_page <= next_page <= len(doc):
        stop = _anchor(doc[next_page - 1], next_text)
        if stop is not None:
            end = next_page
    regions = []
    for number in range(first_page, end + 1):
        page = doc[number - 1]
        top = anchor.y0 if number == first_page and anchor is not None else float("-inf")
        bottom = stop.y0 if number == end and stop is not None else float("inf")
        inside = [r for r in _image_boxes(page) if top - 1 <= r.y0 < bottom]
        if inside:
            regions.append({"page": number, "rect": _stored(page, _union(inside))})
    if regions:
        return regions
    boxes = _image_boxes(doc[first_page - 1])
    if not boxes:
        return []
    return [{"page": first_page, "rect": _stored(doc[first_page - 1], _nearest(boxes, anchor))}]


def _union(rects) -> pymupdf.Rect:
    out = pymupdf.Rect(rects[0])
    for rect in rects[1:]:
        out |= rect
    return out


# ------------------------------------------------------------------ PNG и читаемость


def render_figure_png(doc: pymupdf.Document, page_no: int, stored) -> bytes:
    """PNG 2x из области страницы (а не извлечённый объект): подписи и цифры сохраняются."""
    page = doc[page_no - 1]
    clip = stored_to_clip(page, stored)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(SCALE, SCALE), clip=clip, alpha=False)
    return pix.tobytes("png")


def smallest_label_px(doc: pymupdf.Document, page_no: int, stored) -> float | None:
    """Высота самой мелкой подписи внутри рамки при показе шириной 560 px; None - подписей нет."""
    page = doc[page_no - 1]
    area = cropbox_relative(page, stored)
    shown_width = area.height if page.rotation in (90, 270) else area.width
    if shown_width <= 0:
        return None
    factor = DISPLAY_WIDTH_PX / shown_width
    sizes = []
    for block in page.get_text("dict", clip=area).get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span["text"].strip() and pymupdf.Rect(span["bbox"]).intersects(area):
                    sizes.append(span["size"] * factor)
    return min(sizes) if sizes else None


def labels_readable(doc, page_no, stored) -> bool:
    smallest = smallest_label_px(doc, page_no, stored)
    return smallest is None or smallest >= MIN_LABEL_PX


def regions_readable(doc, regions) -> bool:
    return all(labels_readable(doc, r["page"], r["rect"]) for r in regions)


def render_regions_png(doc: pymupdf.Document, regions: list[dict]) -> bytes:
    """Одна PNG на весь рисунок: области страниц друг под другом, в порядке страниц."""
    if len(regions) == 1:
        return render_figure_png(doc, regions[0]["page"], regions[0]["rect"])
    parts = [pymupdf.Pixmap(render_figure_png(doc, r["page"], r["rect"])) for r in regions]
    width = max(p.width for p in parts)
    sheet = pymupdf.open()
    page = sheet.new_page(width=width, height=sum(p.height for p in parts))
    top = 0
    for part in parts:
        page.insert_image(pymupdf.Rect(0, top, part.width, top + part.height), pixmap=part)
        top += part.height
    return page.get_pixmap(alpha=False).tobytes("png")


def _crop_lines(doc, region) -> list[str]:
    """Нормализованные строки текста, попавшие в рамку (в том числе частично)."""
    page = doc[region["page"] - 1]
    area = cropbox_relative(page, region["rect"])
    lines = []
    for block in page.get_text("dict", clip=area).get("blocks", []):
        for line in block.get("lines", []):
            spans = [
                s["text"]
                for s in line.get("spans", [])
                if s["text"].strip() and pymupdf.Rect(s["bbox"]).intersects(area)
            ]
            text = norm(" ".join(spans))
            if text:
                lines.append(text)
    return lines


def crop_leaks(doc: pymupdf.Document, regions: list[dict], units: list[str]) -> list[str]:
    """Тексты из units (ответ, ключ, варианты), напечатанные внутри рамки.

    Текст рамки сравнивается по строкам в обе стороны: строка содержит текст ответа или
    сама входит в него. Однознаковое и пустое не берётся."""
    wanted = []
    for unit in units:
        unit = norm(unit)
        if significant(unit) and unit not in wanted:
            wanted.append(unit)
    lines = [line for region in regions for line in _crop_lines(doc, region)]
    return [
        unit
        for unit in wanted
        if any(
            contains_phrase(line, unit) or (significant(line) and contains_phrase(unit, line))
            for line in lines
        )
    ]


# --------------------------------------------------------------- проверка alt и пометок


def alt_problem(alt: str, options, correct_labels: list[str]) -> str | None:
    """Причина отказа для alt или None. Сервер отклоняет alt с текстом любого варианта
    ответа (нормализованное сравнение) и с обозначением верного варианта."""
    if not alt.strip():
        return (
            "Описание рисунка (alt) обязательно: ребёнку и экранному диктору нужен короткий текст."
        )
    normalized = norm(alt)
    texts = []
    if isinstance(options, list):
        texts = [o.get("text", "") for o in options]
    elif isinstance(options, dict):
        texts = [o.get("text", "") for side in options.values() for o in side]
    for text in texts:
        phrase = norm(text)
        # Любой текст варианта целой фразой (нормализация, ё = е), буквы и цифры. Пропускается
        # только однознаковый текст: это обозначение, его закрывает проверка меток ниже.
        if len(phrase.replace(" ", "")) >= 2 and contains_phrase(normalized, phrase):
            return f"Описание рисунка содержит вариант ответа «{text}». Уберите его: в alt не должно быть ответа."
    for label in correct_labels:
        if label and re.search(rf"(?<!\w){re.escape(label)}(?!\w)", alt, re.IGNORECASE):
            return f"Описание рисунка называет обозначение верного варианта «{label}». Уберите его."
    return None
