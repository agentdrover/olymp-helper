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

-- Версия панели задания: монотонный счётчик, растёт при любой записи, меняющей то, что видит ребёнок
-- (попытка, подсказка, объяснение, чат, прогон, шаг, правка родителя, удаление сообщений). Клиент
-- применяет ответ, только если версия не меньше уже применённой. Приложение одно, профиля нет:
-- ключ - задание. Строка живёт, пока есть задание.
CREATE TABLE panel_seq (
    task_id INTEGER PRIMARY KEY REFERENCES task_item (id) ON DELETE CASCADE,
    seq INTEGER NOT NULL
);
