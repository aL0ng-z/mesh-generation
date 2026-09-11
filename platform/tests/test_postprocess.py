"""后处理独立子进程通道：登记、转换、超时、恢复、停止与单并发监督测试。"""

from __future__ import annotations

import json
import textwrap
import time
import uuid
from pathlib import Path

import pytest
import mesh_app.worker as worker_module

from mesh_app.config import Settings
from mesh_app.db import Database
from mesh_app.worker import (
    ResourceSnapshot,
    Worker,
    atomic_claim_run,
    mark_run_succeeded,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "platform" / "migrations"


class FakeProcess:
    """可控假受管进程：poll 返回预设退出码，terminate_tree 转为失败退出。"""

    pid = 12345
    uses_job_object = True

    def __init__(self, returncode: int | None = None) -> None:
        self._returncode = returncode
        self.terminated = False
        self.closed = False

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode

    def close(self) -> None:
        self.closed = True

    def terminate_tree(self, *, exit_code: int = 1) -> None:
        self.terminated = True
        if self._returncode is None:
            self._returncode = exit_code


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


def _write_geometry(data_dir: Path, session_id: str) -> Path:
    geometry = data_dir / "geometries" / session_id / "source.geomTurbo"
    geometry.parent.mkdir(parents=True, exist_ok=True)
    geometry.write_text("GEOMETRY", encoding="utf-8")
    return geometry


def _settings(
    tmp_path: Path,
    database: Database,
    data_dir: Path,
    fake_root: Path,
    *,
    postprocess_timeout_seconds: int = 600,
    job_timeout_seconds: int = 10,
) -> Settings:
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
        job_timeout_seconds=job_timeout_seconds,
        postprocess_timeout_seconds=postprocess_timeout_seconds,
        busy_timeout_ms=10_000,
        worker_stale_seconds=30,
        max_upload_bytes=1024,
    )
    settings.ensure_directories()
    return settings


def _always_allowed_probe(count: int, postprocess_active: bool = False) -> ResourceSnapshot:
    return ResourceSnapshot(
        available_memory_gb=100,
        free_disk_gb=100,
        running_count=count,
        reservation_gb=2.5 * (count + 1 + (1 if postprocess_active else 0)),
        min_free_memory_gb=8,
        min_free_disk_gb=20,
        allowed=True,
        reason_code=None,
        reason=None,
        postprocess_active=postprocess_active,
    )


def _success_summary(run_id: str) -> dict[str, object]:
    return {
        "schema_version": 4,
        "run_id": run_id,
        "created_at": "2026-08-06T00:00:00.000Z",
        "autogrid": {"outputs": {}, "returncode": 0, "error": None},
        "quality": {"result": {"status": "PASS", "accepted": True, "reasons": []}},
        "execution_evidence": {
            "completion_event": {"stage": "final", "run_id": run_id, "status": "completed"},
            "protocol_errors": [],
        },
    }


def _write_fake_mesh(fake_root: Path, *, exit_code: int) -> None:
    (fake_root / "src").mkdir(parents=True, exist_ok=True)
    script = textwrap.dedent(
        """
        import argparse, json, sys
        from pathlib import Path
        parser = argparse.ArgumentParser()
        parser.add_argument("geometry")
        parser.add_argument("--out", required=True)
        parser.add_argument("--run-id", required=True)
        args, _ = parser.parse_known_args()
        output = Path(args.out)
        output.mkdir(parents=True, exist_ok=True)
        summary = {
            "schema_version": 4,
            "run_id": args.run_id,
            "created_at": "2026-08-06T00:00:00.000Z",
            "autogrid": {"outputs": {}, "returncode": %d, "error": %s},
            "quality": {"result": {"status": "PASS", "accepted": True, "reasons": []}},
            "execution_evidence": %s,
        }
        (output / "run_summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (output / "report.md").write_text("# 测试报告", encoding="utf-8")
        raise SystemExit(%d)
        """
        % (
            exit_code,
            '"测试失败"' if exit_code else "None",
            '{"completion_event": None, "protocol_errors": [{"code": "missing_completion_event", "message": "未收到完成事件"}]}'
            if exit_code
            else '{"completion_event": {"stage": "final", "run_id": args.run_id, "status": "completed"}, "protocol_errors": []}',
            exit_code,
        )
    )
    (fake_root / "src" / "mesh.py").write_text(script, encoding="utf-8")


def _wait_until(
    worker: Worker,
    database: Database,
    run_id: str,
    predicate,
    *,
    deadline_seconds: float = 15,
    message: str = "未在期限内达到预期状态",
):
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        worker.run_once()
        with database.reading() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if predicate(row):
            return row
        time.sleep(0.02)
    worker.shutdown()
    pytest.fail(message)


def _postprocess_spawn_monkeypatch(monkeypatch: pytest.MonkeyPatch, fakes: list[FakeProcess]):
    """仅对后处理子进程替换为可控假进程；网格命令保持真实生成。"""

    real_spawn = worker_module.spawn_managed_process

    def fake_spawn(command, **kwargs):
        if len(command) >= 3 and command[2] == "mesh_app.postprocess":
            process = FakeProcess(returncode=None)
            fakes.append(process)
            return process
        return real_spawn(command, **kwargs)

    monkeypatch.setattr(worker_module, "spawn_managed_process", fake_spawn)
    return fake_spawn


def test_successful_run_postprocess_completes_and_registers_artifacts(tmp_path: Path) -> None:
    database = _database(tmp_path)
    session_id, run_ids = _insert_session_and_runs(database, 1)
    data_dir = tmp_path / "data"
    _write_geometry(data_dir, session_id)
    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    _write_fake_mesh(fake_root, exit_code=0)
    settings = _settings(tmp_path, database, data_dir, fake_root)
    worker = Worker(
        settings,
        database=database,
        worker_id="postprocess-integration",
        resource_probe=_always_allowed_probe,
    )
    row = _wait_until(
        worker,
        database,
        run_ids[0],
        lambda row: row["status"] == "SUCCEEDED" and row["postprocess_status"] == "COMPLETED",
        message="成功运行的后处理未在期限内完成",
    )
    assert row["postprocess_started_at"] is not None
    assert row["postprocess_finished_at"] is not None
    assert row["postprocess_error"] is None
    assert row["preview_status"] == "UNAVAILABLE"
    with database.reading() as connection:
        kinds = {
            item[0]
            for item in connection.execute(
                "SELECT kind FROM artifacts WHERE run_id = ?", (run_ids[0],)
            ).fetchall()
        }
        stages = {
            item[0]
            for item in connection.execute(
                "SELECT stage FROM run_events WHERE run_id = ?", (run_ids[0],)
            ).fetchall()
        }
    assert {"RUN_SUMMARY", "REPORT", "LOG"}.issubset(kinds)
    assert "POSTPROCESS" in stages
    assert (data_dir / "logs" / f"postprocess-{run_ids[0]}.log").is_file()


def test_failed_run_postprocess_still_registers_artifacts(tmp_path: Path) -> None:
    database = _database(tmp_path)
    session_id, run_ids = _insert_session_and_runs(database, 1)
    data_dir = tmp_path / "data"
    _write_geometry(data_dir, session_id)
    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    _write_fake_mesh(fake_root, exit_code=1)
    settings = _settings(tmp_path, database, data_dir, fake_root)
    worker = Worker(
        settings,
        database=database,
        worker_id="failed-postprocess",
        resource_probe=_always_allowed_probe,
    )
    row = _wait_until(
        worker,
        database,
        run_ids[0],
        lambda row: row["status"] == "FAILED" and row["postprocess_status"] == "COMPLETED",
        message="失败运行的后处理未在期限内完成",
    )
    assert row["error_code"] == "MESH_PROCESS_FAILED"
    assert row["postprocess_error"] is None
    with database.reading() as connection:
        kinds = {
            item[0]
            for item in connection.execute(
                "SELECT kind FROM artifacts WHERE run_id = ?", (run_ids[0],)
            ).fetchall()
        }
    assert {"RUN_SUMMARY", "LOG"}.issubset(kinds)


def test_postprocess_timeout_terminates_tree_and_marks_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    session_id, run_ids = _insert_session_and_runs(database, 1)
    data_dir = tmp_path / "data"
    _write_geometry(data_dir, session_id)
    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    _write_fake_mesh(fake_root, exit_code=0)
    settings = _settings(
        tmp_path, database, data_dir, fake_root, postprocess_timeout_seconds=1
    )
    fakes: list[FakeProcess] = []
    _postprocess_spawn_monkeypatch(monkeypatch, fakes)
    worker = Worker(
        settings,
        database=database,
        worker_id="timeout-worker",
        resource_probe=_always_allowed_probe,
    )
    row = _wait_until(
        worker,
        database,
        run_ids[0],
        lambda row: row["postprocess_status"] == "FAILED",
        deadline_seconds=10,
        message="后处理超时未在期限内被标记失败",
    )
    assert row["status"] == "SUCCEEDED"
    assert row["postprocess_error"] and "超时" in row["postprocess_error"]
    assert row["postprocess_finished_at"] is not None
    assert fakes and fakes[0].terminated is True


def test_recovery_requeues_pending_and_running_without_sync_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    session_id, run_ids = _insert_session_and_runs(database, 2)
    for run_id in run_ids:
        assert atomic_claim_run(database, "crashed-worker") is not None
        mark_run_succeeded(
            database,
            run_id,
            run_summary={"schema_version": 3, "autogrid": {"outputs": {}, "returncode": 0, "error": None}},
            quality={},
            quality_status="PASS",
        )
    # 模拟上次中断：第二个运行的后处理残留 RUNNING。
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE runs SET postprocess_status = 'RUNNING', postprocess_started_at = ? WHERE id = ?",
            ("2026-08-06T00:00:00.000Z", run_ids[1]),
        )
    data_dir = tmp_path / "data"
    settings = _settings(tmp_path, database, data_dir, tmp_path / "fake-project")
    spawned: list[str] = []

    def fake_spawn(command, **kwargs):
        assert len(command) >= 3 and command[2] == "mesh_app.postprocess"
        spawned.append(str(command[3]))
        return FakeProcess(returncode=None)

    monkeypatch.setattr(worker_module, "spawn_managed_process", fake_spawn)
    worker = Worker(settings, database=database, worker_id="recovery-worker")
    assert worker.run_once() is True
    with database.reading() as connection:
        rows = {row["id"]: row for row in connection.execute("SELECT * FROM runs").fetchall()}
    # 恢复把 RUNNING 重置为 PENDING 后重新排队；固定单并发只投递第一个。
    assert spawned == [run_ids[0]]
    assert rows[run_ids[0]]["postprocess_status"] == "RUNNING"
    assert rows[run_ids[1]]["postprocess_status"] == "PENDING"
    assert rows[run_ids[1]]["postprocess_started_at"] is None


def test_shutdown_terminates_postprocess_tree_and_state_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    session_id, run_ids = _insert_session_and_runs(database, 1)
    assert atomic_claim_run(database, "crashed-worker") is not None
    mark_run_succeeded(
        database,
        run_ids[0],
        run_summary={"schema_version": 3, "autogrid": {"outputs": {}, "returncode": 0, "error": None}},
        quality={},
        quality_status="PASS",
    )
    data_dir = tmp_path / "data"
    settings = _settings(tmp_path, database, data_dir, tmp_path / "fake-project")
    first = FakeProcess(returncode=None)
    monkeypatch.setattr(
        worker_module, "spawn_managed_process", lambda command, **kwargs: first
    )
    worker = Worker(settings, database=database, worker_id="stop-worker")
    assert worker.run_once() is True
    with database.reading() as connection:
        status = connection.execute(
            "SELECT postprocess_status FROM runs WHERE id = ?", (run_ids[0],)
        ).fetchone()[0]
    assert status == "RUNNING"

    worker.shutdown()
    assert first.terminated is True
    assert worker._postprocess_task is None
    # 停止保留可恢复状态：RUNNING 由下次启动恢复重新排队。
    with database.reading() as connection:
        status = connection.execute(
            "SELECT postprocess_status FROM runs WHERE id = ?", (run_ids[0],)
        ).fetchone()[0]
    assert status == "RUNNING"

    second = FakeProcess(returncode=None)
    monkeypatch.setattr(
        worker_module, "spawn_managed_process", lambda command, **kwargs: second
    )
    restarted = Worker(settings, database=database, worker_id="restart-worker")
    restarted.run_once()
    with database.reading() as connection:
        row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_ids[0],)).fetchone()
    assert row["postprocess_status"] == "RUNNING"
    assert row["postprocess_started_at"] is not None


def test_main_loop_supervises_mesh_deadline_while_postprocess_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    session_id, run_ids = _insert_session_and_runs(database, 2)
    data_dir = tmp_path / "data"
    _write_geometry(data_dir, session_id)
    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    _write_fake_mesh(fake_root, exit_code=0)
    settings = _settings(tmp_path, database, data_dir, fake_root, job_timeout_seconds=1)
    postprocess_procs: list[FakeProcess] = []
    mesh_procs: dict[str, FakeProcess] = {}

    def fake_spawn(command, **kwargs):
        if len(command) >= 3 and command[2] == "mesh_app.postprocess":
            process = FakeProcess(returncode=None)
            postprocess_procs.append(process)
            return process
        run_id = str(command[command.index("--run-id") + 1])
        out_dir = Path(str(command[command.index("--out") + 1]))
        if run_id == run_ids[0]:
            # A 立即成功，其后处理子进程则一直挂起。
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "run_summary.json").write_text(
                json.dumps(_success_summary(run_id)), encoding="utf-8"
            )
            process = FakeProcess(returncode=0)
        else:
            # B 的网格进程一直挂起，必须按外层期限被终止。
            process = FakeProcess(returncode=None)
        mesh_procs[run_id] = process
        return process

    monkeypatch.setattr(worker_module, "spawn_managed_process", fake_spawn)
    worker = Worker(
        settings,
        database=database,
        worker_id="deadline-worker",
        resource_probe=_always_allowed_probe,
    )
    b = _wait_until(
        worker,
        database,
        run_ids[1],
        lambda row: row["status"] == "FAILED",
        deadline_seconds=10,
        message="后处理运行期间主循环未按外层期限终止 B",
    )
    with database.reading() as connection:
        a = connection.execute("SELECT * FROM runs WHERE id = ?", (run_ids[0],)).fetchone()
    assert b["error_code"] == "MESH_TIMEOUT"
    assert a["status"] == "SUCCEEDED"
    assert a["postprocess_status"] == "RUNNING"
    assert b["postprocess_status"] == "PENDING"
    assert len(postprocess_procs) == 1
    assert mesh_procs[run_ids[1]].terminated is True


def test_postprocess_runs_single_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    session_id, run_ids = _insert_session_and_runs(database, 2)
    data_dir = tmp_path / "data"
    _write_geometry(data_dir, session_id)
    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    _write_fake_mesh(fake_root, exit_code=0)
    settings = _settings(tmp_path, database, data_dir, fake_root)
    postprocess_procs: list[FakeProcess] = []

    def fake_spawn(command, **kwargs):
        if len(command) >= 3 and command[2] == "mesh_app.postprocess":
            process = FakeProcess(returncode=None)
            postprocess_procs.append(process)
            return process
        run_id = str(command[command.index("--run-id") + 1])
        out_dir = Path(str(command[command.index("--out") + 1]))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "run_summary.json").write_text(
            json.dumps(_success_summary(run_id)), encoding="utf-8"
        )
        return FakeProcess(returncode=0)

    monkeypatch.setattr(worker_module, "spawn_managed_process", fake_spawn)
    worker = Worker(
        settings,
        database=database,
        worker_id="single-worker",
        resource_probe=_always_allowed_probe,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        worker.run_once()
        with database.reading() as connection:
            rows = {row["id"]: row for row in connection.execute("SELECT * FROM runs").fetchall()}
        if all(rows[run_id]["status"] == "SUCCEEDED" for run_id in run_ids):
            break
        time.sleep(0.02)
    else:
        worker.shutdown()
        pytest.fail("网格任务未在期限内成功")

    # 两个网格任务都成功后，同一时刻最多只有一个后处理子进程。
    assert len(postprocess_procs) == 1
    assert postprocess_procs[0].terminated is False
    with database.reading() as connection:
        b = connection.execute("SELECT * FROM runs WHERE id = ?", (run_ids[1],)).fetchone()
    assert b["postprocess_status"] == "PENDING"

    # 第一个后处理退出后，第二个才被投递。
    postprocess_procs[0]._returncode = 0
    worker.run_once()
    assert len(postprocess_procs) == 2
    with database.reading() as connection:
        b = connection.execute("SELECT * FROM runs WHERE id = ?", (run_ids[1],)).fetchone()
    assert b["postprocess_status"] == "RUNNING"


def test_resource_probe_includes_active_postprocess_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    session_id, run_ids = _insert_session_and_runs(database, 1)
    data_dir = tmp_path / "data"
    _write_geometry(data_dir, session_id)
    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    _write_fake_mesh(fake_root, exit_code=0)
    settings = _settings(tmp_path, database, data_dir, fake_root)
    fakes: list[FakeProcess] = []
    _postprocess_spawn_monkeypatch(monkeypatch, fakes)
    probe_calls: list[tuple[int, bool]] = []

    def probe(count: int, postprocess_active: bool) -> ResourceSnapshot:
        probe_calls.append((count, postprocess_active))
        return _always_allowed_probe(count, postprocess_active)

    worker = Worker(
        settings,
        database=database,
        worker_id="gate-worker",
        resource_probe=probe,
    )
    _wait_until(
        worker,
        database,
        run_ids[0],
        lambda row: row["status"] == "SUCCEEDED",
        deadline_seconds=10,
        message="网格任务未在期限内成功",
    )
    worker.run_once()
    assert probe_calls
    assert any(active for _count, active in probe_calls)
