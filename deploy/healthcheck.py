"""Проверка живости для HEALTHCHECK: GET /healthz на себя (HTTPS, если задан OLYMP_TLS_CERT)."""

import http.client
import os
import ssl
import sys


def main() -> int:
    port = int(os.environ.get("OLYMP_PORT", "8443"))
    try:
        if os.environ.get("OLYMP_TLS_CERT"):
            # Проверка идёт на loopback внутри контейнера; цепочку доверия проверяют снаружи.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=ctx)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/healthz")
        return 0 if conn.getresponse().status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
