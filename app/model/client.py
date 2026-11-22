"""ModelChannel приложения: запрос к контейнеру-исполнителю по HTTP (SPEC-1.0 1.4)."""

import json
import logging
import os
import urllib.error
import urllib.request

from app.model.control import WORKER_PROOF_TIMEOUT_S, NoRedirect
from app.model.images import encode_images
from app.model.redaction import redact
from app.model.types import ERROR_MESSAGE, ChannelError, ModelResult

log = logging.getLogger("app.model")
# Исполнитель сам ждёт CLI 120 с; запас на сеть и разбор.
REQUEST_TIMEOUT_S = 130


# Исполнитель отказывает от запроса больше 1 000 000 байт; запас на канал, модель и заголовки.
MAX_REQUEST_BYTES = 900_000
# Запрос с изображениями: исполнитель принимает до 3 000 000 байт тела; тот же запас.
MAX_IMAGE_REQUEST_BYTES = 2_500_000


def encode_payload(payload: dict) -> bytes:
    """Тело запроса к исполнителю ровно в том виде, в каком его отправляет http_post."""
    return json.dumps(payload).encode("utf-8")


def request_payload(channel, model, system, messages, schema, images=(), *, attempt=None):
    payload = {
        "channel": channel,
        "model": model,
        "system": system,
        "messages": messages,
        "schema": schema,
    }
    if images:
        payload["images"] = encode_images(list(images))
    if attempt is not None:
        payload["attempt"] = attempt
    return payload


def request_size(
    system: str,
    messages: list,
    schema: dict | None,
    images=(),
    channel: str = "claude",
    model: str = "",
    *,
    tracked: bool = False,
) -> int:
    """Байт в теле запроса (кириллица в JSON занимает по 6 байт на знак): мерить только так.
    Канал и имя модели входят в тело, поэтому проверка до резерва лимита берёт фактические."""
    attempt = "0" * 32 if tracked else None
    payload = request_payload(channel, model, system, messages, schema, images, attempt=attempt)
    return len(encode_payload(payload))


def http_post(url: str, payload: dict, timeout: float, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=encode_payload(payload),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        with opener.open(req, timeout=timeout) as resp:
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
        self.base_url = base_url.rstrip("/")
        self.url = self.base_url + "/run"
        self.post = post or http_post
        self.timeout = timeout
        # Общий ключ приложения и исполнителя; в окружение CLI он не попадает.
        self.key = key if key is not None else os.environ.get("EXECUTOR_SHARED_KEY", "")

    def outcome(self, attempt: str, model: str) -> ModelResult | None:
        request = urllib.request.Request(
            self.base_url + "/outcome/" + attempt, headers={"X-Executor-Key": self.key}
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
            with opener.open(request, timeout=WORKER_PROOF_TIMEOUT_S) as response:
                content = response.read(1_100_001)
                if len(content) > 1_100_000:
                    return None
                body = json.loads(content)
            if (
                not isinstance(body, dict)
                or type(body.get("version")) is not int
                or body["version"] != 1
                or body.get("channel") != self.channel
                or body.get("attempt") != attempt
                or body.get("state") != "complete"
            ):
                return None
            return self._decode(body.get("result"), model, strict=True)
        except Exception:  # noqa: BLE001 - absent/quarantined receipt is unknown
            return None

    def _decode(self, body, model, *, strict=False):
        if not isinstance(body, dict) or (
            strict and (body.get("channel") != self.channel or "error" not in body)
        ):
            raise TypeError("invalid executor response")
        error = body.get("error")
        return ModelResult(
            text=body.get("text"),
            json=body.get("json"),
            channel=self.channel,
            model=str(body.get("model") or model),
            duration_ms=int(body.get("duration_ms") or 0),
            usage=body.get("usage"),
            error=None if error is None else ERROR_MESSAGE,
            error_kind=None if error is None else redact(str(body.get("error_kind") or "error")),
        )

    def complete(
        self,
        system,
        messages,
        schema=None,
        *,
        model=None,
        images=(),
        attempt=None,
    ) -> ModelResult:
        model = model or ""
        try:
            try:
                payload = request_payload(
                    self.channel, model, system, messages, schema, images, attempt=attempt
                )
                limit = MAX_IMAGE_REQUEST_BYTES if images else MAX_REQUEST_BYTES
                if len(encode_payload(payload)) > limit:
                    raise ChannelError("request_too_large")
            except ChannelError as exc:
                return ModelResult.failure(self.channel, model, exc.kind)
            body = self.post(self.url, payload, self.timeout, headers={"X-Executor-Key": self.key})
            no_dispatch = isinstance(body, dict) and body.get("error_kind") in (
                "route_unavailable",
                "dispatch_unknown",
                "receipt_capacity",
                "busy",
                "faulty",
            )
            strict = attempt is not None and not no_dispatch
            if strict and (not isinstance(body, dict) or body.get("attempt") != attempt):
                raise TypeError("executor result belongs to another attempt")
            return self._decode(body, model, strict=strict)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "executor call failed: channel=%s kind=%s", self.channel, type(exc).__name__
            )
            return ModelResult.failure(self.channel, model, "executor_unreachable")


def configured_channel(name):
    return ExecutorChannel(
        name, os.environ.get(f"EXECUTOR_{name.upper()}_URL", f"http://executor-{name}:8099")
    )
