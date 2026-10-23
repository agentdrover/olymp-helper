"""Ревью #2013, раунд 2: убийство CLI при разных UID, один запуск за раз, ключ, неисправность."""

import errno
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.model import runner
from app.model.adapters import ClaudeAdapter
from app.model.executor_server import Gate, handle_request, make_server
from app.model.runner import run_cli

ENV = {"PATH": "/usr/bin:/bin", "CLAUDE_CODE_OAUTH_TOKEN": "tok", "CURSOR_API_KEY": "key"}
MSGS = [{"role": "user", "content": "q"}]
REQ = {"channel": "claude", "model": "m", "system": "s", "messages": MSGS, "schema": None}
OK = '{"is_error":false,"result":"ok"}'


def ok_spawn(argv, *, input, env, cwd, timeout):
    return SimpleNamespace(returncode=0, stderr="", stdout=OK)


def stuck_cli(tmp_path):
    """Заглушка claude: пишет свой pid и зависает (как зависший CLI)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pidfile = tmp_path / "pid"
    script = bindir / "claude"
    script.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nsleep 60\n")
    script.chmod(0o755)
    return str(bindir), pidfile


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_kills_cli_through_launcher_when_server_cannot_signal(tmp_path, monkeypatch):
    """P1: сервер (другой UID) не может сделать killpg; убивает тот же launcher, процесс исчез."""

    def eperm(*a, **k):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", eperm)  # сервер на другом UID: нет CAP_KILL
    bindir, pidfile = stuck_cli(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    env = dict(ENV, PATH=f"{bindir}:/usr/bin:/bin")
    pid = None
    try:
        res = run_cli(
            "claude",
            "m",
            "s",
            MSGS,
            None,
            env=env,
            workroot=str(work),
            timeout=1,
            launcher=["env", "--"],
            kill_wait=3,
        )
        pid = int(pidfile.read_text())
        assert res.error_kind == "timeout"
        assert not alive(pid)
    finally:
        if pid and alive(pid):
            os.kill(pid, 9)


def test_failed_kill_marks_the_executor_faulty(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "kill_group", lambda *a, **k: None)
    bindir, pidfile = stuck_cli(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    env = dict(ENV, PATH=f"{bindir}:/usr/bin:/bin")
    faults = []
    pid = None
    try:
        res = run_cli(
            "claude",
            "m",
            "s",
            MSGS,
            None,
            env=env,
            workroot=str(work),
            timeout=1,
            kill_wait=0.5,
            on_fault=faults.append,
        )
        pid = int(pidfile.read_text())
        assert res.error_kind == "timeout" and faults == ["kill"]
    finally:
        if pid and alive(pid):
            os.killpg(pid, 9)


def test_only_one_run_at_a_time_second_request_gets_busy(tmp_path):
    """P1: один активный запуск на контейнер; параллельный получает «занят» и CLI не запускает."""
    entered, release, calls = threading.Event(), threading.Event(), []

    def slow(argv, *, input, env, cwd, timeout):
        calls.append(argv)
        entered.set()
        release.wait(5)
        return SimpleNamespace(returncode=0, stderr="", stdout=OK)

    gate = Gate()
    first = []
    t = threading.Thread(
        target=lambda: first.append(
            handle_request(REQ, env=ENV, spawn=slow, workroot=str(tmp_path), gate=gate)
        )
    )
    t.start()
    assert entered.wait(5)
    second = handle_request(REQ, env=ENV, spawn=slow, workroot=str(tmp_path), gate=gate)
    release.set()
    t.join(5)
    assert second["error_kind"] == "busy" and second["error"]
    assert first[0]["text"] == "ok" and len(calls) == 1
    # Замок отпущен: следующий запрос проходит.
    assert handle_request(REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=gate)["text"]


def test_leftover_processes_are_swept_before_and_after_a_run(tmp_path):
    events = []

    def sweeper():
        events.append("sweep")
        return True

    def spawn(argv, *, input, env, cwd, timeout):
        events.append("run")
        return SimpleNamespace(returncode=0, stderr="", stdout=OK)

    gate = Gate()
    handle_request(REQ, env=ENV, spawn=spawn, workroot=str(tmp_path), gate=gate, sweeper=sweeper)
    assert events == ["sweep", "run", "sweep"]
    # Если прежний процесс убрать не удалось - CLI не запускается, исполнитель неисправен.
    events.clear()
    gate = Gate()
    out = handle_request(
        REQ, env=ENV, spawn=spawn, workroot=str(tmp_path), gate=gate, sweeper=lambda: False
    )
    assert out["error_kind"] == "faulty" and "run" not in events and gate.fault_reason


def test_cleanup_failure_makes_the_executor_faulty(tmp_path, monkeypatch):
    """P2: уборка не удалась (rmtree ничего не удалил) - неисправен, дальше отказ."""
    monkeypatch.setattr(runner.shutil, "rmtree", lambda *a, **k: None)
    gate = Gate()
    out = handle_request(REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=gate)
    assert out["text"] == "ok" and gate.fault_reason == "cleanup"
    monkeypatch.undo()
    again = handle_request(REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=gate)
    assert again["error_kind"] == "faulty" and again["error"]


def test_cleanup_runs_as_cli_user_with_chmod_before_rm(tmp_path):
    calls = []

    def spawn(argv, *, input, env, cwd, timeout):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="", stdout=OK)

    launcher = ["setpriv", "--reuid=exec-cli", "--"]
    run_cli(
        "claude",
        "m",
        "s",
        MSGS,
        None,
        env=ENV,
        spawn=spawn,
        workroot=str(tmp_path),
        launcher=launcher,
    )
    assert [c[len(launcher)] for c in calls] == ["mkdir", "sh", "sh"]
    cli = calls[1]
    assert cli[-len(ClaudeAdapter().build("m", "s", None)) :][0] == "claude"
    assert cli[len(launcher) + 3].endswith("/work")  # в каталог переходит пользователь CLI
    assert all(c[: len(launcher)] == launcher for c in calls)
    mkdir = calls[0]
    assert "-m" in mkdir and mkdir[mkdir.index("-m") + 1] == "0700"
    script = calls[2][len(launcher) + 2]
    assert script.index("chmod -R u+rwX") < script.index("rm -rf")


def test_enospc_on_workdir_is_a_run_error_not_an_exception(tmp_path, monkeypatch):
    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(runner.tempfile, "mkdtemp", full)
    out = handle_request(REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=Gate())
    assert out["error_kind"] == "workdir" and out["error"] and out["text"] is None
    res = run_cli("claude", "m", "s", MSGS, None, env=ENV, spawn=ok_spawn)
    assert res.error_kind == "workdir"


def serve(**kw):
    kw.setdefault("shared_key", "k" * 16)
    srv = make_server("127.0.0.1", 0, env=ENV, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def post(url, payload, key=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-Executor-Key"] = key
    req = urllib.request.Request(url + "/run", data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_run_requires_the_shared_key(tmp_path):
    """P1: без ключа 401 и CLI не запускается; с верным - обычный ответ; healthz открыт."""
    calls = []

    def spawn(argv, *, input, env, cwd, timeout):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="", stdout=OK)

    srv, url = serve(workroot=str(tmp_path), spawn=spawn)
    try:
        assert post(url, REQ)[0] == 401
        assert post(url, REQ, key="wrong-key-wrong-k")[0] == 401
        assert post(url, REQ, key="")[0] == 401
        assert not calls
        status, body = post(url, REQ, key="k" * 16)
        assert status == 200 and body["text"] == "ok" and len(calls) == 1
        assert json.load(urllib.request.urlopen(url + "/healthz", timeout=5))["status"] == "ok"
    finally:
        srv.shutdown()
        srv.server_close()


def test_server_refuses_to_start_without_a_key(tmp_path):
    for bad in (None, ""):
        with pytest.raises(ValueError):
            make_server("127.0.0.1", 0, env=ENV, workroot=str(tmp_path), shared_key=bad)


def test_faulty_executor_answers_503_and_refuses_runs(tmp_path):
    gate = Gate()
    gate.fault("cleanup")
    srv, url = serve(workroot=str(tmp_path), spawn=ok_spawn, gate=gate)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(url + "/healthz", timeout=5)
        assert err.value.code == 503
        status, body = post(url, REQ, key="k" * 16)
        assert status == 503 and body["error_kind"] == "faulty"
    finally:
        srv.shutdown()
        srv.server_close()


def test_enospc_over_http_keeps_the_server_alive(tmp_path, monkeypatch):
    srv, url = serve(workroot=str(tmp_path), spawn=ok_spawn)
    try:

        def full(*a, **k):
            raise OSError(errno.ENOSPC, "full")

        monkeypatch.setattr(runner.tempfile, "mkdtemp", full)
        status, body = post(url, REQ, key="k" * 16)
        assert status == 200 and body["error_kind"] == "workdir"
        monkeypatch.undo()
        assert post(url, REQ, key="k" * 16)[1]["text"] == "ok"
    finally:
        srv.shutdown()
        srv.server_close()


def test_client_sends_the_shared_key():
    from app.model.client import ExecutorChannel

    seen = {}

    def fake_post(url, payload, timeout, headers=None):
        seen["headers"] = headers
        return {
            "text": "hi",
            "json": None,
            "model": "m",
            "duration_ms": 1,
            "usage": None,
            "error": None,
            "error_kind": None,
            "channel": "claude",
        }

    res = ExecutorChannel("claude", "http://x", key="secret-shared-key", post=fake_post).complete(
        "s", MSGS, model="m"
    )
    assert res.text == "hi" and seen["headers"] == {"X-Executor-Key": "secret-shared-key"}


def test_compose_and_sandbox_carry_the_shared_key_and_restart_policy():
    root = Path(__file__).parent.parent / "deploy" / "executor"
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    assert compose.count("EXECUTOR_SHARED_KEY: ${EXECUTOR_SHARED_KEY:?") == 2
    assert compose.count("init: true") == 2 and compose.count("healthcheck:") == 2
    assert compose.count("restart: unless-stopped") == 2
    assert "EXECUTOR_SHARED_KEY" in (root / "sandbox.sh").read_text(encoding="utf-8")
    assert "--init" in (root / "sandbox.sh").read_text(encoding="utf-8")
    # Ключ не должен попасть в окружение CLI: белый список окружения его не содержит.
    from app.model.adapters import build_env

    assert "EXECUTOR_SHARED_KEY" not in build_env(
        "claude", dict(ENV, EXECUTOR_SHARED_KEY="x"), "/h"
    )


def test_spec_records_one_run_key_and_faulty_state():
    spec = (Path(__file__).parent.parent / "docs" / "spec" / "SPEC-1.0.md").read_text("utf-8")
    for phrase in ("Один запуск за раз", "X-Executor-Key", "неисправен", "«занят»"):
        assert phrase in spec, phrase


def test_unexpected_error_in_the_handler_is_a_run_error_and_releases_the_lock(tmp_path):
    def broken():
        raise RuntimeError("boom")

    gate = Gate()
    out = handle_request(
        REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=gate, sweeper=broken
    )
    assert out["error_kind"] == "internal" and "boom" not in json.dumps(out)
    assert gate.try_enter()  # замок отпущен
    gate.leave()
