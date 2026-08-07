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
from pathlib import Path
from typing import IO, Mapping, Sequence


_IS_WINDOWS = os.name == "nt"
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


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

    def __init__(self, process: subprocess.Popen[bytes], job_handle: int | None = None):
        self.process = process
        self._job_handle = job_handle
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

    def terminate_tree(self, *, exit_code: int = 1, grace_seconds: float = 5.0) -> None:
        """终止整个进程树；重复调用是安全的。"""

        if self.process.poll() is not None:
            self.close()
            return
        if _IS_WINDOWS and self._job_handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel32.TerminateJobObject.restype = ctypes.c_int
            kernel32.TerminateJobObject(self._job_handle, max(1, int(exit_code)))
        elif _IS_WINDOWS:
            self._terminate_windows_fallback()
        else:
            self._terminate_posix_group(signal.SIGTERM)

        try:
            self.process.wait(timeout=max(0.0, grace_seconds))
        except subprocess.TimeoutExpired:
            if _IS_WINDOWS:
                self.process.kill()
            else:
                self._terminate_posix_group(signal.SIGKILL)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
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
        try:
            os.killpg(os.getpgid(self.pid), sig)
        except (ProcessLookupError, PermissionError):
            if self.process.poll() is None:
                if sig == signal.SIGKILL:
                    self.process.kill()
                else:
                    self.process.terminate()

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
    return ManagedProcess(process, job_handle)


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
