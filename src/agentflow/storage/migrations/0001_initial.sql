CREATE TABLE workflow_runs (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 4000),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'planning', 'plan_review', 'awaiting_approval', 'executing',
        'validating', 'paused', 'succeeded', 'failed', 'cancelled'
    )),
    planner_kind TEXT NOT NULL CHECK (planner_kind IN ('ollama', 'rule_based')),
    plan_json TEXT,
    source_run_id TEXT REFERENCES workflow_runs(id) ON DELETE SET NULL,
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) <= 100),
    error_summary TEXT CHECK (error_summary IS NULL OR length(error_summary) <= 4000),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE workflow_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_key TEXT NOT NULL CHECK (length(step_key) BETWEEN 1 AND 100),
    position INTEGER NOT NULL CHECK (position >= 0),
    tool_name TEXT NOT NULL CHECK (length(tool_name) BETWEEN 1 AND 100),
    arguments_json TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    approval_required INTEGER NOT NULL CHECK (approval_required IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'ready', 'running', 'succeeded', 'failed', 'skipped', 'blocked', 'cancelled'
    )),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (run_id, step_key),
    UNIQUE (run_id, position),
    UNIQUE (id, run_id)
);

CREATE TABLE step_attempts (
    id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    input_hash TEXT CHECK (input_hash IS NULL OR length(input_hash) <= 128),
    output_json TEXT,
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) <= 100),
    error_summary TEXT CHECK (error_summary IS NULL OR length(error_summary) <= 4000),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (step_id, attempt_number)
);

CREATE TABLE approval_requests (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    plan_digest TEXT NOT NULL CHECK (length(plan_digest) BETWEEN 1 AND 128),
    decision_note TEXT CHECK (decision_note IS NULL OR length(decision_note) <= 4000),
    requested_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (step_id, run_id) REFERENCES workflow_steps(id, run_id) ON DELETE CASCADE
);

CREATE TABLE run_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 100),
    details_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE workflow_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(name) BETWEEN 1 AND 200),
    description TEXT NOT NULL CHECK (length(description) <= 2000),
    plan_json TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE INDEX idx_workflow_runs_history ON workflow_runs(created_at DESC, id DESC);
CREATE INDEX idx_workflow_runs_status_history ON workflow_runs(status, created_at DESC, id DESC);
CREATE INDEX idx_workflow_steps_run_position ON workflow_steps(run_id, position);
CREATE INDEX idx_workflow_steps_status ON workflow_steps(status, run_id);
CREATE INDEX idx_step_attempts_step_number ON step_attempts(step_id, attempt_number);
CREATE INDEX idx_approval_requests_run_status ON approval_requests(run_id, status);
CREATE INDEX idx_run_events_run_time ON run_events(run_id, occurred_at, id);
CREATE INDEX idx_workflow_templates_active ON workflow_templates(archived_at, name COLLATE NOCASE);
