"""Контейнер-исполнитель: временный каталог на запрос и приём запросов."""

import json
import os
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from app.model.executor_server import handle_request, make_server

ENV = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "tok", "CURSOR_API_KEY": "key"}
REQ = {
    "channel": "claude",
    "model": "m",
    "system": "s",
    "messages": [{"role": "user", "content": "q"}],
    "schema": None,
}


def test_workdir_is_fresh_and_removed_after_answer(tmp_path):
    dirs = []

    def spawn(argv, *, input, env, cwd, timeout):
        assert os.listdir(cwd) == []  # рабочий каталог пуст на входе
        assert env["HOME"].startswith(str(tmp_path))
        Path(cwd, "cli.log").write_text("log")
        os.makedirs(os.path.join(env["HOME"], ".claude"), exist_ok=True)
        dirs.append((cwd, env["HOME"]))
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "is_error": False,
                    "result": "ok",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ),
        )

    out = handle_request(REQ, env=ENV, spawn=spawn, workroot=str(tmp_path))
    assert out["text"] == "ok" and out["error"] is None
    assert dirs and not os.path.exists(dirs[0][0]) and not os.path.exists(dirs[0][1])
    assert os.listdir(tmp_path) == []


def test_bad_request_is_an_error_not_a_crash(tmp_path):
    out = handle_request({"channel": "nope"}, env=ENV, workroot=str(tmp_path))
    assert out["error"]


def test_http_roundtrip(tmp_path):
    srv = make_server(
        "127.0.0.1",
        0,
        env=ENV,
        workroot=str(tmp_path),
        spawn=lambda *a, **k: SimpleNamespace(
            returncode=0, stderr="", stdout='{"is_error":false,"result":"hi"}'
        ),
    )
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        req = urllib.request.Request(
            url + "/run",
            data=json.dumps(REQ).encode(),
            headers={"Content-Type": "application/json"},
        )
        body = json.load(urllib.request.urlopen(req, timeout=5))
        assert body["text"] == "hi"
        assert json.load(urllib.request.urlopen(url + "/healthz", timeout=5))["status"] == "ok"
    finally:
        srv.shutdown()
        srv.server_close()


def _stub_cli(tmp_path, name, body):
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path.parent)


def test_real_process_roundtrip_and_timeout(tmp_path):
    """Настоящий запуск процесса (заглушка вместо CLI): окружение, stdin, таймаут, уборка."""
    from app.model.runner import run_cli

    ok = _stub_cli(
        tmp_path,
        "claude",
        'cat >/dev/null; echo "$CURSOR_API_KEY$AWS_X" > leak.txt; '
        'echo \'{"is_error":false,"result":"pong"}\'\n',
    )
    env = dict(ENV, PATH=ok + ":/usr/bin:/bin", AWS_X="leak")
    work = tmp_path / "w"
    work.mkdir()
    res = run_cli("claude", "m", "s", REQ["messages"], None, env=env, workroot=str(work))
    assert res.text == "pong" and res.error is None
    assert os.listdir(work) == []
    slow = _stub_cli(tmp_path, "cursor-agent", "sleep 30\n")
    env = dict(ENV, PATH=slow + ":/usr/bin:/bin")
    res = run_cli("cursor", "m", "s", REQ["messages"], None, env=env, workroot=str(work), timeout=1)
    assert res.error_kind == "timeout" and res.error
    assert os.listdir(work) == []
