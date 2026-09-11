"""网格运行后处理独立子进程。

``python -m mesh_app.postprocess <run_id>`` 由 Worker 经受管进程树启动，只负责
单个运行的后处理：散列登记 run_dir 下的产物（成功与失败运行均登记），成功运行
再执行 CGNS 表面/线框预览转换。子进程不向 run_dir 写入诊断文件，stdout/stderr
由 Worker 重定向到 data_dir/logs 下；后处理状态（RUNNING/COMPLETED/FAILED）
由 Worker 监督落库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import Settings
from .db import Database
from .preview import prepare_preview
from .worker import (
    _append_event_in_transaction,
    _load_run_summary,
    _safe_child_path,
    _sanitize_text,
    _timestamp,
)


def run_postprocess(database: Database, settings: Settings, run_id: str) -> int:
    """执行单个运行的后处理；返回进程退出码，后处理终态由 Worker 落库。"""

    with database.reading() as connection:
        row = connection.execute(
            "SELECT session_id, status, run_summary_json FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError(f"找不到后处理目标运行：{run_id}")
    session_id = str(row["session_id"])
    run_dir = _safe_child_path(settings.artifact_dir, session_id, run_id)
    register_run_artifacts(database, settings.data_dir, session_id, run_id, run_dir)
    if row["status"] != "SUCCEEDED":
        # 失败运行只登记诊断产物，不做预览转换。
        return 0
    summary = _load_run_summary(run_dir)
    if summary is None:
        try:
            stored = json.loads(str(row["run_summary_json"]))
        except (TypeError, json.JSONDecodeError):
            stored = {}
        summary = stored if isinstance(stored, dict) else {}
    cgns_path = _summary_cgns_path(summary, run_dir)
    if cgns_path is None:
        _update_preview_status(
            database,
            run_id,
            "UNAVAILABLE",
            reason_code="CGNS_ARTIFACT_MISSING",
            message="运行成功，但没有可用于 Viewer 的 CGNS 产物。",
        )
        return 0
    preview_cache = _safe_child_path(settings.preview_dir, run_id)
    manifest = prepare_preview(cgns_path, preview_cache)
    reason_code = str(manifest.get("reason_code") or "")
    if manifest.get("available"):
        preview_status = "READY"
        message = "网格表面与结构线框预览已生成"
    elif reason_code in {"ADF_UNSUPPORTED", "PREVIEW_DEPENDENCY_MISSING"}:
        preview_status = "UNAVAILABLE"
        message = str(manifest.get("reason") or "当前网格格式不支持 Viewer")
    else:
        preview_status = "FAILED"
        message = str(manifest.get("reason") or "网格预览转换失败")
    register_preview_artifacts(database, settings.data_dir, session_id, run_id, preview_cache)
    _update_preview_status(
        database,
        run_id,
        preview_status,
        reason_code=reason_code or None,
        message=_sanitize_text(message),
    )
    return 0


def register_run_artifacts(
    database: Database,
    data_dir: Path,
    session_id: str,
    run_id: str,
    run_dir: Path,
) -> None:
    kinds = {
        ".cgns": ("CGNS", "application/x-cgns"),
        ".trb": ("TRB", "application/octet-stream"),
        ".igg": ("IGG", "application/octet-stream"),
        ".bcs": ("BCS", "text/plain; charset=utf-8"),
        ".info": ("INFO", "text/plain; charset=utf-8"),
        ".geom": ("GEOM", "application/octet-stream"),
        ".qualityreport": ("QUALITY_REPORT", "text/plain; charset=utf-8"),
        ".md": ("REPORT", "text/markdown; charset=utf-8"),
        ".json": ("RUN_SUMMARY", "application/json"),
        ".log": ("LOG", "text/plain; charset=utf-8"),
        ".geomturbo": ("GEOMETRY", "text/plain; charset=utf-8"),
    }
    candidates = [path for path in run_dir.iterdir() if path.is_file() and path.suffix.lower() in kinds]
    _insert_artifacts(database, data_dir, session_id, run_id, candidates, kinds)


def register_preview_artifacts(
    database: Database,
    data_dir: Path,
    session_id: str,
    run_id: str,
    preview_dir: Path,
) -> None:
    if not preview_dir.is_dir():
        return
    candidates = list(preview_dir.rglob("*.vtp"))
    kind_map: dict[str, tuple[str, str]] = {}
    for path in candidates:
        if path.name == "surface.vtp":
            kind = "PREVIEW_SURFACE"
        elif path.name == "wireframe.vtp":
            kind = "PREVIEW_WIREFRAME"
        else:
            kind = "PREVIEW_SLICE"
        kind_map[str(path.resolve())] = (kind, "application/vnd.vtk.vtp+xml")
    _insert_artifacts(database, data_dir, session_id, run_id, candidates, kind_map, key_by_path=True)


def _insert_artifacts(
    database: Database,
    data_dir: Path,
    session_id: str,
    run_id: str,
    paths: Sequence[Path],
    kinds: Mapping[str, tuple[str, str]],
    *,
    key_by_path: bool = False,
) -> None:
    records = []
    root = data_dir.resolve()
    for path in paths:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            continue
        key = str(resolved) if key_by_path else path.suffix.lower()
        kind_mime = kinds.get(key)
        if kind_mime is None:
            continue
        stat = resolved.stat()
        records.append(
            (
                str(uuid.uuid4()),
                session_id,
                run_id,
                kind_mime[0],
                path.name,
                relative,
                _sha256_file(resolved),
                stat.st_size,
                kind_mime[1],
                _timestamp(),
            )
        )
    if not records:
        return
    with database.transaction(immediate=True) as connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO artifacts (
                id, session_id, run_id, kind, display_name, relative_path,
                sha256, size_bytes, mime_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )


def _summary_cgns_path(summary: Mapping[str, Any], run_dir: Path) -> Path | None:
    outputs = ((summary.get("autogrid") or {}).get("outputs") or {}) if isinstance(summary.get("autogrid"), Mapping) else {}
    raw = outputs.get("cgns") if isinstance(outputs, Mapping) else None
    candidates = [Path(raw)] if isinstance(raw, str) else []
    candidates.append(run_dir / "mesh.cgns")
    for candidate in candidates:
        resolved = candidate if candidate.is_absolute() else (run_dir / candidate)
        if resolved.is_file() and resolved.stat().st_size > 0:
            try:
                resolved.resolve().relative_to(run_dir.resolve())
            except ValueError:
                continue
            return resolved.resolve()
    return None


def _update_preview_status(
    database: Database,
    run_id: str,
    status: str,
    *,
    reason_code: str | None,
    message: str,
) -> None:
    if status not in {"READY", "UNAVAILABLE", "FAILED"}:
        raise ValueError("预览状态无效")
    timestamp = _timestamp()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE runs SET preview_status = ?, updated_at = ? WHERE id = ? AND status = 'SUCCEEDED'",
            (status, timestamp, run_id),
        )
        _append_event_in_transaction(
            connection,
            run_id,
            stage="PREVIEW",
            level="INFO" if status == "READY" else "WARNING",
            progress=1.0,
            message=message,
            data={"preview_status": status, "reason_code": reason_code},
            timestamp=timestamp,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    """``python -m mesh_app.postprocess`` 子进程入口。"""

    parser = argparse.ArgumentParser(description="网格运行后处理子进程")
    parser.add_argument("run_id", help="平台运行标识")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    settings.ensure_directories()
    database = Database(settings.database_path, settings.migrations_dir, settings.busy_timeout_ms)
    database.require_current()
    try:
        return run_postprocess(database, settings, args.run_id)
    except Exception as exc:
        print(f"后处理失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "main",
    "register_preview_artifacts",
    "register_run_artifacts",
    "run_postprocess",
]
