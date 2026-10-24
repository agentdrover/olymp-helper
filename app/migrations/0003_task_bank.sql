-- Банк заданий и проверка родителем (SPEC-1.0 1.1, 1.2, 1.7, 1.8).

-- Оригинальный текст страниц: FTS5 хранит нормализованные токены, показывать надо исходный.
CREATE TABLE material_page_text (
    material_id INTEGER NOT NULL REFERENCES material (id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (material_id, page)
);

-- Строки загруженного ключа. Это только кандидат: ключ становится ключом задания
-- (task_item.answer_key, key_source = 'official') лишь после подтверждения родителем.
CREATE TABLE key_entry (
    id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES material (id) ON DELETE CASCADE,
    number TEXT NOT NULL,
    answer TEXT NOT NULL,
    UNIQUE (material_id, number)
);

-- Снимок задания на каждую ревизию: попытки ссылаются на (ревизия, версия ключа).
CREATE TABLE task_revision (
    task_item_id INTEGER NOT NULL REFERENCES task_item (id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    key_version INTEGER NOT NULL,
    snapshot TEXT NOT NULL CHECK (json_valid(snapshot)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_item_id, revision)
);

-- Версия ключа растёт при каждой смене ответа или его источника.
ALTER TABLE task_item ADD COLUMN key_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE task_item ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0, 1));
-- Ответ модели, с которым расхождение с ключом уже закрыто родителем (JSON нормализованного ответа).
ALTER TABLE task_item ADD COLUMN discrepancy_ack TEXT;
-- Последнее событие disputed_key, которое родитель уже разобрал.
ALTER TABLE task_item ADD COLUMN dispute_cleared_event_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task_item ADD COLUMN unsupported_reason TEXT;
