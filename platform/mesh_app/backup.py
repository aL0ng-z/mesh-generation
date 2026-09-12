"""SQLite 在线备份与不可变产物清单工具。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings


def backup_database(
    database_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    busy_timeout_ms: int = 5000,
) -> Path:
    """使用 SQLite Online Backup API 创建一致性快照并原子发布。"""

    source_path = Path(database_path).resolve()
    target_path = Path(destination).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"找不到待备份数据库：{source_path}")
    if source_path == target_path:
        raise ValueError("备份目标不能覆盖在线数据库")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        source = sqlite3.connect(
            source_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        target = sqlite3.connect(temporary_path, isolation_level=None)
        try:
            source.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            source.backup(target)
        finally:
            target.close()
            source.close()
        verification = sqlite3.connect(temporary_path)
        try:
            result = verification.execute("PRAGMA quick_check").fetchone()
        finally:
            verification.close()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"SQLite 备份完整性检查失败：{result}")
        os.replace(temporary_path, target_path)
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return target_path


def build_artifact_manifest(
    data_dir: str | os.PathLike[str],
    *,
    database_path: str | os.PathLike[str] | None = None,
    hash_files: bool = True,
    excluded_paths: Iterable[str | os.PathLike[str]] = (),
) -> dict[str, Any]:
    """对 DB 登记产物与数据目录实际文件建立可核验清单。"""

    root = Path(data_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"找不到数据目录：{root}")
    database = Path(database_path).resolve() if database_path is not None else None
    excluded = {Path(value).resolve() for value in excluded_paths}
    if database is not None:
        excluded.update(
            {
                database,
                Path(str(database) + "-wal"),
                Path(str(database) + "-shm"),
            }
        )
    registered = _registered_artifacts(database) if database is not None else []
    registered_by_path = {item["relative_path"]: item for item in registered}
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if _is_excluded(resolved, excluded):
            continue
        relative = resolved.relative_to(root).as_posix()
        stat = resolved.stat()
        digest = _sha256_file(resolved) if hash_files else None
        record: dict[str, Any] = {
            "relative_path": relative,
            "size_bytes": stat.st_size,
            "sha256": digest,
            "registered": relative in registered_by_path,
        }
        expected = registered_by_path.get(relative)
        if expected is not None:
            record["artifact_id"] = expected["id"]
            record["kind"] = expected["kind"]
            record["verification"] = _verification_status(expected, stat.st_size, digest)
        files.append(record)

    present_paths = {item["relative_path"] for item in files}
    registered_records = []
    for item in registered:
        record = dict(item)
        if item["relative_path"] not in present_paths:
            record["verification"] = "MISSING"
        registered_records.append(record)
    counts = {
        "filesystem_files": len(files),
        "registered_artifacts": len(registered_records),
        "missing_registered": sum(item.get("verification") == "MISSING" for item in registered_records),
        "mismatched_registered": sum(item.get("verification") in {"SIZE_MISMATCH", "HASH_MISMATCH"} for item in files),
        "unregistered_files": sum(not item["registered"] for item in files),
    }
    return {
        "schema_version": 1,
        "created_at": _timestamp(),
        "hash_algorithm": "sha256" if hash_files else None,
        "files": files,
        "registered_artifacts": registered_records,
        "counts": counts,
    }


def create_backup(
    database_path: str | os.PathLike[str],
    data_dir: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    hash_files: bool = True,
    busy_timeout_ms: int = 5000,
) -> dict[str, Any]:
    """创建数据库快照及与该快照一致的产物登记/文件清单。"""

    source = Path(database_path).resolve()
    root = Path(data_dir).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = output / f"mesh-{stamp}-{uuid.uuid4().hex[:12]}.sqlite3"
    backup_database(source, backup_path, busy_timeout_ms=busy_timeout_ms)
    manifest = build_artifact_manifest(
        root,
        database_path=backup_path,
        hash_files=hash_files,
        # 清单从一致的备份 DB 读取登记记录，但在线源库及 WAL/SHM 是数据库
        # 基础设施，不是可下载产物，必须从文件清单排除。
        excluded_paths=(
            output,
            source,
            Path(str(source) + "-wal"),
            Path(str(source) + "-shm"),
        ),
    )
    manifest["database_backup"] = {
        "filename": backup_path.name,
        "size_bytes": backup_path.stat().st_size,
        "sha256": _sha256_file(backup_path),
    }
    manifest_path = backup_path.with_name(backup_path.stem + "-artifacts.json")
    _atomic_write_json(manifest_path, manifest)
    return {
        "database": str(backup_path),
        "manifest": str(manifest_path),
        "counts": manifest["counts"],
    }


def _registered_artifacts(database_path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(
        database_path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'artifacts'"
        ).fetchone()
        if table is None:
            return []
        rows = connection.execute(
            """
            SELECT id, session_id, run_id, kind, display_name, relative_path,
                   sha256, size_bytes, mime_type, created_at
            FROM artifacts ORDER BY relative_path, id
            """
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]
    finally:
        connection.close()


def _verification_status(expected: dict[str, Any], size: int, digest: str | None) -> str:
    if int(expected["size_bytes"]) != size:
        return "SIZE_MISMATCH"
    if digest is not None and expected["sha256"] != digest:
        return "HASH_MISMATCH"
    return "MATCH" if digest is not None else "SIZE_MATCH"


def _is_excluded(path: Path, excluded: set[Path]) -> bool:
    for candidate in excluded:
        if path == candidate:
            return True
        if candidate.is_dir():
            try:
                path.relative_to(candidate)
                return True
            except ValueError:
                pass
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    """``python -m mesh_app.backup`` 运维入口。"""

    parser = argparse.ArgumentParser(description="创建 SQLite 在线备份与产物清单")
    parser.add_argument("--database", type=Path, help="在线数据库路径")
    parser.add_argument("--data-dir", type=Path, help="生产数据目录")
    parser.add_argument("--output-dir", type=Path, help="备份目录，默认 <data-dir>/backups")
    parser.add_argument("--skip-file-hashes", action="store_true", help="只核对文件大小，不扫描产物 SHA-256")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    data_dir = (args.data_dir or settings.data_dir).resolve()
    database = (args.database or settings.database_path).resolve()
    output = (args.output_dir or data_dir / "backups").resolve()
    result = create_backup(
        database,
        data_dir,
        output,
        hash_files=not args.skip_file_hashes,
        busy_timeout_ms=settings.busy_timeout_ms,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# 便于部署脚本使用的语义别名。
online_backup = backup_database
create_artifact_manifest = build_artifact_manifest


__all__ = [
    "backup_database",
    "build_artifact_manifest",
    "create_artifact_manifest",
    "create_backup",
    "online_backup",
]
