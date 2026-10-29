"""Секреты не попадают в логи и ответы (OH-T03, SPEC 1.4): AC-6."""

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from app.db import connect, migrate
from app.model.client import ExecutorChannel
from app.model.executor_server import handle_request
from app.model.service import ModelService

FAKE_CLAUDE = "sk-ant-oat01-FAKE-TOKEN-0123456789abcdef"
FAKE_CURSOR = "key_fake_cursor_9876543210zyxwvu"
ENV = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": FAKE_CLAUDE, "CURSOR_API_KEY": FAKE_CURSOR}


def leaky_spawn(argv, *, input, env, cwd, timeout):
    token = env.get("CLAUDE_CODE_OAUTH_TOKEN") or env.get("CURSOR_API_KEY")
    return SimpleNamespace(
        returncode=1,
        stdout=json.dumps({"is_error": True, "result": f"auth failed for {token}"}),
        stderr=f"Error: invalid credentials {token} (Bearer {token})\n  at Object.<anonymous>",
    )


def test_ac6(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    conn = connect(tmp_path / "olymp.db")
    migrate(conn)

    seen_payloads = []

    def post(url, payload, timeout, headers=None):
        # Путь «приложение -> исполнитель»: ответ исполнителя целиком проходит как JSON.
        body = handle_request(payload, env=ENV, spawn=leaky_spawn)
        seen_payloads.append(json.dumps(body, ensure_ascii=False))
        return json.loads(seen_payloads[-1])

    channels = {
        n: ExecutorChannel(n, "http://executor:8099", post=post) for n in ("codex", "cursor")
    }
    svc = ModelService(conn, channels, lambda: datetime(2026, 10, 23, 10, 0, tzinfo=UTC))
    res = svc.complete("sys", [{"role": "user", "content": "q"}])
    assert res.error  # оба канала упали
    wire = " ".join(seen_payloads)
    shown = repr(res) + (res.error or "")
    stored = " ".join(str(r) for r in conn.execute("SELECT * FROM model_run"))
    stored += " ".join(str(r) for r in conn.execute("SELECT * FROM event"))
    assert len(seen_payloads) == 2
    for secret in (FAKE_CLAUDE, FAKE_CURSOR):
        assert secret not in caplog.text
        assert secret not in wire
        assert secret not in shown
        assert secret not in stored
    assert "Traceback" not in shown and "Object.<anonymous>" not in shown
    # Приложение само ни в какой журнал не пишет сырой stderr CLI.
    assert "invalid credentials" not in caplog.text


def _ok_spawn(result_text):
    def spawn(argv, *, input, env, cwd, timeout):
        return SimpleNamespace(
            returncode=0, stderr="", stdout=json.dumps({"is_error": False, "result": result_text})
        )

    return spawn


def test_ac6_successful_response_is_redacted(tmp_path):
    """Ревью #2013 P1: токен в УСПЕШНОМ ответе (текст и вложенный JSON) не покидает исполнитель."""
    leak = f"ответ {FAKE_CURSOR} и Bearer {FAKE_CLAUDE}"
    body = handle_request(
        {
            "channel": "cursor",
            "model": "m",
            "system": "s",
            "messages": [{"role": "user", "content": "q"}],
        },
        env=ENV,
        spawn=_ok_spawn(leak),
        workroot=str(tmp_path),
    )
    wire = json.dumps(body, ensure_ascii=False)
    assert body["text"] and body["error"] is None
    assert FAKE_CURSOR not in wire and FAKE_CLAUDE not in wire
    nested = json.dumps({"a": {"b": [f"x {FAKE_CURSOR}", {"c": FAKE_CLAUDE}]}, FAKE_CURSOR: 1})
    body = handle_request(
        {
            "channel": "cursor",
            "model": "m",
            "system": "s",
            "schema": {"type": "object"},
            "messages": [{"role": "user", "content": "q"}],
        },
        env=ENV,
        spawn=_ok_spawn(nested),
        workroot=str(tmp_path),
    )
    wire = json.dumps(body, ensure_ascii=False)
    assert body["json"]["a"]["b"][0].startswith("x ") and body["error"] is None
    assert FAKE_CURSOR not in wire and FAKE_CLAUDE not in wire


def test_ac6_secrets_of_both_channels_are_known_by_value(tmp_path):
    """Токен вычищается по значению, а не только по виду; значения обоих каналов известны."""
    env = {
        "PATH": "/usr/bin",
        "CLAUDE_CODE_OAUTH_TOKEN": "plain-secret-A1B2C3D4",
        "CURSOR_API_KEY": "plain-secret-Z9Y8X7W6",
    }
    body = handle_request(
        {
            "channel": "cursor",
            "model": "m",
            "system": "s",
            "messages": [{"role": "user", "content": "q"}],
        },
        env=env,
        spawn=_ok_spawn("a plain-secret-A1B2C3D4 b plain-secret-Z9Y8X7W6"),
        workroot=str(tmp_path),
    )
    wire = json.dumps(body)
    assert "plain-secret" not in wire and body["text"]
