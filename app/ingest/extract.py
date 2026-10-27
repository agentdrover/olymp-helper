"""Текст PDF по страницам (PyMuPDF) и порции для запроса к модели (SPEC-1.0 1.7).

Таблицы передаются строками: ячейки одной строки через « | », изображение в ячейке - «[рисунок]».
Так ответ, стоящий в ячейке рядом с условием, остаётся в своей строке, а не теряется в потоке.
"""

import pymupdf

CHUNK_CHARS = 12_000
CELL_SEPARATOR = " | "
IMAGE_MARK = "[рисунок]"


def _tables(page) -> list:
    try:
        return list(page.find_tables().tables)
    except Exception:  # noqa: BLE001 - разметка таблиц не обязательна: останется обычный текст
        return []


def _cell_text(page, rect, images) -> str:
    text = " ".join(page.get_text("text", clip=rect).split())
    has_image = any(
        rect.contains(pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)) for r in images
    )
    return f"{IMAGE_MARK} {text}".strip() if has_image else text


def _table_lines(page, table, images) -> list[str]:
    lines = []
    for row in table.rows:
        cells = [
            _cell_text(page, pymupdf.Rect(c), images) if c is not None else "" for c in row.cells
        ]
        if any(cells):
            lines.append(CELL_SEPARATOR.join(cells))
    return lines


def _page_text(page) -> str:
    tables = _tables(page)
    if not tables:
        return page.get_text("text").strip()
    images = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
    boxes = [pymupdf.Rect(t.bbox) for t in tables]
    items: list[tuple[float, float, str]] = []
    for table, box in zip(tables, boxes, strict=True):
        lines = _table_lines(page, table, images)
        if lines:
            items.append((box.y0, box.x0, "\n".join(lines)))
    for block in page.get_text("blocks"):
        x0, y0, x1, y1, text, _, kind = block[:7]
        centre = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
        if kind == 0 and text.strip() and not any(box.contains(centre) for box in boxes):
            items.append((y0, x0, text.strip()))
    return "\n".join(text for _, _, text in sorted(items, key=lambda i: (i[0], i[1])))


def extract_pages(data: bytes, on_page=None) -> list[tuple[int, str]]:
    """[(номер страницы с 1, текст)]; страница без текста даёт пустую строку.
    on_page(номер, всего) - после каждой страницы: пульс для сторожка импорта."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        out = []
        for i, page in enumerate(doc, 1):
            out.append((i, _page_text(page)))
            if on_page is not None:
                on_page(i, doc.page_count)
        return out
    finally:
        doc.close()


def chunk_pages(
    pages: list[tuple[int, str]], limit: int = CHUNK_CHARS
) -> list[list[tuple[int, str]]]:
    """Порции целых страниц до limit знаков; слишком длинная страница режется, номер сохраняется."""
    pieces: list[tuple[int, str]] = []
    for number, text in pages:
        if not text:
            continue
        for start in range(0, len(text), limit):
            pieces.append((number, text[start : start + limit]))
    chunks: list[list[tuple[int, str]]] = []
    size = 0
    for piece in pieces:
        if not chunks or size + len(piece[1]) > limit:
            chunks.append([])
            size = 0
        chunks[-1].append(piece)
        size += len(piece[1])
    return chunks
