-- Ответ со страницы задания (кандидат ключа) и заметка родителю («Элементы ответа», баллы).
ALTER TABLE task_item ADD COLUMN answer_text TEXT;
ALTER TABLE task_item ADD COLUMN parent_note TEXT;
-- Тип «число». CHECK нельзя изменить через ALTER, а пересоздание task_item при включённых
-- внешних ключах каскадом удалило бы ревизии и обнулило события. Поэтому правится текст схемы
-- (способ из документации SQLite для смены ограничений); RESET заставляет перечитать схему.
PRAGMA writable_schema = ON;
UPDATE sqlite_master
SET sql = replace(
    sql,
    'type IN (''single'', ''multiple'', ''match'', ''open'')',
    'type IN (''single'', ''multiple'', ''match'', ''open'', ''numeric'')'
)
WHERE type = 'table' AND name = 'task_item';
PRAGMA writable_schema = RESET;
