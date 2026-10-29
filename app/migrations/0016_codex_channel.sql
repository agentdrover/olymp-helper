-- migrate: foreign_keys=off
ALTER TABLE settings ADD COLUMN codex_model TEXT NOT NULL DEFAULT 'gpt-6.1-sol';
ALTER TABLE settings ADD COLUMN codex_enabled INTEGER NOT NULL DEFAULT 0 CHECK (codex_enabled IN (0, 1));
CREATE TABLE model_run_new (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, started_at TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('claude', 'cursor', 'codex')),
    model TEXT NOT NULL, fallback INTEGER NOT NULL DEFAULT 0 CHECK (fallback IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started', 'ok', 'error')),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0), error_kind TEXT
);
INSERT INTO model_run_new SELECT * FROM model_run;
DROP TABLE model_run;
ALTER TABLE model_run_new RENAME TO model_run;
CREATE INDEX idx_model_run_day ON model_run (day);
