"""Рендер страниц PDF локально и лениво, с дисковым кэшем 500 МБ (SPEC-1.0 1.7)."""

import hashlib
import os
import threading
from collections.abc import Callable
from pathlib import Path

CACHE_LIMIT_BYTES = 500 * 1024 * 1024


class PageCache:
    """Файлы PNG в каталоге; при превышении лимита вытесняются самые давние по обращению."""

    def __init__(self, root: Path, limit: int = CACHE_LIMIT_BYTES):
        self.root = Path(root)
        self.limit = limit
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".png")

    def get(self, key: str, render: Callable[[], bytes]) -> bytes:
        path = self._path(key)
        with self._lock:
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                pass
            else:
                os.utime(path)  # обращение омолаживает запись
                self.hits += 1
                return data
            self.misses += 1
        data = render()
        with self._lock:
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            self._evict(keep=path)
        return data

    def _evict(self, keep: Path) -> None:
        entries = []
        for entry in self.root.iterdir():
            if entry.suffix == ".png":
                st = entry.stat()
                entries.append((st.st_mtime, entry, st.st_size))
        total = sum(size for _, _, size in entries)
        for _, entry, size in sorted(entries, key=lambda e: e[0]):
            if total <= self.limit:
                break
            if entry == keep:
                continue
            entry.unlink(missing_ok=True)
            total -= size
