"""进程树清理的实机与场景验证（CR-09）。

Windows（本机）运行真实子进程树验证：子进程再派生的孙进程在
``terminate_tree`` 之后整树消失（用 psutil 断言），并验证 Job 句柄关闭的
兜底清理路径。POSIX 场景（孙进程忽略 SIGTERM、父进程先退出、正常结束后
残余后代、重复清理幂等）在非 POSIX 平台跳过。

POSIX 测试在 Linux 上的验证方法（在 ``platform/`` 目录下执行）：

    python -m pytest tests/test_process_cleanup.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from mesh_app.windows_job import (
    ManagedProcess,
    job_objects_supported,
    spawn_managed_process,
)


def _spawner_code(*, ignore_sigterm: bool, child_sleep: float) -> str:
    """生成子进程代码：派生孙进程、打印其 pid，随后自身按需继续存活。"""
    if ignore_sigterm:
        grandchild = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(300)\n"
        )
    else:
        grandchild = "import time\ntime.sleep(300)\n"
    return (
        "import subprocess, sys, time\n"
        f"grandchild = subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "print(grandchild.pid, flush=True)\n"
        f"time.sleep({child_sleep})\n"
    )


def _spawn_tree(*, ignore_sigterm: bool, child_sleep: float) -> tuple[ManagedProcess, int]:
    managed = spawn_managed_process(
        [sys.executable, "-c", _spawner_code(ignore_sigterm=ignore_sigterm, child_sleep=child_sleep)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert managed.process.stdout is not None
    line = managed.process.stdout.readline()
    assert line, "子进程未报告孙进程 pid"
    return managed, int(line.decode().strip())


def _wait_windows_gone(pid: int, timeout: float = 10.0) -> None:
    import psutil

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"进程 {pid} 在 {timeout} 秒内仍存活")


def _posix_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_posix_gone(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _posix_pid_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"进程 {pid} 在 {timeout} 秒内仍存活")


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object 实机验收")
def test_windows_terminate_tree_kills_grandchild() -> None:
    """真实子进程树（子进程再派生孙进程）经 terminate_tree 后整树消失。"""
    managed, grandchild_pid = _spawn_tree(ignore_sigterm=True, child_sleep=300)
    try:
        managed.terminate_tree(exit_code=1, grace_seconds=5.0)
        _wait_windows_gone(grandchild_pid)
        assert managed.process.returncode is not None
    finally:
        managed.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object 实机验收")
def test_windows_close_job_handle_kills_residual_grandchild() -> None:
    """父进程正常退出后关闭 Job 句柄，KILL_ON_JOB_CLOSE 应终止残余孙进程。"""
    if not job_objects_supported():
        pytest.skip("当前环境无法创建带 KILL_ON_JOB_CLOSE 的 Job Object")
    managed, grandchild_pid = _spawn_tree(ignore_sigterm=True, child_sleep=0)
    assert managed.uses_job_object
    assert managed.process.wait(timeout=15) == 0  # 父进程正常退出，孙进程仍存活
    managed.close()
    _wait_windows_gone(grandchild_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组清理场景")
def test_posix_grandchild_ignoring_sigterm_is_killed() -> None:
    """孙进程忽略 SIGTERM 时，宽限期结束后应对残余进程组发送 SIGKILL。

    Linux 验证：cd platform && python -m pytest tests/test_process_cleanup.py -v
    """
    managed, grandchild_pid = _spawn_tree(ignore_sigterm=True, child_sleep=300)
    try:
        managed.terminate_tree(exit_code=1, grace_seconds=0.5)
        _wait_posix_gone(grandchild_pid)
    finally:
        managed.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组清理场景")
def test_posix_parent_exits_first_cleanup_still_sigkills_group() -> None:
    """父进程先退出时，terminate_tree 仍须走到进程组检查与 SIGKILL 分支。

    Linux 验证：cd platform && python -m pytest tests/test_process_cleanup.py -v
    """
    managed, grandchild_pid = _spawn_tree(ignore_sigterm=True, child_sleep=0)
    try:
        assert managed.process.wait(timeout=15) == 0  # 父进程先退出
        managed.terminate_tree(exit_code=1, grace_seconds=0.5)
        _wait_posix_gone(grandchild_pid)
    finally:
        managed.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组清理场景")
def test_posix_close_cleans_residual_descendants() -> None:
    """正常结束后调用 close() 也应终止残余的进程组成员。

    Linux 验证：cd platform && python -m pytest tests/test_process_cleanup.py -v
    """
    managed, grandchild_pid = _spawn_tree(ignore_sigterm=True, child_sleep=0)
    try:
        assert managed.process.wait(timeout=15) == 0
        managed.close()  # 正常结束路径
        _wait_posix_gone(grandchild_pid)
    finally:
        managed.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组清理场景")
def test_posix_repeated_cleanup_is_idempotent() -> None:
    """terminate_tree 与 close 重复调用安全（正常关闭/超时/停止共用幂等路径）。

    Linux 验证：cd platform && python -m pytest tests/test_process_cleanup.py -v
    """
    managed, grandchild_pid = _spawn_tree(ignore_sigterm=True, child_sleep=300)
    try:
        managed.terminate_tree(exit_code=1, grace_seconds=0.5)
        _wait_posix_gone(grandchild_pid)
        managed.terminate_tree(exit_code=1, grace_seconds=0.5)  # 重复调用不抛错
        managed.close()
        managed.close()  # 重复 close 不抛错
    finally:
        managed.close()
