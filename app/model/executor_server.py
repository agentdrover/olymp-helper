"""Сервер контейнера-исполнителя: принимает запрос приложения и запускает CLI (SPEC-1.0 1.4).

Только стандартная библиотека. Запускается в контейнере с файловой системой только для чтения.
Одновременно выполняется один запуск (параллельный получает «занят»); перед запуском и после него
убиваются все процессы пользователя CLI; каталоги запуска создаются в tmpfs заново и удаляются.
Запрос принимается только с общим ключом приложения (заголовок X-Executor-Key).
"""

import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.model.adapters import ADAPTERS
from app.model.runner import make_sweeper, run_cli
from app.model.types import ModelResult

log = logging.getLogger("app.model.executor")
MAX_BODY = 1_000_000
KEY_HEADER = "X-Executor-Key"
RESTART_DELAY_S = 30


class Gate:
    """Один активный запуск и состояние «неисправен» (после него - отказ и перезапуск)."""

    def __init__(self, on_fault=None):
        self._lock = threading.Lock()
        self._on_fault = on_fault
        self.fault_reason = None

    def try_enter(self) -> bool:
        return self._lock.acquire(blocking=False)

    def leave(self) -> None:
        self._lock.release()

    def fault(self, reason: str) -> None:
        if self.fault_reason is None:
            self.fault_reason = reason
            if self._on_fault:
                self._on_fault(reason)


def _failure(channel, kind) -> dict:
    return ModelResult.failure(str(channel), "", kind).to_dict()


def _valid(payload):
    """(channel, model, system, messages, schema) или None."""
    try:
        system, messages = payload["system"], payload["messages"]
        schema, model = payload.get("schema"), payload["model"]
        if not isinstance(system, str) or not isinstance(messages, list) or not messages:
            return None
        if schema is not None and not isinstance(schema, dict):
            return None
        for m in messages:
            if not isinstance(m, dict) or not isinstance(m.get("content"), str):
                return None
    except (KeyError, TypeError):
        return None
    return model, system, messages, schema


def handle_request(
    payload,
    *,
    env=None,
    spawn=None,
    workroot=None,
    only_channel=None,
    launcher=None,
    gate=None,
    sweeper=None,
) -> dict:
    env = os.environ if env is None else env
    channel = payload.get("channel") if isinstance(payload, dict) else None
    if channel not in ADAPTERS or (only_channel and channel != only_channel):
        return _failure(channel, "bad_request")
    parts = _valid(payload)
    if parts is None:
        return _failure(channel, "bad_request")
    model, system, messages, schema = parts
    if gate is not None:
        if gate.fault_reason:
            return _failure(channel, "faulty")
        if not gate.try_enter():
            return _failure(channel, "busy")
    try:
        # Ничего от прежнего запуска не должно остаться живым.
        if sweeper is not None and not sweeper():
            if gate is not None:
                gate.fault("sweep")
            return _failure(channel, "faulty")
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
            on_fault=gate.fault if gate is not None else None,
            sweeper=sweeper,
        ).to_dict()
    except Exception:
        log.exception("run failed")
        return _failure(channel, "internal")
    finally:
        if gate is not None:
            gate.leave()


def make_server(
    host,
    port,
    *,
    shared_key,
    env=None,
    spawn=None,
    workroot=None,
    only_channel=None,
    launcher=None,
    sweeper=None,
    gate=None,
):
    if not shared_key:
        raise ValueError("shared key is required")
    key = shared_key.encode("utf-8")
    gate = gate or Gate()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/healthz":
                return self._send(404, {"error": "not_found"})
            if gate.fault_reason:
                return self._send(503, {"status": "faulty"})
            self._send(200, {"status": "ok"})

        def do_POST(self):
            if self.path != "/run":
                return self._send(404, {"error": "not_found"})
            given = (self.headers.get(KEY_HEADER) or "").encode("utf-8")
            if not hmac.compare_digest(given, key):
                return self._send(401, _failure("", "unauthorized"))
            if gate.fault_reason:
                return self._send(503, _failure("", "faulty"))
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise ValueError
                payload = json.loads(self.rfile.read(size))
            except ValueError:
                return self._send(400, _failure("", "bad_request"))
            reply = handle_request(
                payload,
                env=env,
                spawn=spawn,
                workroot=workroot,
                only_channel=only_channel,
                launcher=launcher,
                gate=gate,
                sweeper=sweeper,
            )
            self._send(503 if reply.get("error_kind") == "faulty" else 200, reply)

        def log_message(self, fmt, *args):  # запросы с содержимым не журналируются
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.gate = gate
    return server


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
    shared_key = os.environ.get("EXECUTOR_SHARED_KEY", "")
    if not shared_key:
        raise SystemExit("EXECUTOR_SHARED_KEY is not set")
    host = os.environ.get("EXECUTOR_HOST", "0.0.0.0")
    port = int(os.environ.get("EXECUTOR_PORT", "8099"))
    root = os.environ.get("EXECUTOR_WORKROOT", "/work")
    cli_user = os.environ.get("EXECUTOR_CLI_USER")
    launcher = cli_launcher(cli_user) if cli_user else None

    def restart_later(reason):
        # Неисправен: /healthz отвечает 503, запуски отклоняются; выходим, и политика перезапуска
        # (restart: unless-stopped) поднимает контейнер с чистым tmpfs.
        log.error("faulty (%s): exiting in %s s", reason, RESTART_DELAY_S)
        timer = threading.Timer(RESTART_DELAY_S, os._exit, args=(3,))
        timer.daemon = True
        timer.start()

    make_server(
        host,
        port,
        shared_key=shared_key,
        workroot=root,
        only_channel=os.environ.get("EXECUTOR_CHANNEL") or None,
        launcher=launcher,
        sweeper=make_sweeper(launcher, cli_user) if launcher else None,
        gate=Gate(on_fault=restart_later),
    ).serve_forever()


if __name__ == "__main__":
    main()
