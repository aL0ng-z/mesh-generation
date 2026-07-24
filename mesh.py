"""提供从 ``.geomTurbo`` 几何生成 AutoGrid 网格的命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from autogrid import run_autogrid_init
from controls import (
    ControlRequest,
    ControlValidationError,
    describe_control,
    list_control_specs,
    parse_control_assignments,
    resolve_control_requests,
    validate_wizard_compatibility,
)
from geomturbo import parse_geomturbo
from quality import summarize_quality


def main() -> int:
    """解析命令行参数，执行网格生成并输出运行摘要。"""

    parser = argparse.ArgumentParser(
        description="从 .geomTurbo 直接生成 NUMECA AutoGrid 17.1 CFD 网格。"
    )
    parser.add_argument("geomturbo", nargs="?", help="输入 .geomTurbo 文件。查询控制目录时可省略。")
    parser.add_argument("--out", default=None, help="运行产物目录；默认 runs/<文件名>_<时间戳>。")
    parser.add_argument("--igg", default=None, help="IGG 可执行文件或完整路径；优先于 .env。")
    parser.add_argument("--no-row-wizard", action="store_true", help="不执行 RowWizard。")
    parser.add_argument("--dry-run", action="store_true", help="只生成脚本和摘要，不启动 IGG。")
    parser.add_argument("--timeout", type=int, default=None, help="AutoGrid 超时秒数。")
    parser.add_argument(
        "--mesh-level",
        choices=("coarse", "medium", "fine", "user"),
        help="所有行的网格级别。",
    )
    parser.add_argument("--target-points", type=int, help="所有行 user 级别的目标点数。")
    parser.add_argument("--first-cell-width", type=float, help="所有行首层单元宽度，固定以米输入。")
    parser.add_argument("--spanwise-paths", type=int, help="所有行 RowWizard 展向 flow paths 数。")
    parser.add_argument("--gap-points", type=int, help="所有已有 gap 的展向点数。")
    parser.add_argument("--optimization-steps", type=int, help="所有行普通优化步数。")
    parser.add_argument("--gap-optimization-steps", type=int, help="所有行 gap 优化步数。")
    parser.add_argument(
        "--set",
        dest="set_values",
        action="append",
        default=[],
        metavar="PATH=VALUE",
        help="设置一个注册控制，可重复使用。",
    )
    parser.add_argument(
        "--list-controls",
        nargs="?",
        const="ALL",
        choices=("ALL", "P0", "P1", "P2"),
        metavar="P0|P1|P2",
        help="列出中文控制目录，无需输入几何。",
    )
    parser.add_argument("--describe-control", metavar="KEY", help="详细说明一个控制键，无需输入几何。")
    args = parser.parse_args()

    if args.list_controls or args.describe_control:
        if args.list_controls and args.describe_control:
            print("错误：--list-controls 与 --describe-control 不能同时使用。", file=sys.stderr)
            return 2
        try:
            if args.describe_control:
                _print_control_description(args.describe_control)
            else:
                _print_control_catalog(None if args.list_controls == "ALL" else args.list_controls)
        except ControlValidationError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        return 0

    if not args.geomturbo:
        print("错误：网格生成必须提供 .geomTurbo 文件。", file=sys.stderr)
        return 2
    geomturbo_path = Path(args.geomturbo)
    if not geomturbo_path.exists():
        print(f"错误：找不到 geomTurbo 文件：{geomturbo_path}", file=sys.stderr)
        return 2

    try:
        geometry = parse_geomturbo(geomturbo_path)
        requests = _build_control_requests(args)
        validate_wizard_compatibility(requests, use_row_wizard=not args.no_row_wizard)
        resolved_controls = resolve_control_requests(requests, geometry)
    except (ControlValidationError, OSError, UnicodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    env_settings = _load_env_file(Path(".env"))
    igg_executable = args.igg or env_settings.get("IGG_EXE") or env_settings.get("IGG_PATH") or "igg"

    run_dir = Path(args.out) if args.out else _default_run_dir(geomturbo_path)
    run_dir.mkdir(parents=True, exist_ok=True)

    autogrid_run = run_autogrid_init(
        geomturbo_path,
        run_dir,
        igg_executable=igg_executable,
        use_row_wizard=not args.no_row_wizard,
        dry_run=args.dry_run,
        timeout_seconds=args.timeout,
        controls=resolved_controls,
    )

    quality_summary: dict[str, Any] | None = None
    missing_mesh_outputs = (
        not args.dry_run and autogrid_run.returncode == 0 and "igg" not in autogrid_run.outputs
    )
    if missing_mesh_outputs:
        quality_summary = {
            "metrics_source": None,
            "metadata": {},
            "project": {},
            "entities": [],
            "metrics": {},
            "result": {
                "status": "UNKNOWN",
                "accepted": False,
                "reasons": ["No AutoGrid mesh outputs found"],
            },
        }
    elif not args.dry_run and autogrid_run.returncode == 0:
        try:
            quality_summary = summarize_quality(
                autogrid_run.outputs,
                units_factor=geometry.units_factor,
                units=geometry.units,
            )
        except Exception as exc:
            quality_summary = {
                "metrics_source": None,
                "metadata": {},
                "project": {},
                "entities": [],
                "metrics": {},
                "result": {
                    "status": "UNKNOWN",
                    "accepted": False,
                    "reasons": [f"{type(exc).__name__}: {exc}"],
                },
            }

    run_summary = {
        "schema_version": 2,
        "run_dir": str(run_dir),
        "geometry": geometry.to_dict(),
        "controls": {
            "requested": [request.to_dict() for request in requests],
            "resolved": [control.to_dict() for control in resolved_controls],
            "applied": [] if args.dry_run else autogrid_run.control_results,
        },
        "autogrid": autogrid_run.to_dict(),
        "quality": quality_summary,
    }
    _write_json(run_dir / "run_summary.json", run_summary)

    report = _render_report(
        geometry.to_dict(),
        run_summary["controls"],
        autogrid_run.to_dict(),
        quality_summary,
        dry_run=args.dry_run,
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8")

    print(json.dumps(run_summary, indent=2, ensure_ascii=False, default=str))
    if autogrid_run.returncode not in (None, 0):
        return 1
    if missing_mesh_outputs:
        return 1
    return 0


def _build_control_requests(args: argparse.Namespace) -> list[ControlRequest]:
    """将快捷参数和通用设置合并为网格控制请求。"""

    assignments = list(args.set_values)
    explicit = (
        (args.mesh_level, f"row:*/wizard/grid_level={args.mesh_level}" if args.mesh_level else None),
        (args.target_points, f"row:*/target_points={args.target_points}" if args.target_points is not None else None),
        (
            args.target_points if args.target_points is not None and args.mesh_level != "user" else None,
            "row:*/wizard/grid_level=user",
        ),
        (
            args.first_cell_width,
            f"row:*/wizard/first_cell_width={args.first_cell_width}"
            if args.first_cell_width is not None
            else None,
        ),
        (
            args.spanwise_paths,
            f"row:*/wizard/spanwise_paths={args.spanwise_paths}" if args.spanwise_paths is not None else None,
        ),
        (
            args.gap_points,
            f"row:*/blade:*/gap:*/spanwise_points={args.gap_points}" if args.gap_points is not None else None,
        ),
        (
            args.optimization_steps,
            f"row:*/optimization.steps={args.optimization_steps}"
            if args.optimization_steps is not None
            else None,
        ),
        (
            args.gap_optimization_steps,
            f"row:*/optimization.gap_steps={args.gap_optimization_steps}"
            if args.gap_optimization_steps is not None
            else None,
        ),
    )
    assignments.extend(expression for value, expression in explicit if value is not None and expression is not None)
    return parse_control_assignments(assignments)


def _default_run_dir(geomturbo_path: Path) -> Path:
    """根据几何文件名和当前时间生成默认运行目录。"""

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("runs") / f"{geomturbo_path.stem}_{stamp}"


def _write_json(path: Path, data: Any) -> None:
    """以便于审阅的格式写入 JSON 文件。"""

    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _load_env_file(path: Path) -> dict[str, str]:
    """读取简单的 ``KEY=VALUE`` 环境配置文件。"""

    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _render_report(
    geometry: dict[str, Any],
    controls: dict[str, Any],
    autogrid_run: dict[str, Any],
    quality_summary: dict[str, Any] | None,
    *,
    dry_run: bool,
) -> str:
    """根据几何、控制、执行和质量信息生成 Markdown 报告。"""

    lines = [
        "# AutoGrid 17.1 网格生成报告",
        "",
        "## 几何输入",
        "",
        f"- 来源：`{geometry['path']}`",
        f"- 单位：`{geometry.get('units')}`，换算因子：{geometry.get('units_factor')}",
        f"- 叶排数：{geometry['row_count']}",
        f"- 多叶排：{geometry['multi_row']}",
        f"- 含分流叶片：{geometry['has_splitter']}",
        f"- 含叶尖间隙：{geometry['has_tip_gap']}",
        "",
        "| 叶排 | 周期数 | 主叶片数 | 叶片实体数 | 分流叶片 | 叶尖间隙 |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in geometry["rows"]:
        lines.append(
            f"| {row['name']} | {row.get('periodicity')} | {row.get('main_blades')} | "
            f"{len(row.get('blades', []))} | {row.get('has_splitter')} | {row.get('has_tip_gap')} |"
        )
    lines.extend(
        [
            "",
            "## 网格控制",
            "",
            f"- 请求控制数：{len(controls.get('requested', []))}",
            f"- 解析后实体控制数：{len(controls.get('resolved', []))}",
            f"- 模式：{'仅规划（dry-run）' if dry_run else '实际执行'}",
            "",
        ]
    )
    resolved = controls.get("resolved", [])
    if resolved:
        lines.extend(
            [
                "| 控制键 | 目标 | 请求值 | 项目单位值 | 阶段 | 状态 | 回读 | 错误 |",
                "|---|---|---:|---:|---|---|---:|---|",
            ]
        )
        applied_by_id = {item.get("id"): item for item in controls.get("applied", [])}
        for item in resolved:
            applied = applied_by_id.get(item.get("id"), {})
            status = "planned" if dry_run else applied.get("status", "not_applied")
            lines.append(
                f"| `{item.get('key')}` | `{item.get('target_path')}` | "
                f"{_display_value(item.get('requested'))} | {_display_value(item.get('project_value'))} | "
                f"{item.get('stage')} | {status} | {_display_value(applied.get('readback'))} | "
                f"{_display_value(applied.get('error'))} |"
            )
    else:
        lines.append("- 未设置额外控制，使用 AutoGrid 默认值。")

    lines.extend(
        [
            "",
            "## AutoGrid 执行",
            "",
            f"- 返回码：{autogrid_run.get('returncode')}",
            f"- 错误：{autogrid_run.get('error') or '无'}",
            f"- 脚本：`{autogrid_run.get('script')}`",
            f"- 命令：`{_command_line(autogrid_run.get('command', []))}`",
            "",
            "## 生成产物",
            "",
        ]
    )
    outputs = autogrid_run.get("outputs", {})
    if outputs:
        for key, value in outputs.items():
            lines.append(f"- {key}: `{value}`")
    else:
        lines.append("- 未记录网格产物。")

    lines.extend(["", "## 网格质量", ""])
    if quality_summary:
        result = quality_summary.get("result", {})
        metrics = quality_summary.get("metrics", {})
        metadata = quality_summary.get("metadata", {})
        lines.extend(
            [
                f"- 数据源：{quality_summary.get('metrics_source')}",
                f"- 判定：{result.get('status')}，接受：{result.get('accepted')}",
                f"- AutoGrid 版本：{metadata.get('autogrid_version')}",
                f"- 生成日期：{metadata.get('generation_date')}，耗时：{metadata.get('generation_time')}",
                f"- 网格有效性：{metadata.get('mesh_validity')}，重叠状态：{metadata.get('overlapping_status')}",
                f"- 负体积单元：{metrics.get('negative_cells')}，总点数：{metrics.get('number_of_points')}，网格层级：{metrics.get('grid_levels')}",
            ]
        )
        reasons = result.get("reasons") or []
        if reasons:
            lines.append("- 原因：" + "；".join(_translate_quality_reason(reason) for reason in reasons))
        entities = quality_summary.get("entities", [])
        if entities:
            lines.extend(
                [
                    "",
                    "### 全局与逐行统计",
                    "",
                    "| 范围 | 指标 | 最小值 | 最大值 | 平均值 | 最差位置 |",
                    "|---|---|---:|---:|---:|---|",
                ]
            )
            criterion_labels = {
                "skewness_angle": "偏斜角",
                "spanwise_skewness_angle": "展向偏斜角",
                "spanwise_expansion_ratio": "展向增长率",
                "aspect_ratio": "长宽比",
                "expansion_ratio": "增长率",
                "wall_distance": "壁面距离",
            }
            for entity in entities:
                for criterion_name, criterion in entity.get("criteria", {}).items():
                    lines.append(
                        f"| {entity.get('name')} | {criterion_labels.get(criterion_name, criterion_name)} | "
                        f"{_display_value(criterion.get('minimum'))} | {_display_value(criterion.get('maximum'))} | "
                        f"{_display_value(criterion.get('average'))} | {_format_location(criterion.get('critical_location'))} |"
                    )
    else:
        lines.append("- 未评估。")
    return "\n".join(lines) + "\n"


def _command_line(command: list[str]) -> str:
    """将命令参数列表格式化为单行文本。"""

    return " ".join(str(part) for part in command)


def _display_value(value: Any) -> str:
    """将报告字段转换为适合 Markdown 展示的文本。"""

    if value is None:
        return "—"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _format_location(location: dict[str, Any] | None) -> str:
    """格式化质量极值所在的块与网格索引。"""

    if not location:
        return "—"
    block = location.get("block") or "?"
    indices = ",".join(str(location.get(axis, "?")) for axis in ("i", "j", "k"))
    extreme = "最小值" if location.get("extreme") == "minimum" else "最大值"
    return f"`{block}` I/J/K={indices}（{extreme}）"


def _translate_quality_reason(reason: str) -> str:
    """将已知的英文质量判定原因翻译为中文。"""

    translations = {
        "Negative cells detected": "检测到负体积单元",
        "Insufficient grid levels": "网格层级不足",
        "Minimum skewness angle below hard limit": "最小偏斜角低于硬限制",
        "Maximum expansion ratio above hard limit": "最大增长率超过硬限制",
        "Spanwise angular deviation above hard limit": "展向角偏差超过硬限制",
        "Spanwise expansion ratio above hard limit": "展向增长率超过硬限制",
        "Maximum aspect ratio above hard limit": "最大长宽比超过硬限制",
        "No AutoGrid mesh outputs found": "未找到 AutoGrid 网格产物",
        "No quality source found": "未找到质量数据源",
    }
    if reason.startswith("Missing metric: "):
        return "缺少质量指标：" + reason.split(": ", 1)[1]
    return translations.get(reason, reason)


def _print_control_catalog(priority: str | None) -> None:
    """按优先级输出可用网格控制项目录。"""

    specs = list_control_specs(priority)
    title = f"AutoGrid 17.1 {priority or '全部'}纯网格控制（{len(specs)} 项）"
    print(title)
    print("键 | 优先级 | 作用域 | 类型 | 阶段 | 说明")
    for spec in specs:
        print(
            f"{spec.key} | {spec.priority} | {spec.scope} | {spec.value_type} | "
            f"{spec.stage} | {spec.description}"
        )


def _print_control_description(key: str) -> None:
    """输出指定网格控制项的详细说明。"""

    spec = describe_control(key)
    print(f"控制键：{spec.key}")
    print(f"说明：{spec.description}")
    print(f"优先级：{spec.priority}")
    print(f"作用域：{spec.scope}（目标 {spec.target_kind}）")
    print(f"类型：{spec.value_type}")
    if spec.enum_values:
        print("可选值：" + ", ".join(spec.enum_values))
    if spec.minimum is not None or spec.maximum is not None:
        print(f"已知范围：{spec.minimum if spec.minimum is not None else '-∞'} ～ {spec.maximum if spec.maximum is not None else '+∞'}")
    print(f"SI 长度输入：{'是（米）' if spec.si_length else '否'}")
    print(f"应用阶段：{spec.stage}")
    print(f"setter：{spec.setter or dict(spec.setter_by_value)}")
    print(f"getter：{spec.getter or '无；以 setter 无异常作为 applied'}")
    print(f"拓扑限制：{', '.join(spec.topologies) if spec.topologies else '无'}")
    print(f"不适用条件：{spec.not_applicable_when or '无额外条件'}")


if __name__ == "__main__":
    raise SystemExit(main())
