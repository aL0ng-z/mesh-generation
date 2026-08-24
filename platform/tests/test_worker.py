"""Worker 持久队列、资源门控与终态语义测试。"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import textwrap
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import mesh_app.worker as worker_module

from mesh_app.config import Settings
from mesh_app.backup import create_backup
from mesh_app.db import Database
from mesh_app.worker import (
    LicenseBackoff,
    ResourceSnapshot,
    Worker,
    atomic_claim_run,
    build_mesh_command,
    control_snapshot_to_assignments,
    evaluate_resource_gate,
    is_license_failure,
    mark_run_succeeded,
    recover_stale_runs,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "platform" / "migrations"


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "mesh.sqlite3", MIGRATIONS_DIR, 10_000)
    database.migrate()
    return database


def _insert_session_and_runs(database: Database, count: int) -> tuple[str, list[str]]:
    session_id = str(uuid.uuid4())
    timestamp = "2026-08-06T00:00:00.000Z"
    run_ids = [str(uuid.uuid4()) for _ in range(count)]
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                id, title, source_filename, geometry_sha256,
                geometry_relative_path, geometry_summary_json, status,
                version, created_at, updated_at
            ) VALUES (?, '测试会话', 'case.geomTurbo', ?, ?, '{}', 'ACTIVE', 1, ?, ?)
            """,
            (session_id, "a" * 64, f"geometries/{session_id}/source.geomTurbo", timestamp, timestamp),
        )
        for sequence, run_id in enumerate(run_ids, start=1):
            connection.execute(
                """
                INSERT INTO runs (
                    id, session_id, sequence, request_id,
                    control_snapshot_json, control_delta_json, parse_result_json,
                    status, quality_status, quality_json, preview_status,
                    run_summary_json, progress, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '{}', 'QUEUED', 'UNKNOWN', '{}',
                          'PENDING', '{}', 0, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    sequence,
                    f"request-{sequence}",
                    json.dumps({"schema_version": 1, "items": []}),
                    json.dumps({"schema_version": 1, "items": []}),
                    timestamp,
                    timestamp,
                ),
            )
    return session_id, run_ids


def test_atomic_claim_is_concurrent_and_never_exceeds_hard_cap(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, run_ids = _insert_session_and_runs(database, 32)

    def claim(index: int) -> str | None:
        claimed = atomic_claim_run(
            database,
            f"worker-{index}",
            max_concurrency=999,
            busy_timeout_ms=10_000,
        )
        return str(claimed["id"]) if claimed else None

    with ThreadPoolExecutor(max_workers=32) as executor:
        claimed_ids = list(executor.map(claim, range(32)))

    actual = [run_id for run_id in claimed_ids if run_id is not None]
    assert len(actual) == 20
    assert len(set(actual)) == 20
    assert set(actual).issubset(run_ids)
    with database.reading() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE status = 'RUNNING'"
        ).fetchone()[0] == 20
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE status = 'QUEUED'"
        ).fetchone()[0] == 12


def test_resource_gate_reserves_next_slot_and_checks_disk() -> None:
    allowed = evaluate_resource_gate(
        available_memory_gb=13,
        free_disk_gb=20,
        running_count=1,
        memory_reservation_gb=2.5,
        min_free_memory_gb=8,
        min_free_disk_gb=20,
    )
    assert allowed.allowed is True
    assert allowed.reservation_gb == 5

    memory_blocked = evaluate_resource_gate(
        available_memory_gb=12.99,
        free_disk_gb=100,
        running_count=1,
        memory_reservation_gb=2.5,
        min_free_memory_gb=8,
        min_free_disk_gb=20,
    )
    assert memory_blocked.allowed is False
    assert memory_blocked.reason_code == "INSUFFICIENT_MEMORY"

    disk_blocked = evaluate_resource_gate(
        available_memory_gb=100,
        free_disk_gb=19.99,
        running_count=0,
    )
    assert disk_blocked.allowed is False
    assert disk_blocked.reason_code == "INSUFFICIENT_DISK"


def test_recover_stale_run_writes_failed_terminal_and_event(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, run_ids = _insert_session_and_runs(database, 2)
    old = datetime(2026, 8, 6, tzinfo=timezone.utc)
    recent = old + timedelta(seconds=25)
    assert atomic_claim_run(database, "dead-worker", now=old)["id"] == run_ids[0]
    assert atomic_claim_run(database, "live-worker", now=recent)["id"] == run_ids[1]

    recovered = recover_stale_runs(
        database,
        stale_after_seconds=30,
        now=old + timedelta(seconds=31),
    )
    assert recovered == 1
    with database.reading() as connection:
        stale = connection.execute("SELECT * FROM runs WHERE id = ?", (run_ids[0],)).fetchone()
        live = connection.execute("SELECT * FROM runs WHERE id = ?", (run_ids[1],)).fetchone()
        assert stale["status"] == "FAILED"
        assert stale["error_code"] == "WORKER_HEARTBEAT_LOST"
        assert stale["preview_status"] == "UNAVAILABLE"
        assert live["status"] == "RUNNING"
        event = connection.execute(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
            (run_ids[0],),
        ).fetchone()
        assert event["stage"] == "RECOVERY"
        assert event["level"] == "ERROR"


def test_control_snapshot_builds_stable_mesh_cli(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "mesh.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    geometry = root / "case.geomTurbo"
    geometry.write_text("GEOMETRY", encoding="utf-8")
    snapshot = {
        "schema_version": 1,
        "items": [
            {"key": "wizard/spanwise_paths", "selector": "row:#1", "value": 33},
            {"key": "configuration/grid_levels", "selector": "configuration", "value": 3},
            {"key": "row/mesh_level", "selector": "row:#1", "value": "user"},
            {"key": "row/periodicity", "selector": "row:#2", "value": True},
            {"key": "row/example_tuple", "selector": "row:#3", "value": [1, 2, 3]},
        ],
    }
    assert control_snapshot_to_assignments(snapshot) == [
        "configuration/grid_levels=3",
        "row:#1/mesh_level=user",
        "row:#1/wizard/spanwise_paths=33",
        "row:#2/periodicity=true",
        "row:#3/example_tuple=1,2,3",
    ]
    command = build_mesh_command(
        project_root=root,
        geometry_path=geometry,
        output_dir=root / "out",
        control_snapshot=snapshot,
        igg_path=root / "igg.exe",
        timeout_seconds=123,
    )
    assert command[1] == str((root / "src" / "mesh.py").resolve())
    assert command[2] == str(geometry.resolve())
    assert command.count("--set") == 5
    assert command[command.index("--timeout") + 1] == "123"


def test_license_failure_uses_bounded_consecutive_backoff() -> None:
    assert is_license_failure("FlexLM checkout failed: all licenses in use")
    assert is_license_failure("许可证签出失败，容量不足")
    assert not is_license_failure("license 文件路径配置完成")
    backoff = LicenseBackoff(threshold=2, base_seconds=10, max_seconds=25)
    assert backoff.record_license_failure(now=100) == 0
    assert backoff.blocked(now=100) is False
    assert backoff.record_license_failure(now=100) == 10
    assert backoff.remaining(now=105) == 5
    assert backoff.record_license_failure(now=105) == 20
    assert backoff.record_license_failure(now=105) == 25
    assert backoff.remaining(now=105) == 25
    backoff.record_non_license_result()
    assert backoff.blocked(now=105) is False
    assert backoff.consecutive_failures == 0


def test_worker_child_process_quality_fail_still_succeeds_and_heartbeats(tmp_path: Path) -> None:
    database = _database(tmp_path)
    session_id, run_ids = _insert_session_and_runs(database, 1)
    data_dir = tmp_path / "data"
    geometry = data_dir / "geometries" / session_id / "source.geomTurbo"
    geometry.parent.mkdir(parents=True)
    geometry.write_text("GEOMETRY", encoding="utf-8")
    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    (fake_root / "src").mkdir()
    (fake_root / "src" / "mesh.py").write_text(
        textwrap.dedent(
            """
            import argparse, json
            from pathlib import Path
            parser = argparse.ArgumentParser()
            parser.add_argument("geometry")
            parser.add_argument("--out", required=True)
            args, _ = parser.parse_known_args()
            output = Path(args.out)
            output.mkdir(parents=True, exist_ok=True)
            summary = {
                "schema_version": 3,
                "autogrid": {"outputs": {}, "returncode": 0, "error": None},
                "quality": {"result": {"status": "FAIL", "accepted": False, "reasons": ["测试阈值"]}},
            }
            (output / "run_summary.json").write_text(json.dumps(summary), encoding="utf-8")
            (output / "report.md").write_text("# 测试报告", encoding="utf-8")
            (output / "mesh.qualityReport").write_text("quality", encoding="utf-8")
            print(json.dumps(summary))
            """
        ),
        encoding="utf-8",
    )
    settings = Settings(
        project_root=fake_root,
        platform_dir=PROJECT_ROOT / "platform",
        data_dir=data_dir,
        database_path=database.path,
        migrations_dir=MIGRATIONS_DIR,
        geometry_dir=data_dir / "geometries",
        artifact_dir=data_dir / "artifacts",
        preview_dir=data_dir / "previews",
        ui_dist_dir=fake_root / "ui",
        igg_path=None,
        max_concurrency=20,
        memory_reservation_gb=2.5,
        min_free_memory_gb=8,
        min_free_disk_gb=20,
        job_timeout_seconds=10,
        busy_timeout_ms=10_000,
        worker_stale_seconds=30,
        max_upload_bytes=1024,
    )
    settings.ensure_directories()
    always_allowed = lambda count: ResourceSnapshot(
        available_memory_gb=100,
        free_disk_gb=100,
        running_count=count,
        reservation_gb=2.5 * (count + 1),
        min_free_memory_gb=8,
        min_free_disk_gb=20,
        allowed=True,
        reason_code=None,
        reason=None,
    )
    worker = Worker(
        settings,
        database=database,
        worker_id="integration-worker",
        resource_probe=always_allowed,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        worker.run_once()
        with database.reading() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_ids[0],)).fetchone()
        if row["status"] in {"SUCCEEDED", "FAILED"}:
            break
        time.sleep(0.05)
    else:
        worker.shutdown()
        pytest.fail("Worker 子进程未在期限内完成")

    assert row["status"] == "SUCCEEDED"
    assert row["quality_status"] == "FAIL"
    assert row["preview_status"] == "UNAVAILABLE"
    assert row["progress"] == 1
    with database.reading() as connection:
        heartbeat = connection.execute(
            "SELECT * FROM workers WHERE id = 'integration-worker'"
        ).fetchone()
        kinds = {
            item[0]
            for item in connection.execute(
                "SELECT kind FROM artifacts WHERE run_id = ?", (run_ids[0],)
            ).fetchall()
        }
    assert heartbeat is not None
    assert heartbeat["max_concurrency"] == 20
    assert {"RUN_SUMMARY", "REPORT", "QUALITY_REPORT", "LOG"}.issubset(kinds)


def test_post_spawn_database_failure_terminates_process_tree_and_releases_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    session_id, _run_ids = _insert_session_and_runs(database, 1)
    claimed = atomic_claim_run(database, "cleanup-worker")
    assert claimed is not None
    data_dir = tmp_path / "data"
    geometry = data_dir / "geometries" / session_id / "source.geomTurbo"
    geometry.parent.mkdir(parents=True)
    geometry.write_text("GEOMETRY", encoding="utf-8")
    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    (fake_root / "src").mkdir()
    (fake_root / "src" / "mesh.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    settings = Settings(
        project_root=fake_root,
        platform_dir=PROJECT_ROOT / "platform",
        data_dir=data_dir,
        database_path=database.path,
        migrations_dir=MIGRATIONS_DIR,
        geometry_dir=data_dir / "geometries",
        artifact_dir=data_dir / "artifacts",
        preview_dir=data_dir / "previews",
        ui_dist_dir=data_dir / "ui",
        igg_path=None,
        busy_timeout_ms=10_000,
    )
    settings.ensure_directories()

    class FakeProcess:
        pid = 12345
        uses_job_object = True
        terminated = False

        def terminate_tree(self, *, exit_code: int = 1) -> None:
            self.terminated = True

    fake_process = FakeProcess()
    captured_streams: list[object] = []

    def fake_spawn(*_args: object, **kwargs: object) -> FakeProcess:
        captured_streams.extend((kwargs["stdout"], kwargs["stderr"]))
        return fake_process

    @contextmanager
    def failing_transaction(*, immediate: bool = False):
        del immediate
        raise sqlite3.OperationalError("模拟 PID 写入失败")
        yield  # pragma: no cover

    worker = Worker(settings, database=database, worker_id="cleanup-worker")
    monkeypatch.setattr(worker_module, "spawn_managed_process", fake_spawn)
    monkeypatch.setattr(database, "transaction", failing_transaction)

    with pytest.raises(sqlite3.OperationalError, match="PID 写入失败"):
        worker._start_claimed_run(claimed)
    assert fake_process.terminated is True
    assert worker._tasks == {}
    assert captured_streams and all(stream.closed for stream in captured_streams)


def test_worker_startup_recovers_succeeded_pending_postprocess(tmp_path: Path) -> None:
    database = _database(tmp_path)
    session_id, run_ids = _insert_session_and_runs(database, 1)
    run_id = run_ids[0]
    assert atomic_claim_run(database, "crashed-worker") is not None
    summary = {
        "schema_version": 3,
        "autogrid": {"outputs": {}, "returncode": 0, "error": None},
        "quality": {"result": {"status": "PASS", "accepted": True, "reasons": []}},
    }
    mark_run_succeeded(
        database,
        run_id,
        run_summary=summary,
        quality=summary["quality"],
        quality_status="PASS",
    )
    data_dir = tmp_path / "data"
    run_dir = data_dir / "artifacts" / session_id / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    settings = Settings(
        project_root=PROJECT_ROOT,
        platform_dir=PROJECT_ROOT / "platform",
        data_dir=data_dir,
        database_path=database.path,
        migrations_dir=MIGRATIONS_DIR,
        geometry_dir=data_dir / "geometries",
        artifact_dir=data_dir / "artifacts",
        preview_dir=data_dir / "previews",
        ui_dist_dir=data_dir / "ui",
        igg_path=None,
        busy_timeout_ms=10_000,
    )
    settings.ensure_directories()

    worker = Worker(settings, database=database, worker_id="recovery-worker")
    assert worker.run_once() is True
    with database.reading() as connection:
        row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        stages = [
            event[0]
            for event in connection.execute(
                "SELECT stage FROM run_events WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
        ]
    assert row["status"] == "SUCCEEDED"
    assert row["preview_status"] == "UNAVAILABLE"
    assert "RECOVERY" in stages
    assert "PREVIEW" in stages


def test_preview_blocking_section_keeps_other_running_jobs_alive(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, run_ids = _insert_session_and_runs(database, 1)
    atomic_claim_run(database, "preview-worker", now="2026-08-06T00:00:00.000Z")
    data_dir = tmp_path / "data"
    settings = Settings(
        project_root=PROJECT_ROOT,
        platform_dir=PROJECT_ROOT / "platform",
        data_dir=data_dir,
        database_path=database.path,
        migrations_dir=MIGRATIONS_DIR,
        geometry_dir=data_dir / "geometries",
        artifact_dir=data_dir / "artifacts",
        preview_dir=data_dir / "previews",
        ui_dist_dir=data_dir / "ui",
        igg_path=None,
        max_concurrency=20,
        job_timeout_seconds=10,
        busy_timeout_ms=10_000,
        worker_stale_seconds=1,
    )
    settings.ensure_directories()
    worker = Worker(settings, database=database, worker_id="preview-worker")
    with database.reading() as connection:
        before = connection.execute(
            "SELECT heartbeat_at FROM runs WHERE id = ?", (run_ids[0],)
        ).fetchone()[0]
    with worker._heartbeats_during_preview():
        time.sleep(0.8)
    with database.reading() as connection:
        after = connection.execute(
            "SELECT heartbeat_at FROM runs WHERE id = ?", (run_ids[0],)
        ).fetchone()[0]
    assert after > before
    assert recover_stale_runs(database, stale_after_seconds=1) == 0


def test_online_backup_contains_consistent_database_and_artifact_manifest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = Database(data_dir / "mesh.sqlite3", MIGRATIONS_DIR, 10_000)
    database.migrate()
    session_id, run_ids = _insert_session_and_runs(database, 1)
    artifact = data_dir / "artifacts" / session_id / run_ids[0] / "report.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("不可变报告", encoding="utf-8")
    payload = artifact.read_bytes()
    relative = artifact.relative_to(data_dir).as_posix()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO artifacts (
                id, session_id, run_id, kind, display_name, relative_path,
                sha256, size_bytes, mime_type, created_at
            ) VALUES (?, ?, ?, 'REPORT', 'report.md', ?, ?, ?, 'text/markdown', ?)
            """,
            (
                str(uuid.uuid4()),
                session_id,
                run_ids[0],
                relative,
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                "2026-08-06T00:00:00.000Z",
            ),
        )
    result = create_backup(database.path, data_dir, data_dir / "backups")
    backup_path = Path(result["database"])
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    assert manifest["counts"]["registered_artifacts"] == 1
    assert manifest["counts"]["missing_registered"] == 0
    assert manifest["counts"]["filesystem_files"] == 1
    assert manifest["counts"]["unregistered_files"] == 0
    listed = next(item for item in manifest["files"] if item["relative_path"] == relative)
    assert listed["verification"] == "MATCH"

    second = create_backup(database.path, data_dir, data_dir / "backups")
    assert second["database"] != result["database"]
    assert second["manifest"] != result["manifest"]
