"""Извлечение JSON из ответа со вступлением (#2112): общий разбор обоих адаптеров."""

import json

import pytest

from app.model.adapters import ClaudeAdapter, CursorAdapter, parse_cli_json
from app.model.types import ChannelError

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
INTRO = "Сначала посмотрю изображение, затем отвечу только JSON.\n\n"


def out(result):
    return json.dumps({"type": "result", "result": result})


@pytest.mark.parametrize("adapter", [ClaudeAdapter, CursorAdapter])
def test_intro_plus_fenced_block(adapter):
    stdout = out(INTRO + '```json\n{"a": 1}\n```')
    assert adapter.parse(stdout, SCHEMA).json == {"a": 1}


@pytest.mark.parametrize("adapter", [ClaudeAdapter, CursorAdapter])
def test_garbage_is_bad_schema_json(adapter):
    with pytest.raises(ChannelError, match="bad_schema_json"):
        adapter.parse(out("Не могу ответить {не json} ```json\nнет\n```"), SCHEMA)


def test_whole_json_and_whole_fence_unchanged():
    assert parse_cli_json(out('{"a": 2}'), SCHEMA).json == {"a": 2}
    assert parse_cli_json(out('```json\n{"a": 3}\n```'), SCHEMA).json == {"a": 3}
    assert parse_cli_json(out('```\n{"a": 4}\n```'), SCHEMA).json == {"a": 4}


def test_bare_object_after_intro_and_last_valid_block():
    assert parse_cli_json(out(INTRO + 'Ответ: {"a": 5} готово'), SCHEMA).json == {"a": 5}
    text = '```json\n{"a": 1}\n```\nпоправка\n```json\n{"a": 2}\n```'
    assert parse_cli_json(out(text), SCHEMA).json == {"a": 2}
    text = '```json\n{"a": 1}\n```\n```json\nне json\n```'
    assert parse_cli_json(out(text), SCHEMA).json == {"a": 1}


def test_garbage_raises_exact_kind():
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out("просто текст без json"), SCHEMA)
