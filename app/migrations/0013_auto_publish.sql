-- Автопубликация задач с официальным ключом (K5b, решение владельца 27.10): пометка «опубликовано
-- автоматически» (для карточки родителя и выборки K5c) и причина, по которой автоматические
-- условия не прошли. Обе колонки сбрасываются новой ревизией.
ALTER TABLE task_item ADD COLUMN auto_published INTEGER NOT NULL DEFAULT 0 CHECK (auto_published IN (0, 1));
ALTER TABLE task_item ADD COLUMN auto_hold TEXT;
