"""ModelChannel приложения: запрос к контейнеру-исполнителю по HTTP (SPEC-1.0 1.4)."""

import json
import logging
import urllib.request

from app.model.redaction import redact
from app.model.types import ERROR_MESSAGE, ModelResult

log = logging.getLogger("app.model")
# Исполнитель сам ждёт CLI 120 с; запас на сеть и разбор.
REQUEST_TIMEOUT_S = 130


def http_post(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class ExecutorChannel:
    def __init__(
        self, channel: str, base_url: str, *, post=None, timeout: float = REQUEST_TIMEOUT_S
    ):
        self.channel = channel
        self.url = base_url.rstrip("/") + "/run"
        self.post = post or http_post
        self.timeout = timeout

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
            body = self.post(self.url, payload, self.timeout)
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
