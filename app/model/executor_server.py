"""Сервер контейнера-исполнителя: принимает запрос приложения и запускает CLI (SPEC-1.0 1.4).

Только стандартная библиотека. Запускается в контейнере с файловой системой только для чтения;
на каждый запрос - свежий пустой каталог (и домашний каталог CLI) внутри tmpfs, который удаляется
после ответа.
"""

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.model.adapters import ADAPTERS
from app.model.runner import run_cli
from app.model.types import ModelResult

log = logging.getLogger("app.model.executor")
MAX_BODY = 1_000_000


def handle_request(
    payload, *, env=None, spawn=None, workroot=None, only_channel=None, launcher=None
) -> dict:
    env = os.environ if env is None else env
    channel = payload.get("channel") if isinstance(payload, dict) else None
    if channel not in ADAPTERS or (only_channel and channel != only_channel):
        return ModelResult.failure(str(channel), "", "bad_request").to_dict()
    try:
        system, messages = payload["system"], payload["messages"]
        schema, model = payload.get("schema"), payload["model"]
        if not isinstance(system, str) or not isinstance(messages, list) or not messages:
            raise ValueError
        if schema is not None and not isinstance(schema, dict):
            raise ValueError
        for m in messages:
            if not isinstance(m, dict) or not isinstance(m.get("content"), str):
                raise TypeError
    except (KeyError, ValueError, TypeError):
        return ModelResult.failure(channel, "", "bad_request").to_dict()
    return run_cli(
        channel,
        model,
        system,
        messages,
        schema,
        env=env,
        spawn=spawn,
        workroot=workroot,
        launcher=launcher,
    ).to_dict()


def make_server(
    host, port, *, env=None, spawn=None, workroot=None, only_channel=None, launcher=None
):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self):
            if self.path != "/run":
                return self._send(404, {"error": "not_found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise ValueError
                payload = json.loads(self.rfile.read(size))
            except ValueError:
                return self._send(400, ModelResult.failure("", "", "bad_request").to_dict())
            reply = handle_request(
                payload,
                env=env,
                spawn=spawn,
                workroot=workroot,
                only_channel=only_channel,
                launcher=launcher,
            )
            self._send(200, reply)

        def log_message(self, fmt, *args):  # запросы с содержимым не журналируются
            pass

    return ThreadingHTTPServer((host, port), Handler)


def cli_launcher(user: str) -> list[str]:
    """Префикс запуска CLI под другим UID без привилегий: сервер, его /proc и токен недоступны."""
    return [
        "setpriv",
        f"--reuid={user}",
        f"--regid={user}",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
        "--",
    ]


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    host = os.environ.get("EXECUTOR_HOST", "0.0.0.0")
    port = int(os.environ.get("EXECUTOR_PORT", "8099"))
    root = os.environ.get("EXECUTOR_WORKROOT", "/work")
    cli_user = os.environ.get("EXECUTOR_CLI_USER")
    make_server(
        host,
        port,
        workroot=root,
        only_channel=os.environ.get("EXECUTOR_CHANNEL") or None,
        launcher=cli_launcher(cli_user) if cli_user else None,
    ).serve_forever()


if __name__ == "__main__":
    main()
