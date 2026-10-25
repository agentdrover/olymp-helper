-- migrate: foreign_keys=off
-- Тип «число», ответ со страницы задания (answer_text, кандидат ключа) и заметка родителю
-- (parent_note). CHECK на type нельзя изменить через ALTER, поэтому task_item пересоздаётся по
-- процедуре SQLite: новая таблица, копирование, удаление старой, переименование, индексы.
-- Раннер выключает внешние ключи ДО транзакции и проверяет foreign_key_check перед COMMIT.
CREATE TABLE task_item_new (
    id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES material (id) ON DELETE CASCADE,
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
    key_source TEXT CHECK (key_source IS NULL OR key_source IN ('official', 'parent')),
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
    CHECK (type <> 'open' OR import_status = 'unsupported'),
    CHECK (answer_key IS NULL OR key_source IS NOT NULL)
);
INSERT INTO task_item_new (
    id, material_id, page_from, page_to, variant, source_number, subject, type, text, options,
    points, topic, import_status, answer_key, key_source, model_answer, revision, figure,
    key_version, excluded, discrepancy_ack, dispute_cleared_event_id, unsupported_reason,
    section, stage, review_note, text_page
)
SELECT
    id, material_id, page_from, page_to, variant, source_number, subject, type, text, options,
    points, topic, import_status, answer_key, key_source, model_answer, revision, figure,
    key_version, excluded, discrepancy_ack, dispute_cleared_event_id, unsupported_reason,
    section, stage, review_note, text_page
FROM task_item;
DROP TABLE task_item;
ALTER TABLE task_item_new RENAME TO task_item;
CREATE INDEX idx_task_item_material ON task_item (material_id);
CREATE INDEX idx_task_item_status ON task_item (import_status);
CREATE UNIQUE INDEX idx_task_item_section_number
    ON task_item (material_id, COALESCE(section, ''), source_number);
