"""Ход разбора материала в памяти процесса (OH-UI3): фаза, порция N из M, принятые задания.

Импорт идёт в этом же процессе, а после перезапуска он помечается «прервано перезапуском»,
поэтому в базе ход не хранится. Запись принадлежит запуску (run_id): чужой запуск её не трогает.
Каждый отчёт - ещё и пульс: сторожок (ImportJobs) прерывает запуск, который долго молчит.
Запись создаёт только begin() самого запуска: поздний отчёт истёкшего запуска её не оживит.
"""

import threading
import time

PHASES = {
    "reading": "Читаем страницы",
    "chunks": "Разбирается: порция {chunk} из {chunks}, принято заданий {accepted}",
    "writing": "Сохраняем задания и рисунки: {chunk} из {chunks}",
}


def progress_text(item: dict | None, running: bool = True) -> str:
    """Что видит родитель; одна строка для страницы и для опроса."""
    if item is None:
        return "Разбирается: готовим порции" if running else "В очереди"
    return PHASES[item["phase"]].format(**item)


class ImportProgress:
    def __init__(self, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._items: dict[int, dict] = {}
        self._clock = clock

    def _fresh(self, run_id: str, now: float) -> dict:
        return {
            "run_id": run_id,
            "phase": "reading",
            "chunk": 0,
            "chunks": 0,
            "accepted": 0,
            "cap_chunks": 0,
            "beat": now,
        }

    def claim(self, material_id: int, run_id: str) -> None:
        """Новый запуск материала (из submit, по порядку запусков): заменяет запись прежнего."""
        now = self._clock()
        with self._lock:
            self._items[material_id] = self._fresh(run_id, now)

    def begin(self, material_id: int, run_id: str) -> None:
        """Начало работы самого запуска: создаёт запись, только если материал ничей. Запись
        более нового запуска не заменяется (поздний begin истёкшего запуска безвреден)."""
        now = self._clock()
        with self._lock:
            if material_id not in self._items:
                self._items[material_id] = self._fresh(run_id, now)

    def update(
        self,
        material_id: int,
        run_id: str,
        chunk: int,
        chunks: int,
        accepted: int = 0,
        phase: str = "chunks",
    ) -> None:
        with self._lock:
            item = self._items.get(material_id)
            if item is None or item["run_id"] != run_id:
                return  # нет записи этого запуска: он истёк или завершён
            item.update(phase=phase, chunk=chunk, chunks=chunks, accepted=accepted)
            if phase == "chunks":
                item["cap_chunks"] = chunks
            item["beat"] = self._clock()

    def clear(self, material_id: int, run_id: str) -> None:
        with self._lock:
            item = self._items.get(material_id)
            if item is not None and item["run_id"] == run_id:
                del self._items[material_id]

    def snapshot(self, material_id: int, run_id: str) -> dict | None:
        """Полная запись своего запуска (для сторожка)."""
        with self._lock:
            item = self._items.get(material_id)
            if item is None or item["run_id"] != run_id:
                return None
            return dict(item)

    def get(self, material_id: int, current_run: str | None) -> dict | None:
        """Ход только текущего запуска материала (run_id из базы): запись истёкшего запуска
        не показывается. Наружу уходят только числа и фаза."""
        item = self.snapshot(material_id, current_run) if current_run else None
        if item is None:
            return None
        return {k: item[k] for k in ("phase", "chunk", "chunks", "accepted")}

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
