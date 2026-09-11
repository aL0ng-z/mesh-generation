"""会话、不可变运行树、重试、冻结和乐观并发。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .db import Database


EMPTY_CONTROL_SNAPSHOT = {"schema_version": 1, "items": []}
EMPTY_CONTROL_DELTA = {"schema_version": 1, "items": []}
EMPTY_QUALITY = {"schema_version": 1, "status": "UNKNOWN", "metrics": []}
EMPTY_RUN_SUMMARY = {"schema_version": 1}


class ServiceError(RuntimeError):
    """可稳定映射为统一 API 错误结构的业务异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})
        super().__init__(message)


def utc_now() -> str:
    """返回便于 SQLite 排序的 UTC ISO 时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dump_json(value: Any) -> str:
    """以稳定格式持久化 JSON；拒绝非有限数值。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_json(value: str | None, default: Any) -> Any:
    """读取内部 JSON；数据库损坏时保留可诊断默认值。"""

    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _enqueue_source_snapshot(project_root: Path) -> dict[str, Any]:
    """记录入队时刻的来源快照：git 提交、控制注册表签名与 src 五模块 sha256。

    与执行时摘要（schema v4 ``sources``）使用同一套算法，供运行详情
    比较入队与执行来源是否发生漂移。
    """

    commit: str | None = None
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        candidate = completed.stdout.strip()
        if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", candidate):
            commit = candidate
    except (OSError, subprocess.SubprocessError):
        commit = None
    registry_signature: str | None = None
    source_files: dict[str, str] = {}
    try:
        from mesh import SOURCE_MODULE_FILES, _control_registry_signature

        registry_signature, _key_count = _control_registry_signature()
        src_root = Path(project_root) / "src"
        for name in SOURCE_MODULE_FILES:
            path = src_root / name
            if not path.is_file():
                continue
            source_files[f"src/{name}"] = _sha256_file(path)
    except Exception:
        # 来源快照是诊断证据；内核不可用时降级为缺失，不阻断运行创建。
        registry_signature = None
        source_files = {}
    return {
        "git_commit": commit,
        "control_registry_signature": registry_signature,
        "source_files": source_files,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _append_enqueue_source_event(
    connection: sqlite3.Connection,
    run_id: str,
    source: Mapping[str, Any],
    timestamp: str,
) -> None:
    """在 run_events 中记录入队来源快照；stage 无约束，允许新增事件类型。"""

    sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO run_events (
            run_id, sequence, stage, level, progress, message, data_json, created_at
        ) VALUES (?, ?, 'ENQUEUE_SOURCE', 'INFO', NULL, '运行入队来源快照', ?, ?)
        """,
        (run_id, sequence, dump_json(source), timestamp),
    )


class SessionService:
    """共享会话和运行树的事务服务。"""

    def __init__(
        self,
        database: Database,
        *,
        project_root: str | Path | None = None,
    ) -> None:
        self.database = database
        self.project_root = Path(project_root).resolve() if project_root is not None else None

    def create_session(
        self,
        *,
        title: str,
        expert_name: str | None,
        source_filename: str,
        geometry_sha256: str,
        geometry_relative_path: str,
        geometry_summary: Mapping[str, Any],
        session_id: str | None = None,
        baseline_id: str | None = None,
        geometry_artifact_size: int | None = None,
        geometry_artifact_mime_type: str = "text/plain",
    ) -> dict[str, Any]:
        """原子创建会话、自动排队的 baseline 和可选几何产物。"""

        normalized_title = title.strip()
        if not normalized_title:
            raise ServiceError("INVALID_TITLE", "会话标题不能为空", status_code=422)
        if len(normalized_title) > 200:
            raise ServiceError("INVALID_TITLE", "会话标题不得超过 200 个字符", status_code=422)
        if expert_name is not None and len(expert_name.strip()) > 200:
            raise ServiceError("INVALID_EXPERT_NAME", "专家署名不得超过 200 个字符", status_code=422)
        if not source_filename or len(source_filename) > 255 or "\x00" in source_filename:
            raise ServiceError("INVALID_SOURCE_FILENAME", "源文件名无效", status_code=422)
        if re.fullmatch(r"[0-9a-fA-F]{64}", geometry_sha256) is None:
            raise ServiceError("INVALID_GEOMETRY_HASH", "几何文件散列无效", status_code=422)
        _validate_relative_path(geometry_relative_path)
        if geometry_artifact_size is not None and geometry_artifact_size < 0:
            raise ServiceError("INVALID_ARTIFACT_SIZE", "几何产物大小不能为负数", status_code=422)
        if not geometry_artifact_mime_type.strip():
            raise ServiceError("INVALID_ARTIFACT_MIME_TYPE", "几何产物 MIME 类型不能为空", status_code=422)
        session_id = session_id or str(uuid.uuid4())
        baseline_id = baseline_id or str(uuid.uuid4())
        timestamp = utc_now()
        geometry_json = dump_json(dict(geometry_summary))
        parse_result = {
            "schema_version": 1,
            "geometry": dict(geometry_summary),
        }
        enqueue_source = (
            _enqueue_source_snapshot(self.project_root) if self.project_root is not None else None
        )
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, title, expert_name, source_filename, geometry_sha256,
                    geometry_relative_path, geometry_summary_json, status,
                    satisfied_run_id, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', NULL, 1, ?, ?)
                """,
                (
                    session_id,
                    normalized_title,
                    expert_name.strip() if expert_name and expert_name.strip() else None,
                    source_filename,
                    geometry_sha256,
                    geometry_relative_path,
                    geometry_json,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    id, session_id, parent_run_id, retry_of_run_id, sequence,
                    request_id, control_snapshot_json, control_delta_json,
                    parse_result_json, status, quality_status, quality_json,
                    preview_status, run_summary_json, progress, created_at, updated_at
                ) VALUES (?, ?, NULL, NULL, 1, ?, ?, ?, ?, 'QUEUED',
                          'UNKNOWN', ?, 'PENDING', ?, 0, ?, ?)
                """,
                (
                    baseline_id,
                    session_id,
                    f"baseline:{session_id}",
                    dump_json(EMPTY_CONTROL_SNAPSHOT),
                    dump_json(EMPTY_CONTROL_DELTA),
                    dump_json(parse_result),
                    dump_json(EMPTY_QUALITY),
                    dump_json(EMPTY_RUN_SUMMARY),
                    timestamp,
                    timestamp,
                ),
            )
            if enqueue_source is not None:
                _append_enqueue_source_event(connection, baseline_id, enqueue_source, timestamp)
            if geometry_artifact_size is not None:
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        id, session_id, run_id, kind, display_name, relative_path,
                        sha256, size_bytes, mime_type, created_at
                    ) VALUES (?, ?, ?, 'GEOMETRY', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        session_id,
                        baseline_id,
                        source_filename,
                        geometry_relative_path.replace("\\", "/"),
                        geometry_sha256.lower(),
                        geometry_artifact_size,
                        geometry_artifact_mime_type.strip(),
                        timestamp,
                    ),
                )
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            runs = connection.execute(
                "SELECT * FROM runs WHERE session_id = ? ORDER BY sequence", (session_id,)
            ).fetchall()
            if session is None:  # pragma: no cover - 前述 INSERT 成功后的防御性检查
                raise RuntimeError("会话事务内回读失败")
            result = _session_summary(session)
            result["geometry_summary"] = load_json(session["geometry_summary_json"], {})
            result["runs"] = [_run_summary(row) for row in runs]
        return result

    def list_sessions(
        self,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """按创建时间倒序游标分页列出会话。"""

        if status is not None:
            status = status.upper()
            if status not in {"ACTIVE", "COMPLETED"}:
                raise ServiceError("INVALID_STATUS", "会话状态仅接受 ACTIVE 或 COMPLETED", status_code=422)
        if limit < 1 or limit > 100:
            raise ServiceError("INVALID_LIMIT", "分页数量必须位于 1 到 100 之间", status_code=422)
        cursor_pair = _decode_cursor(cursor) if cursor else None
        clauses: list[str] = []
        parameters: list[Any] = []
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if cursor_pair:
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            parameters.extend((cursor_pair[0], cursor_pair[0], cursor_pair[1]))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit + 1)
        with self.database.reading() as connection:
            rows = connection.execute(
                f"SELECT * FROM sessions{where} ORDER BY created_at DESC, id DESC LIMIT ?",
                parameters,
            ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        items = [_session_summary(row) for row in page]
        next_cursor = (
            _encode_cursor(str(page[-1]["created_at"]), str(page[-1]["id"]))
            if has_more and page
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def get_session(self, session_id: str) -> dict[str, Any]:
        """返回会话及精简的不可变运行树。"""

        with self.database.reading() as connection:
            session = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if session is None:
                raise ServiceError("SESSION_NOT_FOUND", "找不到指定会话", status_code=404)
            runs = connection.execute(
                "SELECT * FROM runs WHERE session_id = ? ORDER BY sequence", (session_id,)
            ).fetchall()
        result = _session_summary(session)
        result["geometry_summary"] = load_json(session["geometry_summary_json"], {})
        result["runs"] = [_run_summary(row) for row in runs]
        return result

    def get_run(self, run_id: str) -> dict[str, Any]:
        """返回单轮控制、质量、经验、事件摘要、产物元数据与样本资格。"""

        with self.database.reading() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise ServiceError("RUN_NOT_FOUND", "找不到指定运行", status_code=404)
            events = connection.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY sequence DESC LIMIT 100",
                (run_id,),
            ).fetchall()
            artifacts = connection.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            ).fetchall()
            enqueue_row = connection.execute(
                """
                SELECT data_json FROM run_events
                WHERE run_id = ? AND stage = 'ENQUEUE_SOURCE'
                ORDER BY sequence LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        summary = load_json(run["run_summary_json"], EMPTY_RUN_SUMMARY)
        enqueue_source = load_json(enqueue_row["data_json"], {}) if enqueue_row is not None else {}
        result = _run_summary(run)
        result.update(
            {
                "controls": load_json(run["control_snapshot_json"], EMPTY_CONTROL_SNAPSHOT),
                "control_changes": load_json(run["control_delta_json"], EMPTY_CONTROL_DELTA).get("items", []),
                "parse_result": load_json(run["parse_result_json"], {}),
                "quality": load_json(run["quality_json"], EMPTY_QUALITY),
                "run_summary": summary,
                "sample_eligibility": _sample_eligibility(
                    summary,
                    artifacts,
                    session_id=str(run["session_id"]),
                    run_id=run_id,
                    enqueue_source=enqueue_source,
                ),
                "experience_note": run["experience_note"],
                "note_version": run["note_version"],
                "events": [_event_dict(row) for row in reversed(events)],
                "artifacts": [_artifact_dict(row) for row in artifacts],
                "error_code": run["error_code"],
                "error_message": run["error_message"],
            }
        )
        return result

    def create_child_run(
        self,
        *,
        session_id: str,
        parent_run_id: str,
        request_id: str,
        expected_version: int,
        control_snapshot: Mapping[str, Any],
        control_delta: Mapping[str, Any],
    ) -> dict[str, Any]:
        """从成功父节点创建子运行，支持会话内请求幂等。"""

        normalized_request_id = _validate_request_id(request_id)
        snapshot_json = dump_json(control_snapshot)
        delta_json = dump_json(control_delta)
        enqueue_source = (
            _enqueue_source_snapshot(self.project_root) if self.project_root is not None else None
        )
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM runs WHERE session_id = ? AND request_id = ?",
                (session_id, normalized_request_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["parent_run_id"] != parent_run_id
                    or existing["control_snapshot_json"] != snapshot_json
                    or existing["control_delta_json"] != delta_json
                ):
                    raise ServiceError(
                        "IDEMPOTENCY_CONFLICT",
                        "同一 request_id 已用于不同的运行请求，请生成新的请求标识",
                        status_code=409,
                    )
                run_id = str(existing["id"])
            else:
                session = _get_session_for_write(connection, session_id)
                _assert_active_and_version(session, expected_version)
                parent = connection.execute("SELECT * FROM runs WHERE id = ?", (parent_run_id,)).fetchone()
                if parent is None or parent["session_id"] != session_id:
                    raise ServiceError("PARENT_RUN_NOT_FOUND", "父运行不属于当前会话", status_code=404)
                if parent["status"] != "SUCCEEDED":
                    raise ServiceError(
                        "PARENT_RUN_NOT_SUCCEEDED",
                        "只能从成功运行创建控制分支",
                        status_code=409,
                    )
                sequence = _next_sequence(connection, session_id)
                run_id = str(uuid.uuid4())
                timestamp = utc_now()
                connection.execute(
                    """
                    INSERT INTO runs (
                        id, session_id, parent_run_id, retry_of_run_id, sequence,
                        request_id, control_snapshot_json, control_delta_json,
                        parse_result_json, status, quality_status, quality_json,
                        preview_status, run_summary_json, progress, created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'QUEUED', 'UNKNOWN', ?,
                              'PENDING', ?, 0, ?, ?)
                    """,
                    (
                        run_id,
                        session_id,
                        parent_run_id,
                        sequence,
                        normalized_request_id,
                        snapshot_json,
                        delta_json,
                        parent["parse_result_json"],
                        dump_json(EMPTY_QUALITY),
                        dump_json(EMPTY_RUN_SUMMARY),
                        timestamp,
                        timestamp,
                    ),
                )
                if enqueue_source is not None:
                    _append_enqueue_source_event(connection, run_id, enqueue_source, timestamp)
                _bump_session_version(connection, session_id, expected_version, timestamp)
        return self.get_run(run_id)

    def retry_run(
        self,
        *,
        run_id: str,
        request_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """为失败运行创建具有相同完整控制快照的重试子节点。"""

        normalized_request_id = _validate_request_id(request_id)
        enqueue_source = (
            _enqueue_source_snapshot(self.project_root) if self.project_root is not None else None
        )
        with self.database.transaction(immediate=True) as connection:
            source = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if source is None:
                raise ServiceError("RUN_NOT_FOUND", "找不到要重试的运行", status_code=404)
            session_id = str(source["session_id"])
            existing = connection.execute(
                "SELECT * FROM runs WHERE session_id = ? AND request_id = ?",
                (session_id, normalized_request_id),
            ).fetchone()
            if existing is not None:
                if existing["retry_of_run_id"] != run_id:
                    raise ServiceError(
                        "IDEMPOTENCY_CONFLICT",
                        "同一 request_id 已用于不同的重试请求，请生成新的请求标识",
                        status_code=409,
                    )
                retry_id = str(existing["id"])
            else:
                session = _get_session_for_write(connection, session_id)
                _assert_active_and_version(session, expected_version)
                if source["status"] != "FAILED":
                    raise ServiceError("RUN_NOT_FAILED", "只有失败运行可以重试", status_code=409)
                retry_id = str(uuid.uuid4())
                sequence = _next_sequence(connection, session_id)
                timestamp = utc_now()
                connection.execute(
                    """
                    INSERT INTO runs (
                        id, session_id, parent_run_id, retry_of_run_id, sequence,
                        request_id, control_snapshot_json, control_delta_json,
                        parse_result_json, status, quality_status, quality_json,
                        preview_status, run_summary_json, progress, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', 'UNKNOWN', ?,
                              'PENDING', ?, 0, ?, ?)
                    """,
                    (
                        retry_id,
                        session_id,
                        run_id,
                        run_id,
                        sequence,
                        normalized_request_id,
                        source["control_snapshot_json"],
                        dump_json(EMPTY_CONTROL_DELTA),
                        source["parse_result_json"],
                        dump_json(EMPTY_QUALITY),
                        dump_json(EMPTY_RUN_SUMMARY),
                        timestamp,
                        timestamp,
                    ),
                )
                if enqueue_source is not None:
                    _append_enqueue_source_event(connection, retry_id, enqueue_source, timestamp)
                _bump_session_version(connection, session_id, expected_version, timestamp)
        return self.get_run(retry_id)

    def update_experience_note(
        self,
        *,
        run_id: str,
        note: str,
        expected_note_version: int,
    ) -> dict[str, Any]:
        """独立使用 note_version 更新成功运行的经验文本。"""

        if len(note) > 20000:
            raise ServiceError("NOTE_TOO_LONG", "经验文本不得超过 20000 个字符", status_code=422)
        with self.database.transaction(immediate=True) as connection:
            run = connection.execute(
                """
                SELECT r.*, s.status AS session_status
                FROM runs r JOIN sessions s ON s.id = r.session_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                raise ServiceError("RUN_NOT_FOUND", "找不到指定运行", status_code=404)
            if run["session_status"] != "ACTIVE":
                raise ServiceError("SESSION_FROZEN", "会话已完成，经验文本已冻结", status_code=409)
            if run["status"] != "SUCCEEDED":
                raise ServiceError("RUN_NOT_SUCCEEDED", "只能为成功运行保存经验文本", status_code=409)
            timestamp = utc_now()
            changed = connection.execute(
                """
                UPDATE runs
                SET experience_note = ?, note_version = note_version + 1, updated_at = ?
                WHERE id = ? AND note_version = ?
                """,
                (note, timestamp, run_id, expected_note_version),
            ).rowcount
            if changed != 1:
                current = connection.execute(
                    "SELECT note_version FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                raise ServiceError(
                    "VERSION_CONFLICT",
                    "经验文本已被其他访问者修改，请刷新后重试",
                    status_code=409,
                    details={"current_note_version": current["note_version"] if current else None},
                )
        return self.get_run(run_id)

    def complete_session(
        self,
        *,
        session_id: str,
        run_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """选择成功运行并冻结整个会话。"""

        with self.database.transaction(immediate=True) as connection:
            session = _get_session_for_write(connection, session_id)
            if session["status"] == "COMPLETED":
                if session["satisfied_run_id"] != run_id:
                    raise ServiceError("SESSION_FROZEN", "会话已完成，不能重新选择满意运行", status_code=409)
            else:
                _assert_active_and_version(session, expected_version)
                run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                if run is None or run["session_id"] != session_id:
                    raise ServiceError("RUN_NOT_FOUND", "满意运行不属于当前会话", status_code=404)
                if run["status"] != "SUCCEEDED":
                    raise ServiceError("RUN_NOT_SUCCEEDED", "只能选择成功运行并完成会话", status_code=409)
                active_runs = connection.execute(
                    """
                    SELECT id FROM runs
                    WHERE session_id = ? AND status IN ('QUEUED', 'RUNNING')
                    ORDER BY sequence
                    """,
                    (session_id,),
                ).fetchall()
                if active_runs:
                    raise ServiceError(
                        "RUNS_STILL_ACTIVE",
                        "会话仍有排队或运行中的任务，请等待其结束后再完成会话",
                        status_code=409,
                        details={"run_ids": [row["id"] for row in active_runs]},
                    )
                pending_previews = connection.execute(
                    """
                    SELECT id FROM runs
                    WHERE session_id = ? AND status = 'SUCCEEDED' AND preview_status = 'PENDING'
                    ORDER BY sequence
                    """,
                    (session_id,),
                ).fetchall()
                if pending_previews:
                    raise ServiceError(
                        "PREVIEWS_STILL_PENDING",
                        "成功网格仍在登记产物或生成预览，请等待后处理完成后再冻结会话",
                        status_code=409,
                        details={"run_ids": [row["id"] for row in pending_previews]},
                    )
                timestamp = utc_now()
                changed = connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'COMPLETED', satisfied_run_id = ?, version = version + 1, updated_at = ?
                    WHERE id = ? AND status = 'ACTIVE' AND version = ?
                    """,
                    (run_id, timestamp, session_id, expected_version),
                ).rowcount
                if changed != 1:
                    _raise_version_conflict(connection, session_id)
        return self.get_session(session_id)

    def get_events(self, run_id: str, *, after: int = 0, limit: int = 200) -> dict[str, Any]:
        """按运行内序号增量读取事件。"""

        if after < 0 or limit < 1 or limit > 1000:
            raise ServiceError("INVALID_CURSOR", "事件游标或分页数量无效", status_code=422)
        with self.database.reading() as connection:
            exists = connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            if exists is None:
                raise ServiceError("RUN_NOT_FOUND", "找不到指定运行", status_code=404)
            rows = connection.execute(
                """
                SELECT * FROM run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (run_id, after, limit),
            ).fetchall()
        items = [_event_dict(row) for row in rows]
        return {"items": items, "next_after": items[-1]["sequence"] if items else after}


def _validate_request_id(request_id: str) -> str:
    normalized = request_id.strip()
    if not normalized or len(normalized) > 200:
        raise ServiceError("INVALID_REQUEST_ID", "request_id 必须为 1 到 200 个字符", status_code=422)
    return normalized


def _validate_relative_path(value: str) -> None:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not normalized or any(part in {"", ".", ".."} for part in path.parts):
        raise ServiceError("UNSAFE_PATH", "服务端相对路径不安全", status_code=422)
    if path.parts and ":" in path.parts[0]:
        raise ServiceError("UNSAFE_PATH", "服务端相对路径不安全", status_code=422)


def _get_session_for_write(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    session = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None:
        raise ServiceError("SESSION_NOT_FOUND", "找不到指定会话", status_code=404)
    return session


def _assert_active_and_version(session: sqlite3.Row, expected_version: int) -> None:
    if session["status"] != "ACTIVE":
        raise ServiceError("SESSION_FROZEN", "会话已完成，不能再修改运行树", status_code=409)
    if session["version"] != expected_version:
        raise ServiceError(
            "VERSION_CONFLICT",
            "会话已被其他访问者修改，请刷新后重试",
            status_code=409,
            details={"current_version": session["version"]},
        )


def _bump_session_version(
    connection: sqlite3.Connection,
    session_id: str,
    expected_version: int,
    timestamp: str,
) -> None:
    changed = connection.execute(
        """
        UPDATE sessions SET version = version + 1, updated_at = ?
        WHERE id = ? AND status = 'ACTIVE' AND version = ?
        """,
        (timestamp, session_id, expected_version),
    ).rowcount
    if changed != 1:
        _raise_version_conflict(connection, session_id)


def _raise_version_conflict(connection: sqlite3.Connection, session_id: str) -> None:
    current = connection.execute("SELECT status, version FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if current is not None and current["status"] != "ACTIVE":
        raise ServiceError("SESSION_FROZEN", "会话已完成，不能再写入", status_code=409)
    raise ServiceError(
        "VERSION_CONFLICT",
        "会话已被其他访问者修改，请刷新后重试",
        status_code=409,
        details={"current_version": current["version"] if current else None},
    )


def _next_sequence(connection: sqlite3.Connection, session_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM runs WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    )


def _session_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "expert_signature": row["expert_name"],
        "source_filename": row["source_filename"],
        "status": row["status"],
        "satisfied_run_id": row["satisfied_run_id"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _run_summary(row: sqlite3.Row) -> dict[str, Any]:
    if row["sequence"] == 1 and row["parent_run_id"] is None:
        label = "Baseline"
    elif row["retry_of_run_id"]:
        label = f"重试 #{row['sequence']}"
    else:
        label = f"分支 #{row['sequence']}"
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "parent_run_id": row["parent_run_id"],
        "retry_of_run_id": row["retry_of_run_id"],
        "sequence": row["sequence"],
        "label": label,
        "status": row["status"],
        "quality_status": row["quality_status"],
        "preview_status": row["preview_status"],
        "postprocess_status": row["postprocess_status"],
        "postprocess_started_at": row["postprocess_started_at"],
        "postprocess_finished_at": row["postprocess_finished_at"],
        "postprocess_error": row["postprocess_error"],
        "progress": row["progress"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sequence": row["sequence"],
        "stage": row["stage"],
        "level": row["level"],
        "progress": row["progress"],
        "message": row["message"],
        "data": load_json(row["data_json"], {}),
        "created_at": row["created_at"],
    }


def _artifact_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "type": row["kind"],
        "display_name": row["display_name"],
        "size": row["size_bytes"],
        "mime_type": row["mime_type"],
        "sha256": row["sha256"],
        "created_at": row["created_at"],
    }
    block_id = _preview_artifact_block_id(str(row["kind"]), str(row["relative_path"]))
    if block_id is not None:
        result["block_id"] = block_id
    return result


def _preview_artifact_block_id(kind: str, relative_path: str) -> str | None:
    """从已登记的预览相对路径提取 block 标识，不向网页暴露存储路径。"""

    if not kind.startswith("PREVIEW_"):
        return None
    parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    try:
        marker = parts.index("blocks")
    except ValueError:
        return None
    return parts[marker + 1] if marker + 1 < len(parts) else None


def _sample_eligibility(
    summary: Mapping[str, Any],
    artifacts: Sequence[sqlite3.Row],
    *,
    session_id: str,
    run_id: str,
    enqueue_source: Mapping[str, Any],
) -> dict[str, Any]:
    """按共享契约 C 从不可变摘要、已登记产物与入队来源计算样本资格。

    每个不满足项给出对应原因；``quality.result == FAIL`` 不影响资格
    （有效失败经验）。v3 历史摘要视为新证据缺失，不自动认定合格。
    """

    if not isinstance(summary, Mapping) or summary.get("schema_version") != 4:
        if isinstance(summary, Mapping) and summary.get("schema_version") == 3:
            return {"eligible": False, "reasons": ["新证据缺失"]}
        return {"eligible": False, "reasons": ["运行摘要不是 schema v4，无可用执行证据"]}
    reasons: list[str] = []
    summary_run_id = summary.get("run_id")
    evidence = summary.get("execution_evidence")
    completion = evidence.get("completion_event") if isinstance(evidence, Mapping) else None
    if not (
        isinstance(completion, Mapping)
        and completion.get("stage") == "final"
        and completion.get("run_id") == summary_run_id
    ):
        reasons.append("完成事件缺失、损坏或与运行身份不一致")
    protocol_errors = evidence.get("protocol_errors") if isinstance(evidence, Mapping) else None
    if protocol_errors:
        reasons.append("执行证据存在协议错误")
    controls = summary.get("controls")
    verification = controls.get("verification") if isinstance(controls, Mapping) else None
    verification_status = verification.get("status") if isinstance(verification, Mapping) else None
    if verification_status not in {"COMPLETE", "NOT_REQUESTED"}:
        reasons.append(f"控制验证不充分（{verification_status}）")
    quality_validation = summary.get("quality_validation")
    quality_validation_status = (
        quality_validation.get("status") if isinstance(quality_validation, Mapping) else None
    )
    if quality_validation_status != "VALID":
        reasons.append(f"质量数据校验不可判定（{quality_validation_status}）")
    sources = summary.get("sources")
    source_map = sources if isinstance(sources, Mapping) else {}
    git = source_map.get("git")
    if not (isinstance(git, Mapping) and git.get("commit")):
        reasons.append("缺少执行时的 git 提交来源")
    source_signature = source_map.get("source_signature")
    if not (isinstance(source_signature, Mapping) and source_signature.get("files")):
        reasons.append("缺少执行源码签名")
    registry = source_map.get("control_registry")
    if not (isinstance(registry, Mapping) and registry.get("signature")):
        reasons.append("缺少控制注册表签名")
    manifest = summary.get("manifest")
    outputs = manifest.get("outputs") if isinstance(manifest, Mapping) else None
    if not isinstance(outputs, list):
        reasons.append("产物清单缺失或损坏")
    else:
        registered = [str(row["relative_path"]).replace("\\", "/") for row in artifacts]
        unregistered: list[str] = []
        for entry in outputs:
            relative = entry.get("relative_path") if isinstance(entry, Mapping) else None
            if not isinstance(relative, str):
                unregistered.append(str(relative))
                continue
            suffix = f"{session_id}/{run_id}/{relative}"
            if not any(path.endswith("/" + suffix) or path == suffix for path in registered):
                unregistered.append(relative)
        if unregistered:
            reasons.append("产物未全部登记为平台产物：" + ", ".join(unregistered))
    reasons.extend(_source_drift_reasons(summary, enqueue_source))
    return {"eligible": not reasons, "reasons": reasons}


def _source_drift_reasons(
    summary: Mapping[str, Any],
    enqueue_source: Mapping[str, Any],
) -> list[str]:
    """比较入队与执行来源；出现差异时逐项给出「来源漂移」说明。"""

    reasons: list[str] = []
    sources = summary.get("sources")
    source_map = sources if isinstance(sources, Mapping) else {}
    git = source_map.get("git")
    exec_commit = git.get("commit") if isinstance(git, Mapping) else None
    enqueue_commit = enqueue_source.get("git_commit")
    if enqueue_commit and exec_commit and enqueue_commit != exec_commit:
        reasons.append(f"来源漂移：git 提交不一致（入队 {enqueue_commit[:12]}…，执行 {exec_commit[:12]}…）")
    registry = source_map.get("control_registry")
    exec_signature = registry.get("signature") if isinstance(registry, Mapping) else None
    enqueue_signature = enqueue_source.get("control_registry_signature")
    if enqueue_signature and exec_signature and enqueue_signature != exec_signature:
        reasons.append("来源漂移：控制注册表签名不一致")
    source_signature = source_map.get("source_signature")
    exec_files: dict[str, Any] = {}
    if isinstance(source_signature, Mapping):
        for entry in source_signature.get("files", []) or []:
            if isinstance(entry, Mapping) and entry.get("path"):
                exec_files[str(entry["path"])] = entry.get("sha256")
    enqueue_files = enqueue_source.get("source_files")
    if isinstance(enqueue_files, Mapping):
        changed = sorted(
            path
            for path, sha256 in enqueue_files.items()
            if sha256
            and path in exec_files
            and exec_files[path]
            and sha256 != exec_files[path]
        )
        if changed:
            reasons.append("来源漂移：执行源码 sha256 不一致（" + ", ".join(changed) + "）")
    return reasons


def _encode_cursor(created_at: str, session_id: str) -> str:
    payload = dump_json([created_at, session_id]).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, str) for item in value):
            raise ValueError
        return value[0], value[1]
    except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ServiceError("INVALID_CURSOR", "会话分页游标无效，请从第一页重新加载", status_code=422) from exc


__all__ = [
    "EMPTY_CONTROL_DELTA",
    "EMPTY_CONTROL_SNAPSHOT",
    "ServiceError",
    "SessionService",
    "dump_json",
    "load_json",
    "utc_now",
]
