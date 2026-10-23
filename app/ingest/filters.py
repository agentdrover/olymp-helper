"""Потоковая распаковка полной цепочки фильтров PDF для подсчёта распакованного размера.

Каждый декодер читает куски предыдущего этапа и отдаёт куски; целиком вход или выход не
держится в памяти. Один и тот же бюджет байт проверяется на выходе КАЖДОГО этапа.
"""

import base64
import binascii
import zlib
from collections.abc import Iterable, Iterator

CHUNK = 1024 * 1024  # размер куска на выходе
SLICE = 64 * 1024  # вход декодеров нарезается на такие куски: ограничивает временную память
A85_SLICE = 8 * 1024  # «z» раздувается в 5 знаков, а a85decode строит список по группам
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
WHITESPACE = b" \t\r\n\f\v\0"
LZW_MAX_ENTRIES = 4096  # 12-битное пространство кодов


class FilterError(Exception):
    """Неподдерживаемый фильтр или повреждённый поток."""


class OverBudget(Exception):
    """Результат этапа превысил бюджет распакованных данных."""


def canonical(name: str) -> str:
    return ALIASES.get(name, name)


def _slices(chunks: Iterable[bytes], size: int = SLICE) -> Iterator[bytes]:
    for chunk in chunks:
        for start in range(0, len(chunk), size):
            yield chunk[start : start + size]


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
    for chunk in _slices(chunks):
        digits = pending + chunk.translate(None, WHITESPACE)
        end = digits.find(b">")
        if end >= 0:
            digits = digits[:end]
        pending = b""
        if len(digits) % 2:
            if end < 0:
                digits, pending = digits[:-1], digits[-1:]
            else:
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
    pending = b""
    first = True
    for chunk in _slices(chunks, A85_SLICE):
        data = chunk.translate(None, WHITESPACE)
        if first:
            data = pending + data
            if data.startswith(b"<~"):
                data = data[2:]
            elif len(data) < 2 and b"<~".startswith(data):
                pending = data
                continue
            first, pending = False, b""
        else:
            data = pending + data
        end = data.find(b"~>")
        done = end >= 0
        if done:
            data = data[:end]
        # «z» - четыре нулевых байта; вне группы из 5 знаков он равен «!!!!!».
        data = data.replace(b"z", b"!!!!!")
        cut = len(data) // 5 * 5
        body, pending = data[:cut], data[cut:]
        try:
            if body:
                yield base64.a85decode(body)
        except ValueError as exc:
            raise FilterError("повреждённый поток ASCII85") from exc
        if done:
            break
    if pending:
        try:
            yield base64.a85decode(pending)
        except ValueError as exc:
            raise FilterError("повреждённый поток ASCII85") from exc


def _run_length(chunks: Iterable[bytes]) -> Iterator[bytes]:
    out = bytearray()
    buf = b""
    for chunk in _slices(chunks):
        buf += chunk
        i = 0
        while i < len(buf):
            n = buf[i]
            if n == 128:  # конец данных
                if out:
                    yield bytes(out)
                return
            if n < 128:
                if i + 2 + n > len(buf):
                    break
                out += buf[i + 1 : i + 2 + n]
                i += 2 + n
            else:
                if i + 2 > len(buf):
                    break
                out += buf[i + 1 : i + 2] * (257 - n)
                i += 2
            if len(out) >= CHUNK:
                yield bytes(out)
                out.clear()
        buf = buf[i:]
    if buf:
        raise FilterError("повреждённый поток RunLength: оборванная серия")
    if out:
        yield bytes(out)


class LzwDecoder:
    """LZW PDF (EarlyChange=1, старший бит вперёд). Словарь не больше 4096 записей: после
    заполнения он не растёт, а код вне словаря - ошибка."""

    def __init__(self):
        self.table: dict[int, bytes] = {}
        self._reset()

    def _reset(self):
        self.table = {i: bytes([i]) for i in range(256)}
        self.next_code, self.width, self.prev = 258, 9, None

    def decode(self, chunks: Iterable[bytes]) -> Iterator[bytes]:
        buf, bits = 0, 0
        out = bytearray()
        for chunk in _slices(chunks):
            for byte in chunk:
                buf = (buf << 8) | byte
                bits += 8
                while bits >= self.width:
                    code = (buf >> (bits - self.width)) & ((1 << self.width) - 1)
                    bits -= self.width
                    buf &= (1 << bits) - 1
                    if code == 256:
                        self._reset()
                        continue
                    if code == 257:
                        if out:
                            yield bytes(out)
                        return
                    out += self._entry(code)
                    if len(out) >= CHUNK:
                        yield bytes(out)
                        out.clear()
        if out:
            yield bytes(out)

    def _entry(self, code: int) -> bytes:
        table, prev = self.table, self.prev
        entry = table.get(code)
        if entry is None:
            # единственный допустимый неизвестный код - тот, что создаётся прямо сейчас
            if prev is None or code != self.next_code or self.next_code >= LZW_MAX_ENTRIES:
                raise FilterError("повреждённый поток LZW: код вне словаря")
            entry = prev + prev[:1]
        if prev is not None and self.next_code < LZW_MAX_ENTRIES:
            table[self.next_code] = prev + entry[:1]
            self.next_code += 1
            if self.next_code + 1 >= 1 << self.width and self.width < 12:
                self.width += 1
        self.prev = entry
        return entry


def _lzw(chunks: Iterable[bytes]) -> Iterator[bytes]:
    return LzwDecoder().decode(chunks)


DECODERS = {
    "FlateDecode": _flate,
    "ASCIIHexDecode": _hex,
    "ASCII85Decode": _a85,
    "RunLengthDecode": _run_length,
    "LZWDecode": _lzw,
}


def _bounded(chunks: Iterable[bytes], budget: int) -> Iterator[bytes]:
    """Пропускает куски и обрывает поток, как только выход этапа превысил бюджет."""
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > budget:
            raise OverBudget
        yield chunk


def decoded_chunks(raw: bytes, filters: list[str], budget: int) -> Iterator[bytes]:
    """Куски результата полной цепочки. Бюджет проверяется на выходе каждого этапа;
    неизвестный фильтр или ошибка потока - FilterError, превышение - OverBudget."""
    chunks: Iterable[bytes] = [raw]
    for name in filters:
        decoder = DECODERS.get(canonical(name))
        if decoder is None:
            raise FilterError(f"неподдерживаемый фильтр {name}")
        chunks = _bounded(decoder(chunks), budget)
    return iter(chunks)
