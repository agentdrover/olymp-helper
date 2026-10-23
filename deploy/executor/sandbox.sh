#!/usr/bin/env bash
# Шаг CI executor-sandbox (OH-T03, AC-5 и AC-8). Секретов не использует: токенов провайдеров нет,
# реальных вызовов CLI нет. Собирает образ и проверяет его песочницу изнутри.
set -euo pipefail
cd "$(dirname "$0")/../.."

IMG=olymp-executor:ci
docker build -f deploy/executor/Containerfile -t "$IMG" .

# AC-8: версии в собранном образе совпадают с заявленными в Containerfile.
want_claude="$(sed -n 's/^ARG CLAUDE_CODE_VERSION=//p' deploy/executor/Containerfile)"
want_cursor="$(sed -n 's/^ARG CURSOR_AGENT_VERSION=//p' deploy/executor/Containerfile)"
run_ro=(docker run --rm --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges -e HOME=/tmp)
"${run_ro[@]}" --entrypoint claude "$IMG" --version | grep -F "$want_claude"
"${run_ro[@]}" --entrypoint cursor-agent "$IMG" --version | grep -F "$want_cursor"

# AC-5: корень только для чтения, пустой tmpfs в /work, ни тома данных, ни настроек CLI.
docker run --rm --read-only \
    --tmpfs /work:rw,noexec,nosuid,size=64m,uid=1000,gid=1000,mode=0700 \
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 \
    --entrypoint python3 "$IMG" /opt/executor/sandbox_check.py
