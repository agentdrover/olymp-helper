-- migrate: foreign_keys=off
-- Пачки сгенерированных вопросов (G2): таблица question_pack, ссылка задания на пачку, источник
-- ключа 'generated'. У сгенерированного задания нет материала и страницы, поэтому material_id
-- перестаёт быть обязательным. CHECK и NOT NULL нельзя изменить через ALTER, поэтому task_item
-- пересоздаётся по процедуре 0008: новая таблица, копирование, удаление старой, переименование,
-- индексы. Раннер выключает внешние ключи ДО транзакции и проверяет foreign_key_check перед COMMIT.
CREATE TABLE question_pack (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    topic TEXT NOT NULL REFERENCES topic (code),
    task_type TEXT NOT NULL CHECK (task_type IN ('single', 'multiple', 'match', 'numeric', 'truefalse')),
    requested_count INTEGER NOT NULL CHECK (requested_count BETWEEN 1 AND 20),
    accepted_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),
    channel TEXT,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    error TEXT,
    rejected TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(rejected))
);
CREATE TABLE task_item_new (
    id INTEGER PRIMARY KEY,
    material_id INTEGER REFERENCES material (id) ON DELETE CASCADE,
    page_from INTEGER,
    page_to INTEGER,
    variant TEXT,
    source_number TEXT,
    subject TEXT NOT NULL DEFAULT 'biology' CHECK (subject = 'biology'),
    type TEXT NOT NULL CHECK (type IN ('single', 'multiple', 'match', 'open', 'numeric')),
    text TEXT NOT NULL,
    options TEXT CHECK (options IS NULL OR json_valid(options)),
    points REAL,
    topic TEXT,
    import_status TEXT NOT NULL DEFAULT 'draft'
        CHECK (import_status IN ('draft', 'checked', 'unsupported')),
    answer_key TEXT CHECK (answer_key IS NULL OR json_valid(answer_key)),
    key_source TEXT CHECK (key_source IS NULL OR key_source IN ('official', 'parent', 'generated')),
    model_answer TEXT CHECK (model_answer IS NULL OR json_valid(model_answer)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    figure TEXT CHECK (figure IS NULL OR json_valid(figure)),
    key_version INTEGER NOT NULL DEFAULT 1,
    excluded INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0, 1)),
    discrepancy_ack TEXT,
    dispute_cleared_event_id INTEGER NOT NULL DEFAULT 0,
    unsupported_reason TEXT,
    section TEXT,
    stage TEXT,
    review_note TEXT,
    text_page INTEGER,
    answer_text TEXT,
    parent_note TEXT,
    pack_id INTEGER REFERENCES question_pack (id) ON DELETE SET NULL,
    CHECK (type <> 'open' OR import_status = 'unsupported'),
    CHECK (answer_key IS NULL OR key_source IS NOT NULL)
);
INSERT INTO task_item_new (
    id, material_id, page_from, page_to, variant, source_number, subject, type, text, options,
    points, topic, import_status, answer_key, key_source, model_answer, revision, figure,
    key_version, excluded, discrepancy_ack, dispute_cleared_event_id, unsupported_reason,
    section, stage, review_note, text_page, answer_text, parent_note
)
SELECT
    id, material_id, page_from, page_to, variant, source_number, subject, type, text, options,
    points, topic, import_status, answer_key, key_source, model_answer, revision, figure,
    key_version, excluded, discrepancy_ack, dispute_cleared_event_id, unsupported_reason,
    section, stage, review_note, text_page, answer_text, parent_note
FROM task_item;
DROP TABLE task_item;
ALTER TABLE task_item_new RENAME TO task_item;
CREATE INDEX idx_task_item_material ON task_item (material_id);
CREATE INDEX idx_task_item_status ON task_item (import_status);
CREATE UNIQUE INDEX idx_task_item_section_number
    ON task_item (material_id, COALESCE(section, ''), source_number);
CREATE INDEX idx_task_item_pack ON task_item (pack_id);
