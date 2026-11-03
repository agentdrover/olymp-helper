-- Очередь родителя (OH-K5c, решение владельца 27.10): исход конвейера составленного задания
-- (прошло / не прошло, ворота и причина), выборка прошедших родителю (pending -> ok | error;
-- порядок проверок - sample_review_no) и «Снять задание» (withdrawn_at: ребёнок задание не
-- видит, события ребёнка остаются).
ALTER TABLE task_item ADD COLUMN withdrawn_at TEXT;
ALTER TABLE task_item ADD COLUMN pipeline_outcome TEXT CHECK (pipeline_outcome IS NULL OR pipeline_outcome IN ('passed', 'failed'));
ALTER TABLE task_item ADD COLUMN pipeline_gate TEXT;
ALTER TABLE task_item ADD COLUMN pipeline_reason TEXT;
ALTER TABLE task_item ADD COLUMN sample_state TEXT CHECK (sample_state IS NULL OR sample_state IN ('pending', 'ok', 'error'));
ALTER TABLE task_item ADD COLUMN sample_review_no INTEGER;
-- Задания, уже прошедшие конвейер до этой миграции: исход берётся из признаков K5a.
UPDATE task_item SET pipeline_outcome = 'passed' WHERE pack_id IS NOT NULL AND auto_checked = 1;
UPDATE task_item SET pipeline_outcome = 'failed', pipeline_gate = 'other',
    pipeline_reason = rtrim(substr(review_note, instr(review_note, 'Не опубликовано без вас: ') + length('Не опубликовано без вас: ')), '.')
    WHERE pack_id IS NOT NULL AND review_note LIKE '%Не опубликовано без вас: %';
