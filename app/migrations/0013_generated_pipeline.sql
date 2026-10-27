-- Конвейер составленных заданий (OH-K5a): задание, прошедшее автора, критика, редактора и все
-- проверки кода, публикуется без родителя и несёт пометку «проверено двумя моделями».
-- auto_checked = 1 только у такого задания; подтверждение или правка родителя сбрасывает признак.
ALTER TABLE task_item ADD COLUMN auto_checked INTEGER NOT NULL DEFAULT 0 CHECK (auto_checked IN (0, 1));
ALTER TABLE question_pack ADD COLUMN published_count INTEGER NOT NULL DEFAULT 0 CHECK (published_count >= 0);
