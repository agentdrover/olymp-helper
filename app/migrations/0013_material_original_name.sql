-- Исходное имя загруженного файла; у старых строк NULL.
ALTER TABLE material ADD COLUMN original_name TEXT;
