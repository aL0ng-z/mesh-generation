"""样本资格（共享契约 C）与入队来源快照测试。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from mesh_app.config import Settings
from mesh_app.db import Database
from mesh_app.sessions import SessionService, utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_database(tmp_path: Path) -> tuple[Settings, Database]:
    settings = Settings.from_env(
        {"MESH_DATA_DIR": str(tmp_path / "data")}, project_root=PROJECT_ROOT
    )
    settings.ensure_directories()
    database = Database(settings.database_path, settings.migrations_dir, settings.busy_timeout_ms)
    database.migrate()
    return settings, database


def create_session(service: SessionService) -> dict[str, Any]:
    relative = "geometries/session/source.geomTurbo"
    return service.create_session(
        title="资格测试",
        expert_name=None,
        source_filename="case.geomTurbo",
        geometry_sha256=hashlib.sha256(b"geometry").hexdigest(),
        geometry_relative_path=relative,
        geometry_summary={
            "path": relative,
            "version": "5.6",
            "units": "METER",
            "units_factor": 1.0,
            "row_count": 1,
            "rows": [{"name": "Rotor", "periodicity": 36, "blades": []}],
            "multi_row": False,
            "has_splitter": False,
            "has_tip_gap": False,
        },
    )


def transition(database: Database, run_id: str, status: str) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), run_id),
        )


def v4_summary(
    run_id: str,
    *,
    quality_status: str = "PASS",
    git_commit: str | None = "a" * 40,
    protocol_errors: list[dict[str, str]] | None = None,
    completion: bool = True,
    verification_status: str = "NOT_REQUESTED",
    quality_validation_status: str = "VALID",
    files: list[dict[str, str]] | None = None,
    registry_signature: str | None = "c" * 64,
    outputs: tuple[str, ...] = ("mesh.igg", "mesh.cgns"),
) -> dict[str, Any]:
    completion_event = (
        {"stage": "final", "run_id": run_id, "status": "completed"} if completion else None
    )
    return {
        "schema_version": 4,
        "run_id": run_id,
        "created_at": "2026-08-06T00:00:00.000Z",
        "autogrid": {"outputs": {}, "returncode": 0, "error": None},
        "quality": {
            "result": {"status": quality_status, "accepted": quality_status != "FAIL", "reasons": []}
        },
        "quality_validation": {"status": quality_validation_status, "reasons": []},
        "controls": {"verification": {"status": verification_status, "results": []}},
        "execution_evidence": {
            "completion_event": completion_event,
            "protocol_errors": protocol_errors or [],
        },
        "sources": {
            "git": {"commit": git_commit, "dirty": False},
            "source_signature": {
                "algorithm": "sha256",
                "files": files if files is not None else [{"path": "src/mesh.py", "sha256": "b" * 64}],
            },
            "control_registry": {"signature": registry_signature, "key_count": 1},
        },
        "manifest": {
            "stages_completed": ["generation"],
            "outputs": [
                {"relative_path": name, "size_bytes": 1, "sha256": "d" * 64} for name in outputs
            ],
        },
    }


def succeed_with_summary(
    database: Database,
    run_id: str,
    summary: dict[str, Any],
    *,
    quality_status: str = "PASS",
) -> None:
    transition(database, run_id, "RUNNING")
    quality = summary.get("quality") if isinstance(summary.get("quality"), dict) else {}
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE runs SET status = 'SUCCEEDED', quality_status = ?, quality_json = ?,
                run_summary_json = ?, progress = 1, finished_at = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING'
            """,
            (
                quality_status,
                json.dumps(quality, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
                utc_now(),
                utc_now(),
                run_id,
            ),
        )


def register_artifact(
    database: Database,
    session_id: str,
    run_id: str,
    relative_name: str,
) -> None:
    relative = f"artifacts/{session_id}/{run_id}/{relative_name}"
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO artifacts (
                id, session_id, run_id, kind, display_name, relative_path,
                sha256, size_bytes, mime_type, created_at
            ) VALUES (?, ?, ?, 'IGG', ?, ?, ?, 1, 'application/octet-stream', ?)
            """,
            (
                str(uuid.uuid4()),
                session_id,
                run_id,
                relative_name,
                relative,
                "d" * 64,
                utc_now(),
            ),
        )


def enqueue_event_data(database: Database, run_id: str) -> dict[str, Any] | None:
    with database.reading() as connection:
        row = connection.execute(
            "SELECT data_json FROM run_events WHERE run_id = ? AND stage = 'ENQUEUE_SOURCE'",
            (run_id,),
        ).fetchone()
    return json.loads(row["data_json"]) if row is not None else None


def test_v3_summary_is_readable_but_not_eligible(tmp_path: Path) -> None:
    _settings, database = make_database(tmp_path)
    service = SessionService(database)
    detail = create_session(service)
    run_id = str(detail["runs"][0]["id"])
    v3 = {
        "schema_version": 3,
        "autogrid": {"outputs": {}, "returncode": 0, "error": None},
        "quality": {"result": {"status": "PASS", "accepted": True, "reasons": []}},
    }
    succeed_with_summary(database, run_id, v3)
    result = service.get_run(run_id)
    assert result["run_summary"]["schema_version"] == 3
    assert result["sample_eligibility"] == {"eligible": False, "reasons": ["新证据缺失"]}


def test_v4_complete_evidence_is_eligible(tmp_path: Path) -> None:
    _settings, database = make_database(tmp_path)
    service = SessionService(database)
    detail = create_session(service)
    run_id = str(detail["runs"][0]["id"])
    succeed_with_summary(database, run_id, v4_summary(run_id))
    register_artifact(database, str(detail["id"]), run_id, "mesh.igg")
    register_artifact(database, str(detail["id"]), run_id, "mesh.cgns")
    result = service.get_run(run_id)
    assert result["sample_eligibility"] == {"eligible": True, "reasons": []}


def test_quality_fail_remains_eligible(tmp_path: Path) -> None:
    _settings, database = make_database(tmp_path)
    service = SessionService(database)
    detail = create_session(service)
    run_id = str(detail["runs"][0]["id"])
    succeed_with_summary(
        database, run_id, v4_summary(run_id, quality_status="FAIL"), quality_status="FAIL"
    )
    register_artifact(database, str(detail["id"]), run_id, "mesh.igg")
    register_artifact(database, str(detail["id"]), run_id, "mesh.cgns")
    result = service.get_run(run_id)
    assert result["sample_eligibility"] == {"eligible": True, "reasons": []}


def test_v4_incomplete_evidence_lists_one_reason_per_failure(tmp_path: Path) -> None:
    _settings, database = make_database(tmp_path)
    service = SessionService(database)
    detail = create_session(service)
    run_id = str(detail["runs"][0]["id"])
    summary = v4_summary(
        run_id,
        completion=False,
        protocol_errors=[{"code": "missing_completion_event", "message": "未收到完成事件"}],
        verification_status="INCOMPLETE",
        quality_validation_status="INVALID",
        git_commit=None,
        files=[],
        registry_signature=None,
        outputs=("mesh.igg", "unregistered.bin"),
    )
    succeed_with_summary(database, run_id, summary)
    register_artifact(database, str(detail["id"]), run_id, "mesh.igg")
    result = service.get_run(run_id)
    eligibility = result["sample_eligibility"]
    assert eligibility["eligible"] is False
    assert "完成事件缺失、损坏或与运行身份不一致" in eligibility["reasons"]
    assert "执行证据存在协议错误" in eligibility["reasons"]
    assert "控制验证不充分（INCOMPLETE）" in eligibility["reasons"]
    assert "质量数据校验不可判定（INVALID）" in eligibility["reasons"]
    assert "缺少执行时的 git 提交来源" in eligibility["reasons"]
    assert "缺少执行源码签名" in eligibility["reasons"]
    assert "缺少控制注册表签名" in eligibility["reasons"]
    assert "产物未全部登记为平台产物：unregistered.bin" in eligibility["reasons"]


def test_enqueue_execution_source_drift_marks_ineligible(tmp_path: Path) -> None:
    _settings, database = make_database(tmp_path)
    service = SessionService(database)
    detail = create_session(service)
    run_id = str(detail["runs"][0]["id"])
    enqueue = {
        "git_commit": "1" * 40,
        "control_registry_signature": "2" * 64,
        "source_files": {"src/mesh.py": "3" * 64},
    }
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO run_events (
                run_id, sequence, stage, level, progress, message, data_json, created_at
            ) VALUES (?, 1, 'ENQUEUE_SOURCE', 'INFO', NULL, '运行入队来源快照', ?, ?)
            """,
            (run_id, json.dumps(enqueue, ensure_ascii=False), utc_now()),
        )
    summary = v4_summary(
        run_id,
        git_commit="9" * 40,
        registry_signature="8" * 64,
        files=[{"path": "src/mesh.py", "sha256": "7" * 64}],
    )
    succeed_with_summary(database, run_id, summary)
    register_artifact(database, str(detail["id"]), run_id, "mesh.igg")
    register_artifact(database, str(detail["id"]), run_id, "mesh.cgns")
    result = service.get_run(run_id)
    eligibility = result["sample_eligibility"]
    assert eligibility["eligible"] is False
    drift = [reason for reason in eligibility["reasons"] if reason.startswith("来源漂移")]
    assert len(drift) == 3
    assert any("git 提交不一致" in reason for reason in drift)
    assert any("控制注册表签名不一致" in reason for reason in drift)
    assert any("src/mesh.py" in reason for reason in drift)


def test_child_and_retry_runs_record_enqueue_source_snapshot(tmp_path: Path) -> None:
    _settings, database = make_database(tmp_path)
    service = SessionService(database, project_root=PROJECT_ROOT)
    detail = create_session(service)
    baseline_id = str(detail["runs"][0]["id"])

    baseline_source = enqueue_event_data(database, baseline_id)
    assert baseline_source is not None
    assert isinstance(baseline_source.get("source_files"), dict)
    assert re.fullmatch(r"[0-9a-f]{64}", baseline_source["source_files"].get("src/mesh.py") or "")
    assert re.fullmatch(r"[0-9a-f]{64}", baseline_source.get("control_registry_signature") or "")
    assert baseline_source.get("git_commit") is None or re.fullmatch(
        r"[0-9a-f]{40}", baseline_source["git_commit"]
    )

    transition(database, baseline_id, "RUNNING")
    transition(database, baseline_id, "SUCCEEDED")
    child = service.create_child_run(
        session_id=str(detail["id"]),
        parent_run_id=baseline_id,
        request_id="branch-1",
        expected_version=1,
        control_snapshot={"schema_version": 1, "items": []},
        control_delta={"schema_version": 1, "items": []},
    )
    child_id = str(child["id"])
    child_source = enqueue_event_data(database, child_id)
    assert child_source is not None
    assert child_source["control_registry_signature"] == baseline_source["control_registry_signature"]

    transition(database, child_id, "RUNNING")
    transition(database, child_id, "FAILED")
    retry = service.retry_run(run_id=child_id, request_id="retry-1", expected_version=2)
    retry_source = enqueue_event_data(database, str(retry["id"]))
    assert retry_source is not None
    assert retry_source["control_registry_signature"] == baseline_source["control_registry_signature"]
