"""Контракт канала модели (SPEC-1.0 1.4). Только стандартная библиотека: модуль едет в образ исполнителя."""

from dataclasses import asdict, dataclass
from typing import Any, Protocol

CHANNELS = ("claude", "cursor")

# Единственное, что видит ребёнок: без технических подробностей и без трассы.
ERROR_MESSAGE = "Помощник сейчас не отвечает — проверка по ключу работает."
LIMIT_MESSAGE = "На сегодня всё — вопросы к помощнику кончились, проверка по ключу работает."


class ChannelError(Exception):
    """Сбой запуска или разбора ответа CLI. kind - короткий код без деталей."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class ModelResult:
    text: str | None
    json: Any
    channel: str
    model: str
    duration_ms: int
    usage: dict | None
    error: str | None = None
    error_kind: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def failure(cls, channel: str, model: str, kind: str, duration_ms: int = 0, message=None):
        return cls(
            None,
            None,
            channel,
            model,
            duration_ms,
            None,
            error=message or ERROR_MESSAGE,
            error_kind=kind,
        )


class ModelChannel(Protocol):
    def complete(
        self,
        system: str,
        messages: list[dict],
        schema: dict | None = None,
        *,
        model: str | None = None,
    ) -> ModelResult: ...
