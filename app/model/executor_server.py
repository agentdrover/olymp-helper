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
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.model.adapters import ADAPTERS
from app.model.images import decode_images
from app.model.runner import cli_launcher, make_sweeper, run_cli
from app.model.types import ChannelError, ModelResult

log = logging.getLogger("app.model.executor")
# Текст и до MAX_IMAGES PNG в base64 (app.model.images); клиент мерит тот же предел до вызова.
MAX_BODY = 3_000_000
KEY_HEADER = "X-Executor-Key"
RESTART_DELAY_S = 30
_ATTEMPT = re.compile(r"[0-9a-f]{32}")
RESULT_TTL = 600
MAX_RESULTS = 64
MAX_TOMBSTONES = 512
MAX_RESULT_BYTES = 1_000_000


__all__ = ["Gate", "cli_launcher", "handle_request", "main", "make_server"]


class Outcomes:
    """Short-lived receipt cache. Worker restarts make missing evidence unknown."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active = set()
        self._finished = {}
        self._seen = {}

    def _expire(self, now):
        for attempt, until in list(self._seen.items()):
            if until <= now:
                del self._seen[attempt]
                self._finished.pop(attempt, None)

    def expire(self):
        with self._lock:
            self._expire(time.monotonic())

    def active(self):
        with self._lock:
            return bool(self._active)

    def accept(self, attempt):
        with self._lock:
            self._expire(time.monotonic())
            if attempt in self._active or attempt in self._seen:
                return "dispatch_unknown"
            if len(self._active) + len(self._seen) >= MAX_TOMBSTONES:
                return "receipt_capacity"
            self._active.add(attempt)
            return None

    def complete(self, attempt, result):
        stored = result if len(json.dumps(result).encode("utf-8")) <= MAX_RESULT_BYTES else None
        with self._lock:
            self._active.discard(attempt)
            self._seen[attempt] = time.monotonic() + RESULT_TTL
            if stored is not None:
                if len(self._finished) == MAX_RESULTS:
                    del self._finished[next(iter(self._finished))]
                self._finished[attempt] = stored

    def status(self, attempt):
        with self._lock:
            self._expire(time.monotonic())
            if attempt in self._active:
                return {"state": "active"}
            receipt = self._finished.get(attempt)
            if receipt is None:
                return {"state": "unknown"}
            return {"state": "complete", "result": receipt}


class Gate:
    """Один активный запуск и состояние «неисправен» (после него - отказ и перезапуск)."""

    def __init__(self, on_fault=None):
        self._lock = threading.Lock()
        self._on_fault = on_fault
        self.fault_reason = None

    def try_enter(self) -> str:
        """ "ok" - замок взят; "busy" - идёт другой запуск; "faulty" - исполнитель неисправен.

        Неисправность проверяется ПОСЛЕ взятия замка: между быстрой проверкой и замком другой
        запуск мог сломать исполнитель.
        """
        if not self._lock.acquire(blocking=False):
            return "busy"
        if self.fault_reason:
            self._lock.release()
            return "faulty"
        return "ok"

    def status(self) -> dict:
        return {
            "state": "faulty" if self.fault_reason else "ready",
            "active": int(self._lock.locked()),
        }

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
    try:
        images = decode_images(payload.get("images") or [])
    except ChannelError as exc:  # неверное вложение отклоняется до замка и до запуска CLI
        return _failure(channel, exc.kind)
    if gate is not None:
        if gate.fault_reason:  # быстрый ответ; окончательная проверка - под замком
            return _failure(channel, "faulty")
        entered = gate.try_enter()
        if entered != "ok":
            return _failure(channel, entered)
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
            images=images,
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
    outcomes = Outcomes()

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
            if self.path == "/status" or self.path.startswith("/outcome/"):
                given = (self.headers.get(KEY_HEADER) or "").encode("utf-8")
                if not hmac.compare_digest(given, key):
                    return self._send(401, {"error": "unauthorized"})
                identity = {"version": 1, "channel": only_channel}
                if self.path == "/status":
                    report = gate.status()
                    report["active"] = int(report["active"] or outcomes.active())
                    return self._send(200, {**identity, **report})
                attempt = self.path.removeprefix("/outcome/")
                if not _ATTEMPT.fullmatch(attempt):
                    return self._send(404, {"error": "not_found"})
                return self._send(200, {**identity, "attempt": attempt, **outcomes.status(attempt)})
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
            attempt = payload.get("attempt") if isinstance(payload, dict) else None
            if attempt is not None:
                if not isinstance(attempt, str) or not _ATTEMPT.fullmatch(attempt):
                    return self._send(400, _failure(only_channel, "bad_request"))
                refused = outcomes.accept(attempt)
                if refused:
                    return self._send(503, _failure(only_channel, refused))
            try:
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
            except BaseException:
                if attempt is not None:
                    outcomes.complete(attempt, None)
                raise
            if attempt is not None:
                outcomes.complete(attempt, reply)
                reply = {**reply, "attempt": attempt}
            self._send(503 if reply.get("error_kind") == "faulty" else 200, reply)

        def log_message(self, fmt, *args):  # запросы с содержимым не журналируются
            pass

    class Server(ThreadingHTTPServer):
        def service_actions(self):
            outcomes.expire()

    server = Server((host, port), Handler)
    server.gate = gate
    return server


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
