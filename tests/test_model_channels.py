"""Каналы модели (OH-T03, SPEC 1.4): AC-1, AC-2, AC-4, AC-7."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import connect, migrate
from app.model.adapters import TOKEN_VARS, ClaudeAdapter, CursorAdapter, build_env
from app.model.ledger import usage_report
from app.model.runner import run_cli
from app.model.service import ModelService
from app.model.types import ModelResult

FIXTURES = Path(__file__).parent / "fixtures" / "model"
HISTORY = [
    {"role": "user", "content": "Что такое хлоропласт?"},
    {"role": "assistant", "content": "Органоид растительной клетки."},
    {"role": "user", "content": "А что внутри?"},
]
SYSTEM = "Ты помощник по биологии. Не давай готовый ответ."


def sample(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fake_spawn(stdout="", returncode=0, stderr="", exc=None, seen=None):
    def spawn(argv, *, input, env, cwd, timeout):
        if seen is not None:
            seen.append(SimpleNamespace(argv=argv, input=input, env=env, cwd=cwd, timeout=timeout))
        if exc is not None:
            raise exc
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return spawn


ENV = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "tok-claude", "CURSOR_API_KEY": "key-cursor"}


def test_ac1():
    # Команда Claude: без инструментов, без MCP, без пользовательских настроек и плагинов.
    argv = ClaudeAdapter().build("sonnet", SYSTEM, None)
    assert argv[:7] == ["claude", "-p", "--output-format", "json", "--model", "sonnet", "--tools"]
    assert argv[7] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == SYSTEM
    # Команда Cursor: режим без записи, без флагов, разрешающих всё.
    cargv = CursorAdapter().build("auto", SYSTEM, None)
    assert cargv[:6] == ["cursor-agent", "-p", "--output-format", "json", "--model", "auto"]
    assert cargv[cargv.index("--mode") + 1] == "ask"
    assert not {"--force", "--yolo", "--trust", "--approve-mcps"} & set(cargv)
    # История и system передаются явно.
    seen = []
    res = run_cli(
        "claude",
        "sonnet",
        SYSTEM,
        HISTORY,
        None,
        env=ENV,
        spawn=fake_spawn(sample("claude_ok.json"), seen=seen),
    )
    assert seen[0].argv[0] == "claude"
    for m in HISTORY:
        assert m["content"] in seen[0].input
    assert SYSTEM not in seen[0].input  # у Claude system идёт отдельным флагом
    assert res.error is None and res.text == "Хлоропласты содержат хлорофилл."
    assert (res.channel, res.model) == ("claude", "sonnet")
    assert res.usage == {"input_tokens": 120, "output_tokens": 45}
    seen = []
    res = run_cli(
        "cursor",
        "auto",
        SYSTEM,
        HISTORY,
        None,
        env=ENV,
        spawn=fake_spawn(sample("cursor_ok.json"), seen=seen),
    )
    assert SYSTEM in seen[0].input  # у Cursor system вклеен в запрос
    assert all(m["content"] in seen[0].input for m in HISTORY)
    assert res.text == "Митохондрии вырабатывают АТФ." and res.usage is None
    # Схема: ответ разбирается как JSON.
    schema = {"type": "object"}
    res = run_cli(
        "cursor",
        "auto",
        SYSTEM,
        HISTORY,
        schema,
        env=ENV,
        spawn=fake_spawn(json.dumps({"is_error": False, "result": '{"hint": 1}'})),
    )
    assert res.json == {"hint": 1} and res.error is None
    # Ошибки: ненулевой код, пустой и битый JSON, ошибка CLI, таймаут.
    bad = {
        "rc": fake_spawn(sample("claude_ok.json"), returncode=2, stderr="boom"),
        "empty": fake_spawn(""),
        "broken": fake_spawn("{not json"),
        "cli_error": fake_spawn(sample("claude_error.json")),
        "empty_result": fake_spawn(json.dumps({"is_error": False, "result": ""})),
        "bad_schema_json": fake_spawn(json.dumps({"is_error": False, "result": "not json"})),
        "timeout": fake_spawn(exc=subprocess.TimeoutExpired("claude", 120)),
    }
    for name, spawn in bad.items():
        res = run_cli(
            "claude",
            "sonnet",
            SYSTEM,
            HISTORY,
            schema if name == "bad_schema_json" else None,
            env=ENV,
            spawn=spawn,
        )
        assert res.error, name
        assert res.text is None and res.json is None, name
        assert "Traceback" not in res.error and "boom" not in res.error, name
    seen = []
    run_cli(
        "claude",
        "m",
        SYSTEM,
        HISTORY,
        None,
        env=ENV,
        spawn=fake_spawn(sample("claude_ok.json"), seen=seen),
    )
    assert seen[0].timeout == 120
    res = run_cli(
        "cursor",
        "auto",
        SYSTEM,
        HISTORY,
        None,
        env=ENV,
        spawn=fake_spawn(sample("cursor_error.json")),
    )
    assert res.error and res.channel == "cursor"


def test_ac4():
    noisy = dict(
        ENV,
        AWS_SECRET_ACCESS_KEY="x",
        HOME="/root",
        OLYMP_DB="/srv/olymp/olymp.db",
        PARENT_PIN="1234",
        LANG="C.UTF-8",
    )
    home = "/work/home"
    claude_env = build_env("claude", noisy, home)
    assert claude_env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-claude"
    assert "CURSOR_API_KEY" not in claude_env
    cursor_env = build_env("cursor", noisy, home)
    assert cursor_env["CURSOR_API_KEY"] == "key-cursor"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in cursor_env
    for env in (claude_env, cursor_env):
        assert env["HOME"] == home
        assert not {"AWS_SECRET_ACCESS_KEY", "OLYMP_DB", "PARENT_PIN"} & set(env)
        assert set(env) <= {"PATH", "LANG", "LC_ALL", "TZ", "HOME", "TMPDIR", *TOKEN_VARS.values()}
    # Тот же состав доходит до реально запускаемого процесса.
    for channel, other in (("claude", "CURSOR_API_KEY"), ("cursor", "CLAUDE_CODE_OAUTH_TOKEN")):
        seen = []
        run_cli(
            channel,
            "m",
            SYSTEM,
            HISTORY,
            None,
            env=noisy,
            spawn=fake_spawn(sample(f"{channel}_ok.json"), seen=seen),
        )
        assert TOKEN_VARS[channel] in seen[0].env
        assert other not in seen[0].env
        assert "PARENT_PIN" not in seen[0].env


class FakeChannel:
    def __init__(self, name, *, fail=False, duration_ms=100, raises=None):
        self.name, self.fail, self.duration_ms, self.raises = name, fail, duration_ms, raises
        self.calls = []

    def complete(self, system, messages, schema=None, *, model=None):
        self.calls.append((system, messages, schema, model))
        if self.raises:
            raise self.raises
        if self.fail:
            return ModelResult(
                None,
                None,
                self.name,
                model or "",
                self.duration_ms,
                None,
                error="boom",
                error_kind="exec",
            )
        return ModelResult(
            f"ответ {self.name}", None, self.name, model or "", self.duration_ms, None
        )


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "olymp.db")
    migrate(conn)
    conn.execute("UPDATE settings SET claude_model='sonnet', cursor_model='auto-c'")
    conn.execute(
        "INSERT INTO study_session (id, started_at, last_activity_at) "
        "VALUES (1, '2026-10-23T10:00:00+00:00', '2026-10-23T10:00:00+00:00')"
    )
    return conn


def clock():
    return datetime(2026, 10, 23, 10, 0, tzinfo=UTC)


def test_ac2(db, caplog):
    claude, cursor = FakeChannel("claude", fail=True), FakeChannel("cursor")
    svc = ModelService(db, {"claude": claude, "cursor": cursor}, clock)
    res = svc.complete(SYSTEM, HISTORY, session_id=1)
    assert res.error is None and res.channel == "cursor" and res.text == "ответ cursor"
    assert len(claude.calls) == 1 and len(cursor.calls) == 1  # ровно одна запасная попытка
    assert claude.calls[0][3] == "sonnet" and cursor.calls[0][3] == "auto-c"  # своя модель
    events = db.execute("SELECT type, data FROM event").fetchall()
    assert [e[0] for e in events] == ["fallback"]
    data = json.loads(events[0][1])
    assert data["from"] == "claude" and data["to"] == "cursor"
    # Основной канал берётся из настроек.
    db.execute("UPDATE settings SET primary_channel='cursor'")
    claude, cursor = FakeChannel("claude"), FakeChannel("cursor")
    svc = ModelService(db, {"claude": claude, "cursor": cursor}, clock)
    assert svc.complete(SYSTEM, HISTORY, session_id=1).channel == "cursor"
    assert not claude.calls
    # Оба канала упали (в том числе исключением): понятная ошибка без трассы.
    db.execute("UPDATE settings SET primary_channel='claude'")
    claude = FakeChannel("claude", raises=RuntimeError("Traceback secret detail"))
    cursor = FakeChannel("cursor", fail=True)
    svc = ModelService(db, {"claude": claude, "cursor": cursor}, clock)
    res = svc.complete(SYSTEM, HISTORY, session_id=1)
    assert res.error and res.text is None
    for bad in ("Traceback", "secret detail", "boom", "RuntimeError"):
        assert bad not in res.error
    assert len(claude.calls) == 1 and len(cursor.calls) == 1
    assert "secret detail" not in caplog.text  # текст исключения канала не пишется в журнал
    # Событие fallback пишется и в учёт: запасной запуск помечен.
    flags = [r[0] for r in db.execute("SELECT fallback FROM model_run ORDER BY id")]
    assert flags[:2] == [0, 1]


def test_ac7(db):
    claude, cursor = FakeChannel("claude", duration_ms=1500), FakeChannel("cursor", duration_ms=700)
    svc = ModelService(db, {"claude": claude, "cursor": cursor}, clock)
    svc.complete(SYSTEM, HISTORY)
    db.execute("UPDATE settings SET primary_channel='cursor'")
    svc.complete(SYSTEM, HISTORY)
    claude.fail = True
    db.execute("UPDATE settings SET primary_channel='claude'")
    svc.complete(SYSTEM, HISTORY)  # claude падает -> cursor
    rows = usage_report(db)
    assert len(rows) == 4
    for row in rows:
        assert {"channel", "model", "duration_ms"} <= set(row)
        assert row["duration_ms"] is not None
    assert [(r["channel"], r["model"], r["duration_ms"]) for r in rows[:2]] == [
        ("claude", "sonnet", 1500),
        ("cursor", "auto-c", 700),
    ]
    assert [r["fallback"] for r in rows] == [False, False, False, True]


def test_proxy_vars_reach_the_cli():
    """На k8 CLI выходят к своим API только через egress: прокси доходит до CLI, ключи - нет."""
    proxy = {
        "HTTPS_PROXY": "http://egress:3128",
        "HTTP_PROXY": "http://egress:3128",
        "NO_PROXY": "localhost,127.0.0.1",
        "NODE_USE_ENV_PROXY": "1",
    }
    source = dict(ENV, **proxy, EXECUTOR_SHARED_KEY="shared", UNRELATED="x")
    for channel in ("claude", "cursor"):
        env = build_env(channel, source, "/work/home")
        assert {k: env.get(k) for k in proxy} == proxy
        assert "EXECUTOR_SHARED_KEY" not in env and "UNRELATED" not in env
    # Без прокси в окружении состав прежний.
    plain = build_env("claude", dict(ENV), "/work/home")
    assert not set(proxy) & set(plain)
