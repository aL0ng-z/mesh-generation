from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from mesh_app.config import Settings
from mesh_app.db import Database, DatabaseVersionError
from mesh_app.sessions import EMPTY_CONTROL_DELTA, EMPTY_CONTROL_SNAPSHOT, SessionService, ServiceError, dump_json, utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_database(tmp_path: Path) -> tuple[Settings, Database]:
    settings = Settings.from_env({"MESH_DATA_DIR": str(tmp_path / "data")}, project_root=PROJECT_ROOT)
    settings.ensure_directories()
    database = Database(settings.database_path, settings.migrations_dir, settings.busy_timeout_ms)
    database.migrate()
    return settings, database


def geometry_summary(relative_path: str) -> dict[str, object]:
    return {
        "path": relative_path,
        "version": "5.6",
        "units": "METER",
        "units_factor": 1.0,
        "row_count": 1,
        "rows": [{"name": "Rotor", "periodicity": 36, "blades": []}],
        "multi_row": False,
        "has_splitter": False,
        "has_tip_gap": False,
    }


def create_session(database: Database) -> dict[str, object]:
    relative = "geometries/session/source.geomTurbo"
    return SessionService(database).create_session(
        title="Rotor37",
        expert_name="测试专家",
        source_filename="Rotor37.geomTurbo",
        geometry_sha256=hashlib.sha256(b"geometry").hexdigest(),
        geometry_relative_path=relative,
        geometry_summary=geometry_summary(relative),
    )


def test_session_baseline_and_geometry_artifact_share_one_transaction(tmp_path: Path) -> None:
    _settings, database = make_database(tmp_path)
    service = SessionService(database)
    relative = "geometries/shared/source.geomTurbo"
    digest = hashlib.sha256(b"geometry").hexdigest()
    first = service.create_session(
        title="原子创建一",
        expert_name=None,
        source_filename="first.geomTurbo",
        geometry_sha256=digest,
        geometry_relative_path=relative,
        geometry_summary=geometry_summary(relative),
        geometry_artifact_size=8,
    )
    with database.reading() as connection:
        artifact = connection.execute(
            "SELECT session_id, run_id FROM artifacts WHERE relative_path = ?", (relative,)
        ).fetchone()
    assert artifact is not None
    assert artifact["session_id"] == first["id"]
    assert artifact["run_id"] == first["runs"][0]["id"]

    with pytest.raises(sqlite3.IntegrityError):
        service.create_session(
            title="原子创建二",
            expert_name=None,
            source_filename="second.geomTurbo",
            geometry_sha256=digest,
            geometry_relative_path=relative,
            geometry_summary=geometry_summary(relative),
            geometry_artifact_size=8,
        )
    with database.reading() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE title = '原子创建二'"
        ).fetchone()[0] == 0


def transition(database: Database, run_id: str, status: str) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), run_id),
        )


def test_explicit_migration_and_connection_pragmas(tmp_path: Path) -> None:
    settings = Settings.from_env({"MESH_DATA_DIR": str(tmp_path / "data")}, project_root=PROJECT_ROOT)
    database = Database(settings.database_path, settings.migrations_dir, 4321)

    with pytest.raises(DatabaseVersionError):
        database.require_current()

    assert database.migrate() == 1
    database.require_current()
    with database.reading() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"sessions", "runs", "artifacts", "run_events", "workers"}.issubset(tables)


def test_wal_reader_is_not_blocked_by_uncommitted_writer(tmp_path: Path) -> None:
    _settings, database = make_database(tmp_path)
    first = database.connect()
    second = database.connect()
    try:
        first.execute("BEGIN IMMEDIATE")
        timestamp = utc_now()
        first.execute(
            """
            INSERT INTO workers (
                id, heartbeat_at, max_concurrency, running_count,
                resource_status_json, created_at, updated_at
            ) VALUES ('writer', ?, 20, 0, '{}', ?, ?)
            """,
            (timestamp, timestamp, timestamp),
        )
        result: list[int] = []
        failure: list[BaseException] = []

        def read_count() -> None:
            try:
                result.append(int(second.execute("SELECT COUNT(*) FROM workers").fetchone()[0]))
            except BaseException as exc:  # pragma: no cover - 仅用于保留线程异常
                failure.append(exc)

        thread = threading.Thread(target=read_count)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not failure
        assert result == [0]
        first.commit()
        assert second.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 1
    finally:
        first.close()
        second.close()


def test_state_machine_terminal_immutability_and_append_only_tables(tmp_path: Path) -> None:
    settings, database = make_database(tmp_path)
    detail = create_session(database)
    run_id = detail["runs"][0]["id"]

    with pytest.raises(sqlite3.IntegrityError, match="非法运行状态转换"):
        transition(database, run_id, "SUCCEEDED")
    transition(database, run_id, "RUNNING")
    transition(database, run_id, "SUCCEEDED")

    with pytest.raises(sqlite3.IntegrityError, match="终态运行"):
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE runs SET control_snapshot_json = ? WHERE id = ?",
                (dump_json({"schema_version": 1, "items": [{"key": "x"}]}), run_id),
            )
    with pytest.raises(sqlite3.IntegrityError, match="终态运行"):
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE runs SET quality_status = 'FAIL', progress = 0.5 WHERE id = ?",
                (run_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="终态运行"):
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE runs SET created_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
                (run_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="运行记录只允许追加"):
        with database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    # 终态后仅经验文本和预览后处理属于明确允许的可变字段。
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE runs SET experience_note = '可复用经验', note_version = 1,
                            preview_status = 'UNAVAILABLE', updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), run_id),
        )

    geometry_path = settings.data_dir / "geometries" / "session" / "source.geomTurbo"
    geometry_path.parent.mkdir(parents=True, exist_ok=True)
    geometry_path.write_bytes(b"geometry")
    artifact_id = "artifact-1"
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO artifacts (
                id, session_id, run_id, kind, display_name, relative_path,
                sha256, size_bytes, mime_type, created_at
            ) VALUES (?, ?, ?, 'GEOMETRY', 'source.geomTurbo', ?, ?, 8, 'text/plain', ?)
            """,
            (
                artifact_id,
                detail["id"],
                run_id,
                geometry_path.relative_to(settings.data_dir).as_posix(),
                hashlib.sha256(b"geometry").hexdigest(),
                utc_now(),
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="只允许追加"):
        with database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))


def test_idempotent_branch_version_conflict_retry_note_and_freeze(tmp_path: Path) -> None:
    _settings, database = make_database(tmp_path)
    service = SessionService(database)
    detail = create_session(database)
    session_id = str(detail["id"])
    baseline_id = str(detail["runs"][0]["id"])
    transition(database, baseline_id, "RUNNING")
    transition(database, baseline_id, "SUCCEEDED")

    snapshot = {"schema_version": 1, "items": []}
    delta = {"schema_version": 1, "items": []}
    child = service.create_child_run(
        session_id=session_id,
        parent_run_id=baseline_id,
        request_id="branch-1",
        expected_version=1,
        control_snapshot=snapshot,
        control_delta=delta,
    )
    replay = service.create_child_run(
        session_id=session_id,
        parent_run_id=baseline_id,
        request_id="branch-1",
        expected_version=1,
        control_snapshot=snapshot,
        control_delta=delta,
    )
    assert replay["id"] == child["id"]

    with pytest.raises(ServiceError) as conflict:
        service.create_child_run(
            session_id=session_id,
            parent_run_id=baseline_id,
            request_id="branch-2",
            expected_version=1,
            control_snapshot=snapshot,
            control_delta=delta,
        )
    assert conflict.value.code == "VERSION_CONFLICT"

    transition(database, str(child["id"]), "RUNNING")
    transition(database, str(child["id"]), "FAILED")
    retry = service.retry_run(
        run_id=str(child["id"]), request_id="retry-1", expected_version=2
    )
    assert retry["retry_of_run_id"] == child["id"]
    assert retry["parent_run_id"] == child["id"]
    transition(database, str(retry["id"]), "RUNNING")
    transition(database, str(retry["id"]), "FAILED")

    note = service.update_experience_note(
        run_id=baseline_id, note="该网格适合作为基准。", expected_note_version=0
    )
    assert note["note_version"] == 1
    with pytest.raises(ServiceError) as note_conflict:
        service.update_experience_note(
            run_id=baseline_id, note="过期覆盖", expected_note_version=0
        )
    assert note_conflict.value.code == "VERSION_CONFLICT"

    with pytest.raises(ServiceError) as preview_pending:
        service.complete_session(
            session_id=session_id, run_id=baseline_id, expected_version=3
        )
    assert preview_pending.value.code == "PREVIEWS_STILL_PENDING"
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE runs SET preview_status = 'UNAVAILABLE', updated_at = ? WHERE id = ?",
            (utc_now(), baseline_id),
        )

    completed = service.complete_session(
        session_id=session_id, run_id=baseline_id, expected_version=3
    )
    assert completed["status"] == "COMPLETED"
    assert completed["version"] == 4
    with pytest.raises(ServiceError) as frozen:
        service.update_experience_note(
            run_id=baseline_id, note="冻结后写入", expected_note_version=1
        )
    assert frozen.value.code == "SESSION_FROZEN"
