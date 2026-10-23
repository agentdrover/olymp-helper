"""Потоковая распаковка полной цепочки фильтров PDF для подсчёта распакованного размера."""

import base64
import binascii
import zlib
from collections.abc import Iterable, Iterator

CHUNK = 1024 * 1024
ALIASES = {
    "Fl": "FlateDecode",
    "A85": "ASCII85Decode",
    "AHx": "ASCIIHexDecode",
    "LZW": "LZWDecode",
    "RL": "RunLengthDecode",
    "DCT": "DCTDecode",
    "CCF": "CCITTFaxDecode",
}
# Декодеры изображений: размер результата оценивается по пикселям, а не распаковкой.
IMAGE_CODECS = {"DCTDecode", "JPXDecode", "CCITTFaxDecode", "JBIG2Decode"}


class FilterError(Exception):
    """Неподдерживаемый фильтр или повреждённый поток."""


def canonical(name: str) -> str:
    return ALIASES.get(name, name)


def _flate(chunks: Iterable[bytes]) -> Iterator[bytes]:
    dec = zlib.decompressobj()
    try:
        for chunk in chunks:
            buf = chunk
            while True:
                data = dec.decompress(buf, CHUNK)
                if data:
                    yield data
                buf = dec.unconsumed_tail
                if not buf:
                    break
        tail = dec.flush()
        if tail:
            yield tail
        if not dec.eof:
            raise FilterError("поток Flate оборван")
    except zlib.error as exc:
        raise FilterError("повреждённый поток Flate") from exc


def _hex(chunks: Iterable[bytes]) -> Iterator[bytes]:
    pending = b""
    for chunk in chunks:
        digits = pending + bytes(b for b in chunk if b not in b" \t\r\n\f\0")
        end = digits.find(b">")
        if end >= 0:
            digits = digits[:end]
        pending = b""
        if len(digits) % 2 and end < 0:
            digits, pending = digits[:-1], digits[-1:]
        elif len(digits) % 2:
            digits += b"0"
        try:
            yield binascii.unhexlify(digits)
        except binascii.Error as exc:
            raise FilterError("повреждённый поток ASCIIHex") from exc
        if end >= 0:
            return
    if pending:
        yield binascii.unhexlify(pending + b"0")


def _a85(chunks: Iterable[bytes]) -> Iterator[bytes]:
    # Вход ограничен размером файла, а результат короче входа: целиком в память безопасно.
    raw = b"".join(chunks).strip()
    if raw.startswith(b"<~"):
        raw = raw[2:]
    end = raw.find(b"~>")
    if end >= 0:
        raw = raw[:end]
    try:
        yield base64.a85decode(raw, ignorechars=b" \t\n\r\v\f")
    except ValueError as exc:
        raise FilterError("повреждённый поток ASCII85") from exc


def _run_length(chunks: Iterable[bytes]) -> Iterator[bytes]:
    data = b"".join(chunks)
    out = bytearray()
    i = 0
    while i < len(data):
        n = data[i]
        if n == 128:
            break
        if n < 128:
            out += data[i + 1 : i + 2 + n]
            i += 2 + n
        else:
            if i + 1 >= len(data):
                raise FilterError("повреждённый поток RunLength")
            out += data[i + 1 : i + 2] * (257 - n)
            i += 2
        if len(out) >= CHUNK:
            yield bytes(out)
            out.clear()
    if out:
        yield bytes(out)


def _lzw(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """LZW PDF (EarlyChange=1, старший бит вперёд)."""
    data = b"".join(chunks)
    table = {i: bytes([i]) for i in range(256)}
    next_code, width = 258, 9
    buf, bits, prev = 0, 0, None
    out = bytearray()
    for byte in data:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= width:
            code = (buf >> (bits - width)) & ((1 << width) - 1)
            bits -= width
            buf &= (1 << bits) - 1
            if code == 256:
                table = {i: bytes([i]) for i in range(256)}
                next_code, width, prev = 258, 9, None
                continue
            if code == 257:
                if out:
                    yield bytes(out)
                return
            if prev is None:
                entry = table.get(code)
                if entry is None:
                    raise FilterError("повреждённый поток LZW")
            else:
                entry = table.get(code)
                if entry is None:
                    if code != next_code:
                        raise FilterError("повреждённый поток LZW")
                    entry = prev + prev[:1]
                table[next_code] = prev + entry[:1]
                next_code += 1
                if next_code + 1 >= 1 << width and width < 12:
                    width += 1
            out += entry
            prev = entry
            if len(out) >= CHUNK:
                yield bytes(out)
                out.clear()
    if out:
        yield bytes(out)


DECODERS = {
    "FlateDecode": _flate,
    "ASCIIHexDecode": _hex,
    "ASCII85Decode": _a85,
    "RunLengthDecode": _run_length,
    "LZWDecode": _lzw,
}


def decoded_chunks(raw: bytes, filters: list[str]) -> Iterator[bytes]:
    """Куски результата полной цепочки; неизвестный фильтр или ошибка - FilterError."""
    chunks: Iterable[bytes] = [raw]
    for name in filters:
        decoder = DECODERS.get(canonical(name))
        if decoder is None:
            raise FilterError(f"неподдерживаемый фильтр {name}")
        chunks = decoder(chunks)
    return iter(chunks)
