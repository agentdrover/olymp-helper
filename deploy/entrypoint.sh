#!/bin/sh
# Запуск приложения. С OLYMP_TLS_CERT и OLYMP_TLS_KEY слушает по HTTPS (SPEC 1.7: голос требует
# secure context); без них - HTTP (только локальная проверка и CI, на адресе LAN так не запускают).
set -eu

HOST="${OLYMP_HOST:-0.0.0.0}"
PORT="${OLYMP_PORT:-8443}"

if [ -n "${OLYMP_TLS_CERT:-}" ] || [ -n "${OLYMP_TLS_KEY:-}" ]; then
    if [ ! -r "${OLYMP_TLS_CERT:-}" ] || [ ! -r "${OLYMP_TLS_KEY:-}" ]; then
        echo "OLYMP_TLS_CERT и OLYMP_TLS_KEY должны указывать на читаемые файлы" >&2
        exit 1
    fi
    exec python -m uvicorn app.main:create_app --factory --host "$HOST" --port "$PORT" \
        --ssl-certfile "$OLYMP_TLS_CERT" --ssl-keyfile "$OLYMP_TLS_KEY"
fi
exec python -m uvicorn app.main:create_app --factory --host "$HOST" --port "$PORT"
