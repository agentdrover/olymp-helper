import json
from pathlib import Path

import pytest

from app.model.adapters import (
    CodexAdapter,
    codex_error_kind,
    codex_schema,
    parse_codex_jsonl,
    restore_optional_fields,
)
from app.model.types import ChannelError


def test_codex_uses_isolated_noninteractive_mode():
    argv = CodexAdapter().build("gpt-6.1-sol", "system", {"type": "object"}, [b"png"])
    assert argv[:2] == ["codex", "exec"]
    assert "--ephemeral" in argv and "--ignore-user-config" not in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "features.shell_tool=false" in argv and "web_search=disabled" in argv
    assert argv[argv.index("--output-schema") + 1] == "schema.json"
    assert argv[argv.index("-i") + 1] == "image-1.png"
    assert argv.index("-") < argv.index("-i")


def test_codex_requires_success_and_rejects_tool_use():
    ok = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}},
    ]
    def encode(events):
        return "\n".join(json.dumps(e) for e in events)
    assert parse_codex_jsonl(encode(ok), {"type": "object"}).json == {"ok": True}
    with pytest.raises(ChannelError):
        parse_codex_jsonl(encode(ok[:1]), None)
    with pytest.raises(ChannelError):
        parse_codex_jsonl(encode([{"type": "item.completed", "item": {"type": "command_execution"}}, *ok]), None)


def test_schema_normalization_preserves_optional_fields_and_rejects_unknown_shapes():
    source = {"type": "object", "required": ["name"], "properties": {
        "name": {"type": "string"}, "count": {"type": "integer"},
    }, "x-strict-json": True}
    result = codex_schema(source)
    assert result["additionalProperties"] is False
    assert result["required"] == ["name", "count"]
    assert result["properties"]["count"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert "x-strict-json" not in result
    assert "additionalProperties" not in source
    with pytest.raises(ChannelError):
        codex_schema({"type": "object", "properties": {"arbitrary": {}}})


def test_optional_placeholders_are_removed_recursively():
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "required": ["id"], "properties": {
            "id": {"type": "integer"}, "hint": {"type": "string"},
            "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }}}}
    value = {"items": [{"id": 1, "hint": None, "nullable": None}]}
    assert restore_optional_fields(value, schema) == {"items": [{"id": 1, "nullable": None}]}


def test_codex_error_classification_ignores_model_authored_content():
    assert codex_error_kind('', '429 too many requests') == 'provider_rate_limit'
    assert codex_error_kind('', 'not logged in') == 'codex_auth_unavailable'
    event = json.dumps({'type': 'turn.failed', 'error': {'message': 'usage limit exceeded'}})
    assert codex_error_kind(event, '') == 'provider_limit'
    model_text = json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '429'}})
    assert codex_error_kind(model_text, '') == 'exit_code'


def test_all_product_schemas_can_be_prepared():
    from app.chat.prompts import CHAT_SCHEMA
    from app.ingest.packs import SCHEMA as PACK_SCHEMA
    from app.ingest.prompt import ANSWER_SCHEMA, TASKS_SCHEMA
    from app.tutor.prompts import JUDGE_SCHEMA, OUTPUT_SCHEMA

    for schema in (CHAT_SCHEMA, PACK_SCHEMA, ANSWER_SCHEMA, TASKS_SCHEMA, JUDGE_SCHEMA, OUTPUT_SCHEMA):
        assert codex_schema(schema)['additionalProperties'] is False


def test_runner_uses_fresh_minimal_proxy_home_and_cleans_it(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    from app.model import runner

    seed = tmp_path / 'seed'
    seed.mkdir()
    (seed / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'proxy-managed'}))
    bearer = 'fake-sbx-proxy-managed'
    monkeypatch.setattr(runner, 'SBX_CODEX_BEARER_SHA256', hashlib.sha256(bearer.encode()).hexdigest())
    (seed / 'config.toml').write_text(
        '[model_providers.sandboxd]\nbase_url="https://chatgpt.com/backend-api/codex"\n'
        f'experimental_bearer_token="{bearer}"\nrequires_openai_auth=false\n'
        '[mcp_servers.forbidden]\nurl="https://example.invalid"\n'
    )
    seen = {}
    def spawn(argv, *, input, env, cwd, timeout):
        home = Path(env['HOME'])
        seen['home'] = home
        seen['config'] = (home / '.codex' / 'config.toml').read_text()
        seen['schema'] = json.loads(Path(cwd, 'schema.json').read_text())
        seen['image'] = Path(cwd, 'image-1.png').read_bytes()
        seen['env'] = env
        events = [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}},
            {'type': 'turn.completed'},
        ]
        return SimpleNamespace(returncode=0, stdout='\n'.join(map(json.dumps, events)), stderr='')

    result = runner.run_cli(
        'codex', 'gpt-6.1-sol', 'system', [{'role': 'user', 'content': 'hello'}],
        {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']},
        env={'PATH': '/usr/bin', 'CODEX_SEED_DIR': str(seed), 'CURSOR_API_KEY': 'real-key'},
        spawn=spawn, workroot=str(tmp_path), images=[b'png'],
    )
    assert result.ok and result.json == {'ok': True}
    assert 'mcp_servers' not in seen['config'] and seen['image'] == b'png'
    assert seen['schema']['additionalProperties'] is False
    assert 'CURSOR_API_KEY' not in seen['env'] and 'CODEX_SEED_DIR' not in seen['env']
    assert not seen['home'].exists()
    (seed / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'real-secret-proxy-managed'}))
    result = runner.run_cli('codex', 'gpt-6.1-sol', 's', [{'role': 'user', 'content': 'x'}], None,
                            env={'CODEX_SEED_DIR': str(seed)}, spawn=spawn, workroot=str(tmp_path))
    assert result.error_kind == 'codex_auth_unavailable'
