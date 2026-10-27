-- Сверка сгенерированного вопроса второй моделью (G3): результат хранится с заданием.
-- crosscheck - JSON {status, channel, answer, value, reasoning, invalid}; crosscheck_seen - родитель
-- открыл довод расхождения (без этого подтвердить задание нельзя).
ALTER TABLE task_item ADD COLUMN crosscheck TEXT CHECK (crosscheck IS NULL OR json_valid(crosscheck));
ALTER TABLE task_item ADD COLUMN crosscheck_seen INTEGER NOT NULL DEFAULT 0 CHECK (crosscheck_seen IN (0, 1));
