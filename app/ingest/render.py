"""Рендер страниц PDF локально и лениво, с дисковым кэшем 500 МБ (SPEC-1.0 1.7)."""

import hashlib
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path

_LEGACY_NAME = re.compile(r"[0-9a-f]{64}\.png")
CACHE_LIMIT_BYTES = 500 * 1024 * 1024


class PageCache:
    """Файлы PNG в каталоге; при превышении лимита вытесняются самые давние по обращению."""

    def __init__(self, root: Path, limit: int = CACHE_LIMIT_BYTES):
        self.root = Path(root)
        self.limit = limit
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        # Ключ страницы начинается с хэша материала: по этой приставке кэш чистится при
        # удалении материала.
        group = key.split(":", 1)[0] if ":" in key else ""
        prefix = f"{group}-" if group else ""
        return self.root / (prefix + hashlib.sha256(key.encode("utf-8")).hexdigest() + ".png")

    def purge_legacy(self) -> int:
        """Файлы кэша со старым именем (без хэша материала в начале) не находятся по
        `discard`; кэш восстановим, поэтому при запуске их просто убираем."""
        removed = 0
        with self._lock:
            if self.root.is_dir():
                for entry in self.root.glob("*.png"):
                    if _LEGACY_NAME.fullmatch(entry.name):
                        entry.unlink(missing_ok=True)
                        removed += 1
        return removed

    def discard(self, group: str) -> int:
        """Убрать из кэша все страницы материала с этим хэшем."""
        removed = 0
        with self._lock:
            if self.root.is_dir():
                for entry in self.root.glob(f"{group}-*.png"):
                    entry.unlink(missing_ok=True)
                    removed += 1
        return removed

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
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            self._evict()
        return data

    def _evict(self) -> None:
        entries = []
        for entry in self.root.iterdir():
            if entry.suffix == ".png":
                st = entry.stat()
                entries.append((st.st_mtime, entry, st.st_size))
        total = sum(size for _, _, size in entries)
        for _, entry, size in sorted(entries, key=lambda e: e[0]):
            if total <= self.limit:
                break
            entry.unlink(missing_ok=True)
            total -= size
