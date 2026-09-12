BEGIN IMMEDIATE;

-- 仅收敛后处理已失败却仍等待预览的遗留记录，不重算网格或质量。
INSERT INTO run_events (run_id, sequence, stage, level, progress, message, data_json, created_at)
SELECT r.id,
       COALESCE((SELECT MAX(e.sequence) FROM run_events e WHERE e.run_id = r.id), 0) + 1,
       'RECOVERY', 'ERROR', NULL,
       '后处理已失败，迁移将遗留的等待预览状态修正为失败',
       '{"postprocess_status":"FAILED","preview_status":"FAILED","reason_code":"POSTPROCESS_FAILED"}',
       strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM runs r
WHERE r.postprocess_status = 'FAILED' AND r.preview_status = 'PENDING';

UPDATE runs SET preview_status = 'FAILED', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE postprocess_status = 'FAILED' AND preview_status = 'PENDING';

INSERT INTO schema_migrations (version, name, applied_at)
VALUES (3, '0003_failed_postprocess_preview.sql', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 3;
COMMIT;
