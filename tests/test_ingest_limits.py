import base64
import binascii
import time
import zlib

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.ingest import limits
from app.ingest.filters import FilterError, LzwDecoder, OverBudget, decoded_chunks
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
        return b"".join(decoded_chunks(data, list(names), 10**9))

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


def _lzw_encode(data: bytes, reset: bool = True) -> bytes:
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
        if reset or next_code < 4096:  # a full table is never extended (and never reset)
            table[candidate] = next_code
            next_code += 1
            if next_code + 1 > (1 << width) and width < 12:
                width += 1
        if reset and next_code >= 4094:
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
    assert b"".join(decoded_chunks(encoded, ["LZWDecode"], 10**9)) == data


def test_lzw_dictionary_never_grows_past_4096_entries():
    state = 12345
    noise = bytearray()
    for _ in range(150_000):  # incompressible enough to fill the table, never cleared
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        noise.append((state >> 16) & 0xFF)
    data = bytes(noise)
    decoder = LzwDecoder()
    out = b"".join(decoder.decode([_lzw_encode(data, reset=False)]))
    assert out == data
    assert len(decoder.table) <= 4096
    # a code that is not defined yet is a decode error, not a silent guess
    with pytest.raises(FilterError):
        b"".join(LzwDecoder().decode([b"\x80\x40\x80"]))


def _check(data, chain, monkeypatch, limit=1000):
    monkeypatch.setattr(limits, "MAX_UNPACKED_BYTES", limit)
    with pytest.raises(limits.UploadRejected) as err:
        limits.check_pdf(raw_pdf(f"/Filter {chain}", data))
    return err.value


def test_the_budget_holds_at_every_stage_not_only_at_the_end(monkeypatch):
    # Flate -> ASCIIHex: a 100 KB intermediate of ignorable blanks, a 1-byte final result.
    blanks = zlib.compress(b" " * 100_000 + b"41>")
    err = _check(blanks, "[/FlateDecode /ASCIIHexDecode]", monkeypatch)
    assert err.status == 422 and "200 МБ" in err.message
    # Flate -> RunLength: the end-of-data marker comes first, the intermediate is large.
    big = zlib.compress(b"\x80" + b"x" * 100_000)
    err = _check(big, "[/FlateDecode /RunLengthDecode]", monkeypatch)
    assert err.status == 422 and "200 МБ" in err.message
    # the same small intermediate is fine
    monkeypatch.setattr(limits, "MAX_UNPACKED_BYTES", 1000)
    ok = zlib.compress(b" " * 100 + b"41>")
    assert limits.check_pdf(raw_pdf("/Filter [/FlateDecode /ASCIIHexDecode]", ok)) == 1
    # the stage budget itself, below the PDF layer
    with pytest.raises(OverBudget):
        list(decoded_chunks(blanks, ["FlateDecode", "ASCIIHexDecode"], 1000))


def test_malformed_run_length_is_rejected(monkeypatch):
    for bad in (b"\x05ab", b"\x02abc\xfd", b"\x81"):  # short literal, cut repeat
        with pytest.raises(FilterError):
            b"".join(decoded_chunks(bad, ["RunLengthDecode"], 10**9))
        err = _check(bad, "/RunLengthDecode", monkeypatch, limit=10**9)
        assert err.status == 422


def test_decoders_stream_without_holding_their_input_or_output():
    import tracemalloc

    def peak(raw, chain):
        tracemalloc.start()
        tracemalloc.reset_peak()
        produced = sum(len(c) for c in decoded_chunks(raw, chain, 10**12))
        _, top = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return produced, top

    # name: (input, chain, peak limit). The limits sit above the streaming peak and below what
    # holding the input (a85, hex) or the output (run-length, flate) would cost.
    cases = {
        "run-length": (b"\x81\x00" * 100_000, ["RunLengthDecode"], 4_500_000),  # 0.2 -> 12.8 MB
        "flate": (zlib.compress(b"\0" * 16_000_000), ["FlateDecode"], 4_500_000),  # -> 16 MB
        "a85": (b"z" * 1_200_000, ["ASCII85Decode"], 1_900_000),  # 1.2 MB -> 4.8 MB
        "hex": (b"00" * 2_600_000 + b">", ["ASCIIHexDecode"], 1_500_000),  # 5 MB -> 2.6 MB
        "chain": (
            zlib.compress(b"\x81\x00" * 100_000),
            ["FlateDecode", "RunLengthDecode"],
            4_500_000,
        ),
    }
    for name, (raw, chain, limit) in cases.items():
        produced, top = peak(raw, chain)
        assert produced >= 2_000_000, name
        assert top < limit, (name, top)


def test_a_stage_nobody_reads_to_the_end_is_still_counted(monkeypatch):
    # Flate gives 4 MB that starts with the run-length end marker: RunLength stops at once and
    # never reads the rest; the unread output of the earlier stage must still be counted.
    payload = zlib.compress(b"\x80" + b"x" * 4_000_000)
    err = _check(payload, "[/FlateDecode /RunLengthDecode]", monkeypatch, limit=2_000_000)
    assert err.status == 422 and "200 МБ" in err.message
    with pytest.raises(OverBudget):
        list(decoded_chunks(payload, ["FlateDecode", "RunLengthDecode"], 2_000_000))
    # the same for a middle stage: Flate -> ASCIIHex (stops at '>') -> RunLength
    hexed = zlib.compress(b"80>" + b" " * 4_000_000)  # 0x80 = run-length end marker
    err = _check(hexed, "[/FlateDecode /ASCIIHexDecode /RunLengthDecode]", monkeypatch, 2_000_000)
    assert err.status == 422
    # within the budget the drained chain is accepted
    small = zlib.compress(b"\x80" + b"x" * 1000)
    ok = raw_pdf("/Filter [/FlateDecode /RunLengthDecode]", small)
    monkeypatch.setattr(limits, "MAX_UNPACKED_BYTES", 2_000_000)
    assert limits.check_pdf(ok) == 1


def test_ascii85_end_marker_split_across_chunks():
    raw = b"  " + b"!" * 8189 + b"~>"
    expected = base64.a85decode(b"!" * 8189)
    assert b"".join(decoded_chunks(raw, ["ASCII85Decode"], 10**9)) == expected
    # the marker cut at every possible position of a small stream
    body = base64.a85encode(b"Hello, world!!")
    for pad in range(8185, 8195):
        data = b" " * (pad - len(body) % 5) + body + b"~>"
        assert b"".join(decoded_chunks(data, ["ASCII85Decode"], 10**9)) == b"Hello, world!!"
    # a lone '~' that is not followed by '>' is malformed
    with pytest.raises(FilterError):
        b"".join(decoded_chunks(b'87cURD]i,"Ebo80~', ["ASCII85Decode"], 10**9))


def test_image_size_keys_given_as_indirect_references_are_resolved_and_fail_closed(monkeypatch):
    base = "/Filter /DCTDecode /ColorSpace /DeviceRGB /BitsPerComponent 8 "
    nums = b"5 0 obj 8500 endobj\n6 0 obj 8500 endobj\n"
    # the reviewer's case: 8500 x 8500 x 3 = 217 MB through indirect Width/Height
    with pytest.raises(limits.UploadRejected) as err:
        limits.check_pdf(raw_pdf(base + "/Width 5 0 R /Height 6 0 R", b"jpegbytes", nums))
    assert err.value.status == 422 and "200 МБ" in err.value.message
    # a small image with indirect numbers is fine, like direct ones
    small = b"5 0 obj 100 endobj\n6 0 obj 100 endobj\n"
    assert limits.check_pdf(raw_pdf(base + "/Width 5 0 R /Height 6 0 R", b"jpegbytes", small)) == 1
    # indirect BitsPerComponent counts too (16 bits double the raster)
    monkeypatch.setattr(limits, "MAX_UNPACKED_BYTES", 100_000_000)
    wide = b"5 0 obj 5000 endobj\n6 0 obj 5000 endobj\n7 0 obj 16 endobj\n"
    entries = "/Filter /DCTDecode /ColorSpace /DeviceRGB /Width 5 0 R /Height 6 0 R "
    with pytest.raises(limits.UploadRejected):
        limits.check_pdf(raw_pdf(entries + "/BitsPerComponent 7 0 R", b"jpegbytes", wide))
    # ICCBased: the component count comes from the profile (N 1 = 36 MB, N 3 = 108 MB at 6000^2)
    six = b"5 0 obj 6000 endobj\n6 0 obj 6000 endobj\n"
    for n, accepted in ((1, True), (3, False)):
        icc = six + b"7 0 obj<</N %d /Length 0>>stream\n\nendstream endobj\n" % n
        pdf = raw_pdf(
            "/Filter /DCTDecode /ColorSpace [/ICCBased 7 0 R] /BitsPerComponent 8 "
            "/Width 5 0 R /Height 6 0 R",
            b"jpegbytes",
            icc,
        )
        if accepted:
            assert limits.check_pdf(pdf) == 1
        else:
            with pytest.raises(limits.UploadRejected):
                limits.check_pdf(pdf)
    # fail closed: unresolvable, non-positive or missing values are rejected
    broken = b"5 0 obj /Foo endobj\n6 0 obj 100 endobj\n"
    cases = [
        (base + "/Width 5 0 R /Height 6 0 R", broken),
        (base + "/Width 0 /Height 100", b""),
        (base + "/Width 100 /Height -4", b""),
        (base + "/Width 100", b""),  # no Height at all
        (base + "/Width 9 0 R /Height 100", b""),  # dangling reference
    ]
    for entries, extra in cases:
        with pytest.raises(limits.UploadRejected) as err:
            limits.check_pdf(raw_pdf(entries, b"jpegbytes", extra))
        assert err.value.status == 422, entries
