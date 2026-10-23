#!/usr/bin/env bash
# Шаг CI image-smoke (OH-T04 AC-1..AC-3): образ приложения собран и работает.
#   AC-1: /healthz за 60 с по HTTPS; запись на томе переживает пересоздание контейнера.
#   AC-2: в образе есть ffmpeg и whisper.cpp; короткий WAV из tests/fixtures/ распознаётся без сети.
#   AC-3: в файлах и истории образа нет шаблонов токенов.
# Секреты не нужны. Любой сбой завершает скрипт ненулевым кодом; контейнеры, том и временные файлы
# убираются всегда (trap EXIT). Использование: deploy/image-smoke.sh [образ]
set -euo pipefail

IMAGE="${1:-olymp-app:ci}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="$$"
CONTAINER="olymp-smoke-${RUN_ID}"
VOLUME="olymp-smoke-vol-${RUN_ID}"
WORK="$(mktemp -d)"
WEIGHTS_SHA256="1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"

cleanup() {
    status=$?
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
    rm -rf "$WORK"
    exit "$status"
}
trap cleanup EXIT

step() { echo "== $*"; }
fail() { echo "ОШИБКА: $*" >&2; exit 1; }

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    step "образ $IMAGE не найден, собираю"
    docker build -f "$ROOT/Containerfile" -t "$IMAGE" "$ROOT"
fi

step "сертификаты для проверки HTTPS"
bash "$ROOT/deploy/make-certs.sh" "$WORK/certs"
chmod 644 "$WORK/certs/server.key"  # временный ключ CI; в контейнере читает пользователь olymp

docker volume create "$VOLUME" >/dev/null

start_container() {
    docker run -d --name "$CONTAINER" -p 127.0.0.1::8443 -v "$VOLUME:/srv/olymp" \
        -v "$WORK/certs:/certs:ro" \
        -e OLYMP_TLS_CERT=/certs/server.pem -e OLYMP_TLS_KEY=/certs/server.key \
        "$IMAGE" >/dev/null
}

wait_healthz() {
    local port deadline body
    port="$(docker port "$CONTAINER" 8443/tcp | head -n1 | sed 's/.*://')"
    deadline=$((SECONDS + 60))
    while [ "$SECONDS" -lt "$deadline" ]; do
        # Проверка цепочки: сертификат подписан нашим CA и содержит SAN на адрес K8.
        if body="$(curl --silent --fail --connect-timeout 2 --max-time 5 --cacert "$WORK/certs/ca.pem" \
                --connect-to "192.168.0.138:8443:127.0.0.1:${port}" \
                https://192.168.0.138:8443/healthz 2>/dev/null)"; then
            echo "healthz: $body"
            return 0
        fi
        sleep 1
    done
    docker logs "$CONTAINER" >&2 || true
    fail "/healthz не ответил за 60 с"
}

step "AC-1: запуск и /healthz"
start_container
wait_healthz

step "AC-1: контрольная запись на томе"
docker exec "$CONTAINER" python -c "
import sqlite3
c = sqlite3.connect('/srv/olymp/olymp.db')
c.execute('CREATE TABLE IF NOT EXISTS smoke_marker (v TEXT)')
c.execute('INSERT INTO smoke_marker VALUES (?)', ('${RUN_ID}',))
c.commit()
"

step "AC-1: пересоздание контейнера (rm -f, затем новый запуск на том же томе)"
docker rm -f "$CONTAINER" >/dev/null
start_container
wait_healthz
got="$(docker exec "$CONTAINER" python -c "
import sqlite3
print(sqlite3.connect('/srv/olymp/olymp.db').execute('SELECT v FROM smoke_marker').fetchone()[0])
")"
[ "$got" = "${RUN_ID}" ] || fail "контрольная запись не пережила пересоздание (получено: '$got')"
echo "запись сохранилась"

step "TLS обязателен: без сертификата контейнер не стартует"
if docker run --rm --network none "$IMAGE" >"$WORK/notls.log" 2>&1; then
    fail "контейнер без OLYMP_TLS_CERT/KEY запустился"
fi
grep -q OLYMP_TLS_CERT "$WORK/notls.log" || fail "нет сообщения о TLS"

step "в рантайме нет установщиков и загрузчиков"
docker run --rm --network none --entrypoint sh "$IMAGE" -c '
    for t in uv pip pip3 curl wget; do
        if command -v "$t" >/dev/null 2>&1; then echo "в образе есть $t"; exit 1; fi
    done
'

step "AC-2: ffmpeg и whisper.cpp в образе, веса с ожидаемой контрольной суммой"
docker run --rm --network none --entrypoint sh "$IMAGE" -c '
    set -e
    ffmpeg -version | head -n1
    whisper-cli --help >/dev/null 2>&1 || test -x /usr/local/bin/whisper-cli
    echo "'"${WEIGHTS_SHA256}"'  /opt/whisper/ggml-small.bin" | sha256sum -c -
'

step "AC-2: распознавание WAV-образца без сети"
text="$(docker run --rm --network none -v "$ROOT/tests/fixtures:/fixtures:ro" --entrypoint sh \
    "$IMAGE" -c '
    set -e
    ffmpeg -nostdin -loglevel error -i /fixtures/sample-ru.wav -ac 1 -ar 16000 -c:a pcm_s16le /tmp/in.wav
    whisper-cli -m "$WHISPER_MODEL" -f /tmp/in.wav -l ru -nt -np
')"
text="$(echo "$text" | tr -s '[:space:]' ' ' | sed 's/^ *//; s/ *$//')"
[ -n "$text" ] || fail "распознавание вернуло пустой текст"
echo "распознано: $text"

step "AC-3: шаблоны токенов в образе"
docker save "$IMAGE" -o "$WORK/image.tar"
python3 "$ROOT/deploy/scan_image.py" "$WORK/image.tar"

step "image-smoke: всё в порядке"
