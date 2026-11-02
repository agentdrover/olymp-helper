-- Шаг с моделью (OH-T06b): подсказка, разбор ошибки, объяснение, чат идут в фоне. Строка хранит
-- время старта и итог, чтобы панель после перезагрузки или на другом устройстве видела тот же шаг.
CREATE TABLE tutor_step (
    id INTEGER PRIMARY KEY,
    task_item_id INTEGER NOT NULL REFERENCES task_item (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'failed')),
    -- Что показать в панели по окончании: down, limit, no_hints... (done) и пояснение ребёнку.
    panel_state TEXT,
    notice TEXT,
    error TEXT
);
CREATE INDEX idx_tutor_step_task ON tutor_step (task_item_id, id);

-- Версия панели: ОДИН глобальный монотонный счётчик (одна строка, без ключей на задания: он не
-- откатывается ни при удалении материала, ни при повторном использовании id). Каждый писатель, меняющий
-- то, что видит ребёнок, растит его в ТОЙ ЖЕ транзакции, что и запись. Клиент применяет ответ, только
-- если версия не меньше уже применённой.
CREATE TABLE panel_seq (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    seq INTEGER NOT NULL
);
INSERT INTO panel_seq (id, seq) VALUES (1, 0);
