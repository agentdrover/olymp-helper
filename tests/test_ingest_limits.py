import base64
import binascii
import time
import zlib

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.ingest import limits
from app.ingest.filters import FilterError, decoded_chunks
from tests.bank_helpers import (
    PARENT,
    BlockingModel,
    build_app,
    db,
    raw_pdf,
    text_pdf,
    upload,
)

MB = 1024 * 1024


def _huge_stream_pdf(unpacked: int) -> bytes:
    """Tiny file, huge content once unpacked (a compression bomb)."""
    doc = pymupdf.open()
    doc.new_page()
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, b"\0" * unpacked, compress=True)
    return doc.tobytes(garbage=0, deflate=True)


def _wait_status(client, mid, want, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        st = client.get(f"/parent/materials/{mid}", headers=PARENT).json()
        if st["import_status"] == want:
            return st
        time.sleep(0.05)
    raise AssertionError(f"material {mid} never became {want}: {st}")


def test_ac6(tmp_path):
    assert limits.MAX_PDF_BYTES == 50 * MB
    assert limits.MAX_PAGES == 400
    assert limits.MAX_UNPACKED_BYTES == 200 * MB
    assert limits.IMPORT_TIMEOUT_S == 600

    blocker = BlockingModel()
    app = build_app(tmp_path, blocker, sync_imports=False, import_timeout=0.4)
    with TestClient(app) as c:
        # Over 50 MB: refused before anything is parsed.
        r = upload(c, b"%PDF-1.4\n" + b"0" * (50 * MB + 1))
        assert r.status_code == 413 and "50 МБ" in r.json()["error"]

        # Over 400 pages.
        many = pymupdf.open()
        for _ in range(401):
            many.new_page()
        r = upload(c, many.tobytes(deflate=True), name="many.pdf")
        assert r.status_code == 422 and "400" in r.json()["error"]

        # Over 200 MB once unpacked.
        r = upload(c, _huge_stream_pdf(201 * MB), name="bomb.pdf")
        assert r.status_code == 422 and "200 МБ" in r.json()["error"]

        # A bomb just under the limit is not refused for size.
        ok = upload(c, _huge_stream_pdf(20 * MB), name="fine.pdf", variant="9")
        assert ok.status_code == 202, ok.text
        _wait_status(c, ok.json()["material_id"], "failed")  # no text layer: a different error

        conn = db(tmp_path)
        assert conn.execute("SELECT COUNT(*) FROM material").fetchone()[0] == 1

        # A hung import (model never answers) is aborted with an error status;
        # /healthz keeps answering while it runs.
        hung = upload(c, text_pdf([["1. задание, которое зависнет"]]), name="hang.pdf")
        assert hung.status_code == 202
        mid = hung.json()["material_id"]
        assert blocker.entered.wait(5)
        assert c.get("/healthz").status_code == 200
        st = _wait_status(c, mid, "failed")
        assert "время" in st["import_error"]
        assert c.get("/healthz").status_code == 200

        # The late worker cannot resurrect the import or write tasks.
        blocker.release.set()
        app.state.imports.wait_idle(5)
        st = c.get(f"/parent/materials/{mid}", headers=PARENT).json()
        assert st["import_status"] == "failed" and st["tasks"] == 0


def _rl_literal(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 128):
        chunk = data[i : i + 128]
        out += bytes([len(chunk) - 1]) + chunk
    return bytes(out) + b"\x80"


def test_the_unpacked_limit_covers_the_whole_filter_chain(monkeypatch):
    monkeypatch.setattr(limits, "MAX_UNPACKED_BYTES", 300_000)
    payload = zlib.compress(b"\0" * 600_000)
    chains = {
        "[/ASCII85Decode /FlateDecode]": base64.a85encode(payload, adobe=True),
        "[/ASCIIHexDecode /FlateDecode]": binascii.hexlify(payload) + b">",
        "[/RunLengthDecode /FlateDecode]": _rl_literal(payload),
        "[/A85 /Fl]": base64.a85encode(payload, adobe=True),
    }
    for chain, data in chains.items():
        with pytest.raises(limits.UploadRejected) as err:
            limits.check_pdf(raw_pdf(f"/Filter {chain}", data))
        assert err.value.status == 422 and "200 МБ" in err.value.message, chain
    # the same chains with a small payload are accepted
    small = zlib.compress(b"\0" * 1000)
    ok = raw_pdf("/Filter [/ASCII85Decode /FlateDecode]", base64.a85encode(small, adobe=True))
    assert limits.check_pdf(ok) == 1


def test_unknown_filters_and_decode_errors_are_rejected():
    for entries in ("/Filter /FooDecode", "/Filter [/Crypt /FlateDecode]"):
        with pytest.raises(limits.UploadRejected) as err:
            limits.check_pdf(raw_pdf(entries, b"abc"))
        assert err.value.status == 422 and "фильтр" in err.value.message
    with pytest.raises(limits.UploadRejected) as err:  # not a zlib stream
        limits.check_pdf(raw_pdf("/Filter /FlateDecode", b"this is not zlib data"))
    assert err.value.status == 422


def test_image_codecs_are_bounded_by_their_pixels():
    base = "/Filter /DCTDecode /ColorSpace /DeviceRGB /BitsPerComponent 8 "
    assert limits.check_pdf(raw_pdf(base + "/Width 100 /Height 100", b"jpegbytes")) == 1
    with pytest.raises(limits.UploadRejected) as err:
        limits.check_pdf(raw_pdf(base + "/Width 20000 /Height 20000", b"jpegbytes"))
    assert err.value.status == 422 and "200 МБ" in err.value.message


def test_stream_decoders():
    def run(data, *names):
        return b"".join(decoded_chunks(data, list(names)))

    assert run(b"48 65 6C6c 6F>", "ASCIIHexDecode") == b"Hello"
    assert run(b"4865 6C6C 6F7>", "ASCIIHexDecode") == b"Hello\x70"  # odd digit padded with 0
    assert run(base64.a85encode(b"Hello, world", adobe=True), "ASCII85Decode") == b"Hello, world"
    assert run(b"\x02abc\xfdz\x80", "RunLengthDecode") == b"abczzzz"
    # the LZW example of the PDF specification
    lzw = bytes([0x80, 0x0B, 0x60, 0x50, 0x22, 0x0C, 0x0C, 0x85, 0x01])
    assert run(lzw, "LZWDecode") == bytes([45, 45, 45, 45, 45, 65, 45, 45, 45, 66])
    assert run(zlib.compress(b"abc"), "FlateDecode") == b"abc"
    with pytest.raises(FilterError):
        run(b"abc", "FooDecode")
    with pytest.raises(FilterError):
        run(b"not zlib", "FlateDecode")


def _lzw_encode(data: bytes) -> bytes:
    """PDF LZW encoder (EarlyChange=1) used to check the decoder on long inputs."""
    out, buf, bits = bytearray(), 0, 0

    def put(code, width):
        nonlocal buf, bits
        buf = (buf << width) | code
        bits += width
        while bits >= 8:
            out.append((buf >> (bits - 8)) & 0xFF)
            bits -= 8
            buf &= (1 << bits) - 1

    table = {bytes([i]): i for i in range(256)}
    next_code, width = 258, 9
    put(256, width)
    current = b""
    for byte in data:
        candidate = current + bytes([byte])
        if candidate in table:
            current = candidate
            continue
        put(table[current], width)
        table[candidate] = next_code
        next_code += 1
        if next_code + 1 > (1 << width) and width < 12:
            width += 1
        if next_code >= 4094:
            put(256, width)
            table = {bytes([i]): i for i in range(256)}
            next_code, width = 258, 9
        current = bytes([byte])
    if current:
        put(table[current], width)
    put(257, width)
    if bits:
        put(0, 8 - bits)
    return bytes(out)


def test_lzw_decoder_round_trips_long_data_with_width_changes_and_table_resets():
    data = bytes((i * 7 + i // 3) % 251 for i in range(30_000)) + b"abc" * 5000
    encoded = _lzw_encode(data)
    assert b"".join(decoded_chunks(encoded, ["LZWDecode"])) == data
