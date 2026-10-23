#!/usr/bin/env bash
# Локальный корневой сертификат и сертификат сервера с SAN на адрес K8 (SPEC 1.7).
# Использование: deploy/make-certs.sh <каталог> [--renew-server]   (OLYMP_HOST_IP - адрес, по умолчанию 192.168.0.138;
# OLYMP_EXTRA_SAN - дополнительные SAN через запятую, например "DNS:olymp.lan").
# --renew-server: оставить корневой сертификат из каталога и выпустить сервер заново (раз в год).
# Корневой ключ ca.key остаётся у владельца и на K8 не копируется; на K8 идут server.pem и server.key.
set -euo pipefail

OUT="${1:?каталог для сертификатов}"
RENEW="${2:-}"
HOST_IP="${OLYMP_HOST_IP:-192.168.0.138}"
SAN="IP:${HOST_IP}"
if [ -n "${OLYMP_EXTRA_SAN:-}" ]; then
    SAN="${SAN},${OLYMP_EXTRA_SAN}"
fi

mkdir -p "$OUT"
if [ "$RENEW" = "--renew-server" ]; then
    [ -s "$OUT/ca.pem" ] && [ -s "$OUT/ca.key" ] || { echo "в $OUT нет корневого сертификата для --renew-server" >&2; exit 1; }
elif [ -n "$RENEW" ]; then
    echo "неизвестный параметр: $RENEW" >&2
    exit 1
elif [ -e "$OUT/ca.pem" ] || [ -e "$OUT/ca.key" ]; then
    echo "корневой сертификат уже есть в $OUT; для нового сервера используйте --renew-server" >&2
    exit 1
fi
umask 077

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/ca.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = Olymp Helper local CA
[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
CNF

cat > "$WORK/server.cnf" <<CNF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = ${HOST_IP}
[v3_server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = ${SAN}
authorityKeyIdentifier = keyid
CNF

if [ "$RENEW" != "--renew-server" ]; then
    openssl ecparam -name prime256v1 -genkey -noout -out "$OUT/ca.key"
    openssl req -x509 -new -key "$OUT/ca.key" -sha256 -days 3650 -config "$WORK/ca.cnf" -out "$OUT/ca.pem"
fi

rm -f "$OUT/server.key" "$OUT/server.pem"
openssl ecparam -name prime256v1 -genkey -noout -out "$OUT/server.key"
openssl req -new -key "$OUT/server.key" -config "$WORK/server.cnf" -out "$WORK/server.csr"
# 397 дней: предел, который принимают Safari и Chrome для сертификата сервера.
openssl x509 -req -in "$WORK/server.csr" -CA "$OUT/ca.pem" -CAkey "$OUT/ca.key" -CAcreateserial \
    -CAserial "$WORK/ca.srl" -days 397 -sha256 -extfile "$WORK/server.cnf" -extensions v3_server \
    -out "$OUT/server.pem"

chmod 600 "$OUT/ca.key" "$OUT/server.key"
chmod 644 "$OUT/ca.pem" "$OUT/server.pem"
echo "готово: $OUT/ca.pem (на ноутбук), $OUT/server.pem и $OUT/server.key (на K8)"
