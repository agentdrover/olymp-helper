"""Панель помощника: состояния запроса к модели - один общий компонент (SPEC-1.0 1.3а).

T06 делает панель и состояния; T07 (чат, голос) использует тот же компонент: шаблон
`child/_panel_status.html` и функции этого модуля. Запрос к модели на задание - один за раз:
пока он в работе, второй не уходит (`Inflight`).
"""

import threading

TEXTS = {
    "idle": "",
    "thinking": "Думаю… обычно 5–15 с",
    "second_channel": "Основной канал не ответил, пробую второй",
    "down": "Помощник сейчас не отвечает — проверка по ключу работает.",
    "limit": "На сегодня всё: вопросы к помощнику кончились — проверка по ключу работает.",
    "no_hints": "К этому заданию подсказок нет — проверь ответ по ключу.",
}
BUSY = ("thinking", "second_channel")


def panel(state: str) -> dict:
    return {"state": state, "text": TEXTS[state], "busy": state in BUSY}


class Inflight:
    """Запросы к модели в работе: ключ -> id последнего события на момент старта."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict = {}

    def acquire(self, key, since_event: int) -> bool:
        with self._lock:
            if key in self._items:
                return False
            self._items[key] = since_event
            return True

    def release(self, key) -> None:
        with self._lock:
            self._items.pop(key, None)

    def since(self, key) -> int | None:
        with self._lock:
            return self._items.get(key)


def live_state(conn, inflight: Inflight, key) -> str:
    """idle | thinking | second_channel для экрана, открытого во время запроса."""
    since = inflight.since(key)
    if since is None:
        return "idle"
    switched = conn.execute(
        "SELECT 1 FROM event WHERE type = 'fallback' AND id > ? LIMIT 1", (since,)
    ).fetchone()
    return "second_channel" if switched else "thinking"


def failure_state(result) -> str:
    """Состояние панели по неудачному ответу модели."""
    return "limit" if getattr(result, "error_kind", None) == "limit" else "down"
