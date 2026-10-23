#!/usr/bin/env bash
# Шаг CI executor-sandbox (OH-T03, AC-5 и AC-8). Секретов не использует: вместо токенов - заведомо
# пустышки, реальных вызовов CLI нет. Собирает образ и проверяет песочницу каждого контейнера
# (по одному на канал) изнутри - и от имени сервера, и от имени CLI.
set -euo pipefail
cd "$(dirname "$0")/../.."

IMG=olymp-executor:ci
docker build -f deploy/executor/Containerfile -t "$IMG" .

# AC-8: версии в собранном образе совпадают с заявленными в Containerfile.
want_claude="$(sed -n 's/^ARG CLAUDE_CODE_VERSION=//p' deploy/executor/Containerfile)"
want_cursor="$(sed -n 's/^ARG CURSOR_AGENT_VERSION=//p' deploy/executor/Containerfile)"
run_ro=(docker run --rm --read-only --tmpfs /tmp --cap-drop ALL -e HOME=/tmp)
"${run_ro[@]}" --entrypoint claude "$IMG" --version | grep -F "$want_claude"
"${run_ro[@]}" --entrypoint cursor-agent "$IMG" --version | grep -F "$want_cursor"

# AC-5: на каждый канал свой контейнер и ТОЛЬКО его токен; корень только для чтения, /work -
# ограниченный tmpfs, ни тома данных, ни настроек CLI.
cleanup() { docker rm -f olymp-exec-claude olymp-exec-cursor >/dev/null 2>&1 || true; }
trap cleanup EXIT
for pair in "claude:CLAUDE_CODE_OAUTH_TOKEN" "cursor:CURSOR_API_KEY"; do
    channel="${pair%%:*}"
    token_var="${pair##*:}"
    name="olymp-exec-${channel}"
    docker run -d --name "$name" --read-only \
        --tmpfs /work:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0711 \
        --cap-drop ALL --cap-add SETUID --cap-add SETGID --pids-limit 256 \
        -e "EXECUTOR_CHANNEL=${channel}" \
        -e "${token_var}=ci-placeholder-not-a-secret" \
        "$IMG" >/dev/null
    for _ in $(seq 1 30); do
        if docker exec "$name" python3 -c \
            "import urllib.request as u; u.urlopen('http://127.0.0.1:8099/healthz', timeout=2)" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    docker exec "$name" python3 /opt/executor/sandbox_check.py srv
    docker exec --user exec-cli "$name" python3 /opt/executor/sandbox_check.py cli
done
