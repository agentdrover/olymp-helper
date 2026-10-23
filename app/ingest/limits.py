"""Лимиты загрузки PDF (SPEC-1.0 1.7): размер, страницы, распакованное содержимое."""

import re

import pymupdf

from app.ingest.filters import IMAGE_CODECS, FilterError, OverBudget, canonical, decoded_chunks

MB = 1024 * 1024
MAX_PDF_BYTES = 50 * MB
MAX_PAGES = 400
MAX_UNPACKED_BYTES = 200 * MB
IMPORT_TIMEOUT_S = 600


class UploadRejected(Exception):
    """Файл не принят; message читает родитель, status - код ответа."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


_NAME = re.compile(r"/?([A-Za-z0-9]+)")
_COMPONENTS = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}


def _filters(doc, xref: int) -> list[str]:
    kind, value = doc.xref_get_key(xref, "Filter")
    if kind == "null":
        return []
    return [m.group(1) for m in _NAME.finditer(value.strip("[]"))]


def _image_estimate(doc, xref: int, raw_size: int) -> int:
    """Распакованный размер растра по ширине, высоте и глубине; не меньше размера данных."""

    def number(key):
        kind, value = doc.xref_get_key(xref, key)
        return int(float(value)) if kind == "int" else 0

    cs = doc.xref_get_key(xref, "ColorSpace")[1].lstrip("/")
    bpc = number("BitsPerComponent") or 8
    pixels = number("Width") * number("Height") * _COMPONENTS.get(cs, 4) * bpc // 8
    return max(raw_size, pixels)


def _stream_size(doc, xref: int, budget: int) -> int:
    raw = doc.xref_stream_raw(xref)
    filters = [canonical(f) for f in _filters(doc, xref)]
    if filters and filters[-1] in IMAGE_CODECS:
        data_size = _count(raw, filters[:-1], budget)
        return max(data_size, _image_estimate(doc, xref, data_size))
    return _count(raw, filters, budget)


def _count(raw: bytes, filters: list[str], budget: int) -> int:
    """Размер результата цепочки; бюджет проверяется на каждом этапе, превышение = budget + 1."""
    total = 0
    try:
        for chunk in decoded_chunks(raw, filters, budget):
            total += len(chunk)
    except OverBudget:
        return budget + 1
    return total


def _unpacked_size(doc: pymupdf.Document) -> int:
    """Сумма размеров потоков после ПОЛНОЙ цепочки фильтров; считается кусками."""
    total = 0
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref):
            continue
        try:
            total += _stream_size(doc, xref, MAX_UNPACKED_BYTES - total)
        except FilterError as exc:
            raise UploadRejected(
                422,
                f"Не удалось проверить размер содержимого после распаковки: {exc}. "
                "Нужен PDF без повреждённых потоков и редких фильтров.",
            ) from exc
        if total > MAX_UNPACKED_BYTES:
            break
    return total


def check_pdf(data: bytes) -> int:
    """Проверить лимиты и вернуть число страниц; иначе UploadRejected с понятным текстом."""
    if len(data) > MAX_PDF_BYTES:
        raise UploadRejected(413, "Файл больше 50 МБ. Разбейте сборник на части.")
    try:
        doc = pymupdf.open("pdf", data)
    except Exception as exc:
        raise UploadRejected(422, "Не удалось прочитать файл как PDF.") from exc
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise UploadRejected(422, "PDF защищён паролем: загрузите файл без защиты.")
        if doc.page_count > MAX_PAGES:
            raise UploadRejected(
                422, f"В файле {doc.page_count} страниц, допустимо не больше {MAX_PAGES}."
            )
        if _unpacked_size(doc) > MAX_UNPACKED_BYTES:
            raise UploadRejected(422, "Содержимое файла после распаковки больше 200 МБ.")
        return doc.page_count
    finally:
        doc.close()
