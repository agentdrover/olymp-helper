import time

import pymupdf
from fastapi.testclient import TestClient

from app.ingest import limits
from tests.bank_helpers import (
    PARENT,
    BlockingModel,
    build_app,
    db,
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
