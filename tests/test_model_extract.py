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


def test_bare_object_after_intro_and_the_only_valid_block():
    assert parse_cli_json(out(INTRO + 'Ответ: {"a": 5} готово'), SCHEMA).json == {"a": 5}
    text = '```json\n{"a": 1}\n```\n```json\nне json\n```'
    assert parse_cli_json(out(text), SCHEMA).json == {"a": 1}


def test_garbage_raises_exact_kind():
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out("просто текст без json"), SCHEMA)


def test_two_top_level_objects_are_ambiguous_not_a_choice():
    text = 'Пример: {"a": 1} ... Итог: {"a": 2}'
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out(text), SCHEMA)


def test_two_fenced_blocks_or_fence_plus_loose_object_are_ambiguous():
    for text in (
        '```json\n{"a": 1}\n```\nпоправка\n```json\n{"a": 2}\n```',
        'Пример {"a": 1}\n```json\n{"a": 2}\n```',
        '```json\n{"a": 1}\n```\nитог {"a": 2}',
    ):
        with pytest.raises(ChannelError, match="bad_schema_json"):
            parse_cli_json(out(text), SCHEMA)


def test_nested_object_is_not_a_second_object():
    text = 'Вот: {"a": 1, "meta": {"b": 2}} всё'
    assert parse_cli_json(out(text), SCHEMA).json == {"a": 1, "meta": {"b": 2}}


def test_preamble_with_braces_that_are_not_json_plus_one_object():
    assert parse_cli_json(out('Формула {x} и итог {"a": 7}'), SCHEMA).json == {"a": 7}


def test_one_fenced_block_with_text_around_is_taken():
    text = 'Сначала думаю.\n```json\n{"a": 8}\n```\nГотово.'
    assert parse_cli_json(out(text), SCHEMA).json == {"a": 8}


JUDGE = {"type": "object", "x-strict-json": True}
EX = 'Пример: {"reveals": false, "reason": "пример"}\n'
LEAK = '{"reveals": true, "reason": "раскрывает"}'


@pytest.mark.parametrize("tag", ["JSON", "json5", "jsonc", " json", "javascript", "text", "js", ""])
def test_a_fence_with_any_tag_is_counted_not_hidden(tag):
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out(EX + f"```{tag}\n{LEAK}\n```"), SCHEMA)


def test_a_fence_with_a_trailing_comma_is_an_error_not_a_skip():
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out(EX + '```json\n{"reveals": true, "reason": "r",}\n```'), SCHEMA)


def test_a_broken_outer_object_is_not_searched_for_a_nested_one():
    text = 'Итог: {"reveals": true, "reason": "r", "d": {"reveals": false, "reason": "ok"},}'
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out(text), SCHEMA)
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out('Итог: { reveals: true, "d": {"a": 1}}'), SCHEMA)


def test_honest_preambles_still_parse():
    assert parse_cli_json(out('Дробь $\\frac{1}{2}$, итог {"a": 1}'), SCHEMA).json == {"a": 1}
    text = 'Ответ: {"e": "множество {1,2} и \\"{\\"", "a": 1}'
    assert parse_cli_json(out(text), SCHEMA).json == {"e": 'множество {1,2} и "{"', "a": 1}
    assert parse_cli_json(out("Вывод ```x``` и " + '{"a": 2}'), SCHEMA).json == {"a": 2}


@pytest.mark.parametrize(
    "text",
    [
        EX + LEAK,
        LEAK + "\nпоправка",
        EX + "```JSON\n" + LEAK + "\n```",
        "Пример [1] и " + LEAK,
        "Формат } затем " + LEAK,
        "Вот вердикт:\n```json\n" + LEAK + "\n```\nА ещё " + LEAK,
        "Вот вердикт:\n" + LEAK + "\nи ещё слова",
        "Вот вердикт:\n```json\n" + LEAK + "\n```\nготово",
    ],
)
def test_strict_mode_rejects_anything_but_one_object(text):
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out(text), JUDGE)


@pytest.mark.parametrize(
    "text",
    [LEAK, "  " + LEAK + "\n", "```json\n" + LEAK + "\n```", "```JSON\n" + LEAK + "\n```",
     "```jsonc\n" + LEAK + "\n```", "```" + LEAK + "```", "```text\n" + LEAK + "\n```\n"],
)  # fmt: skip
def test_strict_mode_takes_a_bare_object_or_one_whole_fence_with_any_tag(text):
    assert parse_cli_json(out(text), JUDGE).json == {"reveals": True, "reason": "раскрывает"}
    assert parse_cli_json(out(text), SCHEMA, strict=True).json["reveals"] is True


def test_the_tutor_judge_schema_is_strict():
    from app.tutor.prompts import JUDGE_SCHEMA

    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out(EX + LEAK), JUDGE_SCHEMA)


@pytest.mark.parametrize("tag", ["JSON", "jsonc", "text", "js"])
def test_lenient_mode_takes_one_fenced_object_with_any_tag(tag):
    text = INTRO + f'```{tag}\n{{"a": 9}}\n```'
    assert parse_cli_json(out(text), SCHEMA).json == {"a": 9}


VERDICT = {"reveals": False, "reason": "нет признаков"}


def test_strict_mode_takes_a_brace_free_preamble_before_one_object_or_fence():
    body = json.dumps(VERDICT, ensure_ascii=False)
    for text in (
        "Вот вердикт:\n" + body,
        "Вот вердикт:\n```json\n" + body + "\n```\n",
        "Сначала посмотрю на текст.\n\n```JSON\n" + body + "\n```",
    ):
        assert parse_cli_json(out(text), JUDGE).json == VERDICT


OBJ_T = '{"reveals": true, "reason": "r"}'
OBJ_F = '{"reveals": false, "reason": "ok"}'


@pytest.mark.parametrize(
    "text",
    [
        f"```\n{OBJ_T}\n{OBJ_F}\n```",
        f"```json {OBJ_T}\n{OBJ_F}\n```",
        f"Вот:\n```json\n{OBJ_T}\n{OBJ_F}\n```",
        f"```json\nещё слова\n{OBJ_F}\n```",
    ],
)
def test_strict_mode_does_not_drop_a_first_line_that_is_not_a_tag(text):
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out(text), JUDGE)
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out(text), SCHEMA)  # мягкий режим: то же


def test_the_judge_schema_is_strict_where_lenient_would_accept():
    from app.tutor.prompts import JUDGE_SCHEMA

    text = 'Ответ: {"reveals": false, "reason": "ok"} готово'
    assert parse_cli_json(out(text), SCHEMA).json["reveals"] is False  # мягкий берёт
    with pytest.raises(ChannelError, match="bad_schema_json"):
        parse_cli_json(out(text), JUDGE_SCHEMA)


def test_a_closed_broken_object_next_to_a_valid_one_is_an_error():
    for text in ('Итог {"a": 1,} и {"a": 2}', '{"a": 2} и {"a": 1,}'):
        with pytest.raises(ChannelError, match="bad_schema_json"):
            parse_cli_json(out(text), SCHEMA)
