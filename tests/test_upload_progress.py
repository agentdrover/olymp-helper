"""OH-UI3: the upload form gets a page, not JSON; the parent sees the import progress."""

import re
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from app.model.types import ModelResult
from tests.bank_helpers import (
    PARENT,
    StubModel,
    build_app,
    db,
    make_task,
    reply,
    text_pdf,
    upload,
)

HTML = {**PARENT, "Accept": "text/html,application/xhtml+xml"}
API = {**PARENT, "Accept": "application/json"}
LONG = "а" * 7000  # one page per chunk


def post(client, headers, pdf=b"", kind="tasks", year="2022", name="f.pdf"):
    return client.post(
        "/parent/materials",
        headers=headers,
        files={"file": (name, pdf, "application/pdf")},
        data={"kind": kind, "year": year},
        follow_redirects=False,
    )


def one_task_pdf(text="1. вопрос"):
    return text_pdf([[text]])


def test_the_browser_form_gets_a_page_not_json(tmp_path):
    model = StubModel(split=lambda content: reply(make_task(1)))
    with TestClient(build_app(tmp_path, model)) as c:
        pdf = one_task_pdf()
        got = post(c, HTML, pdf)
        assert got.status_code == 303
        assert got.headers["location"] == "/parent/materials?msg=queued"
        page = c.get(got.headers["location"], headers=HTML)
        assert "Сборник принят, разбор идёт" in page.text

        again = post(c, HTML, pdf)
        assert again.status_code == 303
        assert "Файл уже загружен" in c.get(again.headers["location"], headers=HTML).text

        # an API client keeps the JSON, byte for byte
        api = post(c, API, pdf)
        assert api.status_code == 200
        assert api.headers["content-type"].startswith("application/json")
        assert api.json() == {"duplicate": True, "requeued": False, "material_id": 1}
        fresh = post(c, API, one_task_pdf("1. другой"))
        assert fresh.status_code == 202
        assert fresh.json() == {"duplicate": False, "material_id": 2, "status": "queued"}

        # 422 (bad field), 422/413-class rejection of the file, 409 (deleted): a page with the text
        bad_kind = post(c, HTML, pdf, kind="bogus")
        assert bad_kind.status_code == 422
        assert bad_kind.headers["content-type"].startswith("text/html")
        assert "Тип материала" in bad_kind.text
        bad_file = post(c, HTML, b"not a pdf at all")
        assert bad_file.status_code in (413, 422)
        assert bad_file.headers["content-type"].startswith("text/html")
        assert "<h1>Материалы и модель</h1>" in bad_file.text
        assert 'role="alert"' in bad_file.text
        conn = db(tmp_path)
        conn.execute("UPDATE material SET deleted_at = '2026-01-01T00:00:00' WHERE id = 1")
        conn.close()
        gone = post(c, HTML, pdf)
        assert gone.status_code == 409
        assert gone.headers["content-type"].startswith("text/html")
        assert "удалён вместе с файлом" in gone.text
        # the API still answers JSON for the same refusal
        assert post(c, API, pdf).json()["error"].startswith("Этот материал удалён")


class ChunkGate:
    """A model that stops inside every chunk until the test lets it go on."""

    def __init__(self, chunks=3):
        self.entered = [threading.Event() for _ in range(chunks)]
        self.go = [threading.Event() for _ in range(chunks)]
        self.calls = 0

    def complete(self, system, messages, schema=None, session_id=None):
        index = self.calls
        self.calls += 1
        self.entered[index].set()
        assert self.go[index].wait(30)
        return ModelResult(
            None, reply(make_task(index + 1, pages=[index + 1])), "claude", "s", 1, None
        )


def test_progress_is_reported_per_chunk(tmp_path):
    model = ChunkGate(3)
    pdf = text_pdf([["1. " + LONG], ["2. " + LONG], ["3. " + LONG]])
    app = build_app(tmp_path, model, sync_imports=False)
    with TestClient(app) as c:
        assert upload(c, pdf).status_code == 202
        for n in range(3):
            assert model.entered[n].wait(30)
            status = c.get("/parent/materials/1", headers=API).json()
            assert status["import_status"] == "running"
            assert status["progress"] == {"chunk": n + 1, "chunks": 3, "accepted": n}
            page = c.get("/parent/materials", headers=HTML).text
            assert f"Разбирается: порция {n + 1} из 3, принято заданий {n}" in page
            assert 'data-live="1"' in page
            model.go[n].set()
        app.state.imports.wait_idle(30)
        done = c.get("/parent/materials/1", headers=API).json()
        assert done["import_status"] == "done"
        assert done["progress"] is None
        assert done["tasks"] == 3
        assert len(app.state.ctx.progress) == 0
        page = c.get("/parent/materials", headers=HTML).text
        assert "Разбирается" not in page
        assert re.search(r'data-live="0"', page)


CHROME = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"


def test_accept_is_parsed_with_q_values(tmp_path):
    model = StubModel(split=lambda content: reply(make_task(1)))
    with TestClient(build_app(tmp_path, model)) as c:
        for n, (accept, browser) in enumerate(
            [
                (CHROME, True),
                ("application/json", False),
                ("application/json,text/html;q=0", False),
                ("*/*", False),
                ("text/html;q=0.5,application/json", False),
                ("text/html,application/json", True),  # tie, a multipart form: the page
            ]
        ):
            got = post(c, {**PARENT, "Accept": accept}, one_task_pdf(f"1. вопрос {n}"))
            assert (got.status_code == 303) is browser, accept
            if not browser:
                assert got.headers["content-type"].startswith("application/json"), accept


def test_a_late_entry_of_an_expired_run_is_not_shown(tmp_path):
    model = ChunkGate(4)
    pdf = text_pdf([["1. " + LONG], ["2. " + LONG]])
    app = build_app(tmp_path, model, sync_imports=False, import_timeout=0.5)
    with TestClient(app) as c:
        assert upload(c, pdf).status_code == 202
        assert model.entered[0].wait(30)
        conn = db(tmp_path)
        run_a = conn.execute("SELECT run_id FROM material WHERE id = 1").fetchone()[0]
        conn.close()
        for _ in range(100):  # the watchdog expires run A
            if c.get("/parent/materials/1", headers=API).json()["import_status"] == "failed":
                break
            threading.Event().wait(0.1)
        assert c.get("/parent/materials/1", headers=API).json()["import_status"] == "failed"
        assert len(app.state.ctx.progress) == 0  # A's entry is dropped on expiry
        assert upload(c, pdf).json()["requeued"] is True  # run B
        assert model.entered[1].wait(30)
        status = c.get("/parent/materials/1", headers=API).json()
        assert status["import_status"] == "running"
        assert status["progress"] == {"chunk": 1, "chunks": 2, "accepted": 0}
        app.state.ctx.progress.update(1, run_a, 2, 2, 7)  # run A writes late
        status = c.get("/parent/materials/1", headers=API).json()
        assert status["progress"] is None
        assert "принято заданий 7" not in c.get("/parent/materials", headers=HTML).text
        for gate in model.go:
            gate.set()
        app.state.imports.wait_idle(30)


def test_the_polling_script_chains_timeouts_and_reloads_once():
    # No JS engine in the test environment: the guard is checked on the source.
    src = (Path(__file__).parent.parent / "app/static/progress.js").read_text()
    assert "setInterval" not in src
    assert "setTimeout(tick" in src and src.count("setTimeout(") == 1
    assert "if (!reloading) {" in src and src.count("location.reload()") == 1
    assert src.index("reloading = true") < src.index("location.reload()")
