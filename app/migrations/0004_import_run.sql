-- Владелец запуска импорта: результат пишет только запуск с текущим run_id (SPEC 1.7).
ALTER TABLE material ADD COLUMN run_id TEXT;
