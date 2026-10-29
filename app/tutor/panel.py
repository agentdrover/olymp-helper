"""Панель помощника: состояния запроса к модели - один общий компонент (SPEC-1.0 1.3а).

T06 делает панель и состояния; T07 (чат, голос) использует тот же компонент: шаблон
`child/_panel_status.html` и функции этого модуля. Запрос к модели на задание - один за раз:
пока он в работе, второй не уходит (`Inflight`).
"""

import logging
import threading

from app.tutor.seq import bump_now

log = logging.getLogger(__name__)

LONG_WAIT_S = 60  # после этого срока вместо секунд - «Модель отвечает долго, жду…»

TEXTS = {
    "idle": "",
    "thinking": "Думаю…",
    "second_channel": "Основной канал не ответил, пробую второй",
    "down": "Помощник сейчас не отвечает — проверка по ключу работает.",
    "limit": "На сегодня всё: вопросы к помощнику кончились — проверка по ключу работает.",
    "step_failed": "Помощник не ответил вовремя. Попробуй ещё раз — проверка по ключу работает.",
    "no_hints": "К этому заданию подсказок нет — проверь ответ по ключу.",
}
BUSY = ("thinking", "second_channel")


LONG_WAIT = "Модель отвечает долго, жду…"


def panel(state: str, *, elapsed_s: int | None = None, started_at=None, step=None) -> dict:
    """Состояние панели. Пока шаг идёт, ответ несёт номер шага, время его старта и сколько он
    уже идёт (по часам сервера): «Думаю… N с», после минуты - «Модель отвечает долго, жду…»."""
    text = TEXTS[state]
    out = {"state": state, "text": text, "busy": state in BUSY}
    if state in BUSY and step is not None and elapsed_s is not None:
        if state == "thinking":
            out["text"] = LONG_WAIT if elapsed_s >= LONG_WAIT_S else f"Думаю… {elapsed_s} с"
        out.update(step=step, started_at=started_at, elapsed_s=elapsed_s)
    return out


class Inflight:
    """Запросы к модели в работе: ключ -> id последнего события на момент старта."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict = {}

    def acquire(self, key, since_event: int, step_id: int, conn=None) -> bool:
        """Замок и номер шага берутся одной операцией: у замка нет момента без владельца.
        С `conn` после взятия замка растёт версия панели (замок виден в «думаю»)."""
        with self._lock:
            if key in self._items:
                return False
            self._items[key] = [since_event, step_id]
        if conn is not None:
            try:
                bump_now(conn)
            except BaseException:
                # Версию вырастить не удалось (база занята): замок не остаётся владельцем навсегда,
                # вызывающий видит исключение как неудавшийся шаг.
                with self._lock:
                    item = self._items.get(key)
                    if item is not None and item[1] == step_id:
                        del self._items[key]
                raise
        return True

    def release(self, key, step_id: int, conn=None) -> None:
        """Снимает замок только если им владеет именно этот шаг: опоздавший поток не снимет
        замок следующего шага. Снять замок без номера шага нельзя. С `conn` после снятия растёт
        версия панели."""
        with self._lock:
            item = self._items.get(key)
            if item is None or item[1] != step_id:
                return
            del self._items[key]
        if conn is not None:
            try:
                bump_now(conn)
            except Exception:  # noqa: BLE001 - замок уже снят и остаётся снятым
                log.warning("версия панели не выросла после снятия замка задания %s", key)

    def since(self, key) -> int | None:
        with self._lock:
            item = self._items.get(key)
            return None if item is None else item[0]

    def step_of(self, key) -> int | None:
        with self._lock:
            item = self._items.get(key)
            return None if item is None else item[1]


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
