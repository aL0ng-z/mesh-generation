"""Rotor37 B2B 拓扑控制验证活动执行器。

按照 PLAN.md 方案执行：
1. 生成三种拓扑的 A/A 基线（各 4 份）
2. 测试拓扑切换
3. 对每个控制参数执行 OFAT 验证（单因子测试）
4. 并发执行（最多 32 子进程），超时 1800s
5. 收集结果并按 COMMON_CORE / CONDITIONAL_DEFAULT/HOH/HI / REJECTED 分类

用法：
  python campaign_runner.py --phase 1          # 只生成基线
  python campaign_runner.py --phase 2          # 只执行测试矩阵
  python campaign_runner.py --phase 3          # 只收集结果并生成报告
  python campaign_runner.py                    # 全部执行
  python campaign_runner.py --dry-run          # 只打印测试矩阵，不执行
  python campaign_runner.py --workers 16       # 自定义并发数
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# 项目根目录加入 sys.path（脚本位于 tests/ 子目录）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 项目模块
from controls import (
    COMMON_CORE_KEYS,
    COMMON_KEYS,
    COMMON_TOPOLOGY_KEYS,
    CONDITIONAL_KEY_TOPOLOGY,
    CONTROL_REGISTRY,
    TOPOLOGY_DEFAULT_KEYS,
    TOPOLOGY_HI_KEYS,
    TOPOLOGY_HOH_KEYS,
    TOPOLOGY_SELECTOR_KEY,
    ControlSpec,
)


# ============================================================================
# 配置
# ============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GEOMETRY_PATH = PROJECT_ROOT / "geometries" / "Rotor37.geomTurbo"
MESH_PY = PROJECT_ROOT / "mesh.py"
TIMEOUT_SECONDS = 1800
DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 4))
BASELINE_REPEATS = 4

TOPOLOGY_VALUES = ("default", "hoh", "hi")
TOPOLOGY_KEYS_MAP: dict[str, frozenset[str]] = {
    "default": TOPOLOGY_DEFAULT_KEYS,
    "hoh": TOPOLOGY_HOH_KEYS,
    "hi": TOPOLOGY_HI_KEYS,
}

# Rotor37 几何特征：单排、36 叶片、shroud tip gap、无分流叶片。
# 仅以下 target_kind 的控制可实测。
ROTOR37_APPLICABLE_TARGETS: frozenset[str] = frozenset({
    "configuration",
    "wizard",
    "acoustic-wizard",
    "row",
    "blade",
    "gap",
    "interface",
    "stagnation-point",
})


def _is_rotor37_applicable(key: str, spec: ControlSpec) -> bool:
    """判断控制项是否可用于 Rotor37 几何。"""
    if spec.target_kind not in ROTOR37_APPLICABLE_TARGETS:
        return False
    if spec.not_applicable_when:
        return False
    if "splitter" in key.lower():
        return False
    if "bypass" in key:
        return False
    if spec.target_kind == "interface" and "outlet2" in key:
        return False
    if spec.target_kind == "acoustic-wizard":
        return False  # Rotor37 不是声学行
    return True


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ============================================================================
# 测试值生成
# ============================================================================

def generate_test_values(spec: ControlSpec) -> list[Any]:
    """为单个控制参数生成 OFAT 测试值列表。

    规则（PLAN.md）：
    - 点数类 int（minimum≥2）：取 ~0.75× 和 ~1.25× 合理默认值
    - 长度/比例 float：取基线两侧安全值
    - 枚举：所有非默认值
    - 布尔：测试 true（非默认）
    """
    if spec.value_type == "bool":
        return [True]

    if spec.value_type == "enum":
        return list(spec.enum_values)

    if spec.value_type == "int":
        lo = spec.minimum if spec.minimum is not None else 2
        hi = spec.maximum if spec.maximum is not None else 10001
        # 窄范围 [0,1] 或 [0,2]：直接取边界值
        if hi <= 2:
            return [lo, hi]
        if "target_points" in spec.key:
            # user 模式目标点数——需要较大值。取 ~250k 和 ~500k。
            v1 = max(lo, 250000)
            v2 = min(hi, max(v1 * 2, 500000))
            return [v1, v2]
        # 点数/索引类：两次不同值，确保不低于下限
        if "points" in spec.key or "index" in spec.key:
            v1 = max(lo, 17)
            v2 = max(v1 + 1, min(hi, 33))
            return [v1, v2]
        if "steps" in spec.key:
            return [max(lo, 50), min(hi, 200)]
        if "level" in spec.key:
            return [1, 4]
        # 通用 int：取两个在合法范围内且互异的测试值
        v1 = max(lo, 5)
        v2 = min(hi, max(v1 * 5, v1 + 4))
        if v2 <= v1:
            v2 = min(hi, v1 + 1)
        return [v1, v2]

    if spec.value_type == "float":
        lo = spec.minimum if spec.minimum is not None else 0.0
        hi = spec.maximum if spec.maximum is not None else float("inf")
        if spec.si_length:
            return [max(lo, 1e-6), min(hi, 1e-4)]
        if "relaxation" in spec.key or "clustering" in spec.key:
            return [max(lo, 0.2), min(hi, 0.8)]
        if "expansion" in spec.key or "ratio" in spec.key:
            return [max(lo, 1.5), min(hi, 3.0)]
        if "weight" in spec.key or "orthogonality" in spec.key:
            return [max(lo, 0.3), min(hi, 0.7)]
        return [max(lo, 0.3), min(hi, 0.7)]

    return []


# ============================================================================
# 案例定义
# ============================================================================

def _blade_selector(row: int = 1, blade: int = 1) -> str:
    """返回标准 blade 选择器。"""
    return f"row:#{row}/blade:#{blade}"


def build_baseline_cases(campaign_dir: Path) -> list[dict[str, Any]]:
    """生成 3 拓扑 × 4 重复 = 12 个基线案例。"""
    cases: list[dict[str, Any]] = []
    for topo in TOPOLOGY_VALUES:
        for run_idx in range(1, BASELINE_REPEATS + 1):
            out_dir = campaign_dir / "baselines" / topo / f"A{run_idx:02d}"
            cases.append({
                "case_id": f"baseline/{topo}/A{run_idx:02d}",
                "category": "baseline",
                "topology": topo,
                "set_args": [f"{_blade_selector()}/b2b.topology={topo}"],
                "variable_key": None,
                "variable_value": None,
                "out_dir": str(out_dir),
                "run_index": run_idx,
            })
    return cases


def build_topology_switch_cases(campaign_dir: Path) -> list[dict[str, Any]]:
    """生成拓扑切换验证案例。"""
    cases: list[dict[str, Any]] = []
    for topo in TOPOLOGY_VALUES:
        out_dir = campaign_dir / "topology" / topo
        cases.append({
            "case_id": f"topology/{topo}",
            "category": "topology_switch",
            "topology": topo,
            "set_args": [f"{_blade_selector()}/b2b.topology={topo}"],
            "variable_key": "blade/b2b.topology",
            "variable_value": topo,
            "out_dir": str(out_dir),
        })
    return cases


def build_ofat_cases(campaign_dir: Path) -> list[dict[str, Any]]:
    """为所有待测控制参数生成 OFAT 案例。

    按 PLAN.md 参数分层：
    - COMMON_CORE：拓扑无关，使用 default 拓扑作为固定上下文
    - TOPOLOGY_DEFAULT/HOH/HI：在对应拓扑下测试
    """
    cases: list[dict[str, Any]] = []

    # 通用核心控制（固定 default 拓扑上下文）
    for key in sorted(COMMON_CORE_KEYS):
        spec = CONTROL_REGISTRY[key]
        if not _is_rotor37_applicable(key, spec):
            continue
        if spec.scope in ("configuration",):
            _add_cases_for_key(cases, campaign_dir, key, spec, "core", None, "")
        elif spec.target_kind == "wizard":
            # wizard 控制需要 row:#1/wizard 路径
            _add_cases_for_key(cases, campaign_dir, key, spec, "core", None, f"row:#1/wizard")
        elif spec.target_kind in ("row", "acoustic-wizard"):
            _add_cases_for_key(cases, campaign_dir, key, spec, "core", None, f"row:#1")
        elif spec.target_kind == "blade":
            _add_cases_for_key(cases, campaign_dir, key, spec, "core", "default", _blade_selector())
        elif spec.target_kind in ("gap",):
            # gap 拓扑上下文作用于 blade，参数作用于 gap
            _add_cases_for_key(cases, campaign_dir, key, spec, "core", "default",
                               f"{_blade_selector()}/gap:shroud",
                               topo_entity_path=_blade_selector())
        elif spec.target_kind == "interface":
            _add_cases_for_key(cases, campaign_dir, key, spec, "core", None,
                               f"row:#1/interface:inlet")
        elif spec.target_kind == "stagnation-point":
            # stagnation-point 拓扑上下文作用于 blade，参数作用于 stagnation-point
            _add_cases_for_key(cases, campaign_dir, key, spec, "core", "default",
                               f"{_blade_selector()}/stagnation-point:leading",
                               topo_entity_path=_blade_selector())

    # 拓扑选择器
    for key in sorted(COMMON_TOPOLOGY_KEYS):
        spec = CONTROL_REGISTRY[key]
        for topo in TOPOLOGY_VALUES:
            _add_cases_for_key(cases, campaign_dir, key, spec, "topology", None, _blade_selector(),
                               override_values=[topo])

    # 条件族：Default / HOH / H&I
    for topo, keyset in TOPOLOGY_KEYS_MAP.items():
        for key in sorted(keyset):
            spec = CONTROL_REGISTRY[key]
            if not _is_rotor37_applicable(key, spec):
                continue
            if spec.target_kind == "blade":
                _add_cases_for_key(cases, campaign_dir, key, spec, topo, topo, _blade_selector())
            elif spec.target_kind == "gap":
                _add_cases_for_key(cases, campaign_dir, key, spec, topo, topo,
                                   f"{_blade_selector()}/gap:shroud",
                                   topo_entity_path=_blade_selector())

    return cases


def _add_cases_for_key(
    cases: list[dict[str, Any]],
    campaign_dir: Path,
    key: str,
    spec: ControlSpec,
    category: str,
    topology: str | None,
    entity_path: str,
    *,
    topo_entity_path: str | None = None,
    override_values: list[Any] | None = None,
) -> None:
    """为一个控制键生成 OFAT 测试案例。

    Args:
        topology: 固定拓扑值（None=不注入拓扑上下文）
        entity_path: 参数 --set 的实体路径
        topo_entity_path: 拓扑 --set 的实体路径（默认与 entity_path 相同；
                          对 gap/stagnation-point 应为 blade 路径）
    """
    values = override_values if override_values is not None else generate_test_values(spec)
    if topo_entity_path is None:
        topo_entity_path = entity_path

    for value in values:
        value_str = _format_value(value)
        safe_key = key.replace("/", "_").replace(".", "_")
        safe_val = str(value).replace("/", "_").replace(".", "_").replace(" ", "")
        case_dir = campaign_dir / "cases" / category / safe_key / f"value_{safe_val}"

        set_args: list[str] = []
        # 固定拓扑上下文（作用于 blade 路径）
        if topology and topo_entity_path:
            set_args.append(f"{topo_entity_path}/b2b.topology={topology}")
        # 被测参数（作用于实体路径）
        if entity_path:
            set_args.append(f"{entity_path}/{key.split('/', 1)[1]}={value_str}")
        else:
            set_args.append(f"{key}={value_str}")

        cases.append({
            "case_id": f"cases/{category}/{safe_key}/value_{safe_val}",
            "category": f"conditional_{topology}" if topology else category,
            "topology": topology,
            "set_args": set_args,
            "variable_key": key,
            "variable_value": value,
            "value_type": spec.value_type,
            "out_dir": str(case_dir),
            "si_length": spec.si_length,
            "stage": spec.stage,
        })


def _format_value(value: Any) -> str:
    """将 Python 值格式化为 CLI 参数字符串。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


# ============================================================================
# 执行引擎
# ============================================================================

def run_single_case(
    case: dict[str, Any],
    *,
    dry_run: bool = False,
    igg_exe: str | None = None,
) -> dict[str, Any]:
    """执行单个 mesh.py 调用并收集结果。"""
    out_dir = Path(case["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-B", str(MESH_PY),
        str(GEOMETRY_PATH),
        "--out", str(out_dir),
        "--timeout", str(TIMEOUT_SECONDS),
    ]
    if igg_exe:
        cmd.extend(["--igg", igg_exe])
    for arg in case["set_args"]:
        cmd.extend(["--set", arg])

    if dry_run:
        cmd.append("--dry-run")

    result: dict[str, Any] = {
        "case_id": case["case_id"],
        "category": case.get("category"),
        "topology": case.get("topology"),
        "variable_key": case.get("variable_key"),
        "variable_value": case.get("variable_value"),
        "command": " ".join(cmd),
        "out_dir": str(out_dir),
        "status": "dry_run" if dry_run else "pending",
        "returncode": None,
        "error": None,
        "run_summary": None,
        "quality": None,
        "mesh_outputs": {},
        "duration_seconds": None,
    }

    if dry_run:
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                cwd=str(PROJECT_ROOT),
            )
            result["returncode"] = completed.returncode
            if completed.returncode == 0:
                result["status"] = "dry_run_ok"
            else:
                result["status"] = "dry_run_failed"
                result["error"] = completed.stderr[:2000]
        except Exception as exc:
            result["status"] = "dry_run_error"
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    start = time.monotonic()
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS + 60,
            cwd=str(PROJECT_ROOT),
        )
        result["returncode"] = completed.returncode
        result["duration_seconds"] = round(time.monotonic() - start, 1)

        # 解析 run_summary.json
        summary_path = out_dir / "run_summary.json"
        if summary_path.exists():
            try:
                result["run_summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                result["run_summary"] = None

        if result["run_summary"]:
            result["quality"] = result["run_summary"].get("quality")
            autogrid = result["run_summary"].get("autogrid", {})
            result["mesh_outputs"] = autogrid.get("outputs", {})

        # 判定状态
        if completed.returncode == 0 and result["mesh_outputs"]:
            result["status"] = "success"
        elif completed.returncode == 0:
            result["status"] = "no_output"
        else:
            result["status"] = "failed"
            # 提取错误信息
            if result["run_summary"]:
                autogrid = result["run_summary"].get("autogrid", {})
                result["error"] = autogrid.get("error", "")
            if not result["error"]:
                result["error"] = completed.stderr[-2000:] if completed.stderr else f"returncode={completed.returncode}"

    except subprocess.TimeoutExpired as exc:
        result["status"] = "timeout"
        result["error"] = f"超时（{TIMEOUT_SECONDS}s）"
        result["duration_seconds"] = round(time.monotonic() - start, 1)
    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["duration_seconds"] = round(time.monotonic() - start, 1)

    return result


def execute_batch(
    cases: list[dict[str, Any]],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    dry_run: bool = False,
    igg_exe: str | None = None,
    results_file: Path | None = None,
) -> list[dict[str, Any]]:
    """并发执行案例列表，实时保存结果。"""
    results: list[dict[str, Any]] = []
    total = len(cases)
    completed_count = 0
    success_count = 0

    print(f"\n{'[DRY-RUN] ' if dry_run else ''}执行 {total} 个案例，最大并发 {max_workers}")
    print(f"超时：{TIMEOUT_SECONDS}s/例\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_case = {
            executor.submit(run_single_case, case, dry_run=dry_run, igg_exe=igg_exe): case
            for case in cases
        }

        for future in concurrent.futures.as_completed(future_to_case):
            case = future_to_case[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "case_id": case["case_id"],
                    "status": "executor_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)
            completed_count += 1
            if result.get("status") == "success":
                success_count += 1

            duration = result.get("duration_seconds", "?")
            print(f"[{completed_count:4d}/{total}] {result['status']:16s} "
                  f"({duration}s) {result['case_id']}")

            # 增量保存
            if results_file:
                _save_json(results_file, {
                    "total": total,
                    "completed": completed_count,
                    "success": success_count,
                    "last_update": _timestamp(),
                    "results": results,
                })

    print(f"\n完成：{success_count}/{total} 成功，{total - success_count} 失败/超时")
    return results


# ============================================================================
# 结果收集与分类
# ============================================================================

def collect_quality_metrics(run_summary: dict[str, Any] | None) -> dict[str, Any]:
    """从 run_summary 中提取 .qualityReport 的全部可比对数值指标。

    与旧版相比：不再只取少数 hand-picked 指标，而是抽取 metrics 下的所有
    标量数值字段（number_of_points / min/max/avg_* 等），确保任意指标的
    变化都能被检测到。
    """
    if not run_summary:
        return {}
    quality = run_summary.get("quality", {}) or {}
    metrics = quality.get("metrics", {}) or {}
    result_info = quality.get("result", {}) or {}

    collected: dict[str, Any] = {}
    # 提取 metrics 下所有标量数值（跳过字符串 block 名、嵌套 dict critical_location 等）
    for field, value in metrics.items():
        if isinstance(value, (int, float)):
            collected[field] = value

    # 质量判定与元数据（用于 EFFECT_OK → QUALITY_SENSITIVE 升级）
    collected["_quality_status"] = result_info.get("status")
    collected["_quality_accepted"] = result_info.get("accepted")

    return collected


def hash_file(path: str | None) -> str | None:
    """计算文件的 SHA-256 哈希。"""
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        return None
    try:
        hasher = hashlib.sha256()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def classify_control_result(
    key: str,
    case_results: list[dict[str, Any]],
    baseline_metrics: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """根据 OFAT 测试结果对单个控制参数进行分类。

    分类体系：
    - EFFECT_OK: 至少一个测试值成功生成网格且产生可检测的网格变化
    - CALL_ONLY: setter/getter 成功但网格无变化
    - FAILED_ALL: 所有测试值均失败
    - NOT_TESTED: 未执行测试
    """
    spec = CONTROL_REGISTRY.get(key)
    successes = [r for r in case_results if r.get("status") == "success"]
    failures = [r for r in case_results if r.get("status") in ("failed", "timeout", "error")]

    classification = {
        "key": key,
        "description": spec.description if spec else "",
        "value_type": spec.value_type if spec else "",
        "topologies": list(spec.topologies) if spec else [],
        "total_tests": len(case_results),
        "success_count": len(successes),
        "failure_count": len(failures),
        "level": "NOT_TESTED",
        "notes": [],
    }

    if not case_results:
        return classification

    if not successes:
        classification["level"] = "FAILED_ALL"
        classification["notes"].append(f"全部 {len(failures)} 个测试值均失败")
        if failures:
            classification["sample_error"] = failures[0].get("error", "")[:500]
        return classification

    # 全量指标比对：遍历 .qualityReport 中所有标量数值字段，
    # 任意一项与基线不同即认为控制产生了可检测效果。
    has_effect = False
    quality_changed = False
    for result in successes:
        metrics = collect_quality_metrics(result.get("run_summary"))
        baseline = baseline_metrics.get(result.get("topology", "default"), {})

        for field, value in metrics.items():
            if field.startswith("_"):
                continue  # 跳过 _quality_status / _quality_accepted
            baseline_value = baseline.get(field)
            if baseline_value is not None and value != baseline_value:
                has_effect = True
                # 不再 break——遍历完以便记录所有差异指标数量
        # 任一成功案例的 quality_status 或 accepted 与基线不同 → 质量敏感性
        if (metrics.get("_quality_status") != baseline.get("_quality_status")
                or metrics.get("_quality_accepted") != baseline.get("_quality_accepted")):
            quality_changed = True

    if has_effect:
        if quality_changed:
            classification["level"] = "QUALITY_SENSITIVE"
        else:
            classification["level"] = "EFFECT_OK"
    else:
        classification["level"] = "CALL_ONLY"
        classification["notes"].append("全部标量质量指标与基线一致——控制未产生可检测效果")

    return classification


def build_final_classification(
    all_results: list[dict[str, Any]],
    baseline_metrics: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """构建最终分类报告。"""
    by_category: dict[str, list[dict[str, Any]]] = {
        "COMMON_CORE": [],
        "COMMON_TOPOLOGY": [],
        "CONDITIONAL_DEFAULT": [],
        "CONDITIONAL_HOH": [],
        "CONDITIONAL_HI": [],
        "SUPPORTED_ADVANCED": [],
        "REJECTED": [],
    }

    # 按被测变量键分组
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in all_results:
        key = result.get("variable_key")
        if key:
            by_key[key].append(result)

    for key, case_results in sorted(by_key.items()):
        classification = classify_control_result(key, case_results, baseline_metrics)
        spec = CONTROL_REGISTRY.get(key)

        if not spec:
            by_category["REJECTED"].append(classification)
        elif key in COMMON_CORE_KEYS:
            by_category["COMMON_CORE"].append(classification)
        elif key in COMMON_TOPOLOGY_KEYS:
            by_category["COMMON_TOPOLOGY"].append(classification)
        elif key in TOPOLOGY_DEFAULT_KEYS:
            by_category["CONDITIONAL_DEFAULT"].append(classification)
        elif key in TOPOLOGY_HOH_KEYS:
            by_category["CONDITIONAL_HOH"].append(classification)
        elif key in TOPOLOGY_HI_KEYS:
            by_category["CONDITIONAL_HI"].append(classification)
        elif spec.topologies and spec.target_kind in ("gap", "partial-gap", "fillet",
                                                       "snubber", "blade-sheet",
                                                       "holes-line", "basin-hole",
                                                       "pin-fins-line", "endwall",
                                                       "endwall-holes-line"):
            by_category["SUPPORTED_ADVANCED"].append(classification)
        else:
            by_category["REJECTED"].append(classification)

    return by_category


# ============================================================================
# 报告生成
# ============================================================================

def generate_summary_report(
    campaign_dir: Path,
    baselines: list[dict[str, Any]],
    all_results: list[dict[str, Any]],
    classification: dict[str, list[dict[str, Any]]],
) -> str:
    """生成中文 Markdown 结果报告。"""
    lines = [
        "# Rotor37 B2B 拓扑控制验证报告",
        "",
        f"**执行时间**：{_timestamp()}",
        f"**几何**：Rotor37.geomTurbo",
        f"**总案例数**：{len(all_results)}",
        f"**基线数**：{len(baselines)}",
        "",
        "## 1. 基线结果",
        "",
        "| 拓扑 | 重复 | 总点数 | 质量 | 耗时 | 输出文件 |",
        "|---|---|---:|---|---|---|",
    ]
    for bl in baselines:
        metrics = collect_quality_metrics(bl.get("run_summary"))
        outputs = bl.get("mesh_outputs", {})
        lines.append(
            f"| {bl.get('topology', '?')} | {bl.get('run_index', '?')} | "
            f"{metrics.get('total_points', '?')} | {metrics.get('quality_status', '?')} | "
            f"{bl.get('duration_seconds', '?')}s | "
            f"{','.join(outputs.keys()) or '无'} |"
        )

    lines.extend([
        "",
        "## 2. 执行统计",
        "",
        f"| 状态 | 数量 |",
        f"|---|---|",
    ])
    status_counts = defaultdict(int)
    for r in all_results:
        status_counts[r.get("status", "unknown")] += 1
    for status, count in sorted(status_counts.items()):
        lines.append(f"| {status} | {count} |")

    lines.extend([
        "",
        "## 3. 控制参数分类",
        "",
        "| 分类 | EFFECT_OK | QUALITY_SENSITIVE | CALL_ONLY | FAILED_ALL | NOT_TESTED | 合计 |",
        "|---|---|---|---:|---:|---:|---:|",
    ])
    for cat, items in classification.items():
        counts = defaultdict(int)
        for item in items:
            counts[item.get("level", "NOT_TESTED")] += 1
        lines.append(
            f"| {cat} | {counts.get('EFFECT_OK', 0)} | {counts.get('QUALITY_SENSITIVE', 0)} | "
            f"{counts.get('CALL_ONLY', 0)} | {counts.get('FAILED_ALL', 0)} | "
            f"{counts.get('NOT_TESTED', 0)} | {len(items)} |"
        )

    lines.extend([
        "",
        "## 4. 详细结果",
        "",
    ])
    for cat, items in classification.items():
        if not items:
            continue
        lines.extend([
            f"### 4.{list(classification.keys()).index(cat) + 1} {cat}（{len(items)} 项）",
            "",
            "| 控制键 | 类型 | 说明 | 等级 | 成功/总数 | 备注 |",
            "|---|---|---|---:|---|",
        ])
        for item in items:
            lines.append(
                f"| `{item['key']}` | {item.get('value_type', '?')} | "
                f"{item.get('description', '')} | {item.get('level', '?')} | "
                f"{item.get('success_count', 0)}/{item.get('total_tests', 0)} | "
                f"{'; '.join(item.get('notes', []))} |"
            )

    return "\n".join(lines) + "\n"


# ============================================================================
# 工具函数
# ============================================================================

def _save_json(path: Path, data: Any) -> None:
    """原子写入 JSON 文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path) -> Any:
    """安全加载 JSON 文件。"""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ============================================================================
# 主入口
# ============================================================================

def _print_matrix(campaign_dir: Path) -> None:
    """打印完整测试矩阵概况。"""
    baseline_cases = build_baseline_cases(campaign_dir)
    topology_cases = build_topology_switch_cases(campaign_dir)
    ofat_cases = build_ofat_cases(campaign_dir)

    print(f"基线案例：{len(baseline_cases)}（{len(TOPOLOGY_VALUES)} 拓扑 × {BASELINE_REPEATS} 重复）")
    for c in baseline_cases:
        print(f"  [{c['case_id']}]")
        print(f"    --set {' --set '.join(c['set_args'])}")

    print(f"\n拓扑切换案例：{len(topology_cases)}")
    for c in topology_cases:
        print(f"  [{c['case_id']}]")
        print(f"    --set {' --set '.join(c['set_args'])}")

    print(f"\nOFAT 测试案例：{len(ofat_cases)}")

    # 按分类统计
    by_category: dict[str, int] = defaultdict(int)
    by_key: dict[str, int] = defaultdict(int)
    for c in ofat_cases:
        cat = c.get("category", "other")
        by_category[cat] += 1
        if c.get("variable_key"):
            by_key[c["variable_key"]] += 1

    print(f"\n按分类统计：")
    for cat, count in sorted(by_category.items()):
        print(f"  {cat}: {count}")
    print(f"\n被测参数数：{len(by_key)}")
    print(f"总计（含基线 + 拓扑切换）：{len(baseline_cases) + len(topology_cases) + len(ofat_cases)}")

    # 打印 OFAT 案例前 20 个示例
    print(f"\nOFAT 案例示例（前 20 个）：")
    for c in ofat_cases[:20]:
        print(f"  [{c['case_id']}]")
        print(f"    拓扑={c.get('topology')} 变量={c.get('variable_key')}={c.get('variable_value')}")
        print(f"    --set {' --set '.join(c['set_args'])}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rotor37 B2B 拓扑控制验证活动执行器（PLAN.md 方案）"
    )
    parser.add_argument("--phase", type=int, choices=(1, 2, 3), default=0,
                        help="只执行指定阶段（1=基线 2=测试矩阵 3=报告）；默认全部")
    parser.add_argument("--dry-run", action="store_true",
                        help="只生成测试矩阵和脚本，不实际启动 IGG")
    parser.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS,
                        help=f"最大并发数（默认 {DEFAULT_MAX_WORKERS}）")
    parser.add_argument("--igg", default=None,
                        help="IGG 可执行文件路径；覆盖 .env 和自动检测")
    parser.add_argument("--campaign-dir", default=None,
                        help="活动根目录；默认 runs/rotor37-control-validation/<时间戳>")
    parser.add_argument("--resume", default=None,
                        help="从指定活动目录恢复执行")
    parser.add_argument("--list-matrix", action="store_true",
                        help="只打印测试矩阵，不执行")
    args = parser.parse_args()

    # 确定活动目录
    if args.resume:
        campaign_dir = Path(args.resume)
        if not campaign_dir.exists():
            print(f"错误：活动目录不存在：{campaign_dir}", file=sys.stderr)
            return 2
    elif args.campaign_dir:
        campaign_dir = Path(args.campaign_dir)
    else:
        campaign_dir = PROJECT_ROOT / "runs" / "rotor37-control-validation" / _timestamp()

    campaign_dir.mkdir(parents=True, exist_ok=True)
    state_file = campaign_dir / "campaign_state.json"

    # --list-matrix：打印完整测试矩阵后退出
    if args.list_matrix:
        _print_matrix(campaign_dir)
        return 0

    # 加载或初始化状态
    state = _load_json(state_file) or {"phase": 0, "baselines": [], "results": []}

    do_phase = lambda p: args.phase in (0, p)

    # ---- Phase 1: 基线 ----
    if do_phase(1) and state.get("phase", 0) < 1:
        print("=" * 60)
        print("Phase 1: 生成 A/A 基线")
        print("=" * 60)
        baseline_cases = build_baseline_cases(campaign_dir)
        print(f"基线案例数：{len(baseline_cases)}（{len(TOPOLOGY_VALUES)} 拓扑 × {BASELINE_REPEATS} 重复）")

        baseline_results = execute_batch(
            baseline_cases,
            max_workers=args.workers,
            dry_run=args.dry_run,
            igg_exe=args.igg,
            results_file=campaign_dir / "baselines_results.json",
        )
        state["baselines"] = baseline_results
        state["phase"] = 1
        _save_json(state_file, state)

        if args.dry_run:
            print("\nDry-run 完成。使用 --phase 2 继续测试矩阵。")
            return 0

    # ---- Phase 2: 测试矩阵 ----
    if do_phase(2) and state.get("phase", 0) < 2:
        print("\n" + "=" * 60)
        print("Phase 2: 构建并执行测试矩阵")
        print("=" * 60)

        # 构建测试矩阵
        topology_cases = build_topology_switch_cases(campaign_dir)
        ofat_cases = build_ofat_cases(campaign_dir)
        all_test_cases = topology_cases + ofat_cases
        print(f"测试案例总数：{len(all_test_cases)}（拓扑切换 {len(topology_cases)} + OFAT {len(ofat_cases)}）")

        # 按拓扑和被测键统计
        by_key: dict[str, int] = defaultdict(int)
        for c in ofat_cases:
            if c.get("variable_key"):
                by_key[c["variable_key"]] += 1
        print(f"被测参数数：{len(by_key)}")

        all_results = execute_batch(
            all_test_cases,
            max_workers=args.workers,
            dry_run=args.dry_run,
            igg_exe=args.igg,
            results_file=campaign_dir / "test_results.json",
        )
        state["results"] = all_results
        state["phase"] = 2
        _save_json(state_file, state)

        if args.dry_run:
            print("\nDry-run 完成。使用 --phase 3 生成报告。")
            return 0

    # ---- Phase 3: 报告 ----
    if do_phase(3) and state.get("phase", 0) < 3:
        print("\n" + "=" * 60)
        print("Phase 3: 收集结果并生成报告")
        print("=" * 60)

        baselines = state.get("baselines", [])
        all_results = state.get("results", [])

        if not all_results and not baselines:
            print("无可用结果。请先执行 Phase 1 和 Phase 2。", file=sys.stderr)
            return 2

        # 提取基线指标
        baseline_metrics: dict[str, dict[str, Any]] = {}
        for bl in baselines:
            topo = bl.get("topology", "default")
            if topo not in baseline_metrics:
                baseline_metrics[topo] = collect_quality_metrics(bl.get("run_summary"))

        # 分类
        classification = build_final_classification(all_results, baseline_metrics)

        # 生成报告
        report = generate_summary_report(campaign_dir, baselines, all_results, classification)
        report_path = campaign_dir / "validation_report.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"报告已保存：{report_path}")

        # 保存分类 JSON
        classification_path = campaign_dir / "classification.json"
        _save_json(classification_path, classification)
        print(f"分类数据已保存：{classification_path}")

        # 统计
        total_tested = sum(len(items) for items in classification.values())
        print(f"\n分类统计：")
        for cat, items in classification.items():
            if items:
                print(f"  {cat}: {len(items)} 项")

        state["phase"] = 3
        _save_json(state_file, state)

    print(f"\n活动目录：{campaign_dir}")
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
