"""Текст PDF по страницам (pypdf) и порции для запроса к модели (SPEC-1.0 1.7)."""

import io

from pypdf import PdfReader

CHUNK_CHARS = 12_000


def extract_pages(data: bytes) -> list[tuple[int, str]]:
    """[(номер страницы с 1, текст)]; страница без текста даёт пустую строку."""
    reader = PdfReader(io.BytesIO(data))
    return [(i, (page.extract_text() or "").strip()) for i, page in enumerate(reader.pages, 1)]


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
