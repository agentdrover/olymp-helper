"""Выбор канала, одна попытка на запасном, учёт и лимит (SPEC-1.0 1.4)."""

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime

from app.ingest.store import atomic
from app.model.client import MAX_IMAGE_REQUEST_BYTES, MAX_REQUEST_BYTES, request_size
from app.model.ledger import Ledger
from app.model.redaction import redact
from app.model.types import CHANNELS, LIMIT_MESSAGE, ChannelError, ModelChannel, ModelResult
from app.tutor.seq import bump

__all__ = ["DEFAULT_MODELS", "LIMIT_MESSAGE", "ModelService", "model_family"]

# claude закреплён; cursor - auto (gpt-5 через cursor-agent на k8 падает): без закреплённой
# модели family(cursor) None, автопубликация невозможна - осознанное ограничение.
DEFAULT_MODELS = {"claude": "sonnet", "cursor": "auto"}
_FAMILIES = (
    (re.compile(r"(claude|sonnet|opus|haiku)([-.:\d]|$)"), "anthropic"),
    (re.compile(r"(gpt|o[134]|codex)([-.:\d]|$)"), "openai"),
    (re.compile(r"gemini([-.:\d]|$)"), "google"),
    (re.compile(r"grok([-.:\d]|$)"), "xai"),
)


def model_family(model: str | None) -> str | None:
    """Семейство по имени модели; auto, пустое и неизвестное имя - None."""
    name = (model or "").strip().lower().rsplit("/", 1)[-1]
    for pattern, family in _FAMILIES:
        if pattern.match(name):
            return family
    return None


log = logging.getLogger("app.model")


class ModelService:
    def __init__(self, conn: sqlite3.Connection, channels: dict[str, ModelChannel], clock=None):
        self.conn = conn
        self.channels = channels
        self.clock = clock or (lambda: datetime.now(UTC))
        self.ledger = Ledger(conn, self.clock)

    def complete(
        self,
        system: str,
        messages: list[dict],
        schema: dict | None = None,
        session_id: int | None = None,
    ) -> ModelResult:
        """Ответ модели. Никогда не бросает исключений; ребёнку - только безопасное сообщение."""
        primary, claude_model, cursor_model = self.conn.execute(
            "SELECT primary_channel, claude_model, cursor_model FROM settings WHERE id = 1"
        ).fetchone()
        models = {
            "claude": claude_model or DEFAULT_MODELS["claude"],
            "cursor": cursor_model or DEFAULT_MODELS["cursor"],
        }
        spare = next(c for c in CHANNELS if c != primary)
        first = self._attempt(primary, models[primary], system, messages, schema, fallback=False)
        if first.ok or first.error_kind in ("limit", "request_too_large"):
            return first
        self._fallback_event(session_id, primary, spare, first.error_kind)
        return self._attempt(spare, models[spare], system, messages, schema, fallback=True)

    def complete_on(
        self,
        channel: str,
        system: str,
        messages: list[dict],
        schema: dict | None = None,
        session_id: int | None = None,
        images: list[bytes] | None = None,
    ) -> ModelResult:
        """Запрос к ОДНОМУ названному каналу, без запасного: так работает независимый судья.
        Запуск учитывается в общем дневном лимите, как любой другой."""
        models = self._models()
        if channel not in models:
            return ModelResult.failure(str(channel), "", "bad_channel")
        return self._attempt(
            channel, models[channel], system, messages, schema, fallback=False, images=images
        )

    def family(self, channel: str) -> str | None:
        """Семейство модели канала по настройкам; auto или неизвестная модель - None."""
        if channel not in CHANNELS:
            return None
        return model_family(self._models()[channel])

    def fits(
        self,
        system: str,
        messages: list[dict],
        schema: dict | None = None,
        channel: str | None = None,
    ) -> bool:
        """Помещается ли запрос в лимит исполнителя с фактической моделью канала. Без названного
        канала - на обоих: запрос, который не лезет на запасной, нельзя считать пригодным."""
        models = self._models()
        names = [channel] if channel in models else list(models)
        return all(
            request_size(system, messages, schema, (), n, models[n]) <= MAX_REQUEST_BYTES
            for n in names
        )

    def _models(self) -> dict:
        claude_model, cursor_model = self.conn.execute(
            "SELECT claude_model, cursor_model FROM settings WHERE id = 1"
        ).fetchone()
        return {
            "claude": claude_model or DEFAULT_MODELS["claude"],
            "cursor": cursor_model or DEFAULT_MODELS["cursor"],
        }

    def _attempt(self, name, model, system, messages, schema, fallback, images=None) -> ModelResult:
        # Размер тела (с вложениями и фактическим именем модели) - до резерва лимита:
        # слишком большой запрос не списывает запуск и не уходит на запасной канал.
        try:
            limit = MAX_IMAGE_REQUEST_BYTES if images else MAX_REQUEST_BYTES
            if request_size(system, messages, schema, images or (), name, model) > limit:
                raise ChannelError("request_too_large")
        except ChannelError as exc:
            return ModelResult.failure(name, model, exc.kind)
        run_id = self.ledger.reserve(name, model, fallback)
        if run_id is None:
            return ModelResult.failure(name, model, "limit", message=LIMIT_MESSAGE)
        try:
            extra = {"images": images} if images else {}
            res = self.channels[name].complete(system, messages, schema, model=model, **extra)
        except Exception as exc:  # noqa: BLE001
            log.warning("channel %s raised %s", name, type(exc).__name__)
            res = ModelResult.failure(name, model, "exception")
        if res.error is not None:
            # Что бы ни пришло от канала, наружу уходит только фиксированное сообщение.
            kind = redact(res.error_kind or "error")
            res = ModelResult.failure(name, model, kind, res.duration_ms)
        self.ledger.finish(run_id, res.ok, res.duration_ms, res.error_kind)
        return res

    def _fallback_event(self, session_id, src, dst, reason) -> None:
        if session_id is None:
            return
        with atomic(self.conn):  # переход на второй канал меняет панель («пробую второй»)
            self.conn.execute(
                "INSERT INTO event (session_id, type, data, created_at) "
                "VALUES (?, 'fallback', ?, ?)",
                (
                    session_id,
                    json.dumps({"from": src, "to": dst, "reason": reason}),
                    self.clock().isoformat(),
                ),
            )
            bump(self.conn)
