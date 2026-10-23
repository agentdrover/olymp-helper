-- Учёт запусков CLI модели (SPEC-1.0 1.4): строка пишется ДО запуска, единица учёта - запуск CLI.
-- day - сутки по Europe/Moscow (YYYY-MM-DD); по ним считается дневной лимит.
CREATE TABLE model_run (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    started_at TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('claude', 'cursor')),
    model TEXT NOT NULL,
    fallback INTEGER NOT NULL DEFAULT 0 CHECK (fallback IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started', 'ok', 'error')),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    error_kind TEXT
);
CREATE INDEX idx_model_run_day ON model_run (day);
