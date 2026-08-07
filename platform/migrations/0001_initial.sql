BEGIN IMMEDIATE;

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    expert_name TEXT,
    source_filename TEXT NOT NULL,
    geometry_sha256 TEXT NOT NULL CHECK (length(geometry_sha256) = 64),
    geometry_relative_path TEXT NOT NULL UNIQUE,
    geometry_summary_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'COMPLETED')),
    satisfied_run_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (satisfied_run_id) REFERENCES runs(id)
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    parent_run_id TEXT,
    retry_of_run_id TEXT,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    request_id TEXT NOT NULL CHECK (length(trim(request_id)) > 0),
    control_snapshot_json TEXT NOT NULL,
    control_delta_json TEXT NOT NULL,
    parse_result_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    quality_status TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (quality_status IN ('PASS', 'WARN', 'FAIL', 'UNKNOWN')),
    quality_json TEXT NOT NULL,
    preview_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (preview_status IN ('PENDING', 'READY', 'UNAVAILABLE', 'FAILED')),
    run_summary_json TEXT NOT NULL,
    experience_note TEXT NOT NULL DEFAULT '',
    note_version INTEGER NOT NULL DEFAULT 0 CHECK (note_version >= 0),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
    error_code TEXT,
    error_message TEXT,
    pid INTEGER,
    worker_id TEXT,
    heartbeat_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (parent_run_id) REFERENCES runs(id),
    FOREIGN KEY (retry_of_run_id) REFERENCES runs(id),
    UNIQUE (session_id, sequence),
    UNIQUE (session_id, request_id)
);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT,
    kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    mime_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    stage TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
    progress REAL CHECK (progress IS NULL OR (progress >= 0 AND progress <= 1)),
    message TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id),
    UNIQUE (run_id, sequence)
);

CREATE TABLE workers (
    id TEXT PRIMARY KEY,
    heartbeat_at TEXT NOT NULL,
    max_concurrency INTEGER NOT NULL CHECK (max_concurrency BETWEEN 1 AND 20),
    running_count INTEGER NOT NULL CHECK (running_count BETWEEN 0 AND 20),
    resource_status_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_sessions_list ON sessions (created_at DESC, id DESC);
CREATE INDEX idx_sessions_status ON sessions (status, created_at DESC, id DESC);
CREATE INDEX idx_runs_session_tree ON runs (session_id, sequence);
CREATE INDEX idx_runs_queue ON runs (status, created_at, sequence);
CREATE INDEX idx_runs_worker ON runs (worker_id, status);
CREATE INDEX idx_run_events_incremental ON run_events (run_id, sequence);
CREATE INDEX idx_artifacts_run ON artifacts (run_id, created_at);
CREATE INDEX idx_workers_heartbeat ON workers (heartbeat_at);

CREATE TRIGGER sessions_status_transition
BEFORE UPDATE OF status ON sessions
WHEN OLD.status <> NEW.status
 AND NOT (OLD.status = 'ACTIVE' AND NEW.status = 'COMPLETED')
BEGIN
    SELECT RAISE(ABORT, '非法会话状态转换');
END;

CREATE TRIGGER runs_status_transition
BEFORE UPDATE OF status ON runs
WHEN OLD.status <> NEW.status
 AND NOT (
     (OLD.status = 'QUEUED' AND NEW.status = 'RUNNING')
     OR (OLD.status = 'RUNNING' AND NEW.status IN ('SUCCEEDED', 'FAILED'))
 )
BEGIN
    SELECT RAISE(ABORT, '非法运行状态转换');
END;

CREATE TRIGGER terminal_run_core_immutable
BEFORE UPDATE ON runs
WHEN OLD.status IN ('SUCCEEDED', 'FAILED') AND (
    OLD.session_id <> NEW.session_id
    OR OLD.parent_run_id IS NOT NEW.parent_run_id
    OR OLD.retry_of_run_id IS NOT NEW.retry_of_run_id
    OR OLD.sequence <> NEW.sequence
    OR OLD.request_id <> NEW.request_id
    OR OLD.control_snapshot_json <> NEW.control_snapshot_json
    OR OLD.control_delta_json <> NEW.control_delta_json
    OR OLD.parse_result_json <> NEW.parse_result_json
    OR OLD.status <> NEW.status
    OR OLD.quality_status <> NEW.quality_status
    OR OLD.quality_json <> NEW.quality_json
    OR OLD.run_summary_json <> NEW.run_summary_json
    OR OLD.progress <> NEW.progress
    OR OLD.error_code IS NOT NEW.error_code
    OR OLD.error_message IS NOT NEW.error_message
    OR OLD.pid IS NOT NEW.pid
    OR OLD.worker_id IS NOT NEW.worker_id
    OR OLD.heartbeat_at IS NOT NEW.heartbeat_at
    OR OLD.started_at IS NOT NEW.started_at
    OR OLD.finished_at IS NOT NEW.finished_at
    OR OLD.created_at <> NEW.created_at
 )
BEGIN
    SELECT RAISE(ABORT, '终态运行的核心数据不可修改');
END;

CREATE TRIGGER runs_append_only_delete
BEFORE DELETE ON runs
BEGIN
    SELECT RAISE(ABORT, '运行记录只允许追加');
END;

CREATE TRIGGER artifacts_append_only_update
BEFORE UPDATE ON artifacts
BEGIN
    SELECT RAISE(ABORT, '产物记录只允许追加');
END;

CREATE TRIGGER artifacts_append_only_delete
BEFORE DELETE ON artifacts
BEGIN
    SELECT RAISE(ABORT, '产物记录只允许追加');
END;

CREATE TRIGGER run_events_append_only_update
BEFORE UPDATE ON run_events
BEGIN
    SELECT RAISE(ABORT, '运行事件只允许追加');
END;

CREATE TRIGGER run_events_append_only_delete
BEFORE DELETE ON run_events
BEGIN
    SELECT RAISE(ABORT, '运行事件只允许追加');
END;

INSERT INTO schema_migrations (version, name, applied_at)
VALUES (1, '0001_initial.sql', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 1;
COMMIT;
