-- Codex заменяет Claude; история Claude и остальные настройки сохраняются.
CREATE TABLE settings_new (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    primary_channel TEXT NOT NULL DEFAULT 'codex' CHECK (primary_channel IN ('claude', 'cursor', 'codex')),
    claude_model TEXT,
    cursor_model TEXT,
    codex_model TEXT NOT NULL DEFAULT 'gpt-6.1-sol',
    daily_run_limit INTEGER NOT NULL DEFAULT 200 CHECK (daily_run_limit > 0),
    parent_pin_hash TEXT,
    show_exam_countdown INTEGER NOT NULL DEFAULT 0 CHECK (show_exam_countdown IN (0, 1)),
    stage_date TEXT CHECK (stage_date IS NULL OR date(stage_date) IS stage_date),
    child_notice_seen_at TEXT
);
INSERT INTO settings_new (id, primary_channel, claude_model, cursor_model, daily_run_limit,
                          parent_pin_hash, show_exam_countdown, stage_date, child_notice_seen_at)
SELECT id, CASE primary_channel WHEN 'claude' THEN 'codex' ELSE primary_channel END,
       claude_model, cursor_model, daily_run_limit, parent_pin_hash, show_exam_countdown,
       stage_date, NULL FROM settings; -- уведомить ребёнка о новом внешнем провайдере
DROP TABLE settings;
ALTER TABLE settings_new RENAME TO settings;

CREATE TABLE model_run_new (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    started_at TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('claude', 'cursor', 'codex')),
    model TEXT NOT NULL,
    fallback INTEGER NOT NULL DEFAULT 0 CHECK (fallback IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started', 'ok', 'error')),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    error_kind TEXT
);
INSERT INTO model_run_new SELECT id, day, started_at, channel, model, fallback, status, duration_ms, error_kind FROM model_run;
DROP TABLE model_run;
ALTER TABLE model_run_new RENAME TO model_run;
CREATE INDEX idx_model_run_day ON model_run (day);
