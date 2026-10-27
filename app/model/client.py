"""ModelChannel приложения: запрос к контейнеру-исполнителю по HTTP (SPEC-1.0 1.4)."""

import json
import logging
import os
import urllib.error
import urllib.request

from app.model.redaction import redact
from app.model.types import ERROR_MESSAGE, ModelResult

log = logging.getLogger("app.model")
# Исполнитель сам ждёт CLI 120 с; запас на сеть и разбор.
REQUEST_TIMEOUT_S = 130


# Исполнитель отказывает от запроса больше 1 000 000 байт; запас на канал, модель и заголовки.
MAX_REQUEST_BYTES = 900_000


def encode_payload(payload: dict) -> bytes:
    """Тело запроса к исполнителю ровно в том виде, в каком его отправляет http_post."""
    return json.dumps(payload).encode("utf-8")


def request_size(system: str, messages: list, schema: dict | None) -> int:
    """Байт в теле запроса (кириллица в JSON занимает по 6 байт на знак): мерить только так."""
    return len(
        encode_payload(
            {
                "channel": "claude",
                "model": "",
                "system": system,
                "messages": messages,
                "schema": schema,
            }
        )
    )


def http_post(url: str, payload: dict, timeout: float, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=encode_payload(payload),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:  # 401/503: тело - тот же формат ошибки исполнителя
        return json.load(exc)


class ExecutorChannel:
    def __init__(
        self,
        channel: str,
        base_url: str,
        *,
        key: str | None = None,
        post=None,
        timeout: float = REQUEST_TIMEOUT_S,
    ):
        self.channel = channel
        self.url = base_url.rstrip("/") + "/run"
        self.post = post or http_post
        self.timeout = timeout
        # Общий ключ приложения и исполнителя; в окружение CLI он не попадает.
        self.key = key if key is not None else os.environ.get("EXECUTOR_SHARED_KEY", "")

    def complete(self, system, messages, schema=None, *, model=None) -> ModelResult:
        model = model or ""
        payload = {
            "channel": self.channel,
            "model": model,
            "system": system,
            "messages": messages,
            "schema": schema,
        }
        try:
            body = self.post(self.url, payload, self.timeout, headers={"X-Executor-Key": self.key})
            if not isinstance(body, dict):
                raise TypeError("not an object")
            error = body.get("error")
            return ModelResult(
                text=body.get("text"),
                json=body.get("json"),
                channel=self.channel,
                model=str(body.get("model") or model),
                duration_ms=int(body.get("duration_ms") or 0),
                usage=body.get("usage"),
                error=None if error is None else ERROR_MESSAGE,
                error_kind=None
                if error is None
                else redact(str(body.get("error_kind") or "error")),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "executor call failed: channel=%s kind=%s", self.channel, type(exc).__name__
            )
            return ModelResult.failure(self.channel, model, "executor_unreachable")
