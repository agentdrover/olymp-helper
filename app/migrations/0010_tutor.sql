-- Разбор задания (OH-T06, SPEC-1.0 1.3): состояние хранится на сервере. Один прогон (run) -
-- один заход ребёнка в задание; повтор решённого - новая строка с is_repeat = 1, старые не меняются.
CREATE TABLE tutor_run (
    id INTEGER PRIMARY KEY,
    task_item_id INTEGER NOT NULL REFERENCES task_item (id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES study_session (id) ON DELETE SET NULL,
    is_repeat INTEGER NOT NULL DEFAULT 0 CHECK (is_repeat IN (0, 1)),
    state TEXT NOT NULL DEFAULT 'new' CHECK (state IN (
        'new', 'attempted_wrong', 'hint1', 'hint2', 'hint3', 'solved', 'solved_unknown',
        'answer_shown'
    )),
    opened_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    hints INTEGER NOT NULL DEFAULT 0 CHECK (hints BETWEEN 0 AND 3),
    attempts_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(attempts_json)),
    last_answer TEXT,
    last_confidence TEXT CHECK (last_confidence IS NULL OR last_confidence IN ('sure', 'doubt')),
    hints_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(hints_json)),
    analysis_json TEXT CHECK (analysis_json IS NULL OR json_valid(analysis_json)),
    explains_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(explains_json)),
    solution_json TEXT CHECK (solution_json IS NULL OR json_valid(solution_json)),
    outcome TEXT,
    closed_at TEXT
);
CREATE INDEX idx_tutor_run_task ON tutor_run (task_item_id, id);

-- Ответ модели, не совпавший с ключом: видит только родитель (SPEC 1.2).
CREATE TABLE tutor_discrepancy (
    id INTEGER PRIMARY KEY,
    task_item_id INTEGER NOT NULL REFERENCES task_item (id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES tutor_run (id) ON DELETE SET NULL,
    revision INTEGER NOT NULL,
    key_version INTEGER NOT NULL,
    model_answer TEXT NOT NULL CHECK (json_valid(model_answer)),
    explanation TEXT NOT NULL,
    uncertainty TEXT NOT NULL CHECK (uncertainty IN ('low', 'high')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_tutor_discrepancy_task ON tutor_discrepancy (task_item_id, revision, key_version);
