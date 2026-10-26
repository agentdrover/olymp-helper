"""Ход разбора сборника в памяти процесса (OH-UI3): порция N из M и принятые задания.

Импорт идёт в этом же процессе, а после перезапуска он помечается «прервано перезапуском»,
поэтому в базе ход не хранится. Запись принадлежит запуску (run_id): чужой запуск её не трогает.
"""

import threading


class ImportProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[int, dict] = {}

    def update(self, material_id: int, run_id: str, chunk: int, chunks: int, accepted: int) -> None:
        with self._lock:
            self._items[material_id] = {
                "run_id": run_id,
                "chunk": chunk,
                "chunks": chunks,
                "accepted": accepted,
            }

    def clear(self, material_id: int, run_id: str) -> None:
        with self._lock:
            item = self._items.get(material_id)
            if item is not None and item["run_id"] == run_id:
                del self._items[material_id]

    def get(self, material_id: int, current_run: str | None) -> dict | None:
        """Ход только текущего запуска материала (run_id из базы): запись истёкшего запуска,
        который дописал поздно, не показывается. Наружу уходят только числа."""
        with self._lock:
            item = self._items.get(material_id)
            if item is None or item["run_id"] != current_run:
                return None
            return {k: v for k, v in item.items() if k != "run_id"}

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
