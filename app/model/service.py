"""Выбор канала, одна попытка на запасном, учёт и лимит (SPEC-1.0 1.4)."""

import json
import logging
import sqlite3
from datetime import UTC, datetime

from app.model.ledger import Ledger
from app.model.redaction import redact
from app.model.types import CHANNELS, LIMIT_MESSAGE, ModelChannel, ModelResult

__all__ = ["DEFAULT_MODELS", "LIMIT_MESSAGE", "ModelService"]

DEFAULT_MODELS = {"claude": "sonnet", "cursor": "auto"}
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
        if first.ok or first.error_kind == "limit":
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
    ) -> ModelResult:
        """Запрос к ОДНОМУ названному каналу, без запасного: так работает независимый судья.
        Запуск учитывается в общем дневном лимите, как любой другой."""
        models = self._models()
        return self._attempt(channel, models[channel], system, messages, schema, fallback=False)

    def _models(self) -> dict:
        claude_model, cursor_model = self.conn.execute(
            "SELECT claude_model, cursor_model FROM settings WHERE id = 1"
        ).fetchone()
        return {
            "claude": claude_model or DEFAULT_MODELS["claude"],
            "cursor": cursor_model or DEFAULT_MODELS["cursor"],
        }

    def _attempt(self, name, model, system, messages, schema, fallback) -> ModelResult:
        run_id = self.ledger.reserve(name, model, fallback)
        if run_id is None:
            return ModelResult.failure(name, model, "limit", message=LIMIT_MESSAGE)
        try:
            res = self.channels[name].complete(system, messages, schema, model=model)
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
        self.conn.execute(
            "INSERT INTO event (session_id, type, data, created_at) VALUES (?, 'fallback', ?, ?)",
            (
                session_id,
                json.dumps({"from": src, "to": dst, "reason": reason}),
                self.clock().isoformat(),
            ),
        )
