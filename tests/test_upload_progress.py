"""OH-UI3: the upload form gets a page, not JSON; the parent sees the import progress."""

import re
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.model.types import ModelResult
from tests.bank_helpers import (
    PARENT,
    FakeClock,
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
        assert got.headers["location"] == "/parent/materials?msg=queued&id=1"
        # the import already finished (sync stub): «разбор идёт» must not linger on the page
        assert "Сборник принят" not in c.get(got.headers["location"], headers=HTML).text
        conn = db(tmp_path)
        conn.execute("UPDATE material SET import_status = 'running' WHERE id = 1")
        page = c.get(got.headers["location"], headers=HTML)
        assert "Сборник принят, разбор идёт" in page.text
        conn.execute("UPDATE material SET import_status = 'done' WHERE id = 1")
        conn.close()

        again = post(c, HTML, pdf)
        assert again.status_code == 303
        assert again.headers["location"] == "/parent/materials?msg=duplicate&id=1"
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
            assert status["progress"] == {
                "phase": "chunks",
                "chunk": n + 1,
                "chunks": 3,
                "accepted": n,
            }
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
                ("text/html,application/json;q=NaN", True),  # a broken q reads as 0
                ("text/html;q=inf,application/json", False),
                ("text/html;q=7,application/json", False),
                ("text/html,application/json", True),  # tie, a multipart form: the page
            ]
        ):
            got = post(c, {**PARENT, "Accept": accept}, one_task_pdf(f"1. вопрос {n}"))
            assert (got.status_code == 303) is browser, accept
            if not browser:
                assert got.headers["content-type"].startswith("application/json"), accept


def test_a_late_entry_of_an_expired_run_is_not_shown(tmp_path):
    clock = FakeClock()
    model = ClockedModel(clock, hang_on=[1, 2])  # call 1 = run A, call 2 = run B chunk 1
    app = clocked_app(tmp_path, model, clock, import_stall=1.0)
    pdf = two_pages()
    with TestClient(app) as c:
        assert upload(c, pdf).status_code == 202
        assert model.entered[1].wait(30)
        run_a = current_run(tmp_path)
        clock.advance(2.0)
        wait_status(c, "failed")
        assert len(app.state.ctx.progress) == 0  # A's entry is dropped on expiry
        assert upload(c, pdf).json()["requeued"] is True  # run B
        assert model.entered[2].wait(30)
        status = c.get("/parent/materials/1", headers=API).json()
        assert status["import_status"] == "running"
        assert status["progress"] == {"phase": "chunks", "chunk": 1, "chunks": 2, "accepted": 0}
        app.state.ctx.progress.update(1, run_a, 2, 2, 7)  # run A writes late
        status = c.get("/parent/materials/1", headers=API).json()
        assert status["progress"]["chunk"] == 1 and status["progress"]["accepted"] == 0
        assert "принято заданий 7" not in c.get("/parent/materials", headers=HTML).text
        model.release_all()
        app.state.imports.wait_idle(30)


def test_the_polling_script_chains_timeouts_and_reloads_once():
    # No JS engine in the test environment: the guard is checked on the source.
    src = (Path(__file__).parent.parent / "app/static/progress.js").read_text()
    assert "setInterval" not in src
    assert "setTimeout(tick" in src and src.count("setTimeout(") == 1
    assert (
        "if (!reloading) {" in src
        and src.count("location.replace(location.pathname)") == 1
        and "reload" not in src.replace("reloading", "")
    )
    assert src.index("reloading = true") < src.index("location.replace(")


# ---------------------------------------------------------------- stall deadline (heartbeat)
# The watchdog runs on a fake clock the test moves: no verdict depends on the scheduler.


class ClockedModel:
    """Call number n moves the fake clock by steps.get(n, step) (the model "takes" that long);
    calls in `hang_on` also wait until the test opens their gate."""

    def __init__(self, clock, step=0.0, hang_on=(), steps=None):
        self.clock = clock
        self.step = step
        self.steps = steps or {}
        self.hang_on = set(hang_on)
        self.calls = 0
        self.wait_for = None  # the watchdog has read its start time before time moves
        self.entered = {n: threading.Event() for n in range(1, 12)}
        self.gate = {n: threading.Event() for n in range(1, 12)}

    def release_all(self):
        for gate in self.gate.values():
            gate.set()

    def complete(self, system, messages, schema=None, session_id=None):
        self.calls += 1
        n = self.calls
        if self.wait_for is not None:
            assert self.wait_for.wait(30)
        self.clock.advance(self.steps.get(n, self.step))
        if n in self.hang_on:
            self.entered[n].set()
            assert self.gate[n].wait(30)
        page = int(re.search(r"\[\[стр\. (\d+)\]\]", messages[-1]["content"]).group(1))
        return ModelResult(None, reply(make_task(n, pages=[page])), "claude", "s", 1, None)


def two_pages():
    return text_pdf([[f"{n}. " + LONG] for n in range(1, 3)])


def six_pages():
    return text_pdf([[f"{n}. " + LONG] for n in range(1, 7)])


def clocked_app(tmp_path, model, clock, **kw):
    app = build_app(
        tmp_path, model, sync_imports=False, import_clock=clock.now, import_poll=0.01, **kw
    )
    model.wait_for = app.state.imports.watch_started
    return app


def wait_status(client, want, seconds=30):
    for _ in range(int(seconds * 100)):
        st = client.get("/parent/materials/1", headers=API).json()
        if st["import_status"] == want:
            return st
        time.sleep(0.01)
    raise AssertionError(f"never {want}: {st}")


def current_run(tmp_path):
    conn = db(tmp_path)
    try:
        return conn.execute("SELECT run_id FROM material WHERE id = 1").fetchone()[0]
    finally:
        conn.close()


def test_a_long_import_that_keeps_reporting_is_not_cut_off(tmp_path):
    clock = FakeClock()
    model = ClockedModel(clock, step=0.6)  # 6 calls = 3.6 s of fake time, stall is 1.0 s
    with TestClient(clocked_app(tmp_path, model, clock, import_stall=1.0)) as c:
        upload(c, six_pages())
        done = wait_status(c, "done")
        assert done["tasks"] == 6 and model.calls == 6


def test_real_clock_smoke_import_with_wide_margins(tmp_path):
    model = StubModel(split=lambda content: reply(make_task(1)))
    with TestClient(build_app(tmp_path, model, sync_imports=False, import_stall=30)) as c:
        upload(c, two_pages())
        assert wait_status(c, "done")["tasks"] >= 1


def test_a_silent_import_expires_after_the_stall_limit_and_is_logged(tmp_path, caplog):
    clock = FakeClock()
    model = ClockedModel(clock, hang_on=[1])
    caplog.set_level("WARNING", logger="app.ingest")
    with TestClient(clocked_app(tmp_path, model, clock, import_stall=1.0)) as c:
        upload(c, two_pages())
        assert model.entered[1].wait(30)
        clock.advance(0.9)  # within the limit: the watchdog polls many times and keeps quiet
        time.sleep(0.2)
        assert c.get("/parent/materials/1", headers=API).json()["import_status"] == "running"
        clock.advance(0.2)
        failed = wait_status(c, "failed")
        assert "превышено время ожидания" in failed["import_error"]
        lines = [r.getMessage() for r in caplog.records if "import expired" in r.getMessage()]
        assert len(lines) == 1
        assert "material=1" in lines[0] and "phase=chunks" in lines[0] and "chunk=1/2" in lines[0]
        assert "silent_s=1.1" in lines[0]
        model.release_all()


def test_the_absolute_cap_ends_an_import_that_never_stops_reporting(tmp_path):
    clock = FakeClock()
    model = ClockedModel(clock, step=0.25, hang_on=[2])  # reports all the time; stall is 5 s
    app = clocked_app(
        tmp_path, model, clock, import_stall=5, import_max_base=0.2, import_max_per_chunk=0.1
    )
    with TestClient(app) as c:
        upload(c, two_pages())  # cap = 0.2 + 2 * 0.1 = 0.4 s; the second call ends at 0.5 s
        assert model.entered[2].wait(30)
        assert "превышено время ожидания" in wait_status(c, "failed")["import_error"]
        model.release_all()


def test_a_late_report_of_an_expired_run_does_not_resurrect_it(tmp_path):
    clock = FakeClock()
    model = ClockedModel(clock, hang_on=[1])
    app = clocked_app(tmp_path, model, clock, import_stall=1.0)
    with TestClient(app) as c:
        upload(c, two_pages())
        assert model.entered[1].wait(30)
        run_a = current_run(tmp_path)
        clock.advance(2.0)
        wait_status(c, "failed")
        progress = app.state.ctx.progress
        progress.update(1, run_a, 2, 2, 1)  # run A reports late
        assert len(progress) == 0 and progress.snapshot(1, run_a) is None
        model.release_all()


def test_cancel_stops_further_model_calls(tmp_path):
    clock = FakeClock()
    model = ClockedModel(clock, hang_on=[2])
    app = clocked_app(tmp_path, model, clock, import_stall=1.0)
    with TestClient(app) as c:
        upload(c, six_pages())
        assert model.entered[2].wait(30)
        clock.advance(2.0)
        wait_status(c, "failed")
        model.release_all()  # the hung call returns; the expired run must not ask again
        app.state.imports.wait_idle(30)
        assert model.calls == 2
        assert c.get("/parent/materials/1", headers=API).json()["tasks"] == 0


def test_a_late_begin_and_clear_of_an_old_run_do_not_touch_the_newer_run(tmp_path):
    clock = FakeClock()
    # call 1 = run A (hangs, expires); call 2 = run B chunk 1 (hangs); call 3 = run B chunk 2
    model = ClockedModel(clock, hang_on=[1, 2], steps={3: 0.6})
    app = clocked_app(tmp_path, model, clock, import_stall=1.0)
    with TestClient(app) as c:
        pdf = two_pages()
        upload(c, pdf)
        assert model.entered[1].wait(30)
        run_a = current_run(tmp_path)
        clock.advance(2.0)
        wait_status(c, "failed")
        assert upload(c, pdf).json()["requeued"] is True  # run B
        assert model.entered[2].wait(30)
        run_b = current_run(tmp_path)
        assert run_b != run_a
        progress = app.state.ctx.progress
        progress.begin(1, run_a)  # run A resumes after its ownership check ...
        progress.clear(1, run_a)  # ... and cleans up
        assert progress.snapshot(1, run_b) is not None
        clock.advance(0.6)
        model.release_all()
        done = wait_status(c, "done")  # B keeps beating: 0.6 s + 0.6 s apart, never 1.0 s silent
        assert done["tasks"] == 2


def test_reading_a_big_pdf_keeps_the_heartbeat(tmp_path, monkeypatch):
    from app.ingest import importer

    clock = FakeClock()
    real = importer.extract_pages
    seen = []

    def slow_extract(data, on_page=None):
        for page in range(1, 4):
            clock.advance(0.9)  # a slow machine: each page takes almost the whole stall limit
            time.sleep(0.1)  # the watchdog polls here
            on_page(page, 3)
            seen.append(page)
        return real(data)

    monkeypatch.setattr(importer, "extract_pages", slow_extract)
    model = ClockedModel(clock)
    with TestClient(clocked_app(tmp_path, model, clock, import_stall=1.0)) as c:
        upload(c, two_pages())
        done = wait_status(c, "done")  # 2.7 s of reading in all, yet never expired
        assert seen == [1, 2, 3] and done["tasks"] == 2


def test_reading_is_reported_page_by_page():
    from app.ingest.extract import extract_pages
    from app.ingest.progress import progress_text

    seen = []
    pages = extract_pages(text_pdf([["1. a"], ["2. b"]]), lambda n, total: seen.append((n, total)))
    assert [n for n, _ in pages] == [1, 2] and seen == [(1, 2), (2, 2)]
    assert progress_text({"phase": "reading", "chunk": 2, "chunks": 9, "accepted": 0}) == (
        "Читаем страницы: 2 из 9"
    )


def test_the_writing_phase_is_shown_with_its_own_text(tmp_path):
    from app.ingest.progress import progress_text

    assert progress_text({"phase": "writing", "chunk": 3, "chunks": 9, "accepted": 0}) == (
        "Сохраняем задания и рисунки: 3 из 9"
    )
    assert progress_text({"phase": "reading", "chunk": 0, "chunks": 0, "accepted": 0}) == (
        "Читаем страницы"
    )


def test_two_near_simultaneous_requeues_leave_one_owner(tmp_path, monkeypatch):
    """Codex round 4: run A writes its run_id and pauses, run B writes and claims, then A claims
    over B - B loses its entry and its heartbeats are ignored. The DB write and the claim are one
    step under a lock, so the owner in the database is always the owner of the progress entry."""
    import threading

    from app.ingest import jobs as jobs_module

    clock = FakeClock()
    app = clocked_app(tmp_path, ClockedModel(clock), clock, import_stall=30.0)
    with TestClient(app) as c:
        upload(c, two_pages())
        jobs = app.state.imports
        jobs.wait_idle(5)
        monkeypatch.setattr(jobs_module, "run_import", lambda *a, **k: None)
        real_new_run = jobs._new_run
        paused, resume = threading.Event(), threading.Event()
        calls = []

        def new_run(material_id):
            run_id = real_new_run(material_id)
            calls.append(run_id)
            if len(calls) == 1:
                paused.set()
                resume.wait(1.0)  # A pauses after its DB write; with the lock B cannot slip in
            return run_id

        monkeypatch.setattr(jobs, "_new_run", new_run)
        a = threading.Thread(target=jobs.submit, args=(1,))
        b = threading.Thread(target=jobs.submit, args=(1,))
        a.start()
        assert paused.wait(5)
        b.start()
        b.join(0.3)
        assert len(calls) == 1  # B waits for A to finish its claim
        resume.set()
        a.join(5)
        b.join(5)
        owner = current_run(tmp_path)
        assert owner == calls[-1]
        assert app.state.ctx.progress.snapshot(1, owner) is not None


def test_an_expiry_blocked_by_a_write_lock_is_retried_until_it_is_recorded(
    tmp_path, monkeypatch, caplog
):
    import sqlite3

    from app.ingest import jobs

    real_open = jobs.open_db

    def short_wait(path):
        conn = real_open(path)
        conn.execute("PRAGMA busy_timeout = 50")  # a lock held longer than this fails the write
        return conn

    monkeypatch.setattr(jobs, "open_db", short_wait)
    caplog.set_level("WARNING", logger="app.ingest")
    clock = FakeClock()
    model = ClockedModel(clock, hang_on=[1])
    app = clocked_app(tmp_path, model, clock, import_stall=1.0)
    with TestClient(app) as c:
        upload(c, two_pages())
        assert model.entered[1].wait(30)
        other = sqlite3.connect(str(tmp_path / "olymp.db"), isolation_level=None)
        other.execute("BEGIN IMMEDIATE")  # another writer holds the database
        try:
            clock.advance(2.0)  # the run expires; its failed-write hits the lock
            for _ in range(500):
                if any("expiry not recorded yet" in r.getMessage() for r in caplog.records):
                    break
                time.sleep(0.01)
            assert any("expiry not recorded yet" in r.getMessage() for r in caplog.records)
            assert any(t.name == "watch-1" and t.is_alive() for t in threading.enumerate())
        finally:
            other.execute("ROLLBACK")
            other.close()
        failed = wait_status(c, "failed")
        assert "превышено время ожидания" in failed["import_error"]
        model.release_all()
