from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import mesh_app.api as api_module
from fastapi.testclient import TestClient
from controls import CONTROL_REGISTRY
from geomturbo import MAX_PHYSICAL_LINE_CHARS

from mesh_app.api import create_app
from mesh_app.artifacts import ArtifactStore, parse_range_header
from mesh_app.config import Settings
from mesh_app.db import Database, DatabaseVersionError
from mesh_app.sessions import utc_now
from mesh_app.worker import set_postprocess_terminal


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GEOMETRY_BYTES = b"""GEOMETRY TURBO
VERSION 5.6
UNITS METER
UNITS-FACTOR 1.0
NI_BEGIN nirow
NAME Rotor
PERIODICITY 36
NI_BEGIN niblade
NAME MainBlade
NUMBER_OF_BLADES 36
NI_BEGIN nitipgap
NI_END nitipgap
NI_END niblade
NI_END nirow
"""


def make_app(tmp_path: Path) -> tuple[Settings, Database, object]:
    settings = Settings.from_env({"MESH_DATA_DIR": str(tmp_path / "data")}, project_root=PROJECT_ROOT)
    settings.ensure_directories()
    database = Database(settings.database_path, settings.migrations_dir)
    database.migrate()
    return settings, database, create_app(settings)


def upload_session(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/v1/sessions",
        data={"title": "Rotor37 基准", "expert_signature": "网格专家"},
        files={"file": ("Rotor37.geomTurbo", GEOMETRY_BYTES, "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def transition(database: Database, run_id: str, status: str) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), run_id),
        )


def test_startup_rejects_database_without_explicit_migration(tmp_path: Path) -> None:
    settings = Settings.from_env({"MESH_DATA_DIR": str(tmp_path / "data")}, project_root=PROJECT_ROOT)
    application = create_app(settings)
    with pytest.raises(DatabaseVersionError):
        with TestClient(application):
            pass


def test_upload_list_detail_artifact_range_and_uniform_errors(tmp_path: Path) -> None:
    settings, database, application = make_app(tmp_path)
    with TestClient(application) as client:
        detail = upload_session(client)
        assert detail["status"] == "ACTIVE"
        assert detail["version"] == 1
        assert detail["expert_signature"] == "网格专家"
        assert detail["geometry_summary"]["row_count"] == 1
        assert len(detail["runs"]) == 1

        listed = client.get("/api/v1/sessions", params={"limit": 1})
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == detail["id"]

        baseline_id = detail["runs"][0]["id"]
        run = client.get(f"/api/v1/runs/{baseline_id}")
        assert run.status_code == 200
        geometry_artifact = next(
            item for item in run.json()["artifacts"] if item["type"] == "GEOMETRY"
        )
        full = client.get(f"/api/v1/artifacts/{geometry_artifact['id']}")
        assert full.status_code == 200
        assert full.content == GEOMETRY_BYTES
        assert full.headers["accept-ranges"] == "bytes"
        partial = client.get(
            f"/api/v1/artifacts/{geometry_artifact['id']}", headers={"Range": "bytes=0-7"}
        )
        assert partial.status_code == 206
        assert partial.content == GEOMETRY_BYTES[:8]
        assert partial.headers["content-range"] == f"bytes 0-7/{len(GEOMETRY_BYTES)}"
        invalid_range = client.get(
            f"/api/v1/artifacts/{geometry_artifact['id']}", headers={"Range": "bytes=999999-"}
        )
        assert invalid_range.status_code == 416
        assert invalid_range.json()["error"]["code"] == "RANGE_NOT_SATISFIABLE"
        assert invalid_range.headers["content-range"] == f"bytes */{len(GEOMETRY_BYTES)}"
        empty_range = client.get(
            f"/api/v1/artifacts/{geometry_artifact['id']}", headers={"Range": ""}
        )
        assert empty_range.status_code == 200
        assert "content-range" not in empty_range.headers
        huge_range = client.get(
            f"/api/v1/artifacts/{geometry_artifact['id']}",
            headers={"Range": f"bytes={'9' * 5000}-"},
        )
        assert huge_range.status_code == 416
        assert huge_range.json()["error"]["code"] == "INVALID_RANGE"

        missing = client.get("/api/v1/runs/not-found")
        assert missing.status_code == 404
        assert missing.json() == {
            "error": {"code": "RUN_NOT_FOUND", "message": "找不到指定运行", "details": {}}
        }
        malformed = client.post(
            f"/api/v1/runs/{baseline_id}/retry",
            content=b"{bad-json",
            headers={"Content-Type": "application/json"},
        )
        assert malformed.status_code == 422
        assert set(malformed.json()) == {"error"}
        assert malformed.json()["error"]["code"] == "VALIDATION_ERROR"

        wrong_method = client.post("/api/health")
        assert wrong_method.status_code == 405
        assert wrong_method.json()["error"]["code"] == "HTTP_405"
        assert wrong_method.headers["allow"] == "GET"

        missing_asset = client.get("/assets/not-found.js")
        assert missing_asset.status_code == 404
        assert missing_asset.json()["error"]["code"] == "HTTP_404"
        api_root = client.get("/api")
        assert api_root.status_code == 404
        assert api_root.json()["error"]["code"] == "HTTP_404"

        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["database"] == {"status": "ok", "version": 3}
        assert health.json()["queue"]["queued"] == 1


def test_invalid_upload_filename_and_content_leave_no_geometry_file(tmp_path: Path) -> None:
    settings, _database, application = make_app(tmp_path)
    with TestClient(application) as client:
        traversal = client.post(
            "/api/v1/sessions",
            data={"title": "非法路径"},
            files={"file": ("../escape.geomTurbo", GEOMETRY_BYTES, "text/plain")},
        )
        assert traversal.status_code == 422
        assert traversal.json()["error"]["code"] == "UNSAFE_FILENAME"

        wrong_suffix = client.post(
            "/api/v1/sessions",
            data={"title": "非法后缀"},
            files={"file": ("mesh.txt", GEOMETRY_BYTES, "text/plain")},
        )
        assert wrong_suffix.status_code == 422
        assert wrong_suffix.json()["error"]["code"] == "INVALID_FILE_TYPE"

        empty = client.post(
            "/api/v1/sessions",
            data={"title": "空文件"},
            files={"file": ("empty.geomTurbo", b"", "text/plain")},
        )
        assert empty.status_code == 422
        assert empty.json()["error"]["code"] == "EMPTY_UPLOAD"

        invalid_geometry = client.post(
            "/api/v1/sessions",
            data={"title": "无法解析"},
            files={"file": ("bad.geomTurbo", b"not a geomTurbo", "text/plain")},
        )
        assert invalid_geometry.status_code == 422
        assert invalid_geometry.json()["error"]["code"] == "INVALID_GEOMTURBO"

        overlong_line = client.post(
            "/api/v1/sessions",
            data={"title": "异常超长行"},
            files={
                "file": (
                    "long-line.geomTurbo",
                    b"X" * (MAX_PHYSICAL_LINE_CHARS + 1),
                    "text/plain",
                )
            },
        )
        assert overlong_line.status_code == 422
        assert overlong_line.json()["error"] == {
            "code": "INVALID_GEOMTURBO",
            "message": "几何文件超过安全解析边界，请检查文件是否为有效的 .geomTurbo",
            "details": {"max_line_chars": MAX_PHYSICAL_LINE_CHARS},
        }

    assert list(settings.geometry_dir.iterdir()) == []


def test_upload_limit_rejects_content_length_and_chunked_body_before_multipart_parse(
    tmp_path: Path,
) -> None:
    settings = Settings.from_env({"MESH_DATA_DIR": str(tmp_path / "data")}, project_root=PROJECT_ROOT)
    settings = replace(settings, max_upload_bytes=128)
    settings.ensure_directories()
    database = Database(settings.database_path, settings.migrations_dir)
    database.migrate()
    application = create_app(settings)
    oversized = b"x" * (70 * 1024)

    with TestClient(application) as client:
        declared = client.post(
            "/api/v1/sessions",
            content=oversized,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert declared.status_code == 413
        assert declared.json()["error"] == {
            "code": "UPLOAD_TOO_LARGE",
            "message": "几何文件超过允许的 128 字节",
            "details": {"max_bytes": 128},
        }

        def chunks():
            yield oversized[:32_000]
            yield oversized[32_000:]

        streamed = client.post(
            "/api/v1/sessions",
            content=chunks(),
            headers={
                "Content-Type": "multipart/form-data; boundary=mesh-boundary",
                "Transfer-Encoding": "chunked",
            },
        )
        assert streamed.status_code == 413
        assert streamed.json()["error"]["code"] == "UPLOAD_TOO_LARGE"


def test_branch_idempotency_retry_note_version_conflict_and_freeze(tmp_path: Path) -> None:
    _settings, database, application = make_app(tmp_path)
    with TestClient(application) as client:
        session = upload_session(client)
        session_id = session["id"]
        baseline_id = session["runs"][0]["id"]
        transition(database, baseline_id, "RUNNING")
        transition(database, baseline_id, "SUCCEEDED")
        pending_manifest = client.get(f"/api/v1/runs/{baseline_id}/mesh/manifest")
        assert pending_manifest.status_code == 200
        assert pending_manifest.json()["status"] == "PENDING"

        branch_payload = {
            "parent_run_id": baseline_id,
            "changes": [],
            "expected_version": 1,
            "request_id": "branch-request-1",
        }
        branch = client.post(f"/api/v1/sessions/{session_id}/runs", json=branch_payload)
        assert branch.status_code == 201, branch.text
        child_id = branch.json()["id"]
        replay = client.post(f"/api/v1/sessions/{session_id}/runs", json=branch_payload)
        assert replay.status_code == 201
        assert replay.json()["id"] == child_id

        idempotency_conflict = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            json={
                **branch_payload,
                "changes": [
                    {
                        "key": "configuration/grid_levels",
                        "selector": "configuration",
                        "op": "set",
                        "value": 2,
                    }
                ],
            },
        )
        assert idempotency_conflict.status_code == 409
        assert idempotency_conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        stale = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            json={**branch_payload, "request_id": "branch-request-2"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
        assert stale.json()["error"]["details"]["current_version"] == 2

        transition(database, child_id, "RUNNING")
        transition(database, child_id, "FAILED")
        retry = client.post(
            f"/api/v1/runs/{child_id}/retry",
            json={"expected_version": 2, "request_id": "retry-request-1"},
        )
        assert retry.status_code == 201
        assert retry.json()["retry_of_run_id"] == child_id
        active_completion = client.post(
            f"/api/v1/sessions/{session_id}/complete",
            json={"run_id": baseline_id, "expected_version": 3},
        )
        assert active_completion.status_code == 409
        assert active_completion.json()["error"]["code"] == "RUNS_STILL_ACTIVE"
        transition(database, retry.json()["id"], "RUNNING")
        transition(database, retry.json()["id"], "FAILED")

        note = client.put(
            f"/api/v1/runs/{baseline_id}/experience-note",
            json={"note": "基准网格收敛稳定。", "expected_note_version": 0},
        )
        assert note.status_code == 200
        assert note.json()["note_version"] == 1
        note_conflict = client.put(
            f"/api/v1/runs/{baseline_id}/experience-note",
            json={"note": "过期写入", "expected_note_version": 0},
        )
        assert note_conflict.status_code == 409
        assert note_conflict.json()["error"]["code"] == "VERSION_CONFLICT"

        preview_pending = client.post(
            f"/api/v1/sessions/{session_id}/complete",
            json={"run_id": baseline_id, "expected_version": 3},
        )
        assert preview_pending.status_code == 409
        assert preview_pending.json()["error"]["code"] == "PREVIEWS_STILL_PENDING"
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE runs SET preview_status = 'UNAVAILABLE', updated_at = ? WHERE id = ?",
                (utc_now(), baseline_id),
            )

        completed = client.post(
            f"/api/v1/sessions/{session_id}/complete",
            json={"run_id": baseline_id, "expected_version": 3},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "COMPLETED"
        assert completed.json()["satisfied_run_id"] == baseline_id

        frozen_note = client.put(
            f"/api/v1/runs/{baseline_id}/experience-note",
            json={"note": "冻结后写入", "expected_note_version": 1},
        )
        assert frozen_note.status_code == 409
        assert frozen_note.json()["error"]["code"] == "SESSION_FROZEN"
        frozen_branch = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            json={
                "parent_run_id": baseline_id,
                "changes": [],
                "expected_version": 4,
                "request_id": "after-freeze",
            },
        )
        assert frozen_branch.status_code == 409
        assert frozen_branch.json()["error"]["code"] == "SESSION_FROZEN"
        frozen_preview = client.post(
            f"/api/v1/sessions/{session_id}/control-preview",
            json={"parent_run_id": baseline_id, "changes": []},
        )
        assert frozen_preview.status_code == 409
        assert frozen_preview.json()["error"]["code"] == "SESSION_FROZEN"


def test_failed_postprocess_manifest_returns_failure_without_converting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _settings, database, application = make_app(tmp_path)
    with TestClient(application) as client:
        detail = upload_session(client)
        run_id = detail["runs"][0]["id"]
        transition(database, run_id, "RUNNING")
        transition(database, run_id, "SUCCEEDED")
        set_postprocess_terminal(database, run_id, "FAILED", error="后处理超时", message="后处理失败")

        def unexpected_conversion(*args, **kwargs):
            raise AssertionError("失败 manifest 不应重新转换")

        monkeypatch.setattr(api_module, "_preview_for_run", unexpected_conversion)
        response = client.get(f"/api/v1/runs/{run_id}/mesh/manifest")
        assert response.status_code == 200
        assert response.json() == {
            "status": "FAILED", "reason": "后处理超时", "reason_code": "POSTPROCESS_FAILED", "blocks": [],
        }


def test_control_preview_events_preview_degradation_and_path_safety(tmp_path: Path) -> None:
    settings, database, application = make_app(tmp_path)
    with TestClient(application) as client:
        session = upload_session(client)
        session_id = session["id"]
        baseline_id = session["runs"][0]["id"]
        transition(database, baseline_id, "RUNNING")
        transition(database, baseline_id, "SUCCEEDED")

        invalid_preview = client.post(
            f"/api/v1/sessions/{session_id}/control-preview",
            json={
                "parent_run_id": baseline_id,
                "changes": [
                    {
                        "key": "row/target_points",
                        "selector": "row:#1",
                        "op": "set",
                        "value": 100000,
                    }
                ],
            },
        )
        assert invalid_preview.status_code == 200
        assert invalid_preview.json()["valid"] is False
        assert invalid_preview.json()["errors"][0]["code"] == "INVALID_CONTROL_CHANGE"
        effective_items = invalid_preview.json()["effective_availability"]
        target_effective = next(
            item
            for item in effective_items
            if item["key"] == "row/target_points" and item["selector"] == "row:#1"
        )
        assert target_effective == {
            "key": "row/target_points",
            "selector": "row:#1",
            "availability": "LOCKED",
            "reason": CONTROL_REGISTRY["row/target_points"].unsupported_reason,
            "can_clear": False,
        }

        with database.transaction(immediate=True) as connection:
            # 创建会话时已写入入队来源快照事件，测试事件使用下一个可用序号。
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
                    (baseline_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO run_events (
                    run_id, sequence, stage, level, progress, message, data_json, created_at
                ) VALUES (?, ?, 'geometry', 'INFO', 0.25, '几何解析完成', ?, ?)
                """,
                (baseline_id, sequence, json.dumps({"rows": 1}), utc_now()),
            )
        events = client.get(f"/api/v1/runs/{baseline_id}/events", params={"after": 0})
        assert events.status_code == 200
        assert events.json()["items"][-1]["message"] == "几何解析完成"
        assert events.json()["next_after"] == sequence

        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE runs SET preview_status = 'UNAVAILABLE', updated_at = ? WHERE id = ?",
                (utc_now(), baseline_id),
            )
        manifest = client.get(f"/api/v1/runs/{baseline_id}/mesh/manifest")
        assert manifest.status_code == 200
        assert manifest.json() == {
            "status": "UNAVAILABLE",
            "reason": "该运行尚无 CGNS 产物",
            "blocks": [],
        }

    store = ArtifactStore(database, settings.data_dir)
    with pytest.raises(Exception) as traversal:
        store.resolve_path("../outside.cgns")
    assert getattr(traversal.value, "code", None) == "UNSAFE_PATH"
    assert parse_range_header("bytes=-4", 10).start == 6
