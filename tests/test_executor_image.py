"""Образ контейнера-исполнителя (OH-T03, SPEC 1.4): AC-8 и свойства сборки."""

import re
from pathlib import Path

CONTAINERFILE = Path(__file__).parent.parent / "deploy" / "executor" / "Containerfile"
FLOATING = {"latest", "next", "stable", "beta", "canary", "nightly", "main", "master", ""}


def text():
    return CONTAINERFILE.read_text(encoding="utf-8")


def declared(arg):
    match = re.search(rf"^ARG {arg}=(\S*)\s*$", text(), re.MULTILINE)
    assert match, f"ARG {arg} is not declared"
    return match.group(1)


def test_cli_versions_are_pinned_in_the_executor_image():
    src = text()
    claude = declared("CLAUDE_CODE_VERSION")
    cursor = declared("CURSOR_AGENT_VERSION")
    assert re.fullmatch(r"\d+\.\d+\.\d+", claude), claude
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2}-[0-9a-f]{7,}", cursor), cursor
    for version in (claude, cursor):
        assert version.lower() not in FLOATING
    # Установка идёт ровно по объявленной версии, плавающих тегов нет.
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in src
    assert "downloads.cursor.com/lab/${CURSOR_AGENT_VERSION}/" in src
    assert not re.search(r"@(latest|next|stable)\b", src)
    assert not re.search(r"claude-code(?!@)\b(?=\s*$)", src, re.MULTILINE)
    # Версия собранного образа сверяется с заявленной на сборке.
    assert re.search(r"claude --version.*\$\{?CLAUDE_CODE_VERSION", src, re.DOTALL)
    assert re.search(r"cursor-agent --version.*\$\{?CURSOR_AGENT_VERSION", src, re.DOTALL)
    # Базовый образ не плавающий.
    for ref in re.findall(r"^FROM (\S+)", src, re.MULTILINE):
        assert ":" in ref and not ref.endswith(":latest"), ref


def test_image_runs_unprivileged_without_data():
    src = text()
    assert re.search(r"^USER (?!root)\S+", src, re.MULTILINE)
    assert "VOLUME" not in src
    assert "ANTHROPIC_API_KEY" not in src and "CLAUDE_CODE_OAUTH_TOKEN=" not in src
