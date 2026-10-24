"""Рисунок задания: автопоиск, рамка, PNG, проверка подтверждения (SPEC-1.0 1.1).

Рамка хранится в пунктах PDF в системе НЕповёрнутой страницы относительно MediaBox: то есть
в системе, где PyMuPDF отдаёт координаты изображений и текста, плюс смещение начала CropBox.
Рамка из пикселей рендера переводится: / масштаб, матрица снятия поворота, смещение CropBox.
"""

import re

import pymupdf

from app.ingest.answers import normalize_answer
from app.ingest.leaks import contains_phrase, norm, significant

SCALE = 2.0  # PNG рисунка рендерится в двойном разрешении
DISPLAY_WIDTH_PX = 560  # SPEC 1.1: рисунок показывается не уже 560 px
MIN_LABEL_PX = 10.0  # подписи мельче этого размера при показе - рисунок нечитаем
_NUMBER = re.compile(r"(?<!\d)\d+(?:[.,]\d+)?(?!\d)")
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


AMBIGUOUS = "ambiguous"
START_CHARS = 30  # с этого начала ищется якорь и растёт по словам, пока не станет единственным
FALLBACK_CHARS = 15
CONTINUATION_CHARS = 60  # столько знаков условия должны совпасть на месте начала


def _flat(text: str) -> str:
    """Регистр, «ё» и пробелы не важны."""
    return " ".join(text.lower().replace("ё", "е").split())


class PageIndex:
    """Якоря заданий по ВСЕМУ материалу: слова подряд, место берётся у первого слова, поэтому
    перенос строки не мешает. Якорь - кратчайшее начало условия, которое встречается ровно в
    одном месте."""

    def __init__(self, doc: pymupdf.Document):
        self.pages = [
            [(w, y0) for _, y0, _, _, raw, *_ in page.get_text("words") if (w := _flat(raw))]
            for page in doc
        ]
        self.starts: dict[str, list[tuple[int, int]]] = {}
        for number, words in enumerate(self.pages):
            for i, (word, _) in enumerate(words):
                self.starts.setdefault(word, []).append((number, i))

    def _extend(self, hits, words, count):
        """Места, где подряд идут первые count слов (hits - места первых count - 1 слов)."""
        return [
            (n, i)
            for n, i in hits
            if i + count - 1 < len(self.pages[n]) and self.pages[n][i + count - 1][0] == words[-1]
        ]

    def _prefix(self, words, chars):
        """Места начала из words, набранного по целым словам до chars знаков."""
        count = 1
        while count < len(words) and len(" ".join(words[:count])) < chars:
            count += 1
        hits = list(self.starts.get(words[0], []))
        for size in range(2, count + 1):
            hits = self._extend(hits, words[:size], size)
        return hits, count

    def locate(self, text: str):
        """('known', [место]) - начало условия в одном месте и дальше совпадает с условием;
        ('ambiguous', [все места]) - даже всё условие не в одном месте; ('unknown', []) - начала
        нет на страницах или продолжение не совпадает: искать не у чего."""
        segments = re.split(r"[|\n]", text.replace("[рисунок]", " "))
        while segments and len(segments[0].strip()) < 6:  # метка ячейки, не начало условия
            segments.pop(0)
        words = _flat(" ".join(segments)).split()
        if not words or len(" ".join(words)) < 6:
            return "unknown", []
        hits, count = self._prefix(words, START_CHARS)
        if not hits:  # начало отличается от страницы: короткое начало
            hits, _ = self._prefix(words, FALLBACK_CHARS)
        else:
            while len(hits) > 1 and count < len(words):
                before, count = hits, count + 1
                hits = self._extend(hits, words[:count], count)
                if not hits:  # короткое начало было в нескольких местах, длинное - ни в одном
                    return "ambiguous", [self._place(h) for h in before]
        if len(hits) > 1:
            return "ambiguous", [self._place(h) for h in hits]
        if len(hits) == 1 and self._continues(hits[0], words, _flat(segments[0]).split()):
            return "known", [self._place(hits[0])]
        return "unknown", []

    def _continues(self, hit, words, first_line):
        """Дальше по странице идут те же слова условия: до конца первой строки или ячейки, но
        не меньше CONTINUATION_CHARS знаков, если условие столько длится."""
        need = max(len(first_line), 1)
        while need < len(words) and len(" ".join(words[:need])) < CONTINUATION_CHARS:
            need += 1
        number, i = hit
        page = self.pages[number]
        return i + need <= len(page) and all(page[i + j][0] == words[j] for j in range(need))

    def find(self, text: str):
        """(страница, y), AMBIGUOUS или None: то же, что locate, одним значением."""
        kind, places = self.locate(text)
        return places[0] if kind == "known" else AMBIGUOUS if kind == "ambiguous" else None

    def _place(self, hit):
        number, i = hit
        return (number + 1, self.pages[number][i][1])


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
    def gap(r):
        vertical = max(0.0, r.y0 - anchor.y1, anchor.y0 - r.y1)
        return (vertical, abs((r.y0 + r.y1) / 2 - (anchor.y0 + anchor.y1) / 2))

    return min(boxes, key=gap)


def suggest_regions(
    doc: pymupdf.Document,
    first_page: int,
    start_y: float | None,
    last_page: int,
    stop: tuple[int, float] | None = None,
) -> list[dict]:
    """Области рисунка по геометрии: все растровые изображения от начала этого задания
    (страница first_page, y = start_y) до начала следующего по месту задания (stop = страница и
    y), по одной области (объединению) на страницу, но не дальше последней страницы самого
    задания. Положение stop действует только на странице stop: если она дальше последней
    страницы задания, рисунок идёт до низа последней страницы. Без start_y границ нет и берётся
    ближайшее изображение; если между границами изображений нет, берётся ближайшее к началу, но
    не заходящее за начало следующего задания: такое изображение принадлежит соседу.

    Скан (изображение почти на всю страницу) и векторная схема (изображений нет) не
    предлагаются: рамку рисует родитель.
    """
    first = doc[first_page - 1]
    if start_y is None:  # начало не найдено: рядом быть не с чем
        return []
    end, cut = max(last_page, first_page), None
    if stop is not None and first_page <= stop[0] <= end:
        end, cut = stop
    regions = []
    for number in range(first_page, end + 1):
        page = doc[number - 1]
        top = start_y if number == first_page else float("-inf")
        bottom = cut if number == end and cut is not None else float("inf")
        inside = [r for r in _image_boxes(page) if top - 1 <= r.y0 and r.y1 <= bottom + 1]
        if inside:
            regions.append({"page": number, "rect": _stored(page, _union(inside))})
    if regions:
        return regions
    boxes = _image_boxes(first)
    if stop is not None and stop[0] == first_page:  # за началом соседнего задания рисунок чужой
        boxes = [r for r in boxes if r.y1 <= stop[1] + 1]
    start = pymupdf.Rect(0, start_y, 1, start_y)
    return [{"page": first_page, "rect": _stored(first, _nearest(boxes, start))}] if boxes else []


def regions_of(figure: dict) -> list[dict]:
    """Области рисунка; запись старого вида (page и rect без regions) - одна область."""
    if figure.get("regions"):
        return [dict(r) for r in figure["regions"]]
    if figure.get("rect") and figure.get("page"):
        return [{"page": figure["page"], "rect": list(figure["rect"])}]
    return []


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


def shown_width(doc: pymupdf.Document, page_no: int, stored) -> float:
    """Ширина рамки в пунктах так, как она видна на показанной (повёрнутой) странице."""
    page = doc[page_no - 1]
    area = cropbox_relative(page, stored)
    return area.height if page.rotation in (90, 270) else area.width


def smallest_label_px(
    doc: pymupdf.Document, page_no: int, stored, width: float | None = None
) -> float | None:
    """Высота самой мелкой подписи внутри рамки при показе шириной 560 px; None - подписей нет.
    width - ширина всей показываемой картинки, если рамка лишь её часть."""
    page = doc[page_no - 1]
    area = cropbox_relative(page, stored)
    shown_width = width or (area.height if page.rotation in (90, 270) else area.width)
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


def labels_readable(doc, page_no, stored, width: float | None = None) -> bool:
    smallest = smallest_label_px(doc, page_no, stored, width)
    return smallest is None or smallest >= MIN_LABEL_PX


def regions_readable(doc, regions) -> bool:
    """Области лежат друг под другом в одной картинке шириной 560 px: масштаб общий, по самой
    широкой области, и мелкая подпись узкой области уменьшается вместе с ней."""
    width = max(shown_width(doc, r["page"], r["rect"]) for r in regions)
    return all(labels_readable(doc, r["page"], r["rect"], width) for r in regions)


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
            spans = [s["text"] for s in line.get("spans", []) if s["text"].strip()]
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
        if unit and unit not in wanted:
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
    shown = {n for raw in _NUMBER.findall(alt) if (n := normalize_answer(raw, "numeric"))}
    for label in correct_labels:
        number = normalize_answer(label, "numeric") if label else None
        if number is not None and number in shown:  # «28,5», «28.50» и «28.5» - одно число
            return f"Описание рисунка называет число-ответ «{label}». Уберите его."
        if label and re.search(rf"(?<!\w){re.escape(label)}(?!\w)", alt, re.IGNORECASE):
            return f"Описание рисунка называет обозначение верного варианта «{label}». Уберите его."
    return None
