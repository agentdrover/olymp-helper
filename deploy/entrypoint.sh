#!/bin/sh
# Запуск приложения. Голос требует secure context (SPEC 1.7), поэтому HTTPS обязателен:
# нужны оба OLYMP_TLS_CERT и OLYMP_TLS_KEY, иначе контейнер завершается с ошибкой.
# HTTP разрешается только явным OLYMP_INSECURE_HTTP=1 (CI и локальная проверка) и пишет предупреждение.
set -eu

HOST="${OLYMP_HOST:-0.0.0.0}"
PORT="${OLYMP_PORT:-8443}"

if [ -n "${OLYMP_TLS_CERT:-}" ] || [ -n "${OLYMP_TLS_KEY:-}" ]; then
    if [ ! -r "${OLYMP_TLS_CERT:-}" ] || [ ! -r "${OLYMP_TLS_KEY:-}" ]; then
        echo "ОШИБКА: OLYMP_TLS_CERT и OLYMP_TLS_KEY должны указывать на читаемые файлы" >&2
        exit 1
    fi
    exec python -m uvicorn app.main:create_app --factory --host "$HOST" --port "$PORT" \
        --ssl-certfile "$OLYMP_TLS_CERT" --ssl-keyfile "$OLYMP_TLS_KEY"
fi

if [ "${OLYMP_INSECURE_HTTP:-}" = "1" ]; then
    echo "WARNING: OLYMP_INSECURE_HTTP=1 - приложение слушает HTTP без TLS; голос в браузере не заработает, на адресе LAN так не запускают" >&2
    exec python -m uvicorn app.main:create_app --factory --host "$HOST" --port "$PORT"
fi

echo "ОШИБКА: не заданы OLYMP_TLS_CERT и OLYMP_TLS_KEY. HTTPS обязателен; для проверки без TLS задайте OLYMP_INSECURE_HTTP=1" >&2
exit 1
