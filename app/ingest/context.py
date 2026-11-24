"""Общие объекты приложения для загрузки, импорта и проверки заданий."""

from dataclasses import dataclass, field
from pathlib import Path

from app.db import connect
from app.ingest.autorun import AutoRun
from app.ingest.progress import ImportProgress
from app.ingest.render import PageCache
from app.model.client import configured_channel
from app.model.service import ModelService
from app.model.types import ModelResult


class ExecutorModel:
    """Модель по умолчанию: ModelService на отдельном соединении под каждый запрос."""

    def __init__(self, db_path: Path, *, managed=False):
        self.db_path = db_path
        self.managed = managed

    def complete(self, system, messages, schema=None, session_id=None) -> ModelResult:
        return self._run(lambda svc: svc.complete(system, messages, schema, session_id))

    def complete_on(
        self, channel, system, messages, schema=None, session_id=None, images=None
    ) -> ModelResult:
        """Один названный канал без запасного (независимый судья текста, OH-T06)."""
        return self._run(
            lambda svc: svc.complete_on(channel, system, messages, schema, session_id, images)
        )

    def family(self, channel) -> str | None:
        """Семейство закреплённой модели канала; auto и неизвестная модель - None."""
        return self._run(lambda svc: svc.family(channel))

    def fits(self, system, messages, schema=None, channel=None, images=None) -> bool:
        """Помещается ли запрос в лимит исполнителя с фактической моделью канала."""
        return self._run(lambda svc: svc.fits(system, messages, schema, channel, images))

    def _run(self, call):
        conn = connect(self.db_path)
        try:
            channels = {name: configured_channel(name) for name in ("codex", "cursor")}
            return call(ModelService(conn, channels, managed=self.managed))
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
    autorun: AutoRun = field(default_factory=AutoRun)
    auto_publish: bool = True  # K5b: задания с официальным ключом публикуются без родителя

    @property
    def model(self):
        return self.state.model

    def material_path(self, relative: str) -> Path:
        return self.data_dir / relative
