-- Кандидат ключа из ответа на странице, приведённый моделью (K5e1, #2134). Это не ключ: строка
-- хранит предложение, его источник и причину, ревизию задания и версию правил. Родитель делает
-- его ключом только явным подтверждением. Одна строка на задание: повтор с теми же
-- ревизией, текстом ответа и версией правил модель не вызывает.
CREATE TABLE page_answer (
    task_item_id INTEGER PRIMARY KEY REFERENCES task_item (id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,  -- текст ответа + тип + варианты; ревизия в ключ не входит
    source TEXT NOT NULL CHECK (source IN ('page_model')),
    value TEXT,
    reason TEXT NOT NULL,
    rules_version INTEGER NOT NULL,
    channel TEXT,
    model TEXT,
    created_at TEXT NOT NULL
);

-- Откуда взят ответ, который родитель подтвердил своим: ключ остаётся key_source = 'parent', а это
-- поле хранит источник совпавшего с ним кандидата (загруженный ключ, правило по странице, модель).
ALTER TABLE task_item ADD COLUMN key_candidate_source TEXT
    CHECK (key_candidate_source IS NULL OR key_candidate_source IN ('key_file', 'page', 'page_model'));
