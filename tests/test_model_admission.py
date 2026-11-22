from app.db import connect, migrate
from app.model.admission import Admission


def open_admission(path):
    conn = connect(path)
    migrate(conn)
    return Admission(conn)


def test_closed_gate_keeps_work_durable_without_claim(tmp_path):
    path = tmp_path / "olymp.db"
    admission = open_admission(path)
    admission.pause()
    operation = admission.enqueue("tutor", "42")
    assert admission.claim(operation) is None
    assert admission.status()["queued"] == 1
    admission.conn.close()

    admission = open_admission(path)
    assert admission.enqueue("tutor", "42") == operation
    assert admission.claim(operation) is None
    admission.resume()
    admission.resume()
    assert admission.claim(operation) == ("tutor", "42")
    assert admission.claim(operation) is None


def test_drain_does_not_hide_unknown_or_dispatch_new_work(tmp_path):
    path = tmp_path / "olymp.db"
    app = open_admission(path)
    other = Admission(connect(path))
    active = app.enqueue("import", "42")
    queued = app.enqueue("tutor", "43")
    assert app.claim(active) == ("import", "42")
    assert app.claim(queued) is None
    other.pause()
    assert other.status()["mode"] == "draining"
    assert other.claim(queued) is None
    app.conn.execute(
        "INSERT INTO model_run (day, started_at, channel, model, fallback) "
        "VALUES ('2026-01-01', '2026-01-01', 'codex', 'test', 0)"
    )
    app.conn.execute(
        "INSERT INTO model_dispatch (operation_id, ordinal, attempt, channel, request_sha256, run_id) "
        "VALUES (?, 1, 'fake', 'codex', 'digest', 1)",
        (active,),
    )
    app.recover()
    assert other.status()["unknown"] == 1
    assert other.status()["mode"] == "draining"
    assert other.claim(active) is None
    assert not other.resume(expected=other.status()["revision"])
    assert other.claim(queued) is None
    assert other.status()["queued"] == 1
    assert app.status()["unknown"] == 1


def test_poll_keeps_unavailable_executor_work_and_resumes_exact_operation(tmp_path, monkeypatch):
    from app.model import work

    path = tmp_path / "olymp.db"
    admission = open_admission(path)
    operation_id = admission.enqueue("pack", "31")
    admission.conn.close()
    proofs = {
        "codex": {"state": "ready", "active": 0},
        "cursor": {"state": "unavailable", "active": None},
    }
    monkeypatch.setattr(work, "worker_status", proofs.__getitem__)
    before_restart = work.ModelWork(path)
    received = []
    before_restart.handlers["pack"] = received.append
    assert before_restart.tick() is False
    assert received == []
    with connect(path) as conn:
        assert Admission(conn).queued() == [(operation_id, "pack", "31")]
        assert conn.execute("SELECT COUNT(*) FROM model_run").fetchone()[0] == 0

    after_restart = work.ModelWork(path)
    after_restart.handlers["pack"] = received.append
    proofs["cursor"] = {"state": "ready", "active": 0}
    assert after_restart.tick() is True
    assert received == ["31"]
    assert after_restart.tick() is False


def test_drain_waits_for_both_workers(tmp_path, monkeypatch):
    from app.model import control

    admission = open_admission(tmp_path / "olymp.db")
    admission.pause()
    admission.enqueue("pack", "22")
    proofs = {
        "codex": {"state": "ready", "active": 0},
        "cursor": {"state": "ready", "active": 1},
    }
    monkeypatch.setattr(control, "worker_status", proofs.__getitem__)
    report = control.status(admission)
    assert report["mode"] == "closed"
    assert report["queued"] == 1
    assert not report["drained"]

    proofs["cursor"]["active"] = 0
    report = control.status(admission)
    assert report["drained"]
    assert admission.resume(expected=report["revision"])
    assert admission.resume(expected=report["revision"])
    assert not admission.resume(expected=report["revision"] - 1)


def test_pending_operations_survive_restart_and_resume_once(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.model import work
    from app.model.types import ModelResult
    from tests.bank_helpers import build_app
    from tests.chat_helpers import chat_reply, say
    from tests.tutor_helpers import seed, view

    monkeypatch.setattr(work.ModelWork, "_loop", lambda self: None)
    monkeypatch.setattr(
        work, "worker_status", lambda name: {"state": "unavailable", "active": None}
    )
    sent = []

    def fake_complete(self, system, messages, schema, **kwargs):
        sent.append(self.channel)
        payload = chat_reply() if len(sent) == 1 else {"reveals": False, "reason": "safe"}
        return ModelResult(None, payload, self.channel, "test", 1, None)

    monkeypatch.setattr("app.model.client.ExecutorChannel.complete", fake_complete)
    with TestClient(build_app(tmp_path, sync_steps=False)) as client:
        task_id = seed(tmp_path)
        first = say(client, task_id, "Объясни, что изучает цитология")
        assert first.status_code == 200
        panel = view(client, task_id)["panel"]
        assert panel["state"] == "waiting_model"
        step_id = panel["step"]
        assert not client.app.state.model_work.tick()
        assert sent == []

    with TestClient(build_app(tmp_path, sync_steps=False)) as client:
        panel = view(client, task_id)["panel"]
        assert panel["state"] == "waiting_model"
        assert panel["step"] == step_id

        monkeypatch.setattr(work, "worker_status", lambda name: {"state": "ready", "active": 0})
        assert client.app.state.model_work.tick()
        assert not client.app.state.model_work.tick()
        assert sent == ["codex", "cursor"]  # generation and independent safety judge
        with connect(tmp_path / "olymp.db") as conn:
            assert conn.execute("SELECT COUNT(*) FROM event WHERE type = 'chat'").fetchone()[0] == 1
        assert view(client, task_id)["panel"]["state"] == "idle"


def test_unknown_dispatch_cannot_replay_or_fallback(tmp_path, monkeypatch):
    import threading
    from contextlib import ExitStack, closing
    from functools import partial

    from fastapi.testclient import TestClient

    from app.model import client, executor_server, work
    from app.model.types import ModelResult
    from tests.bank_helpers import build_app
    from tests.chat_helpers import chat_reply, say
    from tests.tutor_helpers import seed, view

    dispatched = []

    def fake_run(payload, **kwargs):
        channel, attempt = payload["channel"], payload["attempt"]
        dispatched.append((channel, attempt))
        result = chat_reply() if channel == "codex" else {"reveals": False, "reason": "safe"}
        return ModelResult(None, result, channel, payload["model"], 1, None).to_dict()

    monkeypatch.setattr(executor_server, "handle_request", fake_run)
    monkeypatch.setattr(work.ModelWork, "_loop", lambda self: None)
    monkeypatch.setenv("EXECUTOR_SHARED_KEY", "synthetic-only-key")
    original_post = client.http_post
    original_outcome = client.ExecutorChannel.outcome
    hide_result = True

    def post(url, payload, timeout, headers):
        result = original_post(url, payload, timeout, headers)
        if payload["channel"] == "codex":
            raise ConnectionError("synthetic loss after worker completed")
        return result

    def outcome(self, attempt, model):
        return None if hide_result else original_outcome(self, attempt, model)

    monkeypatch.setattr(client, "http_post", post)
    monkeypatch.setattr(client.ExecutorChannel, "outcome", outcome)
    with ExitStack() as stack:
        for channel in ("codex", "cursor"):
            server = stack.enter_context(
                executor_server.make_server(
                    "127.0.0.1", 0, shared_key="synthetic-only-key", only_channel=channel
                )
            )
            thread = threading.Thread(target=partial(server.serve_forever, poll_interval=0.01))
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)
            monkeypatch.setenv(
                f"EXECUTOR_{channel.upper()}_URL", f"http://127.0.0.1:{server.server_address[1]}"
            )

        with TestClient(build_app(tmp_path, sync_steps=False)) as web:
            task = seed(tmp_path)
            assert say(web, task, "Объясни, что изучает цитология").status_code == 200
            assert web.app.state.model_work.tick()
            assert not web.app.state.model_work.tick()
            assert say(web, task, "Объясни, что изучает цитология").status_code == 200
            assert len(dispatched) == 1
            assert dispatched[0][0] == "codex"

        with TestClient(build_app(tmp_path, sync_steps=False)) as web:
            runner = web.app.state.model_work
            conn = stack.enter_context(closing(connect(tmp_path / "olymp.db")))
            gate = Admission(conn)
            gate.pause()
            rev = gate.status()["revision"]
            assert gate.status()["unknown"] == 1
            assert not gate.resume(expected=rev)
            assert not runner.tick()
            assert len(dispatched) == 1
            hide_result = False
            assert runner.tick()
            assert gate.status()["unknown"] == 0
            assert gate.status()["queued"] == 1
            assert len(dispatched) == 1
            assert not runner.tick()
            assert gate.resume(expected=rev)
            assert runner.tick()
            assert not runner.tick()
            assert [name for name, _ in dispatched] == ["codex", "cursor"]
            assert len({attempt for _, attempt in dispatched}) == 2
            assert conn.execute("SELECT COUNT(*) FROM model_run").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM event WHERE type = 'chat'").fetchone()[0] == 1
            assert view(web, task)["panel"]["state"] == "idle"


def test_paused_model_work_waits_without_dispatch(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.model import work
    from tests.bank_helpers import CHILD, PARENT, build_app, text_pdf, upload
    from tests.chat_helpers import say
    from tests.test_generated_packs import TOPIC
    from tests.tutor_helpers import seed, view

    monkeypatch.setattr(work.ModelWork, "_loop", lambda self: None)
    sent = []
    monkeypatch.setattr("app.model.client.http_post", lambda *a, **kw: sent.append(a))
    with (
        TestClient(build_app(tmp_path, sync_steps=False)) as web,
        connect(tmp_path / "olymp.db") as conn,
    ):
        gate = Admission(conn)
        gate.pause()
        task = seed(tmp_path)
        assert say(web, task, "Как понять условие?").status_code == 200
        assert view(web, task)["panel"]["state"] == "waiting_model"

        answer = upload(web, text_pdf([["1. Учебное задание"]]))
        assert answer.status_code == 202
        import_id = answer.json()["operation_id"]

        pack_request = {
            "headers": PARENT,
            "data": {"topic": TOPIC, "kind": "single", "count": "1"},
            "follow_redirects": False,
        }
        pack = web.post("/parent/generate", **pack_request)
        assert pack.status_code == 303
        assert (
            web.post("/parent/generate", **pack_request).headers["Location"]
            == pack.headers["Location"]
        )
        assert "в очереди" in web.get(pack.headers["Location"], headers=PARENT).text
        assert web.get("/healthz").json()["schema_version"] == 22
        assert web.get("/parent/login").status_code == 200
        assert web.get("/api/tasks", headers=CHILD).status_code == 200
        assert not web.app.state.model_work.tick()
        operations = gate.queued()
        assert [kind for _, kind, _ in operations] == ["tutor", "import", "pack"]
        assert operations[1][0] == import_id
        assert not sent
        assert conn.execute("SELECT COUNT(*) FROM model_run").fetchone()[0] == 0


def test_admission_contract_and_schema_boundary(tmp_path, monkeypatch):
    from app import db
    from app.model import control

    current = db._migration_files()
    monkeypatch.setattr(db, "_migration_files", lambda: current[:-1])
    path = tmp_path / "schema21.db"
    with connect(path) as conn:
        assert migrate(conn) == 21
        conn.execute("UPDATE settings SET daily_run_limit = 321 WHERE id = 1")

    monkeypatch.setenv("OLYMP_DB", str(path))
    monkeypatch.setattr("sys.argv", ["olymp-control", "status"])
    monkeypatch.setattr(control, "worker_status", lambda name: {"state": "unknown", "active": None})
    assert control.main() == 1
    with connect(path) as conn:
        assert db.schema_version(conn) == 21
    monkeypatch.setattr(db, "_migration_files", lambda: current)
    with connect(path) as conn:
        assert migrate(conn) == 22
        assert migrate(conn) == 22
        assert (
            conn.execute("SELECT daily_run_limit FROM settings WHERE id = 1").fetchone()[0] == 321
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert control.status(Admission(conn))["version"] == 1


def test_deterministic_attempts_continue_and_stale_model_work_is_discarded(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.model import work
    from app.model.types import ModelResult
    from tests.bank_helpers import build_app
    from tests.chat_helpers import say
    from tests.tutor_helpers import attempt, seed, view

    monkeypatch.setattr(work.ModelWork, "_loop", lambda self: None)
    monkeypatch.setattr(work, "worker_status", lambda _: {"state": "ready", "active": 0})
    called = []

    def fake_complete(self, system, messages, schema, **kwargs):
        called.append(self.channel)
        return ModelResult.failure(self.channel, kwargs["model"], "request_too_large")

    monkeypatch.setattr("app.model.client.ExecutorChannel.complete", fake_complete)
    with TestClient(build_app(tmp_path, sync_steps=False)) as web:
        admission = Admission(connect(tmp_path / "olymp.db"))
        admission.pause()
        task_id = seed(tmp_path)
        assert say(web, task_id, "Что изучает цитология?").status_code == 200
        assert attempt(web, task_id, "А").headers["X-Attempt"] == "accepted"
        assert attempt(web, task_id, "В").headers["X-Attempt"] == "accepted"
        assert view(web, task_id)["attempts"] == 2
        assert admission.status()["queued"] == 3
        assert called == []
        assert admission.resume(admission.status()["revision"])
        runner = web.app.state.model_work
        assert runner.tick()  # chat context was superseded by the attempts
        assert runner.tick()  # first attempt was superseded by the second
        assert called == []
        assert runner.tick()
        assert not runner.tick()
        assert called == ["codex"]
        admission.conn.close()


def test_pre_dispatch_decline_leaves_only_the_original_queued_operation(tmp_path, monkeypatch):
    from app.model import work
    from app.model.service import ModelService
    from app.model.types import ModelResult

    path = tmp_path / "olymp.db"
    admission = open_admission(path)
    op_id = admission.enqueue("answer", "12")
    monkeypatch.setattr(work, "worker_status", lambda _: {"state": "ready", "active": 0})

    class Worker:
        unavailable = True

        def complete(self, system, messages, schema, **kwargs):
            if self.unavailable:
                return ModelResult.failure("codex", kwargs["model"], "route_unavailable")
            return ModelResult(None, {"answer": "42"}, "codex", kwargs["model"], 1, None)

    worker = Worker()
    service = ModelService(admission.conn, {"codex": worker}, managed=True)
    runner = work.ModelWork(path)
    received = []
    runner.handlers["answer"] = lambda _: received.append(service.complete("test", []).json)
    assert runner.tick()
    assert admission.queued() == [(op_id, "answer", "12")]
    assert admission.status()["unknown"] == 0
    assert admission.conn.execute("SELECT COUNT(*) FROM model_run").fetchone()[0] == 0
    assert received == []
    worker.unavailable = False
    assert runner.tick()
    assert not runner.tick()
    assert received == [{"answer": "42"}]
    assert admission.conn.execute("SELECT COUNT(*) FROM model_run").fetchone()[0] == 1


def test_local_error_before_dispatch_does_not_freeze_following_operations(tmp_path, monkeypatch):
    from app.model import work

    path = tmp_path / "olymp.db"
    admission = open_admission(path)
    op_id = admission.enqueue("wrong", "17")
    admission.enqueue("right", "18")
    monkeypatch.setattr(work, "worker_status", lambda _: {"state": "ready", "active": 0})

    def bad(_):
        raise ValueError("bad local object")

    runner = work.ModelWork(path)
    received = []
    runner.handlers.update(wrong=bad, right=received.append)
    assert runner.tick()
    assert admission.status()["unknown"] == 0
    assert (
        admission.conn.execute(
            "SELECT status FROM model_operation WHERE id = ?", (op_id,)
        ).fetchone()[0]
        == "failed"
    )
    assert runner.tick()
    assert received == ["18"]


def test_late_proof_finishes_ledger_after_domain_operation_is_superseded(tmp_path, monkeypatch):
    from app.model import work
    from app.model.service import ModelService
    from app.model.types import ModelResult

    path = tmp_path / "olymp.db"
    admission = open_admission(path)
    op_id = admission.enqueue("answer", "12")
    monkeypatch.setattr(work, "worker_status", lambda _: {"state": "ready", "active": 0})

    class Worker:
        outcome_ready = False
        submitted = None

        def complete(self, system, messages, schema, **kwargs):
            self.submitted = kwargs["attempt"]
            return ModelResult.failure("codex", kwargs["model"], "executor_unreachable")

        def outcome(self, attempt, model):
            assert attempt == self.submitted
            if self.outcome_ready:
                return ModelResult(None, {"answer": "42"}, "codex", model, 1, None)
            return None

    worker = Worker()
    monkeypatch.setattr(work, "configured_channel", lambda _: worker)
    service = ModelService(admission.conn, {"codex": worker}, managed=True)
    runner = work.ModelWork(path)
    runner.handlers["answer"] = lambda _: service.complete("test", [])
    assert runner.tick()
    assert admission.status()["unknown"] == 1
    assert runner.submit_again("answer", "12", conn=admission.conn) == op_id
    # The protected domain object no longer needs an answer when proof arrives.
    runner.handlers["answer"] = lambda _: None
    worker.outcome_ready = True
    assert runner.tick()
    assert admission.status()["unknown"] == 0
    assert admission.conn.execute("SELECT status FROM model_run").fetchall() == [("ok",)]
    assert runner.submit_again("answer", "12", conn=admission.conn) != op_id
