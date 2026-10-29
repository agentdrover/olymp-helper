"""Contracts for the isolated Codex checker."""

from app.model.adapters import ADAPTERS, build_env
from app.model.service import model_family


def test_codex_channel_passes_model_images_and_parses():
    adapter = ADAPTERS["codex"]
    argv = adapter.build("gpt-6.1-sol", "rules", {"type": "object"}, [b"png"])
    assert "--ephemeral" in argv and "--ignore-user-config" in argv
    assert "--output-schema" in argv and "-i" in argv
    assert argv[argv.index("-m") + 1] == "gpt-6.1-sol"
    assert model_family("gpt-6.1-sol") == "openai"
    result = adapter.parse(
        '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"ok\\": true}"}}\n{"type":"turn.completed"}',
        {"type": "object"},
    )
    assert result.json == {"ok": True}


def test_codex_environment_keeps_only_own_state():
    env = build_env("codex", {"CODEX_HOME": "/state", "CURSOR_API_KEY": "secret"}, "/run")
    assert env["CODEX_HOME"] == "/state"
    assert "CURSOR_API_KEY" not in env


def test_judge_and_critic_use_codex_without_fallback(tmp_path):
    from app.db import connect, migrate
    from app.model.service import ModelService
    from app.model.types import ModelResult

    conn = connect(tmp_path / "judge.sqlite")
    migrate(conn)
    svc = ModelService(conn, {})
    assert svc.judge_channel("claude") == "cursor"
    conn.execute("UPDATE settings SET codex_enabled=1, cursor_model='auto'")
    assert svc.judge_channel("claude") == "codex"
    assert svc.judge_channel("cursor") == "codex"
    assert svc.independent_checker("cursor") is None
    conn.execute("UPDATE settings SET cursor_model='grok-4.7'")
    assert svc.judge_channel("cursor") == "codex"
    assert svc.independent_checker("cursor") == "codex"
    result = svc.complete_on("codex", "s", [{"role": "user", "content": "x"}])
    assert isinstance(result, ModelResult) and not result.ok
    assert conn.execute("SELECT channel, fallback FROM model_run").fetchall() == [("codex", 0)]


def test_codex_executor_reports_missing_login(tmp_path):
    from types import SimpleNamespace

    from app.model.executor_server import handle_request

    def fake(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="Not logged in; please log in")

    result = handle_request(
        {
            "channel": "codex",
            "model": "gpt-6.1-sol",
            "system": "s",
            "messages": [{"role": "user", "content": "hi"}],
        },
        env={},
        spawn=fake,
        workroot=str(tmp_path),
    )
    assert result["error_kind"] == "not_logged_in"
    assert "Not logged in" not in str(result)


def test_run_cleans_files_and_scrubs_auth(tmp_path, monkeypatch):
    import json
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace

    from app.model import runner

    state = tmp_path / "auth"
    monkeypatch.setattr(runner, "CODEX_STATE_ROOT", str(state))
    state.mkdir()
    (state / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "synthetic-secret-value"}})
    )
    captured = {}

    def spawn(argv, *, input, env, cwd, timeout):
        if argv[0] == "codex":
            captured["files"] = sorted(p.name for p in Path(cwd).iterdir())
            captured["env"] = env
            (state / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": "rotated-secret-value"}})
            )
            (state / "history.jsonl").write_text("temporary trace")
            (state / "sessions").mkdir()
            (state / "sessions" / "one").write_text("temporary trace")
            events = [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": '{"answer":"synthetic-secret-value"}',
                    },
                },
                {"type": "turn.completed"},
            ]
            return SimpleNamespace(
                returncode=0, stdout="\n".join(map(json.dumps, events)), stderr=""
            )
        return subprocess.run(
            argv,
            input=input,
            env=env,
            cwd=cwd,
            timeout=timeout,
            text=True,
            capture_output=True,
            check=False,
        )

    result = runner.run_cli(
        "codex",
        "gpt-6.1-sol",
        "rules",
        [{"role": "user", "content": "x"}],
        {"type": "object"},
        env={"CODEX_HOME": str(state)},
        workroot=str(tmp_path),
        spawn=spawn,
        images=[b"image"],
    )
    assert result.ok and result.json == {"answer": "[redacted]"}
    assert captured["files"] == ["image-1.png", "schema.json"]
    assert captured["env"]["CODEX_HOME"] == str(state)
    assert [p.name for p in state.iterdir()] == ["auth.json"]
    assert [p.name for p in tmp_path.iterdir()] == ["auth"]


def test_migration_preserves_existing_settings_and_model_history(tmp_path, monkeypatch):
    import shutil

    from app import db

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in db.MIGRATIONS_DIR.glob("*.sql"):
        if not path.name.startswith("0016_"):
            shutil.copy(path, migrations / path.name)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    conn = db.connect(tmp_path / "old.sqlite")
    db.migrate(conn)
    conn.execute("UPDATE settings SET cursor_model='grok-4.7', daily_run_limit=42")
    conn.execute(
        "INSERT INTO model_run(day,started_at,channel,model) VALUES ('2026-01-01','now','cursor','grok-4.7')"
    )
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "app/migrations/0016_codex_channel.sql"
    )
    shutil.copy(source, migrations / source.name)
    db.migrate(conn)
    db.migrate(conn)
    assert conn.execute(
        "SELECT cursor_model,daily_run_limit,codex_model,codex_enabled FROM settings"
    ).fetchone() == ("grok-4.7", 42, "gpt-6.1-sol", 0)
    assert conn.execute("SELECT channel,model FROM model_run").fetchall() == [
        ("cursor", "grok-4.7")
    ]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_hint_routes_to_selected_codex_and_fails_closed(tmp_path):
    from tests.test_tutor_judge import COND, OPTS, generation
    from tests.tutor_helpers import Judged, attempt, boot, post, seed

    class CodexJudge(Judged):
        def judge_channel(self, author):
            return "codex"

    model = CodexJudge(
        judge=lambda _: {"reveals": False, "reason": "нет утечки"},
        reply=generation("Вспомни строение клетки."),
    )
    client, _, _ = boot(tmp_path, model)
    with client:
        tid = seed(tmp_path, key="Б", options=OPTS, text=COND)
        attempt(client, tid, "А")
        post(client, tid, "hint", level=1)
    assert model.judge_calls and all(call["channel"] == "codex" for call in model.judge_calls)


def test_wrong_auth_directory_is_never_cleaned(tmp_path):
    from app.model.runner import run_cli

    state = tmp_path / "user-state"
    state.mkdir()
    (state / "valuable-file").write_text("keep")
    result = run_cli(
        "codex",
        "gpt-6.1-sol",
        "s",
        [{"role": "user", "content": "q"}],
        None,
        env={"CODEX_HOME": str(state)},
        workroot=str(tmp_path),
    )
    assert result.error_kind == "bad_auth_path"
    assert (state / "valuable-file").read_text() == "keep"
