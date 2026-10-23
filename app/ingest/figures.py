"""Рисунок задания: автопоиск, рамка, PNG, проверка подтверждения (SPEC-1.0 1.1).

Рамка хранится в пунктах PDF в системе НЕповёрнутой страницы относительно MediaBox: то есть
в системе, где PyMuPDF отдаёт координаты изображений и текста, плюс смещение начала CropBox.
Рамка из пикселей рендера переводится: / масштаб, матрица снятия поворота, смещение CropBox.
"""

import re

import pymupdf

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


def suggest_rect(doc: pymupdf.Document, page_no: int, task_text: str) -> list[float] | None:
    """Рамка по месту встроенного растрового изображения рядом с заданием; иначе None.

    Скан (изображение почти на всю страницу) и векторная схема (изображений нет) не
    предлагаются: рамку рисует родитель.
    """
    page = doc[page_no - 1]
    area = abs(page.cropbox.width * page.cropbox.height)
    boxes = []
    for info in page.get_image_info():
        rect = pymupdf.Rect(info["bbox"])
        if rect.width < MIN_SIDE_PT or rect.height < MIN_SIDE_PT:
            continue
        if rect.width * rect.height >= SCAN_SHARE * area:
            continue
        boxes.append(rect)
    if not boxes:
        return None
    anchor = _anchor(page, task_text)
    if anchor is None:
        best = min(boxes, key=lambda r: (r.y0, r.x0))
    else:

        def gap(r):
            vertical = max(0.0, r.y0 - anchor.y1, anchor.y0 - r.y1)
            return (vertical, abs((r.y0 + r.y1) / 2 - (anchor.y0 + anchor.y1) / 2))

        best = min(boxes, key=gap)
    origin = _crop_origin(page)
    return [best.x0 + origin.x, best.y0 + origin.y, best.x1 + origin.x, best.y1 + origin.y]


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


# --------------------------------------------------------------- проверка alt и пометок

_WORD = re.compile(r"[0-9a-zа-я]+")


def _norm(text: str) -> str:
    return " ".join(_WORD.findall(text.lower().replace("ё", "е")))


def _contains_phrase(haystack: str, phrase: str) -> bool:
    return f" {phrase} " in f" {haystack} "


def alt_problem(alt: str, options, correct_labels: list[str]) -> str | None:
    """Причина отказа для alt или None. Сервер отклоняет alt с текстом любого варианта
    ответа (нормализованное сравнение) и с обозначением верного варианта."""
    if not alt.strip():
        return (
            "Описание рисунка (alt) обязательно: ребёнку и экранному диктору нужен короткий текст."
        )
    normalized = _norm(alt)
    texts = []
    if isinstance(options, list):
        texts = [o.get("text", "") for o in options]
    elif isinstance(options, dict):
        texts = [o.get("text", "") for side in options.values() for o in side]
    for text in texts:
        phrase = _norm(text)
        # Любой текст варианта целой фразой (нормализация, ё = е), буквы и цифры. Пропускается
        # только однознаковый текст: это обозначение, его закрывает проверка меток ниже.
        if len(phrase.replace(" ", "")) >= 2 and _contains_phrase(normalized, phrase):
            return f"Описание рисунка содержит вариант ответа «{text}». Уберите его: в alt не должно быть ответа."
    for label in correct_labels:
        if label and re.search(rf"(?<!\w){re.escape(label)}(?!\w)", alt, re.IGNORECASE):
            return f"Описание рисунка называет обозначение верного варианта «{label}». Уберите его."
    return None
