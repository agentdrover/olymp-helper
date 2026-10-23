-- Схема SPEC-1.0 §1.8. Исполняется в одной транзакции вместе с записью в schema_migrations.

CREATE TABLE material (
    id INTEGER PRIMARY KEY,
    file TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('tasks', 'key', 'textbook')),
    olympiad TEXT,
    year INTEGER,
    stage TEXT,
    variant TEXT,
    import_status TEXT NOT NULL DEFAULT 'queued'
        CHECK (import_status IN ('queued', 'running', 'done', 'failed')),
    import_error TEXT,
    pages_total INTEGER CHECK (pages_total IS NULL OR pages_total >= 0)
);

-- Страницы материалов для поиска по учебникам (SPEC 1.7). Нормализация текста - на стороне
-- приложения (одинаково для индекса и запроса), здесь только токенизатор.
CREATE VIRTUAL TABLE material_page USING fts5(
    material_id UNINDEXED,
    page UNINDEXED,
    text,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE task_item (
    id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES material (id) ON DELETE CASCADE,
    page_from INTEGER,
    page_to INTEGER,
    variant TEXT,
    source_number TEXT,
    subject TEXT NOT NULL DEFAULT 'biology' CHECK (subject = 'biology'),
    type TEXT NOT NULL CHECK (type IN ('single', 'multiple', 'match', 'open')),
    text TEXT NOT NULL,
    options TEXT CHECK (options IS NULL OR json_valid(options)),
    points REAL,
    topic TEXT,
    import_status TEXT NOT NULL DEFAULT 'draft'
        CHECK (import_status IN ('draft', 'checked', 'unsupported')),
    answer_key TEXT CHECK (answer_key IS NULL OR json_valid(answer_key)),
    key_source TEXT CHECK (key_source IS NULL OR key_source IN ('official', 'parent')),
    model_answer TEXT CHECK (model_answer IS NULL OR json_valid(model_answer)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    figure TEXT CHECK (figure IS NULL OR json_valid(figure)),
    -- Открытые задания вне v1 (SPEC 1.1); ключ без источника недопустим (SPEC 1.2).
    CHECK (type <> 'open' OR import_status = 'unsupported'),
    CHECK (answer_key IS NULL OR key_source IS NOT NULL)
);
CREATE INDEX idx_task_item_material ON task_item (material_id);
CREATE INDEX idx_task_item_status ON task_item (import_status);

CREATE TABLE study_session (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT CHECK (end_reason IS NULL OR end_reason IN ('explicit', 'idle')),
    summary TEXT CHECK (summary IS NULL OR json_valid(summary)),
    CHECK ((ended_at IS NULL) = (end_reason IS NULL))
);
CREATE INDEX idx_study_session_open ON study_session (last_activity_at) WHERE ended_at IS NULL;

CREATE TABLE event (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES study_session (id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN (
        'attempt', 'hint', 'answer_shown', 'explain', 'chat', 'voice', 'fallback',
        'disputed_key', 'shown_to_parent', 'parent_opened_chat'
    )),
    task_item_id INTEGER REFERENCES task_item (id) ON DELETE SET NULL,
    data TEXT CHECK (data IS NULL OR json_valid(data)),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_event_session ON event (session_id, id);
CREATE INDEX idx_event_task_item ON event (task_item_id);

-- Одна строка настроек.
CREATE TABLE settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    primary_channel TEXT NOT NULL DEFAULT 'claude' CHECK (primary_channel IN ('claude', 'cursor')),
    claude_model TEXT,
    cursor_model TEXT,
    daily_run_limit INTEGER NOT NULL DEFAULT 200 CHECK (daily_run_limit > 0),
    parent_pin_hash TEXT,
    show_exam_countdown INTEGER NOT NULL DEFAULT 0 CHECK (show_exam_countdown IN (0, 1)),
    -- Дата этапа олимпиады (ISO YYYY-MM-DD) для счётчика дней; SPEC 1.8.
    stage_date TEXT CHECK (stage_date IS NULL OR date(stage_date) IS stage_date)
);
INSERT INTO settings (id) VALUES (1);
