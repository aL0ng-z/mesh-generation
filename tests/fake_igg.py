"""标准库假 IGG 桩：模拟 AutoGrid 17.1 IGG 的控制事件与产物输出。

不依赖任何第三方包或真实 AutoGrid 环境。行为通过环境变量
``FAKE_IGG_BEHAVIOR`` 选择（默认 ``normal``）；生成的启动器可作为
``src/mesh.py --igg`` 的指向对象。

支持的行为：

- ``normal``：输出全部控制事件、完成事件，并写 igg/cgns/trb/质量报告；
- ``no_output_files``：事件完整但不写任何产物文件；
- ``no_events``：不输出任何控制事件，但仍输出完成事件；
- ``corrupt_json``：第一条应用事件输出损坏 JSON；
- ``duplicate_id``：第一条控制的应用事件输出两次；
- ``unknown_id``：额外输出一个未计划控制的执行事件；
- ``wrong_target``：应用事件的 target_path 被篡改；
- ``wrong_stage``：应用事件的 stage 被改为 post_generation；
- ``wrong_run_id``：所有事件使用错误的 run_id；
- ``no_completion``：不输出完成事件；
- ``setter_failure``：第一条控制输出 status=failed 的应用事件；
- ``script_traceback``：向 stderr 输出 Traceback 并退出 1；
- ``readback_error``：应用事件回读报错，生成后事件 status=failed；
- ``readback_mismatch``：应用与生成后回读均为可解释但不匹配的值；
- ``slow``：正常完成但先休眠 2 秒，用于并发占用测试。
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

BEHAVIOR_ENV = "FAKE_IGG_BEHAVIOR"

APPLY_MARKER = "AGMESH_CONTROL_RESULT:"
POST_MARKER = "AGMESH_CONTROL_POST_RESULT:"
COMPLETION_MARKER = "AGMESH_COMPLETION:"

DEFAULT_BEHAVIORS = frozenset(
    {
        "normal",
        "no_output_files",
        "no_events",
        "corrupt_json",
        "duplicate_id",
        "unknown_id",
        "wrong_target",
        "wrong_stage",
        "wrong_run_id",
        "no_completion",
        "setter_failure",
        "script_traceback",
        "readback_error",
        "readback_mismatch",
        "slow",
    }
)

QUALITY_REPORT_TEMPLATE = """AUTOGRID version 17.1-1
PROJECT : fake_contract_run

GRID QUALITY REPORT

Entire Mesh Quality
Number of Points : 100000
Number of grid levels : 4
No Negative Cell
Minimal skewness angle : 35.0
Maximal skewness angle : 40.0
Average skewness angle : 37.0
Minimal spanwise skewness angle : 170.0
Maximal spanwise skewness angle : 175.0
Average spanwise skewness angle : 172.5
Minimal spanwise expansion ratio : 1.0
Maximal spanwise expansion ratio : 1.5
Average spanwise expansion ratio : 1.2
Minimal aspect ratio : 1.0
Maximal aspect ratio : 100.0
Average aspect ratio : 50.0
Minimal expansion ratio : 1.0
Maximal expansion ratio : 2.0
Average expansion ratio : 1.5
Minimal wall distance : 0.001
Maximal wall distance : 0.01
Average wall distance : 0.005
"""


def load_generated_script(path: str | Path) -> tuple[list[dict[str, Any]], str]:
    """从宿主生成的 autogrid_init.py 中提取控制计划与 run_id。"""

    text = Path(path).read_text(encoding="utf-8")
    plan_match = re.search(r"^CONTROL_PLAN = json\.loads\((.*)\)\s*$", text, flags=re.MULTILINE)
    if plan_match is None:
        raise RuntimeError("生成脚本缺少 CONTROL_PLAN 定义")
    plan = json.loads(ast.literal_eval(plan_match.group(1).strip()))
    run_id_match = re.search(r"^RUN_ID = (.*)$", text, flags=re.MULTILINE)
    if run_id_match is None:
        raise RuntimeError("生成脚本缺少 RUN_ID 定义")
    run_id = ast.literal_eval(run_id_match.group(1).strip())
    return plan, str(run_id)


def write_launcher(directory: str | Path) -> Path:
    """在当前平台创建可被 CLI ``--igg`` 直接执行的假 IGG 启动器。"""

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    if os.name == "nt":
        launcher = target / "fake_igg.bat"
        launcher.write_text(
            f'@"{sys.executable}" "{script}" %*\r\n',
            encoding="ascii",
        )
    else:
        launcher = target / "fake_igg"
        launcher.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(script)
            + ' "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return launcher


def _mismatch_value(value: Any) -> Any:
    """生成一个可解释但与请求值不同的回读值。"""

    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    if isinstance(value, str):
        return value + "_x"
    return value


def _apply_event(
    control: dict[str, Any],
    run_id: str,
    *,
    status: str = "applied",
    readback: Any = None,
    error: str | None = None,
    stage: str = "apply",
    target_path: str | None = None,
) -> None:
    event = {
        "id": control.get("id"),
        "key": control.get("key"),
        "target_path": target_path if target_path is not None else control.get("target_path"),
        "requested": control.get("requested"),
        "project_value": control.get("project_value"),
        "status": status,
        "readback": readback,
        "error": error,
        "readback_before": None,
        "readback_before_error": None,
        "run_id": run_id,
        "stage": stage,
    }
    print(APPLY_MARKER + json.dumps(event, sort_keys=True))


def _post_event(
    control: dict[str, Any],
    run_id: str,
    *,
    status: str,
    readback: Any = None,
    error: str | None = None,
) -> None:
    event = {
        "id": control.get("id"),
        "key": control.get("key"),
        "target_path": control.get("target_path"),
        "requested": control.get("requested"),
        "project_value": control.get("project_value"),
        "status": status,
        "readback": readback,
        "error": error,
        "run_id": run_id,
        "stage": "post_generation",
    }
    print(POST_MARKER + json.dumps(event, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    """按 FAKE_IGG_BEHAVIOR 配置模拟一次 IGG 执行。"""

    args = list(sys.argv[1:] if argv is None else argv)
    behavior = os.environ.get(BEHAVIOR_ENV, "normal")
    if behavior not in DEFAULT_BEHAVIORS:
        print("未知 FAKE_IGG_BEHAVIOR：" + behavior, file=sys.stderr)
        return 2
    script_path = None
    for index, arg in enumerate(args):
        if arg == "-script" and index + 1 < len(args):
            script_path = args[index + 1]
    if script_path is None:
        print("缺少 -script 参数", file=sys.stderr)
        return 2

    plan, run_id = load_generated_script(script_path)
    if behavior == "slow":
        time.sleep(2.0)

    if behavior == "script_traceback":
        print(
            'Traceback (most recent call last):\n'
            '  File "autogrid_init.py", line 999, in <module>\n'
            "RuntimeError: fake script failure",
            file=sys.stderr,
        )
        return 1

    event_run_id = run_id if behavior != "wrong_run_id" else "another-run"
    apply_stage = "apply" if behavior != "wrong_stage" else "post_generation"

    if behavior != "no_events":
        first = True
        for control in plan:
            if behavior == "setter_failure" and first:
                first = False
                _apply_event(
                    control,
                    event_run_id,
                    status="failed",
                    readback=None,
                    error="RuntimeError: fake setter failure",
                    stage=apply_stage,
                )
                continue
            target_path = (
                control.get("target_path") + "-x"
                if behavior == "wrong_target"
                else None
            )
            if behavior == "readback_error":
                _apply_event(
                    control,
                    event_run_id,
                    status="applied",
                    readback=None,
                    error="RuntimeError: fake getter failure",
                    stage=apply_stage,
                    target_path=target_path,
                )
            elif behavior == "readback_mismatch":
                _apply_event(
                    control,
                    event_run_id,
                    status="applied",
                    readback=(
                        _mismatch_value(control.get("api_value"))
                        if control.get("getter")
                        else None
                    ),
                    error=None,
                    stage=apply_stage,
                    target_path=target_path,
                )
            else:
                _apply_event(
                    control,
                    event_run_id,
                    status="applied",
                    readback=control.get("api_value") if control.get("getter") else None,
                    error=None,
                    stage=apply_stage,
                    target_path=target_path,
                )

    if behavior == "corrupt_json" and plan:
        print(APPLY_MARKER + "{corrupt json")
    if behavior == "duplicate_id" and plan:
        control = plan[0]
        _apply_event(
            control,
            event_run_id,
            status="applied",
            readback=control.get("api_value"),
            error=None,
            stage=apply_stage,
        )
    if behavior == "unknown_id":
        _apply_event(
            {
                "id": "C9999",
                "key": "unknown/key",
                "target_path": "unknown",
                "requested": 0,
                "project_value": 0,
                "api_value": 0,
            },
            event_run_id,
            status="applied",
            readback=0,
            error=None,
            stage=apply_stage,
        )

    if behavior != "no_events":
        for control in plan:
            if behavior == "readback_error":
                _post_event(
                    control,
                    event_run_id,
                    status="failed",
                    readback=None,
                    error="RuntimeError: fake post getter failure",
                )
            elif behavior == "readback_mismatch":
                _post_event(
                    control,
                    event_run_id,
                    status="readback",
                    readback=_mismatch_value(control.get("api_value")),
                )
            elif control.get("getter"):
                _post_event(
                    control,
                    event_run_id,
                    status="readback",
                    readback=control.get("api_value"),
                )
            else:
                _post_event(control, event_run_id, status="no_getter", readback=None)

    if behavior != "no_completion":
        print(
            COMPLETION_MARKER
            + json.dumps(
                {"stage": "final", "run_id": event_run_id, "status": "completed"},
                sort_keys=True,
            )
        )

    if behavior != "no_output_files":
        cwd = Path(os.getcwd())
        (cwd / "mesh.igg").write_text("fake-igg-binary\n" * 64, encoding="latin1")
        (cwd / "mesh.cgns").write_text("fake-cgns-binary\n" * 64, encoding="latin1")
        (cwd / "mesh.trb").write_text("fake-trb\n", encoding="latin1")
        (cwd / "mesh.qualityReport").write_text(
            QUALITY_REPORT_TEMPLATE, encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
