-- Раздел, вариант и этап - свойства ЗАДАНИЯ, а не файла: в одном сборнике номера повторяются
-- по разделам, и ключ привязывается к (раздел, номер).
ALTER TABLE task_item ADD COLUMN section TEXT;
ALTER TABLE task_item ADD COLUMN stage TEXT;
CREATE UNIQUE INDEX idx_task_item_section_number
    ON task_item (material_id, COALESCE(section, ''), source_number);
