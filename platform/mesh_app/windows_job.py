"""为网格子进程提供可控的进程树生命周期。

Windows 上优先把进程放入启用 ``KILL_ON_JOB_CLOSE`` 的 Job Object；
其他平台用独立进程组实现等价的有界终止。模块只依赖标准库，便于 Worker
在尚未加载 FastAPI 等 Web 依赖时单独启动。
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Mapping, Sequence


_IS_WINDOWS = os.name == "nt"
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# POSIX 进程组清理节奏：terminate_tree 的默认宽限期，以及 SIGKILL 后的确认等待。
_DEFAULT_GRACE_SECONDS = 5.0
_POSIX_CONFIRM_SECONDS = 5.0
_POSIX_POLL_INTERVAL = 0.05


if _IS_WINDOWS:
    from ctypes import wintypes

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class ManagedProcess:
    """包装 ``Popen``，确保超时或 Worker 退出时清理完整子进程树。"""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        job_handle: int | None = None,
        pgid: int | None = None,
    ):
        self.process = process
        self._job_handle = job_handle
        # POSIX 上保存 start_new_session 建立的进程组身份（即子进程 pid）。
        # 父进程先退出后仍用该身份清理，不依赖对已回收 PID 的 waitpid/getpgid。
        self._pgid = None if _IS_WINDOWS else pgid
        self._closed = False

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    @property
    def uses_job_object(self) -> bool:
        return self._job_handle is not None

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout=timeout)

    def terminate_tree(self, *, exit_code: int = 1, grace_seconds: float = _DEFAULT_GRACE_SECONDS) -> None:
        """终止整个进程树；重复调用是安全的。"""

        if _IS_WINDOWS:
            if self.process.poll() is not None:
                # Job 句柄关闭时按 KILL_ON_JOB_CLOSE 清理残余进程，无需再发信号。
                self.close()
                return
            if self._job_handle is not None:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                kernel32.TerminateJobObject.restype = ctypes.c_int
                kernel32.TerminateJobObject(self._job_handle, max(1, int(exit_code)))
            else:
                self._terminate_windows_fallback()
            try:
                self.process.wait(timeout=max(0.0, grace_seconds))
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            self.close()
            return

        # POSIX：按保存的进程组身份清理；直接父进程是否已退出不影响该流程。
        self._cleanup_posix_group(grace_seconds)
        self.close()

    def _terminate_windows_fallback(self) -> None:
        # PID 来自仍在运行的直接子进程，不经过 shell 拼接。taskkill 可覆盖
        # Job Object 因宿主策略不可用的少数 Windows 环境。
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()

    def _terminate_posix_group(self, sig: int) -> None:
        """按保存的进程组身份发信号；组已消失时幂等返回。"""
        if self._pgid is None:
            self._terminate_direct_child(sig)
            return
        try:
            os.killpg(self._pgid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            self._terminate_direct_child(sig)

    def _terminate_direct_child(self, sig: int) -> None:
        if self.process.poll() is None:
            if sig == signal.SIGKILL:
                self.process.kill()
            else:
                self.process.terminate()

    def _posix_group_alive(self) -> bool:
        """保存的进程组是否仍有存活成员；ESRCH 视为已消失，EPERM 视为仍存活。"""
        if self._pgid is None:
            return False
        try:
            os.killpg(self._pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _cleanup_posix_group(self, grace_seconds: float) -> None:
        """统一、幂等的 POSIX 进程组清理：SIGTERM → 宽限期 → SIGKILL → 短等确认。"""
        if self._pgid is None:
            # 未保存进程组身份（直接构造的包装器）时，退化为清理直接子进程。
            self._terminate_direct_child(signal.SIGTERM)
            try:
                self.process.wait(timeout=max(0.0, grace_seconds))
            except subprocess.TimeoutExpired:
                self._terminate_direct_child(signal.SIGKILL)
                try:
                    self.process.wait(timeout=_POSIX_CONFIRM_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            return
        if not self._posix_group_alive():
            return
        self._terminate_posix_group(signal.SIGTERM)
        if self._await_posix_group_exit(max(0.0, grace_seconds)):
            return
        self._terminate_posix_group(signal.SIGKILL)
        self._await_posix_group_exit(_POSIX_CONFIRM_SECONDS)

    def _await_posix_group_exit(self, seconds: float) -> bool:
        """在给定时间内等待进程组消失并回收直接子进程；True 表示组已消失。"""
        deadline = time.monotonic() + seconds
        while True:
            self.process.poll()  # 顺带回收直接子进程状态，避免僵尸进程
            if not self._posix_group_alive():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_POSIX_POLL_INTERVAL, remaining))

    def close(self) -> None:
        """关闭 Job 句柄；正常结束后也会清理遗留的后代进程。"""

        if self._closed:
            return
        self._closed = True
        if _IS_WINDOWS and self._job_handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(self._job_handle)
            self._job_handle = None
        elif not _IS_WINDOWS:
            # 与超时、停止共用同一幂等清理路径：正常结束后仍可能有残余后代。
            self._cleanup_posix_group(_DEFAULT_GRACE_SECONDS)

    def __enter__(self) -> "ManagedProcess":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.poll() is None:
            self.terminate_tree()
        else:
            self.close()


def spawn_managed_process(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdout: int | IO[bytes] | None = None,
    stderr: int | IO[bytes] | None = None,
) -> ManagedProcess:
    """启动独立进程并尽可能纳入 Windows Job Object。"""

    if not command:
        raise ValueError("子进程命令不能为空")
    normalized = [os.fspath(part) for part in command]
    resolved_cwd = str(Path(cwd).resolve()) if cwd is not None else None
    job_handle: int | None = _create_kill_on_close_job() if _IS_WINDOWS else None
    popen_options: dict[str, object] = {
        "cwd": resolved_cwd,
        "env": dict(env) if env is not None else None,
        "stdin": subprocess.DEVNULL,
        "stdout": stdout,
        "stderr": stderr,
        "text": False,
    }
    if _IS_WINDOWS:
        popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_options["start_new_session"] = True

    try:
        process = subprocess.Popen(normalized, **popen_options)  # type: ignore[arg-type]
    except BaseException:
        _close_windows_handle(job_handle)
        raise

    if job_handle is not None and not _assign_process_to_job(job_handle, process):
        _close_windows_handle(job_handle)
        job_handle = None
    # POSIX：start_new_session 使子进程成为会话组长，进程组 ID 即子进程 pid；
    # 保存该身份供清理使用，父进程先退出后也不依赖查询可能已回收的 PID。
    pgid: int | None = None if _IS_WINDOWS else process.pid
    return ManagedProcess(process, job_handle, pgid)


def job_objects_supported() -> bool:
    """报告当前系统是否能创建带关闭清理语义的 Job Object。"""

    if not _IS_WINDOWS:
        return False
    handle = _create_kill_on_close_job()
    if handle is None:
        return False
    _close_windows_handle(handle)
    return True


def _create_kill_on_close_job() -> int | None:
    if not _IS_WINDOWS:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return None
    information = _JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    if not ok:
        _close_windows_handle(int(handle))
        return None
    return int(handle)


def _assign_process_to_job(handle: int, process: subprocess.Popen[bytes]) -> bool:
    if not _IS_WINDOWS:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    process_handle = getattr(process, "_handle", None)
    return bool(process_handle and kernel32.AssignProcessToJobObject(handle, process_handle))


def _close_windows_handle(handle: int | None) -> None:
    if not _IS_WINDOWS or handle is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle(handle)


__all__ = [
    "ManagedProcess",
    "job_objects_supported",
    "spawn_managed_process",
]
