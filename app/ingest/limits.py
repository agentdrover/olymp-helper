"""Лимиты загрузки PDF (SPEC-1.0 1.7): размер, страницы, распакованное содержимое."""

import zlib

import pymupdf

MB = 1024 * 1024
MAX_PDF_BYTES = 50 * MB
MAX_PAGES = 400
MAX_UNPACKED_BYTES = 200 * MB
IMPORT_TIMEOUT_S = 600
_CHUNK = 1 * MB


class UploadRejected(Exception):
    """Файл не принят; message читает родитель, status - код ответа."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _unpacked_size(doc: pymupdf.Document) -> int:
    """Сумма размеров потоков после распаковки; считается кусками, без загрузки в память."""
    total = 0
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref):
            continue
        raw = doc.xref_stream_raw(xref)
        kind, filt = doc.xref_get_key(xref, "Filter")
        if kind != "null" and "FlateDecode" in filt:
            total += _inflated_size(raw, MAX_UNPACKED_BYTES - total)
        else:
            total += len(raw)
        if total > MAX_UNPACKED_BYTES:
            break
    return total


def _inflated_size(raw: bytes, budget: int) -> int:
    """Размер распакованного потока; прекращает счёт, как только он превысит budget."""
    dec = zlib.decompressobj()
    size = 0
    buf = raw
    try:
        while True:
            data = dec.decompress(buf, _CHUNK)
            size += len(data)
            if size > budget:
                return size
            buf = dec.unconsumed_tail
            if dec.eof or (not data and not buf):
                return size
    except zlib.error:
        return len(raw)


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
