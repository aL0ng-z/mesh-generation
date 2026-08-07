"""产物安全路径、散列、登记与 Range 下载。"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator

from .db import Database
from .sessions import ServiceError, utc_now


_RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")


@dataclass(frozen=True)
class StoredFile:
    """已经原子写入数据目录的文件元数据。"""

    path: Path
    relative_path: str
    sha256: str
    size_bytes: int
    display_name: str
    mime_type: str


@dataclass(frozen=True)
class ByteRange:
    """闭区间字节范围。"""

    start: int
    end: int
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class ArtifactStore:
    """仅允许访问 ``MESH_DATA_DIR`` 内受数据库登记的文件。"""

    def __init__(
        self,
        database: Database,
        data_dir: str | Path,
        *,
        geometry_dir: str | Path | None = None,
        artifact_dir: str | Path | None = None,
        max_upload_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.database = database
        self.data_dir = Path(data_dir).resolve()
        self.geometry_dir = Path(geometry_dir).resolve() if geometry_dir else self.data_dir / "geometries"
        self.artifact_dir = Path(artifact_dir).resolve() if artifact_dir else self.data_dir / "artifacts"
        self.max_upload_bytes = max_upload_bytes

    def save_geometry(
        self,
        stream: BinaryIO,
        *,
        session_id: str,
        filename: str,
    ) -> StoredFile:
        """流式限制大小、计算 SHA-256，并原子保存 `.geomTurbo`。"""

        _validate_upload_filename(filename)
        target_directory = self.geometry_dir / session_id
        _ensure_under(self.data_dir, target_directory)
        created_directory = not target_directory.exists()
        target_directory.mkdir(parents=True, exist_ok=True)
        target = target_directory / "source.geomTurbo"
        if target.exists():
            raise ServiceError("GEOMETRY_ALREADY_EXISTS", "会话几何文件已存在", status_code=409)
        temporary = target_directory / f".{uuid.uuid4().hex}.upload"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise ServiceError("INVALID_UPLOAD", "上传流不是二进制文件", status_code=422)
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise ServiceError(
                            "UPLOAD_TOO_LARGE",
                            f"几何文件超过允许的 {self.max_upload_bytes} 字节",
                            status_code=413,
                            details={"max_bytes": self.max_upload_bytes},
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise ServiceError("EMPTY_UPLOAD", "上传的几何文件为空", status_code=422)
            os.replace(temporary, target)
        except BaseException:
            if temporary.is_file():
                temporary.unlink()
            if created_directory:
                try:
                    target_directory.rmdir()
                except OSError:
                    pass
            raise
        return StoredFile(
            path=target,
            relative_path=target.relative_to(self.data_dir).as_posix(),
            sha256=digest.hexdigest(),
            size_bytes=size,
            display_name=filename,
            mime_type="text/plain",
        )

    def register(
        self,
        *,
        session_id: str,
        run_id: str | None,
        kind: str,
        display_name: str,
        relative_path: str,
        sha256: str | None = None,
        size_bytes: int | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """验证实际文件后追加产物元数据。"""

        path = self.resolve_path(relative_path)
        if not path.is_file():
            raise ServiceError("ARTIFACT_FILE_MISSING", "要登记的产物文件不存在", status_code=422)
        actual_sha256, actual_size = hash_file(path)
        if sha256 is not None and sha256 != actual_sha256:
            raise ServiceError("ARTIFACT_HASH_MISMATCH", "产物散列与实际文件不一致", status_code=422)
        if size_bytes is not None and size_bytes != actual_size:
            raise ServiceError("ARTIFACT_SIZE_MISMATCH", "产物大小与实际文件不一致", status_code=422)
        artifact_id = str(uuid.uuid4())
        timestamp = utc_now()
        normalized_kind = kind.strip().upper()
        if not normalized_kind or len(normalized_kind) > 100:
            raise ServiceError("INVALID_ARTIFACT_KIND", "产物类型无效", status_code=422)
        with self.database.transaction(immediate=True) as connection:
            session = connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if session is None:
                raise ServiceError("SESSION_NOT_FOUND", "找不到产物所属会话", status_code=404)
            if run_id is not None:
                run = connection.execute(
                    "SELECT session_id FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run is None or run["session_id"] != session_id:
                    raise ServiceError("RUN_NOT_FOUND", "产物所属运行不在当前会话", status_code=404)
            try:
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        id, session_id, run_id, kind, display_name, relative_path,
                        sha256, size_bytes, mime_type, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        session_id,
                        run_id,
                        normalized_kind,
                        _safe_display_name(display_name),
                        path.relative_to(self.data_dir).as_posix(),
                        actual_sha256,
                        actual_size,
                        mime_type or mimetypes.guess_type(display_name)[0] or "application/octet-stream",
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ServiceError(
                    "ARTIFACT_ALREADY_REGISTERED",
                    "该服务端文件已经登记为产物",
                    status_code=409,
                ) from exc
        return self.get_metadata(artifact_id)

    def get_metadata(self, artifact_id: str) -> dict[str, Any]:
        """读取产物元数据但不暴露服务端绝对路径。"""

        with self.database.reading() as connection:
            row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise ServiceError("ARTIFACT_NOT_FOUND", "找不到指定产物", status_code=404)
        return _artifact_metadata(row)

    def open_download(self, artifact_id: str, range_header: str | None = None) -> tuple[dict[str, Any], Path, ByteRange]:
        """解析受控产物和单段 Range，供 API 构造流式响应。"""

        with self.database.reading() as connection:
            row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise ServiceError("ARTIFACT_NOT_FOUND", "找不到指定产物", status_code=404)
        path = self.resolve_path(str(row["relative_path"]))
        if not path.is_file():
            raise ServiceError(
                "ARTIFACT_FILE_MISSING",
                "产物文件已缺失，请联系运维检查数据目录",
                status_code=410,
            )
        actual_size = path.stat().st_size
        if actual_size != row["size_bytes"]:
            raise ServiceError(
                "ARTIFACT_SIZE_CHANGED",
                "产物文件大小与登记信息不一致，已拒绝下载",
                status_code=409,
            )
        byte_range = parse_range_header(range_header, actual_size)
        return _artifact_metadata(row), path, byte_range

    def resolve_path(self, relative_path: str) -> Path:
        """将数据库相对路径安全解析到数据根目录。"""

        if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
            raise ServiceError("UNSAFE_PATH", "产物相对路径无效", status_code=400)
        normalized = relative_path.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if (
            pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or (pure.parts and ":" in pure.parts[0])
        ):
            raise ServiceError("UNSAFE_PATH", "产物路径越过数据目录", status_code=400)
        candidate = (self.data_dir / Path(*pure.parts)).resolve()
        _ensure_under(self.data_dir, candidate)
        return candidate

    def discard_unregistered(self, stored: StoredFile) -> None:
        """在数据库创建失败时清理由本次上传产生的精确文件。"""

        target = stored.path.resolve()
        _ensure_under(self.geometry_dir, target)
        if target.is_file():
            target.unlink()
        session_directory = target.parent
        if session_directory.parent == self.geometry_dir and session_directory.is_dir():
            try:
                session_directory.rmdir()
            except OSError:
                pass


def parse_range_header(range_header: str | None, size: int) -> ByteRange:
    """解析 HTTP 单段字节范围；不支持多段 Range。"""

    if size < 0:
        raise ValueError("文件大小不能为负数")
    normalized_header = range_header.strip() if range_header is not None else ""
    if size == 0:
        if normalized_header:
            raise ServiceError(
                "RANGE_NOT_SATISFIABLE",
                "空文件不支持字节范围下载",
                status_code=416,
                details={"size": 0},
            )
        return ByteRange(0, -1, 0)
    if not normalized_header:
        return ByteRange(0, size - 1, size)
    match = _RANGE_PATTERN.fullmatch(normalized_header)
    if match is None or "," in normalized_header:
        raise ServiceError(
            "INVALID_RANGE",
            "Range 仅支持一个 bytes 字节范围",
            status_code=416,
            details={"size": size},
        )
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ServiceError("INVALID_RANGE", "Range 字节范围不能为空", status_code=416, details={"size": size})
    if not start_text:
        suffix = _range_integer(end_text, size)
        if suffix <= 0:
            raise ServiceError("INVALID_RANGE", "Range 后缀长度必须大于零", status_code=416, details={"size": size})
        start = max(size - suffix, 0)
        end = size - 1
    else:
        start = _range_integer(start_text, size)
        end = _range_integer(end_text, size) if end_text else size - 1
        if start >= size or end < start:
            raise ServiceError(
                "RANGE_NOT_SATISFIABLE",
                "请求的字节范围超出产物大小",
                status_code=416,
                details={"size": size},
            )
        end = min(end, size - 1)
    return ByteRange(start, end, size)


def _range_integer(value: str, size: int) -> int:
    if len(value) > 20:
        raise ServiceError(
            "INVALID_RANGE",
            "Range 字节位置过长",
            status_code=416,
            details={"size": size},
        )
    try:
        return int(value)
    except ValueError as exc:
        raise ServiceError(
            "INVALID_RANGE",
            "Range 字节位置无效",
            status_code=416,
            details={"size": size},
        ) from exc


def iter_file_range(path: Path, byte_range: ByteRange, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """按闭区间流式读取文件，不把大产物加载进内存。"""

    remaining = byte_range.length
    if remaining <= 0:
        return
    with path.open("rb") as stream:
        stream.seek(byte_range.start)
        while remaining > 0:
            chunk = stream.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def hash_file(path: str | Path) -> tuple[str, int]:
    """流式计算文件 SHA-256 和大小。"""

    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _validate_upload_filename(filename: str) -> None:
    if not filename or "\x00" in filename or "/" in filename or "\\" in filename:
        raise ServiceError("UNSAFE_FILENAME", "上传文件名不能包含路径", status_code=422)
    if not filename.lower().endswith(".geomturbo"):
        raise ServiceError("INVALID_FILE_TYPE", "仅接受 .geomTurbo 几何文件", status_code=422)


def _safe_display_name(value: str) -> str:
    if not value or "\x00" in value or "/" in value or "\\" in value:
        raise ServiceError("UNSAFE_FILENAME", "产物显示文件名不能包含路径", status_code=422)
    return value


def _ensure_under(root: Path, candidate: Path) -> None:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ServiceError("UNSAFE_PATH", "文件路径越过数据目录", status_code=400) from exc


def _artifact_metadata(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "run_id": row["run_id"],
        "type": row["kind"],
        "display_name": row["display_name"],
        "sha256": row["sha256"],
        "size": row["size_bytes"],
        "mime_type": row["mime_type"],
        "created_at": row["created_at"],
    }


__all__ = [
    "ArtifactStore",
    "ByteRange",
    "StoredFile",
    "hash_file",
    "iter_file_range",
    "parse_range_header",
]
