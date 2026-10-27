-- Имитация олимпиады (OH-K3a): формат этапа в настройках родителя, прохождение со снимком,
-- ответы и интервалы просмотра. Чисел официального формата здесь нет: их вводит родитель.
CREATE TABLE exam_format (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    stage TEXT NOT NULL CHECK (stage IN ('school', 'municipal', 'regional', 'final')),
    grade INTEGER NOT NULL CHECK (grade BETWEEN 5 AND 11),
    minutes INTEGER NOT NULL CHECK (minutes BETWEEN 1 AND 1440),
    count_single INTEGER NOT NULL DEFAULT 0 CHECK (count_single BETWEEN 0 AND 200),
    count_multiple INTEGER NOT NULL DEFAULT 0 CHECK (count_multiple BETWEEN 0 AND 200),
    count_match INTEGER NOT NULL DEFAULT 0 CHECK (count_match BETWEEN 0 AND 200),
    count_numeric INTEGER NOT NULL DEFAULT 0 CHECK (count_numeric BETWEEN 0 AND 200),
    points_single REAL NOT NULL DEFAULT 0 CHECK (points_single >= 0),
    points_multiple REAL NOT NULL DEFAULT 0 CHECK (points_multiple >= 0),
    points_match REAL NOT NULL DEFAULT 0 CHECK (points_match >= 0),
    points_numeric REAL NOT NULL DEFAULT 0 CHECK (points_numeric >= 0),
    -- Материалы, из которых собирается вариант «по формату» (JSON-список id).
    material_ids TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(material_ids)),
    updated_at TEXT NOT NULL
);

-- Одно прохождение. snapshot - формат на момент старта (минуты, баллы по типам, этап, класс).
CREATE TABLE exam_attempt (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('real', 'format')),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'submitted')),
    end_reason TEXT CHECK (end_reason IS NULL OR end_reason IN ('manual', 'time')),
    started_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    submitted_at TEXT,
    snapshot TEXT NOT NULL CHECK (json_valid(snapshot)),
    max_score REAL NOT NULL,
    score REAL,
    correct_count INTEGER,
    -- Ревизия бланка; принятые действия - в exam_action.
    rev INTEGER NOT NULL DEFAULT 0,
    CHECK ((status = 'submitted') = (end_reason IS NOT NULL))
);
-- У ребёнка не больше одного активного прохождения.
CREATE UNIQUE INDEX idx_exam_attempt_running ON exam_attempt (status) WHERE status = 'running';

-- Задание прохождения: состав, порядок, условие, ключ и баллы зафиксированы при старте;
-- answer - нормализованный ответ ребёнка (JSON), marked - отметка «вернусь».
CREATE TABLE exam_item (
    attempt_id INTEGER NOT NULL REFERENCES exam_attempt (id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 1),
    task_item_id INTEGER REFERENCES task_item (id) ON DELETE SET NULL,
    task_type TEXT NOT NULL CHECK (task_type IN ('single', 'multiple', 'match', 'numeric')),
    points REAL NOT NULL,
    snapshot TEXT NOT NULL CHECK (json_valid(snapshot)),
    -- Рисунок условия на момент старта: правка задания его не меняет.
    figure_png BLOB,
    answer TEXT CHECK (answer IS NULL OR json_valid(answer)),
    marked INTEGER NOT NULL DEFAULT 0 CHECK (marked IN (0, 1)),
    is_correct INTEGER CHECK (is_correct IS NULL OR is_correct IN (0, 1)),
    earned REAL,
    PRIMARY KEY (attempt_id, position)
);
CREATE INDEX idx_exam_item_task ON exam_item (task_item_id);

-- Интервалы просмотра задания; открытый интервал - closed_at IS NULL.
CREATE TABLE exam_view (
    id INTEGER PRIMARY KEY,
    attempt_id INTEGER NOT NULL REFERENCES exam_attempt (id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX idx_exam_view_attempt ON exam_view (attempt_id, position);

-- Принятые действия бланка: повтор любого принятого идентификатора возвращает тот же результат.
CREATE TABLE exam_action (
    attempt_id INTEGER NOT NULL REFERENCES exam_attempt (id) ON DELETE CASCADE,
    action_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    PRIMARY KEY (attempt_id, action_id)
);
