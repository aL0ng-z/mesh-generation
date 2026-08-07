"""内网网格平台的环境配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _env_int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数") from exc
    if value < minimum:
        raise ValueError(f"环境变量 {name} 不得小于 {minimum}")
    return value


def _env_float(env: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是数值") from exc
    if value < minimum:
        raise ValueError(f"环境变量 {name} 不得小于 {minimum}")
    return value


def _path_from_env(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = env.get(name)
    return Path(raw).expanduser().resolve() if raw and raw.strip() else default.resolve()


@dataclass(frozen=True)
class Settings:
    """服务和 Worker 共享的不可变配置。"""

    project_root: Path
    platform_dir: Path
    data_dir: Path
    database_path: Path
    migrations_dir: Path
    geometry_dir: Path
    artifact_dir: Path
    preview_dir: Path
    ui_dist_dir: Path
    igg_path: Path | None
    max_concurrency: int = 20
    memory_reservation_gb: float = 2.5
    min_free_memory_gb: float = 8.0
    min_free_disk_gb: float = 20.0
    job_timeout_seconds: int = 1800
    busy_timeout_ms: int = 5000
    worker_stale_seconds: int = 30
    max_upload_bytes: int = 512 * 1024 * 1024

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        project_root: str | Path | None = None,
    ) -> "Settings":
        """从环境变量构建配置，不在此阶段创建目录。"""

        env = os.environ if environ is None else environ
        root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[2]
        )
        platform_dir = root / "platform"
        if os.name == "nt":
            default_data = Path(env.get("PROGRAMDATA", r"C:\ProgramData")) / "MeshExperience"
        else:
            default_data = Path("/var/lib/mesh-experience")
        data_dir = _path_from_env(env, "MESH_DATA_DIR", default_data)
        database_path = _path_from_env(env, "MESH_DATABASE_PATH", data_dir / "mesh.sqlite3")
        raw_igg = env.get("MESH_IGG_PATH", "").strip()
        igg_path = Path(raw_igg).expanduser().resolve() if raw_igg else None
        max_concurrency = _env_int(env, "MESH_MAX_CONCURRENCY", 20, minimum=1)
        if max_concurrency > 20:
            raise ValueError("环境变量 MESH_MAX_CONCURRENCY 不得超过 20")
        return cls(
            project_root=root,
            platform_dir=platform_dir,
            data_dir=data_dir,
            database_path=database_path,
            migrations_dir=platform_dir / "migrations",
            geometry_dir=data_dir / "geometries",
            artifact_dir=data_dir / "artifacts",
            preview_dir=data_dir / "previews",
            ui_dist_dir=platform_dir / "ui" / "dist",
            igg_path=igg_path,
            max_concurrency=max_concurrency,
            memory_reservation_gb=_env_float(env, "MESH_MEMORY_RESERVATION_GB", 2.5),
            min_free_memory_gb=_env_float(env, "MESH_MIN_FREE_MEMORY_GB", 8.0),
            min_free_disk_gb=_env_float(env, "MESH_MIN_FREE_DISK_GB", 20.0),
            job_timeout_seconds=_env_int(env, "MESH_JOB_TIMEOUT_SECONDS", 1800, minimum=1),
            busy_timeout_ms=_env_int(env, "MESH_SQLITE_BUSY_TIMEOUT_MS", 5000, minimum=1),
            worker_stale_seconds=_env_int(env, "MESH_WORKER_STALE_SECONDS", 30, minimum=1),
            max_upload_bytes=_env_int(
                env,
                "MESH_MAX_UPLOAD_BYTES",
                512 * 1024 * 1024,
                minimum=1,
            ),
        )

    def ensure_directories(self) -> None:
        """创建运行期所需目录；该方法由迁移命令和进程启动显式调用。"""

        for directory in (
            self.data_dir,
            self.database_path.parent,
            self.geometry_dir,
            self.artifact_dir,
            self.preview_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


__all__ = ["Settings"]
