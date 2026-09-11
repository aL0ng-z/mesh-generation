BEGIN IMMEDIATE;

-- 后处理进入独立子进程后，为每个运行单独记录后处理状态与起止时间。
ALTER TABLE runs ADD COLUMN postprocess_status TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (postprocess_status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED'));
ALTER TABLE runs ADD COLUMN postprocess_started_at TEXT;
ALTER TABLE runs ADD COLUMN postprocess_finished_at TEXT;
ALTER TABLE runs ADD COLUMN postprocess_error TEXT;

-- 迁移不批量重新散列或转换已完成的历史运行：仍等待预览的行保留排队，
-- 其余历史行视为已完成。
UPDATE runs SET postprocess_status = 'COMPLETED' WHERE preview_status <> 'PENDING';

INSERT INTO schema_migrations (version, name, applied_at)
VALUES (2, '0002_postprocess_status.sql', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 2;
COMMIT;
