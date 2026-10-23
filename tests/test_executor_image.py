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


def read(rel):
    return (Path(__file__).parent.parent / rel).read_text(encoding="utf-8")


def service_block(name):
    src = read("deploy/executor/compose.yaml")
    match = re.search(rf"^  {name}:\n((?:    .*\n|\n)+)", src, re.MULTILINE)
    assert match, f"service {name} is not described"
    return match.group(1)


def test_server_and_cli_run_as_different_uids():
    src = text()
    uids = dict(re.findall(r"useradd\b[^\n]*--uid (\d+)[^\n]* (exec-srv|exec-cli)\b", src))
    by_name = {name: uid for uid, name in uids.items()}
    assert set(by_name) == {"exec-srv", "exec-cli"} and by_name["exec-srv"] != by_name["exec-cli"]
    assert re.search(r"^USER exec-srv$", src, re.MULTILINE)
    assert "ENV" in src and "EXECUTOR_CLI_USER=exec-cli" in src
    # Перейти на uid CLI может только процесс сервера: setpriv с file capabilities закрыт группой.
    assert "setcap cap_setuid,cap_setgid+ep /usr/bin/setpriv" in src
    assert re.search(r"chmod 0?750 /usr/bin/setpriv", src)
    assert re.search(r"chgrp exec-srv /usr/bin/setpriv", src)
    assert (
        "exec-cli" not in re.search(r"useradd[^\n]*exec-srv[^\n]*", src).group(0).split("--gid")[0]
    )


def test_one_executor_container_per_channel_with_only_its_token():
    for name, own, other in (
        ("executor-claude", "CLAUDE_CODE_OAUTH_TOKEN", "CURSOR_API_KEY"),
        ("executor-cursor", "CURSOR_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    ):
        block = service_block(name)
        assert own in block and other not in block, name
        assert f"EXECUTOR_CHANNEL: {name.split('-')[1]}" in block
        assert "read_only: true" in block
        assert re.search(r"/work:[^\n]*noexec[^\n]*nosuid[^\n]*nodev[^\n]*size=", block)
        assert "volumes:" not in block and "privileged" not in block
        assert "cap_drop:\n      - ALL" in block
        assert re.findall(r"cap_add:\n((?:      - \w+\n)+)", block)[0].split() == [
            "-",
            "SETUID",
            "-",
            "SETGID",
        ]
    sh = read("deploy/executor/sandbox.sh")
    assert 'for pair in "claude:CLAUDE_CODE_OAUTH_TOKEN" "cursor:CURSOR_API_KEY"' in sh
    assert len(re.findall(r'-e "\$\{?\w*(?:token|TOKEN)\w*\}?=', sh)) == 1  # одна строка, по паре
    assert "CURSOR_API_KEY" not in sh.replace('"cursor:CURSOR_API_KEY"', "")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in sh.replace('"claude:CLAUDE_CODE_OAUTH_TOKEN"', "")


def test_scratch_area_is_a_limited_tmpfs():
    sh = read("deploy/executor/sandbox.sh")
    assert re.search(r"--tmpfs /work:[^ ]*noexec[^ ]*nosuid[^ ]*nodev[^ ]*size=", sh)
    assert "--read-only" in sh
    spec = read("docs/spec/SPEC-1.0.md")
    assert "уточнение по ревью #2013" in spec and "остаточный риск" in spec
    assert "уточнение по ревью #2013" in read("docs/adr/0001-architecture.md")
