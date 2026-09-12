"""SQLite 持久队列与本地网格 Worker。

Worker 只在资源门控通过后原子领取任务，以独立 Python 子进程调用
``src/mesh.py``。进程退出后一次性写入终态；网格质量 FAIL/UNKNOWN 不会被误判为
执行失败。产物登记与预览转换由固定单并发的独立后处理子进程完成，主循环只负责
监督网格进程、后处理超时、心跳、停止与新任务领取。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, IO, Iterator, Mapping, Sequence

from .config import Settings
from .db import Database, connect_database
from .windows_job import ManagedProcess, spawn_managed_process


HARD_MAX_CONCURRENCY = 20
DEFAULT_LICENSE_BACKOFF_THRESHOLD = 2
_GIB = 1024**3
_LICENSE_PATTERNS = (
    re.compile(r"(?:license|licence).{0,80}(?:checkout|unavailable|denied|failed|limit|in use|error)", re.I | re.S),
    re.compile(r"(?:checkout|obtain).{0,80}(?:license|licence)", re.I | re.S),
    re.compile(r"(?:flexlm|lmgrd|licensed number of users)", re.I),
    re.compile(r"(?:no valid|all available).{0,40}(?:license|licence)", re.I),
    re.compile(r"许可证.{0,40}(?:失败|不足|不可用|占用|拒绝)"),
)


@dataclass(frozen=True)
class ResourceSnapshot:
    """一次领取前的资源门控快照。"""

    available_memory_gb: float
    free_disk_gb: float
    running_count: int
    reservation_gb: float
    min_free_memory_gb: float
    min_free_disk_gb: float
    allowed: bool
    reason_code: str | None
    reason: str | None
    postprocess_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunDirectoryUnavailable(OSError):
    """运行目录无法独占创建：已存在、被并发占用或创建失败。"""


@dataclass
class _RunningTask:
    run_id: str
    session_id: str
    run_dir: Path
    process: ManagedProcess
    stdout_stream: IO[bytes]
    stderr_stream: IO[bytes]
    started_monotonic: float

    def close_streams(self) -> None:
        for stream in (self.stdout_stream, self.stderr_stream):
            if not stream.closed:
                stream.close()


@dataclass
class _PostprocessTask:
    run_id: str
    process: ManagedProcess
    stdout_stream: IO[bytes]
    stderr_stream: IO[bytes]
    started_monotonic: float

    def close_streams(self) -> None:
        for stream in (self.stdout_stream, self.stderr_stream):
            if not stream.closed:
                stream.close()


class LicenseBackoff:
    """对连续许可证容量失败执行有上限的指数退避。"""

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_LICENSE_BACKOFF_THRESHOLD,
        base_seconds: float = 15.0,
        max_seconds: float = 300.0,
    ) -> None:
        if threshold < 1 or base_seconds < 0 or max_seconds < base_seconds:
            raise ValueError("许可证退避参数无效")
        self.threshold = threshold
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self.consecutive_failures = 0
        self.until = 0.0

    def record_license_failure(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        self.consecutive_failures += 1
        if self.consecutive_failures < self.threshold:
            return 0.0
        exponent = self.consecutive_failures - self.threshold
        delay = min(self.max_seconds, self.base_seconds * (2**exponent))
        self.until = max(self.until, current + delay)
        return delay

    def record_non_license_result(self) -> None:
        self.consecutive_failures = 0
        self.until = 0.0

    def remaining(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, self.until - current)

    def blocked(self, now: float | None = None) -> bool:
        return self.remaining(now) > 0


def evaluate_resource_gate(
    *,
    available_memory_gb: float,
    free_disk_gb: float,
    running_count: int,
    memory_reservation_gb: float = 2.5,
    min_free_memory_gb: float = 8.0,
    min_free_disk_gb: float = 20.0,
    postprocess_active: bool = False,
) -> ResourceSnapshot:
    """按“现有槽位 + 下一槽位”的虚拟预留量判定能否领取。

    活动后处理额外计入一份内存预留，避免网格已 SUCCEEDED 后忽略仍在进行的
    散列与预览转换占用的资源。
    """

    if running_count < 0:
        raise ValueError("运行任务数不能为负数")
    values = (available_memory_gb, free_disk_gb, memory_reservation_gb, min_free_memory_gb, min_free_disk_gb)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("资源门控参数必须是非负有限数")
    reserved_for_next = memory_reservation_gb * (running_count + 1 + (1 if postprocess_active else 0))
    required_memory = min_free_memory_gb + reserved_for_next
    if available_memory_gb < required_memory:
        allowed = False
        reason_code = "INSUFFICIENT_MEMORY"
        reason = (
            f"可用内存 {available_memory_gb:.2f} GiB，领取下一任务需保留 "
            f"{required_memory:.2f} GiB"
        )
    elif free_disk_gb < min_free_disk_gb:
        allowed = False
        reason_code = "INSUFFICIENT_DISK"
        reason = f"可用磁盘 {free_disk_gb:.2f} GiB，最低要求 {min_free_disk_gb:.2f} GiB"
    else:
        allowed = True
        reason_code = None
        reason = None
    return ResourceSnapshot(
        available_memory_gb=available_memory_gb,
        free_disk_gb=free_disk_gb,
        running_count=running_count,
        reservation_gb=reserved_for_next,
        min_free_memory_gb=min_free_memory_gb,
        min_free_disk_gb=min_free_disk_gb,
        allowed=allowed,
        reason_code=reason_code,
        reason=reason,
        postprocess_active=postprocess_active,
    )


def probe_resources(
    data_dir: str | os.PathLike[str],
    *,
    running_count: int,
    memory_reservation_gb: float = 2.5,
    min_free_memory_gb: float = 8.0,
    min_free_disk_gb: float = 20.0,
    postprocess_active: bool = False,
) -> ResourceSnapshot:
    """读取物理资源并套用可测试的纯门控函数。"""

    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    available_memory = _available_memory_bytes() / _GIB
    free_disk = shutil.disk_usage(root).free / _GIB
    return evaluate_resource_gate(
        available_memory_gb=available_memory,
        free_disk_gb=free_disk,
        running_count=running_count,
        memory_reservation_gb=memory_reservation_gb,
        min_free_memory_gb=min_free_memory_gb,
        min_free_disk_gb=min_free_disk_gb,
        postprocess_active=postprocess_active,
    )


def atomic_claim_run(
    database: Database | sqlite3.Connection | str | os.PathLike[str],
    worker_id: str,
    *,
    max_concurrency: int = HARD_MAX_CONCURRENCY,
    now: datetime | str | None = None,
    busy_timeout_ms: int = 5000,
) -> dict[str, Any] | None:
    """在 ``BEGIN IMMEDIATE`` 内检查全局槽位并领取最早的 QUEUED 运行。"""

    limit = min(int(max_concurrency), HARD_MAX_CONCURRENCY)
    if limit <= 0:
        return None
    timestamp = _timestamp(now)
    with _connection(database, busy_timeout_ms=busy_timeout_ms) as connection:
        _begin_immediate(connection)
        try:
            running_count = int(
                connection.execute("SELECT COUNT(*) FROM runs WHERE status = 'RUNNING'").fetchone()[0]
            )
            if running_count >= limit:
                connection.rollback()
                return None
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE status = 'QUEUED'
                ORDER BY created_at ASC, sequence ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            run_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
            updated = connection.execute(
                """
                UPDATE runs
                SET status = 'RUNNING', worker_id = ?, heartbeat_at = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?, progress = MAX(progress, 0.01),
                    error_code = NULL, error_message = NULL
                WHERE id = ? AND status = 'QUEUED'
                """,
                (worker_id, timestamp, timestamp, timestamp, run_id),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            _append_event_in_transaction(
                connection,
                run_id,
                stage="QUEUE",
                level="INFO",
                progress=0.01,
                message="Worker 已原子领取运行任务",
                data={"worker_id": worker_id},
                timestamp=timestamp,
            )
            claimed = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            connection.commit()
            return _row_to_dict(claimed)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


def recover_stale_runs(
    database: Database | sqlite3.Connection | str | os.PathLike[str],
    *,
    stale_after_seconds: float = 30.0,
    now: datetime | str | None = None,
    busy_timeout_ms: int = 5000,
    exclude_worker_id: str | None = None,
) -> list[str]:
    """把其他 Worker 心跳过期的运行修正为 FAILED，返回本次回收 ID。"""

    if stale_after_seconds < 0:
        raise ValueError("心跳过期秒数不能为负")
    current = _datetime_value(now)
    cutoff = current - timedelta(seconds=stale_after_seconds)
    timestamp = _format_datetime(current)
    recovered: list[str] = []
    with _connection(database, busy_timeout_ms=busy_timeout_ms) as connection:
        _begin_immediate(connection)
        try:
            rows = connection.execute(
                """
                SELECT id, heartbeat_at, started_at, updated_at, created_at
                FROM runs WHERE status = 'RUNNING'
                    AND (? IS NULL OR worker_id IS NULL OR worker_id <> ?)
                """,
                (exclude_worker_id, exclude_worker_id),
            ).fetchall()
            for row in rows:
                last_seen_raw = row["heartbeat_at"] or row["started_at"] or row["updated_at"] or row["created_at"]
                last_seen = _parse_datetime(last_seen_raw) if last_seen_raw else datetime.min.replace(tzinfo=timezone.utc)
                if last_seen > cutoff:
                    continue
                updated = connection.execute(
                    """
                    UPDATE runs
                    SET status = 'FAILED', preview_status = 'UNAVAILABLE',
                        error_code = 'WORKER_HEARTBEAT_LOST',
                        error_message = 'Worker 心跳中断，运行已修正为失败；请创建重试节点。',
                        pid = NULL, finished_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'RUNNING'
                    """,
                    (timestamp, timestamp, row["id"]),
                )
                if updated.rowcount != 1:
                    continue
                recovered.append(str(row["id"]))
                _append_event_in_transaction(
                    connection,
                    row["id"],
                    stage="RECOVERY",
                    level="ERROR",
                    progress=None,
                    message="Worker 心跳过期，运行已修正为失败",
                    data={"error_code": "WORKER_HEARTBEAT_LOST"},
                    timestamp=timestamp,
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
    return recovered


def control_snapshot_to_assignments(snapshot: Mapping[str, Any] | str) -> list[str]:
    """把平台规范化控制快照转换为 ``src/mesh.py --set`` 表达式。"""

    if isinstance(snapshot, str):
        try:
            decoded = json.loads(snapshot)
        except json.JSONDecodeError as exc:
            raise ValueError("control_snapshot_json 不是合法 JSON") from exc
    else:
        decoded = dict(snapshot)
    items = decoded.get("items", [])
    if not isinstance(items, list):
        raise ValueError("控制快照 items 必须是数组")
    normalized: list[tuple[str, str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("控制快照条目必须是对象")
        key = item.get("key")
        selector = item.get("selector")
        if not isinstance(key, str) or "/" not in key or not isinstance(selector, str):
            raise ValueError("控制快照条目缺少有效 key/selector")
        normalized.append((selector, key, item.get("value")))
    assignments: list[str] = []
    for selector, key, value in sorted(normalized, key=lambda item: (item[0], item[1])):
        scope, local_key = key.split("/", 1)
        if scope == "configuration":
            path = f"configuration/{local_key}"
        elif scope == "wizard":
            if not selector or selector == "configuration":
                raise ValueError(f"wizard 控制缺少实体 selector：{key}")
            path = f"{selector}/wizard/{local_key}"
        else:
            if not selector or selector == "configuration":
                raise ValueError(f"实体控制缺少 selector：{key}")
            path = f"{selector}/{local_key}"
        assignments.append(f"{path}={_control_value_text(value)}")
    return assignments


def build_mesh_command(
    *,
    project_root: str | os.PathLike[str],
    geometry_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    run_id: str,
    control_snapshot: Mapping[str, Any] | str,
    igg_path: str | os.PathLike[str] | None = None,
    timeout_seconds: int | None = None,
    python_executable: str | os.PathLike[str] = sys.executable,
) -> list[str]:
    """构造不经过 shell 的网格 CLI 命令；``run_id`` 与平台运行身份一致。"""

    if not run_id or not isinstance(run_id, str):
        raise ValueError("run_id 必须是平台运行标识字符串")
    root = Path(project_root).resolve()
    mesh_script = root / "src" / "mesh.py"
    if not mesh_script.is_file():
        raise FileNotFoundError(f"找不到网格入口：{mesh_script}")
    geometry = Path(geometry_path).resolve()
    if not geometry.is_file():
        raise FileNotFoundError(f"找不到几何输入：{geometry}")
    command = [
        os.fspath(python_executable),
        str(mesh_script),
        str(geometry),
        "--out",
        str(Path(output_dir).resolve()),
        "--run-id",
        run_id,
    ]
    if igg_path is not None:
        command.extend(("--igg", str(Path(igg_path).resolve())))
    if timeout_seconds is not None:
        if timeout_seconds < 1:
            raise ValueError("任务超时秒数必须大于 0")
        command.extend(("--timeout", str(int(timeout_seconds))))
    for assignment in control_snapshot_to_assignments(control_snapshot):
        command.extend(("--set", assignment))
    return command


def is_license_failure(text: str) -> bool:
    """保守识别许可证容量/签出失败，避免普通路径中的 license 单词误报。"""

    return any(pattern.search(text) is not None for pattern in _LICENSE_PATTERNS)


class Worker:
    """单进程并发调度器；每个网格任务仍是独立操作系统进程。"""

    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        worker_id: str | None = None,
        resource_probe: Callable[[int, bool], ResourceSnapshot] | None = None,
        license_backoff: LicenseBackoff | None = None,
    ) -> None:
        self.settings = settings
        self.database = database or Database(
            settings.database_path, settings.migrations_dir, settings.busy_timeout_ms
        )
        self.worker_id = worker_id or f"{os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or 'worker'}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.max_concurrency = min(max(1, int(settings.max_concurrency)), HARD_MAX_CONCURRENCY)
        self.resource_probe = resource_probe or self._default_resource_probe
        self.license_backoff = license_backoff or LicenseBackoff()
        self._tasks: dict[str, _RunningTask] = {}
        self._postprocess_queue: list[str] = []
        self._postprocess_task: _PostprocessTask | None = None
        self._started = False
        self._stop_requested = False
        self._last_heartbeat = 0.0
        self._last_recovery = 0.0
        self._heartbeat_interval = max(0.2, min(5.0, settings.worker_stale_seconds / 3.0))
        self._heartbeat_lock = threading.Lock()
        self.last_resource_snapshot: ResourceSnapshot | None = None

    def run_once(self) -> bool:
        """推进一次调度循环；返回本轮是否发生领取或完成。"""

        activity = False
        if not self._started:
            recover_stale_runs(
                self.database,
                stale_after_seconds=self.settings.worker_stale_seconds,
                busy_timeout_ms=self.settings.busy_timeout_ms,
                exclude_worker_id=self.worker_id,
            )
            if self._recover_pending_postprocess():
                activity = True
            self._started = True
            self._last_recovery = time.monotonic()

        current = time.monotonic()
        if current - self._last_recovery >= self._heartbeat_interval:
            recovered = recover_stale_runs(
                self.database,
                stale_after_seconds=self.settings.worker_stale_seconds,
                busy_timeout_ms=self.settings.busy_timeout_ms,
                exclude_worker_id=self.worker_id,
            )
            self._postprocess_queue.extend(recovered)
            activity = activity or bool(recovered)
            self._last_recovery = current

        if self._poll_tasks():
            activity = True
        if self._supervise_postprocess():
            activity = True
        self._dispatch_postprocess()
        self._heartbeat(force=not self._last_heartbeat)
        if self._stop_requested or self.license_backoff.blocked():
            self._heartbeat(force=True, extra_status={"license_backoff_seconds": self.license_backoff.remaining()})
            return activity

        while len(self._tasks) < self.max_concurrency and not self._stop_requested:
            running_count = _count_running(self.database)
            if running_count >= self.max_concurrency:
                break
            snapshot = self.resource_probe(running_count, self._postprocess_task is not None)
            self.last_resource_snapshot = snapshot
            if not snapshot.allowed:
                self._heartbeat(force=True)
                break
            claimed = atomic_claim_run(
                self.database,
                self.worker_id,
                max_concurrency=self.max_concurrency,
                busy_timeout_ms=self.settings.busy_timeout_ms,
            )
            if claimed is None:
                break
            activity = True
            try:
                self._start_claimed_run(claimed)
            except RunDirectoryUnavailable as exc:
                mark_run_failed(
                    self.database,
                    str(claimed["id"]),
                    error_code="RUN_DIR_UNAVAILABLE",
                    error_message=self._sanitize(f"运行目录不可用：{exc}"),
                )
                set_postprocess_terminal(
                    self.database,
                    str(claimed["id"]),
                    "COMPLETED",
                    message="网格运行未启动，没有本运行的产物需要登记",
                )
            except Exception as exc:
                mark_run_failed(
                    self.database,
                    str(claimed["id"]),
                    error_code="WORKER_START_FAILED",
                    error_message=self._sanitize(f"无法启动网格子进程：{type(exc).__name__}: {exc}"),
                )
                set_postprocess_terminal(
                    self.database,
                    str(claimed["id"]),
                    "COMPLETED",
                    message="网格运行未启动，没有本运行的产物需要登记",
                )
        self._heartbeat(force=activity)
        return activity

    def run_forever(self, *, poll_interval: float = 1.0) -> None:
        """持续运行直到收到中断信号。"""

        if poll_interval <= 0:
            raise ValueError("轮询间隔必须大于 0")
        try:
            while not self._stop_requested:
                self.run_once()
                time.sleep(min(poll_interval, 60.0))
        except KeyboardInterrupt:
            self._stop_requested = True
        finally:
            self.shutdown()

    def request_stop(self) -> None:
        self._stop_requested = True

    def shutdown(self) -> None:
        """终止仍在运行的完整进程树，并把对应运行写入失败终态。"""

        self._stop_requested = True
        for run_id, task in list(self._tasks.items()):
            task.process.terminate_tree(exit_code=1)
            task.close_streams()
            mark_run_failed(
                self.database,
                run_id,
                error_code="WORKER_SHUTDOWN",
                error_message="Worker 已停止，运行被安全终止；请创建重试节点。",
            )
            self._tasks.pop(run_id, None)
        if self._postprocess_task is not None:
            # 后处理保留 RUNNING 状态，由下次启动恢复重新排队；不写失败终态。
            task = self._postprocess_task
            task.process.terminate_tree(exit_code=1)
            task.close_streams()
            self._postprocess_task = None
        self._postprocess_queue.clear()
        self._heartbeat(force=True, extra_status={"stopping": True})

    def _default_resource_probe(self, running_count: int, postprocess_active: bool = False) -> ResourceSnapshot:
        return probe_resources(
            self.settings.data_dir,
            running_count=running_count,
            postprocess_active=postprocess_active,
            memory_reservation_gb=self.settings.memory_reservation_gb,
            min_free_memory_gb=self.settings.min_free_memory_gb,
            min_free_disk_gb=self.settings.min_free_disk_gb,
        )

    def _start_claimed_run(self, claimed: Mapping[str, Any]) -> None:
        run_id = str(claimed["id"])
        with self.database.reading() as connection:
            row = connection.execute(
                """
                SELECT r.*, s.geometry_relative_path
                FROM runs r JOIN sessions s ON s.id = r.session_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"领取后找不到运行：{run_id}")
        session_id = str(row["session_id"])
        geometry_path = _safe_relative_path(self.settings.data_dir, row["geometry_relative_path"])
        run_dir = _safe_child_path(self.settings.artifact_dir, session_id, run_id)
        # 会话父目录可复用，但运行目录必须由本 Worker 独占创建：绝不覆盖既有
        # 目录，也绝不向既有日志追加。
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            run_dir.mkdir()
        except FileExistsError as exc:
            raise RunDirectoryUnavailable(f"运行目录已存在，拒绝复用或覆盖：{run_dir}") from exc
        except OSError as exc:
            raise RunDirectoryUnavailable(f"无法创建运行目录：{run_dir}（{exc}）") from exc
        command = build_mesh_command(
            project_root=self.settings.project_root,
            geometry_path=geometry_path,
            output_dir=run_dir,
            run_id=run_id,
            control_snapshot=row["control_snapshot_json"],
            igg_path=self.settings.igg_path,
            timeout_seconds=self.settings.job_timeout_seconds,
        )
        # 文件名保持共享契约 B 的白名单；CLI 会容忍这两份预写日志。
        stdout_stream = (run_dir / "worker.stdout.log").open("wb")
        stderr_stream = (run_dir / "worker.stderr.log").open("wb")
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            process = spawn_managed_process(
                command,
                cwd=self.settings.project_root,
                env=environment,
                stdout=stdout_stream,
                stderr=stderr_stream,
            )
        except BaseException:
            stdout_stream.close()
            stderr_stream.close()
            raise
        task = _RunningTask(
            run_id=run_id,
            session_id=session_id,
            run_dir=run_dir,
            process=process,
            stdout_stream=stdout_stream,
            stderr_stream=stderr_stream,
            started_monotonic=time.monotonic(),
        )
        self._tasks[run_id] = task
        try:
            timestamp = _timestamp()
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE runs SET pid = ?, heartbeat_at = ?, updated_at = ?, progress = MAX(progress, 0.02) WHERE id = ? AND status = 'RUNNING'",
                    (process.pid, timestamp, timestamp, run_id),
                )
                _append_event_in_transaction(
                    connection,
                    run_id,
                    stage="MESH",
                    level="INFO",
                    progress=0.02,
                    message="已启动独立网格子进程",
                    data={"pid": process.pid, "job_object": process.uses_job_object},
                    timestamp=timestamp,
                )
        except BaseException:
            self._tasks.pop(run_id, None)
            try:
                process.terminate_tree(exit_code=1)
            finally:
                task.close_streams()
            raise

    def _poll_tasks(self) -> bool:
        activity = False
        for run_id, task in list(self._tasks.items()):
            elapsed = time.monotonic() - task.started_monotonic
            timed_out = elapsed > self.settings.job_timeout_seconds
            returncode = task.process.poll()
            if returncode is None and not timed_out:
                continue
            activity = True
            if timed_out and returncode is None:
                task.process.terminate_tree(exit_code=1)
                returncode = task.process.returncode if task.process.returncode is not None else 1
                error_code = "MESH_TIMEOUT"
                error_message = f"网格任务超过 {self.settings.job_timeout_seconds} 秒，进程树已终止。"
            else:
                task.process.close()
                error_code = "MESH_PROCESS_FAILED"
                error_message = "网格子进程执行失败"
            task.close_streams()
            summary = _load_run_summary(task.run_dir)
            output_text = _collect_failure_text(task.run_dir, summary)
            if returncode == 0 and summary is not None:
                self._complete_success(task, summary)
                self.license_backoff.record_non_license_result()
            else:
                if returncode == 0 and summary is None:
                    error_code = "RUN_SUMMARY_MISSING"
                    error_message = "网格子进程返回成功，但未生成有效 run_summary.json"
                elif is_license_failure(output_text):
                    error_code = "LICENSE_UNAVAILABLE"
                    error_message = "AutoGrid 许可证暂不可用；调度器将短暂退避，请稍后重试。"
                    self.license_backoff.record_license_failure()
                else:
                    self.license_backoff.record_non_license_result()
                    detail = _summary_error(summary) or _last_nonempty_line(output_text)
                    if detail:
                        error_message = f"{error_message}：{detail}"
                mark_run_failed(
                    self.database,
                    run_id,
                    error_code=error_code,
                    error_message=self._sanitize(error_message),
                    run_summary=summary,
                )
            # 成功与失败运行的产物登记都进入固定单并发的后处理子进程通道。
            self._postprocess_queue.append(run_id)
            self._tasks.pop(run_id, None)
        return activity

    def _complete_success(self, task: _RunningTask, summary: dict[str, Any]) -> None:
        quality = summary.get("quality") if isinstance(summary.get("quality"), Mapping) else {}
        quality_status = str(((quality.get("result") or {}).get("status") or "UNKNOWN")).upper()
        if quality_status not in {"PASS", "WARN", "FAIL", "UNKNOWN"}:
            quality_status = "UNKNOWN"
        mark_run_succeeded(
            self.database,
            task.run_id,
            run_summary=summary,
            quality=dict(quality),
            quality_status=quality_status,
        )

    def _recover_pending_postprocess(self) -> int:
        """Worker 重启时把未完成的后处理重新排队；不在启动路径同步转换。"""

        with self.database.reading() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM runs
                WHERE status IN ('SUCCEEDED', 'FAILED')
                  AND postprocess_status IN ('PENDING', 'RUNNING')
                ORDER BY finished_at, sequence
                """
            ).fetchall()
        for row in rows:
            run_id = str(row["id"])
            timestamp = _timestamp()
            with self.database.transaction(immediate=True) as connection:
                # 上次中断遗留的 RUNNING 重置为 PENDING，稍后由专用通道重新投递。
                connection.execute(
                    """
                    UPDATE runs SET postprocess_status = 'PENDING',
                        postprocess_started_at = NULL, updated_at = ?
                    WHERE id = ? AND postprocess_status = 'RUNNING'
                    """,
                    (timestamp, run_id),
                )
                _append_event_in_transaction(
                    connection,
                    run_id,
                    stage="RECOVERY",
                    level="INFO",
                    progress=None,
                    message="Worker 启动后恢复未完成的后处理，已重新排队",
                    data={"postprocess_status": "PENDING"},
                    timestamp=timestamp,
                )
            self._postprocess_queue.append(run_id)
        return len(rows)

    def _dispatch_postprocess(self) -> bool:
        """从队列投递一个后处理子进程；固定单并发。"""

        if self._stop_requested or self._postprocess_task is not None or not self._postprocess_queue:
            return False
        run_id = self._postprocess_queue.pop(0)
        if not mark_postprocess_running(self.database, run_id):
            return True
        log_dir = _safe_child_path(self.settings.data_dir, "logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        # 后处理诊断不写入 run_dir，避免与产物登记混淆。
        log_stream = (log_dir / f"postprocess-{run_id}.log").open("wb")
        command = [sys.executable, "-m", "mesh_app.postprocess", run_id]
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["MESH_DATA_DIR"] = str(self.settings.data_dir)
        environment["MESH_DATABASE_PATH"] = str(self.settings.database_path)
        try:
            process = spawn_managed_process(
                command,
                cwd=self.settings.platform_dir,
                env=environment,
                stdout=log_stream,
                stderr=log_stream,
            )
        except BaseException as exc:
            log_stream.close()
            set_postprocess_terminal(
                self.database,
                run_id,
                "FAILED",
                error=self._sanitize(f"无法启动后处理子进程：{type(exc).__name__}: {exc}"),
                message="后处理子进程启动失败",
            )
            return True
        self._postprocess_task = _PostprocessTask(
            run_id=run_id,
            process=process,
            stdout_stream=log_stream,
            stderr_stream=log_stream,
            started_monotonic=time.monotonic(),
        )
        return True

    def _supervise_postprocess(self) -> bool:
        """轮询唯一活动的后处理子进程，执行外层超时与终态登记。"""

        task = self._postprocess_task
        if task is None:
            return False
        elapsed = time.monotonic() - task.started_monotonic
        timed_out = elapsed > self.settings.postprocess_timeout_seconds
        returncode = task.process.poll()
        if returncode is None and not timed_out:
            return False
        if timed_out and returncode is None:
            task.process.terminate_tree(exit_code=1)
            returncode = task.process.returncode if task.process.returncode is not None else 1
            set_postprocess_terminal(
                self.database,
                task.run_id,
                "FAILED",
                error=f"后处理超时：超过 {self.settings.postprocess_timeout_seconds} 秒，进程树已终止。",
                message="后处理超时，进程树已终止",
            )
        elif returncode == 0:
            task.process.close()
            set_postprocess_terminal(
                self.database,
                task.run_id,
                "COMPLETED",
                message="后处理完成",
            )
        else:
            task.process.close()
            detail = _last_nonempty_line(
                _read_tail(_safe_child_path(self.settings.data_dir, "logs") / f"postprocess-{task.run_id}.log")
            )
            set_postprocess_terminal(
                self.database,
                task.run_id,
                "FAILED",
                error=self._sanitize(
                    f"后处理子进程执行失败：{detail}" if detail else "后处理子进程执行失败"
                ),
                message="后处理子进程执行失败",
            )
        task.close_streams()
        self._postprocess_task = None
        return True

    def _heartbeat(self, *, force: bool = False, extra_status: Mapping[str, Any] | None = None) -> None:
        with self._heartbeat_lock:
            current = time.monotonic()
            if not force and current - self._last_heartbeat < self._heartbeat_interval:
                return
            timestamp = _timestamp()
            status: dict[str, Any] = {
                "resource": self.last_resource_snapshot.to_dict() if self.last_resource_snapshot else None,
                "license_backoff_seconds": self.license_backoff.remaining(current),
                "license_consecutive_failures": self.license_backoff.consecutive_failures,
            }
            if extra_status:
                status.update(extra_status)
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO workers (
                        id, heartbeat_at, max_concurrency, running_count,
                        resource_status_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        heartbeat_at = excluded.heartbeat_at,
                        max_concurrency = excluded.max_concurrency,
                        running_count = excluded.running_count,
                        resource_status_json = excluded.resource_status_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        self.worker_id,
                        timestamp,
                        self.max_concurrency,
                        min(len(self._tasks), HARD_MAX_CONCURRENCY),
                        _json_text(status),
                        timestamp,
                        timestamp,
                    ),
                )
                # 不依赖本地任务字典：若转换期间字典发生清理，数据库中仍归属
                # 当前 Worker 的所有 RUNNING 任务都必须保活。
                connection.execute(
                    "UPDATE runs SET heartbeat_at = ?, updated_at = ? WHERE status = 'RUNNING' AND worker_id = ?",
                    (timestamp, timestamp, self.worker_id),
                )
            self._last_heartbeat = current

    def _sanitize(self, message: str) -> str:
        result = message.replace(str(self.settings.data_dir), "<DATA_DIR>")
        result = result.replace(str(self.settings.project_root), "<PROJECT_ROOT>")
        return _sanitize_text(result)


def mark_run_succeeded(
    database: Database | sqlite3.Connection | str | os.PathLike[str],
    run_id: str,
    *,
    run_summary: Mapping[str, Any],
    quality: Mapping[str, Any],
    quality_status: str,
    now: datetime | str | None = None,
) -> None:
    """将 RUNNING 原子推进到 SUCCEEDED；质量结论独立保存。"""

    normalized_quality_status = quality_status.upper()
    if normalized_quality_status not in {"PASS", "WARN", "FAIL", "UNKNOWN"}:
        raise ValueError("质量状态无效")
    timestamp = _timestamp(now)
    with _connection(database) as connection:
        _begin_immediate(connection)
        try:
            updated = connection.execute(
                """
                UPDATE runs SET status = 'SUCCEEDED', quality_status = ?, quality_json = ?,
                    run_summary_json = ?, progress = 1, error_code = NULL, error_message = NULL,
                    pid = NULL, heartbeat_at = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'RUNNING'
                """,
                (
                    normalized_quality_status,
                    _json_text(dict(quality)),
                    _json_text(dict(run_summary)),
                    timestamp,
                    timestamp,
                    timestamp,
                    run_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"运行不处于可成功终结的 RUNNING 状态：{run_id}")
            _append_event_in_transaction(
                connection,
                run_id,
                stage="MESH",
                level="INFO",
                progress=1.0,
                message=f"网格任务成功，质量状态为 {normalized_quality_status}",
                data={"quality_status": normalized_quality_status},
                timestamp=timestamp,
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


def mark_run_failed(
    database: Database | sqlite3.Connection | str | os.PathLike[str],
    run_id: str,
    *,
    error_code: str,
    error_message: str,
    run_summary: Mapping[str, Any] | None = None,
    now: datetime | str | None = None,
) -> None:
    """将 RUNNING 原子推进到 FAILED，并追加不可变事件。"""

    timestamp = _timestamp(now)
    safe_message = _sanitize_text(error_message)
    with _connection(database) as connection:
        _begin_immediate(connection)
        try:
            assignments = [
                "status = 'FAILED'",
                "preview_status = 'UNAVAILABLE'",
                "error_code = ?",
                "error_message = ?",
                "pid = NULL",
                "heartbeat_at = ?",
                "finished_at = ?",
                "updated_at = ?",
            ]
            parameters: list[Any] = [error_code, safe_message, timestamp, timestamp, timestamp]
            if run_summary is not None:
                assignments.append("run_summary_json = ?")
                parameters.append(_json_text(dict(run_summary)))
            parameters.extend((run_id,))
            updated = connection.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE id = ? AND status = 'RUNNING'",
                parameters,
            )
            if updated.rowcount != 1:
                # shutdown/异常清理允许已经被另一恢复者终结，不能覆盖终态。
                state = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
                if state is None or state[0] not in {"SUCCEEDED", "FAILED"}:
                    raise RuntimeError(f"运行不处于可失败终结的 RUNNING 状态：{run_id}")
                connection.rollback()
                return
            _append_event_in_transaction(
                connection,
                run_id,
                stage="MESH",
                level="ERROR",
                progress=None,
                message=safe_message,
                data={"error_code": error_code},
                timestamp=timestamp,
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


def mark_postprocess_running(
    database: Database | sqlite3.Connection | str | os.PathLike[str],
    run_id: str,
    now: datetime | str | None = None,
) -> bool:
    """把 PENDING 的后处理原子推进到 RUNNING 并记录开始时间。"""

    timestamp = _timestamp(now)
    with _connection(database) as connection:
        _begin_immediate(connection)
        try:
            updated = connection.execute(
                """
                UPDATE runs SET postprocess_status = 'RUNNING',
                    postprocess_started_at = ?, updated_at = ?
                WHERE id = ? AND postprocess_status = 'PENDING'
                """,
                (timestamp, timestamp, run_id),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return False
            _append_event_in_transaction(
                connection,
                run_id,
                stage="POSTPROCESS",
                level="INFO",
                progress=None,
                message="已启动独立后处理子进程",
                data={"postprocess_status": "RUNNING"},
                timestamp=timestamp,
            )
            connection.commit()
            return True
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


def set_postprocess_terminal(
    database: Database | sqlite3.Connection | str | os.PathLike[str],
    run_id: str,
    status: str,
    *,
    error: str | None = None,
    message: str,
    now: datetime | str | None = None,
) -> bool:
    """写入后处理终态（COMPLETED/FAILED）并追加 POSTPROCESS 事件。"""

    if status not in {"COMPLETED", "FAILED"}:
        raise ValueError("后处理终态无效")
    timestamp = _timestamp(now)
    safe_message = _sanitize_text(message)
    safe_error = _sanitize_text(error) if error else None
    with _connection(database) as connection:
        _begin_immediate(connection)
        try:
            updated = connection.execute(
                """
                UPDATE runs SET postprocess_status = ?, postprocess_finished_at = ?,
                    postprocess_error = ?, updated_at = ?,
                    preview_status = CASE WHEN ? = 'FAILED' AND preview_status = 'PENDING'
                        THEN 'FAILED' ELSE preview_status END
                WHERE id = ? AND postprocess_status IN ('PENDING', 'RUNNING')
                """,
                (status, timestamp, safe_error, timestamp, status, run_id),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return False
            _append_event_in_transaction(
                connection,
                run_id,
                stage="POSTPROCESS",
                level="INFO" if status == "COMPLETED" else "ERROR",
                progress=None,
                message=safe_message,
                data={"postprocess_status": status},
                timestamp=timestamp,
            )
            connection.commit()
            return True
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


def append_run_event(
    database: Database | sqlite3.Connection | str | os.PathLike[str],
    run_id: str,
    *,
    stage: str,
    level: str = "INFO",
    progress: float | None = None,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> int:
    """追加具有运行内递增序号的事件。"""

    timestamp = _timestamp()
    with _connection(database) as connection:
        _begin_immediate(connection)
        try:
            sequence = _append_event_in_transaction(
                connection,
                run_id,
                stage=stage,
                level=level,
                progress=progress,
                message=_sanitize_text(message),
                data=data or {},
                timestamp=timestamp,
            )
            connection.commit()
            return sequence
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


def _append_event_in_transaction(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    stage: str,
    level: str,
    progress: float | None,
    message: str,
    data: Mapping[str, Any],
    timestamp: str,
) -> int:
    normalized_level = level.upper()
    if normalized_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("事件级别无效")
    sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO run_events (
            run_id, sequence, stage, level, progress, message, data_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            sequence,
            stage,
            normalized_level,
            progress,
            _sanitize_text(message),
            _json_text(dict(data)),
            timestamp,
        ),
    )
    return sequence


def _count_running(database: Database) -> int:
    with database.reading() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM runs WHERE status = 'RUNNING'").fetchone()[0])


@contextmanager
def _connection(
    database: Database | sqlite3.Connection | str | os.PathLike[str],
    *,
    busy_timeout_ms: int = 5000,
) -> Iterator[sqlite3.Connection]:
    owned = not isinstance(database, sqlite3.Connection)
    if isinstance(database, sqlite3.Connection):
        connection = database
    elif isinstance(database, (str, os.PathLike)):
        connection = connect_database(database, busy_timeout_ms=busy_timeout_ms)
    else:
        connection = database.connect()
    previous_row_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.row_factory = previous_row_factory
        if owned:
            connection.close()


def _begin_immediate(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        raise RuntimeError("原子队列操作要求传入不处于事务中的 SQLite 连接")
    connection.execute("BEGIN IMMEDIATE")


def _row_to_dict(row: sqlite3.Row | Sequence[Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    raise TypeError("SQLite 连接必须配置 sqlite3.Row row_factory")


def _control_value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_control_value_text(item) for item in value)
    if value is None:
        raise ValueError("控制快照中的 set 值不能为 null")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("控制值必须是有限数")
    return str(value)


def _safe_relative_path(root: Path, relative: str | os.PathLike[str]) -> Path:
    value = Path(relative)
    if value.is_absolute():
        raise ValueError("数据库文件路径必须相对 MESH_DATA_DIR")
    resolved_root = root.resolve()
    resolved = (resolved_root / value).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("数据库文件路径越出 MESH_DATA_DIR") from exc
    return resolved


def _safe_child_path(root: Path, *parts: str) -> Path:
    if any(not part or part in {".", ".."} or "/" in part or "\\" in part for part in parts):
        raise ValueError("运行标识不能包含路径分隔符")
    resolved_root = root.resolve()
    resolved = resolved_root.joinpath(*parts).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("运行目录越出产物根目录") from exc
    return resolved


def _load_run_summary(run_dir: Path) -> dict[str, Any] | None:
    """读取 run_summary.json；接受 schema v3（历史）与 v4（当前契约）。"""

    path = run_dir / "run_summary.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    schema_version = value.get("schema_version")
    return value if schema_version in {3, 4} else None


def _summary_error(summary: Mapping[str, Any] | None) -> str | None:
    if not summary:
        return None
    autogrid = summary.get("autogrid")
    if isinstance(autogrid, Mapping) and autogrid.get("error"):
        return _sanitize_text(str(autogrid["error"]))
    return None


def _collect_failure_text(run_dir: Path, summary: Mapping[str, Any] | None) -> str:
    chunks = [_summary_error(summary) or ""]
    for name in ("worker.stderr.log", "worker.stdout.log", "stderr.log", "stdout.log"):
        chunks.append(_read_tail(run_dir / name))
    return "\n".join(chunks)


def _read_tail(path: Path, max_bytes: int = 256 * 1024) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            return stream.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _last_nonempty_line(text: str) -> str | None:
    lines = [_sanitize_text(line) for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _sanitize_text(text: str, max_length: int = 2000) -> str:
    collapsed = " ".join(text.replace("\x00", "").split())
    return collapsed[:max_length]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _available_memory_bytes() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except ImportError:
        pass
    if os.name == "nt":
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys)
    try:
        return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        raise RuntimeError("无法读取系统可用内存；请安装 psutil")


def _timestamp(value: datetime | str | None = None) -> str:
    return _format_datetime(_datetime_value(value))


def _datetime_value(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        return _parse_datetime(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    """``python -m mesh_app.worker`` 部署入口。"""

    parser = argparse.ArgumentParser(description="本地网格持久队列 Worker")
    parser.add_argument("--once", action="store_true", help="只推进一次调度循环")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="队列轮询秒数")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    settings.ensure_directories()
    database = Database(settings.database_path, settings.migrations_dir, settings.busy_timeout_ms)
    database.require_current()
    worker = Worker(settings, database=database)

    def stop_handler(signum: int, frame: object) -> None:
        worker.request_stop()

    for signal_name in ("SIGINT", "SIGTERM"):
        current_signal = getattr(signal, signal_name, None)
        if current_signal is not None:
            signal.signal(current_signal, stop_handler)
    if args.once:
        worker.run_once()
        return 0
    worker.run_forever(poll_interval=args.poll_interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# 稳定兼容别名，便于测试和部署脚本按语义调用。
claim_next_run = atomic_claim_run
check_resource_gate = evaluate_resource_gate
repair_stale_runs = recover_stale_runs


__all__ = [
    "HARD_MAX_CONCURRENCY",
    "LicenseBackoff",
    "ResourceSnapshot",
    "RunDirectoryUnavailable",
    "Worker",
    "append_run_event",
    "atomic_claim_run",
    "build_mesh_command",
    "check_resource_gate",
    "claim_next_run",
    "control_snapshot_to_assignments",
    "evaluate_resource_gate",
    "is_license_failure",
    "mark_postprocess_running",
    "mark_run_failed",
    "mark_run_succeeded",
    "probe_resources",
    "recover_stale_runs",
    "repair_stale_runs",
    "set_postprocess_terminal",
]
