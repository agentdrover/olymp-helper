"""Общие объекты приложения для загрузки, импорта и проверки заданий."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.db import connect
from app.ingest.progress import ImportProgress
from app.ingest.render import PageCache
from app.model.client import ExecutorChannel
from app.model.service import ModelService
from app.model.types import ModelResult


class ExecutorModel:
    """Модель по умолчанию: ModelService на отдельном соединении под каждый запрос."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def complete(self, system, messages, schema=None, session_id=None) -> ModelResult:
        return self._run(lambda svc: svc.complete(system, messages, schema, session_id))

    def complete_on(self, channel, system, messages, schema=None, session_id=None) -> ModelResult:
        """Один названный канал без запасного (независимый судья текста, OH-T06)."""
        return self._run(lambda svc: svc.complete_on(channel, system, messages, schema, session_id))

    def _run(self, call) -> ModelResult:
        conn = connect(self.db_path)
        try:
            channels = {
                "claude": ExecutorChannel(
                    "claude", os.environ.get("EXECUTOR_CLAUDE_URL", "http://executor-claude:8099")
                ),
                "cursor": ExecutorChannel(
                    "cursor", os.environ.get("EXECUTOR_CURSOR_URL", "http://executor-cursor:8099")
                ),
            }
            return call(ModelService(conn, channels))
        finally:
            conn.close()


@dataclass
class FeedState:
    """Лента проверки: порядок карточек на момент открытия и позиция для «N из M»."""

    order: list[int] = field(default_factory=list)


@dataclass
class AppContext:
    db_path: Path
    data_dir: Path
    state: object  # app.state: модель берётся при каждом вызове, тест может её подменить
    page_cache: PageCache
    feed: FeedState = field(default_factory=FeedState)
    progress: ImportProgress = field(default_factory=ImportProgress)
    auto_publish: bool = True  # K5b: задания с официальным ключом публикуются без родителя

    @property
    def model(self):
        return self.state.model

    def material_path(self, relative: str) -> Path:
        return self.data_dir / relative
