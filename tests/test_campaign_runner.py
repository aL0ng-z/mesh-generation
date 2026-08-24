"""Rotor37 通用网格控制参数实机验证活动执行器。

该脚本同时承担四项职责：

1. 输出当前 344 个注册控制和 AutoGrid 17.1 API 的可审计清单；
2. 为 156 个通用候选控制建立显式依赖上下文和至少两个测试值；
3. 以最多 32 个独立 ``mesh.py`` 子进程并发生成真实网格；
4. 保存可恢复的 campaign 状态，交给 ``test_analyze_results.py`` 汇总。

所有运行产物均位于 ``runs/rotor37-control-validation/<campaign_id>/``。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SOURCE_DIR))

from controls import (  # noqa: E402
    CONDITIONAL_KEY_TOPOLOGY,
    CONTROL_DEPENDENCY_DEPTH,
    CONTROL_PREREQUISITES,
    CONTROL_REGISTRY,
    EXCLUDED_SETTERS,
    GENERAL_API_ONLY_EXCLUSIONS,
    GENERAL_CONTROL_EXCLUSIONS,
    GENERAL_CONTROL_KEYS,
    GENERAL_CORE_KEYS,
    GENERAL_DEFAULT_KEYS,
    GENERAL_EDGE_TREATMENT_KEYS,
    GENERAL_HI_KEYS,
    GENERAL_HOH_KEYS,
    GENERAL_TOPOLOGY_KEYS,
    MAPPED_SETTERS_BY_OWNER,
    STAGE_ORDER,
    TOPOLOGY_SELECTOR_KEY,
    ControlSpec,
    audit_autogrid_source,
    audit_control_bindings,
)


GEOMETRY_PATH = PROJECT_ROOT / "geometries" / "Rotor37.geomTurbo"
MESH_PY = SOURCE_DIR / "mesh.py"
AUTOGRID_171 = Path(r"C:\ProgramData\NUMECA\fine171\_python\_autogrid\Autogrid.py")
IGG_PYTHON_171 = Path(r"C:\ProgramData\NUMECA\fine171\_python\_igg\PYTHON.py")
RUNS_ROOT = PROJECT_ROOT / "runs" / "rotor37-control-validation"

DEFAULT_MAX_WORKERS = 32
DEFAULT_TIMEOUT_SECONDS = 1800
BASELINE_REPEATS = 4
TOPOLOGY_VALUES = ("default", "hoh", "hi")
REQUIRED_OUTPUTS = frozenset({"igg", "cgns", "trb", "quality_report"})

SOURCE_FILES = (
    SOURCE_DIR / "mesh.py",
    SOURCE_DIR / "controls.py",
    SOURCE_DIR / "autogrid.py",
    SOURCE_DIR / "geomturbo.py",
    SOURCE_DIR / "quality.py",
    Path(__file__).resolve(),
    PROJECT_ROOT / "tests" / "test_analyze_results.py",
    GEOMETRY_PATH,
)

TOPOLOGY_GROUPS: dict[str, frozenset[str]] = {
    "default": GENERAL_DEFAULT_KEYS,
    "hoh": GENERAL_HOH_KEYS,
    "hi": GENERAL_HI_KEYS,
}

HOH_RESCUE_KEYS = (
    "blade/b2b.hoh.boundary_layer_cell_width",
    "blade/b2b.hoh.boundary_layer_factor",
    "blade/b2b.hoh.boundary_layer_points",
    "blade/b2b.hoh.around_boundary_layer_points",
    "row/flow_path.number",
)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _source_signature() -> str:
    payload = [
        (str(path.relative_to(PROJECT_ROOT)), _sha256_file(path))
        for path in SOURCE_FILES
        if path.exists()
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _safe_name(value: Any, *, limit: int = 96) -> str:
    text = str(value)
    text = re.sub(r"[^0-9A-Za-z_-]+", "_", text).strip("_")
    if not text:
        text = "empty"
    if len(text) <= limit:
        return text
    suffix = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return f"{text[: limit - 11]}_{suffix}"


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return ",".join(_format_value(item) for item in value)
    if isinstance(value, float):
        return format(value, ".12g")
    return str(value)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _control_group(key: str) -> str:
    if key in GENERAL_CORE_KEYS:
        return "GENERAL_CORE"
    if key in GENERAL_TOPOLOGY_KEYS:
        return "GENERAL_TOPOLOGY"
    if key in GENERAL_DEFAULT_KEYS:
        return "GENERAL_DEFAULT"
    if key in GENERAL_HOH_KEYS:
        return "GENERAL_HOH"
    if key in GENERAL_HI_KEYS:
        return "GENERAL_HI"
    return "EXCLUDED"


def _registered_setter_names() -> set[tuple[str, str]]:
    return set(MAPPED_SETTERS_BY_OWNER)


def build_extended_api_audit() -> list[dict[str, Any]]:
    """审计 set/enable/disable/unset/compute/generate 与非标准赋值方法。"""

    if not AUTOGRID_171.exists():
        return []
    setter_audit = {
        str(item["setter"]): item for item in audit_autogrid_source(AUTOGRID_171)
    }
    mapped = _registered_setter_names()
    lines = AUTOGRID_171.read_text(encoding="latin1").splitlines()
    owner = ""
    results: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        class_match = re.match(r"class\s+(\w+)", line)
        if class_match:
            owner = class_match.group(1)
        method_match = re.match(r"(\s*)def\s+(\w+)\s*\(([^)]*)\)", line)
        if not method_match:
            continue
        if not method_match.group(1):
            owner = ""
        method = method_match.group(2)
        signature = method_match.group(3)
        lower = method.lower()
        kind = None
        for prefix in ("a5_set_", "set_", "enable_", "disable_", "unset_", "compute_", "generate"):
            if lower.startswith(prefix):
                kind = prefix.rstrip("_")
                break
        if method == "desired_expansion_ratio":
            kind = "nonstandard_setter"
        if kind is None:
            continue

        qualified = f"{owner}.{method}" if owner else method
        audit_item = setter_audit.get(qualified) or setter_audit.get(method)
        if (owner, method) in mapped or ("", method) in mapped:
            classification = "mapped"
            reason = "已映射到注册控制"
        elif audit_item is not None:
            classification = str(audit_item["status"])
            reason = str(audit_item.get("reason") or "")
        elif qualified in GENERAL_API_ONLY_EXCLUSIONS:
            classification = "excluded"
            reason = GENERAL_API_ONLY_EXCLUSIONS[qualified]
        elif lower.startswith(("compute_", "generate")):
            classification = "activation"
            reason = "计算/生成激活动作，不是独立持久控制值"
        elif lower.startswith("unset_"):
            classification = "excluded"
            reason = "旧式清除/恢复接口；规范控制通过显式值表达"
        elif owner in {
            "Gap",
            "PartialGap",
            "Fillet",
            "HolesLine",
            "EndWallHolesLine",
            "BasinHole",
            "PinFinsLine",
            "EndWall",
            "TechnologicalEffectZR",
            "TechnologicalEffect3D",
            "WizardLETE",
        }:
            classification = "excluded"
            reason = "依赖特定几何或既有技术效果实体"
        elif "low_memory" in lower:
            classification = "excluded"
            reason = "运行资源策略，不应改变最终网格"
        elif "full_mesh" in lower or "acoustic" in lower:
            classification = "excluded"
            reason = "物理计算域/声学配置，不属于通用网格控制"
        else:
            classification = "excluded"
            reason = "别名、实体状态或非持久网格控制"
        results.append(
            {
                "owner": owner or "<module>",
                "method": method,
                "qualified_method": qualified,
                "signature": signature,
                "kind": kind,
                "classification": classification,
                "reason": reason,
                "source_line": line_number,
            }
        )
    return results


def write_inventory(campaign_dir: Path) -> dict[str, Any]:
    """输出注册表、策展分组、API 审计和环境指纹。"""

    inventory_dir = campaign_dir / "inventory"
    inventory_dir.mkdir(parents=True, exist_ok=True)

    control_rows: list[dict[str, Any]] = []
    for key, spec in sorted(CONTROL_REGISTRY.items()):
        prerequisites = CONTROL_PREREQUISITES.get(key, ())
        control_rows.append(
            {
                "key": key,
                "description": spec.description,
                "scope": spec.scope,
                "target_kind": spec.target_kind,
                "value_type": spec.value_type,
                "enum_values": "|".join(spec.enum_values),
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "si_length": spec.si_length,
                "stage": spec.stage,
                "priority": spec.priority,
                "setter": spec.setter or "|".join(method for _, method in spec.setter_by_value),
                "getter": spec.getter,
                "topologies": "|".join(spec.topologies),
                "general_group": _control_group(key),
                "general_candidate": key in GENERAL_CONTROL_KEYS,
                "exclusion_reason": GENERAL_CONTROL_EXCLUSIONS.get(key, ""),
                "prerequisites": json.dumps(prerequisites, ensure_ascii=False),
                "dependency_depth": CONTROL_DEPENDENCY_DEPTH.get(key, 0),
            }
        )
    _write_csv(
        inventory_dir / "supported_controls.csv",
        control_rows,
        list(control_rows[0]),
    )
    _write_json(inventory_dir / "supported_controls.json", control_rows)

    setter_audit = (
        audit_autogrid_source(AUTOGRID_171) if AUTOGRID_171.exists() else []
    )
    binding_audit = (
        audit_control_bindings(AUTOGRID_171) if AUTOGRID_171.exists() else []
    )
    extended_audit = build_extended_api_audit()
    if setter_audit:
        _write_csv(
            inventory_dir / "setter_audit.csv",
            setter_audit,
            ["setter", "status", "control_keys", "reason"],
        )
    if binding_audit:
        _write_csv(
            inventory_dir / "binding_audit.csv",
            binding_audit,
            [
                "control_key",
                "role",
                "method",
                "expected_owners",
                "available_owners",
                "status",
            ],
        )
    if extended_audit:
        _write_csv(
            inventory_dir / "extended_api_audit.csv",
            extended_audit,
            list(extended_audit[0]),
        )

    selection_counts = Counter(_control_group(key) for key in CONTROL_REGISTRY)
    source_hashes = {
        str(path): _sha256_file(path)
        for path in (*SOURCE_FILES, AUTOGRID_171, IGG_PYTHON_171)
        if path.exists()
    }
    summary = {
        "registered_controls": len(CONTROL_REGISTRY),
        "general_controls": len(GENERAL_CONTROL_KEYS),
        "excluded_controls": len(GENERAL_CONTROL_EXCLUSIONS),
        "selection_counts": dict(sorted(selection_counts.items())),
        "setter_audit": Counter(item["status"] for item in setter_audit),
        "binding_audit": Counter(item["status"] for item in binding_audit),
        "extended_api_audit": Counter(item["classification"] for item in extended_audit),
        "source_hashes": source_hashes,
        "python": sys.version,
        "platform": sys.platform,
        "source_signature": _source_signature(),
    }
    summary = json.loads(json.dumps(summary, default=dict))
    _write_json(inventory_dir / "inventory_summary.json", summary)

    lines = [
        "# Rotor37 通用网格控制参数清单",
        "",
        f"- 注册控制：{len(CONTROL_REGISTRY)}",
        f"- 通用候选：{len(GENERAL_CONTROL_KEYS)}",
        f"- 明确排除：{len(GENERAL_CONTROL_EXCLUSIONS)}",
        f"- setter 审计：{dict(Counter(item['status'] for item in setter_audit))}",
        f"- binding 审计：{dict(Counter(item['status'] for item in binding_audit))}",
        "",
        "| 分组 | 数量 |",
        "|---|---:|",
    ]
    for group, count in sorted(selection_counts.items()):
        lines.append(f"| {group} | {count} |")
    (inventory_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


VALUE_OVERRIDES: dict[str, list[Any]] = {
    "configuration/grid_levels": [3, 4],
    "configuration/support_curve_control_points": [101, 201],
    "wizard/first_cell_width": [1.0e-6, 5.0e-6],
    "wizard/spanwise_paths": [65, 97],
    "row/flow_path.number": [65, 97],
    "row/target_points": [500000, 1000000],
    "row/streamwise_weight": [(0.75, 1.0, 1.0), (1.25, 1.0, 1.0)],
    "blade/b2b.default.blade_reference_angle": [-5.0, 5.0],
    "blade/b2b.default.inlet_angle": [-5.0, 5.0],
    "blade/b2b.default.outlet_angle": [-5.0, 5.0],
    "blade/b2b.default.throat_points": [0, 9],
    "blade/b2b.default.throat_projection_type": [0, 1],
    "blade/b2b.hoh.boundary_layer_factor": [1.1, 1.4],
    "blade/b2b.hoh.leading_edge_cell_length": [1.0e-5, 5.0e-5],
    "blade/b2b.hoh.trailing_edge_cell_length": [1, 5],
    "stagnation-point/desired_expansion_ratio": [1.1, 1.5],
}


def generate_test_values(spec: ControlSpec) -> list[Any]:
    """为候选控制生成至少两个安全且明显不同的值。"""

    if spec.key in VALUE_OVERRIDES:
        return list(VALUE_OVERRIDES[spec.key])
    if spec.value_type == "bool":
        return [False, True]
    if spec.value_type == "enum":
        values = list(spec.enum_values)
        if spec.key == TOPOLOGY_SELECTOR_KEY:
            values = [value for value in values if value != "user"]
        return values
    if spec.value_type in {"tuple_int", "tuple_float"}:
        if spec.value_type == "tuple_int":
            return [(1, 2, 3), (3, 2, 1)]
        return [(0.75, 1.0, 1.0), (1.25, 1.0, 1.0)]

    minimum = spec.minimum
    maximum = spec.maximum
    if spec.value_type == "int":
        lo = int(minimum if minimum is not None else 0)
        hi = int(maximum if maximum is not None else 100000)
        if "grid_levels" in spec.key:
            return [max(lo, 3), min(hi, 4)]
        if "target_points" in spec.key:
            return [max(lo, 500000), min(hi, 1000000)]
        if "index" in spec.key:
            return [max(lo, 0), min(hi, 9)]
        if "points" in spec.key or "npts" in spec.key:
            return [max(lo, 17), min(hi, 33)]
        if "control_points" in spec.key:
            return [max(lo, 17), min(hi, 33)]
        if "steps" in spec.key:
            return [max(lo, 0), min(hi, 200)]
        if hi <= 1:
            return [lo, hi]
        if hi <= 10:
            return [lo, min(hi, max(lo + 1, 1))]
        return [max(lo, 5), min(hi, max(lo + 4, 9))]

    lo_float = float(minimum if minimum is not None else 0.0)
    hi_float = float(maximum) if maximum is not None else None
    if spec.si_length:
        if "boundary_layer_width" in spec.key:
            values = [5.0e-5, 2.0e-4]
        elif "absolute_distance" in spec.key:
            values = [1.0e-4, 5.0e-4]
        else:
            values = [1.0e-6, 5.0e-6]
    elif minimum is not None and minimum >= 1.0:
        values = [max(lo_float, 1.1), max(lo_float, 1.5)]
    elif maximum is not None and maximum <= 1.0:
        values = [max(lo_float, 0.25), min(float(maximum), 0.75)]
    elif maximum is not None and maximum <= 100.0 and "percent" in spec.key:
        values = [max(lo_float, 20.0), min(float(maximum), 60.0)]
    elif minimum is not None and minimum < 0:
        values = [max(lo_float, -5.0), min(hi_float or 5.0, 5.0)]
    elif "expansion" in spec.key or spec.key.endswith("_ratio"):
        values = [max(lo_float, 1.1), 1.5]
    elif "clustering" in spec.key or "relaxation" in spec.key or "weight" in spec.key:
        values = [max(lo_float, 0.25), 0.75]
    else:
        values = [max(lo_float, 0.25), 0.75]
    if hi_float is not None:
        values = [min(hi_float, value) for value in values]
    if values[0] == values[1]:
        candidate = values[0] + max(abs(values[0]) * 0.5, 0.1)
        values[1] = min(hi_float, candidate) if hi_float is not None else candidate
    return values


def _assignment_for_key(key: str, value: Any, *, target: str | None = None) -> str:
    spec = CONTROL_REGISTRY[key]
    local_key = key.split("/", 1)[1]
    if target is None:
        if spec.scope == "configuration":
            target = "configuration"
        elif spec.target_kind == "wizard":
            target = "row:#1/wizard"
        elif spec.target_kind == "row":
            target = "row:#1"
        elif spec.target_kind == "blade":
            target = "row:#1/blade:#1"
        elif spec.target_kind == "interface":
            target = "row:#1/interface:inlet"
        elif spec.target_kind == "stagnation-point":
            target = "row:#1/blade:#1/stagnation-point:leading"
        else:
            raise ValueError(f"通用 campaign 不支持目标类型：{spec.target_kind}")
    return f"{target}/{local_key}={_format_value(value)}"


def _topology_for_key(key: str) -> str | None:
    if key in GENERAL_DEFAULT_KEYS:
        return "default"
    if key in GENERAL_HOH_KEYS:
        return "hoh"
    if key in GENERAL_HI_KEYS:
        return "hi"
    if key in GENERAL_CORE_KEYS:
        return "default"
    return None


def _add_dependency(
    dependencies: dict[str, Any],
    key: str,
    value: Any,
    *,
    variable_key: str,
    trail: tuple[str, ...] = (),
) -> None:
    if key == variable_key:
        return
    if key in trail:
        raise RuntimeError("campaign 前置条件存在循环：" + " -> ".join(trail + (key,)))
    for prerequisite_key, prerequisite_value in CONTROL_PREREQUISITES.get(key, ()):
        _add_dependency(
            dependencies,
            prerequisite_key,
            prerequisite_value,
            variable_key=variable_key,
            trail=trail + (key,),
        )
    if key in dependencies and dependencies[key] != value:
        previous = dependencies[key]
        raise RuntimeError(f"前置条件冲突：{key} 同时要求 {previous!r} 和 {value!r}")
    dependencies[key] = value


def build_dependency_controls(key: str, value: Any) -> list[tuple[str, Any]]:
    """建立同一案例的完整显式依赖，不把依赖静默注入普通 CLI。"""

    dependencies: dict[str, Any] = {}
    topology = _topology_for_key(key)
    if topology is not None and key != TOPOLOGY_SELECTOR_KEY:
        dependencies[TOPOLOGY_SELECTOR_KEY] = topology
    if key in GENERAL_EDGE_TREATMENT_KEYS:
        dependencies["blade/b2b.default.type"] = "streamwise"
    for prerequisite_key, prerequisite_value in CONTROL_PREREQUISITES.get(key, ()):
        _add_dependency(
            dependencies,
            prerequisite_key,
            prerequisite_value,
            variable_key=key,
        )

    # 值相关的可设置前置条件。
    if key == "wizard/grid_level" and value == "user":
        _add_dependency(
            dependencies,
            "row/target_points",
            750000,
            variable_key=key,
        )

    ordered = sorted(
        dependencies.items(),
        key=lambda item: (
            STAGE_ORDER[CONTROL_REGISTRY[item[0]].stage],
            0 if item[0] == TOPOLOGY_SELECTOR_KEY else 1,
            CONTROL_DEPENDENCY_DEPTH.get(item[0], 0),
            item[0],
        ),
    )
    return ordered


def force_topology_dependency(
    dependencies: Iterable[tuple[str, Any]],
    topology: str,
) -> list[tuple[str, Any]]:
    """在拓扑救援等跨族上下文中显式覆盖控制自身的默认拓扑。"""

    forced = [
        (key, value)
        for key, value in dependencies
        if key != TOPOLOGY_SELECTOR_KEY
    ]
    forced.append((TOPOLOGY_SELECTOR_KEY, topology))
    return sorted(
        forced,
        key=lambda item: (
            STAGE_ORDER[CONTROL_REGISTRY[item[0]].stage],
            0 if item[0] == TOPOLOGY_SELECTOR_KEY else 1,
            CONTROL_DEPENDENCY_DEPTH.get(item[0], 0),
            item[0],
        ),
    )


def _context_id(dependencies: Iterable[tuple[str, Any]]) -> str:
    normalized = json.dumps(list(dependencies), ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _case_signature(case: dict[str, Any]) -> str:
    payload = {
        "case_id": case["case_id"],
        "set_args": case.get("set_args", []),
        "source_signature": _source_signature(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()


def make_case(
    campaign_dir: Path,
    *,
    phase: str,
    category: str,
    variable_key: str | None,
    variable_value: Any,
    dependencies: list[tuple[str, Any]],
    topology: str | None,
    target: str | None = None,
    case_suffix: str | None = None,
    subroot: str = "cases",
) -> dict[str, Any]:
    dependency_assignments = [
        _assignment_for_key(dep_key, dep_value)
        for dep_key, dep_value in dependencies
    ]
    set_args = list(dependency_assignments)
    if variable_key is not None:
        set_args.append(_assignment_for_key(variable_key, variable_value, target=target))
    key_name = _safe_name(variable_key or category)
    value_name = _safe_name(
        case_suffix if case_suffix is not None else _format_value(variable_value)
    )
    out_dir = campaign_dir / subroot / category / key_name / f"value_{value_name}"
    context_id = _context_id(dependencies)
    case_id = f"{phase}/{category}/{key_name}/value_{value_name}"
    case = {
        "case_id": case_id,
        "phase": phase,
        "category": category,
        "topology": topology,
        "variable_key": variable_key,
        "variable_value": variable_value,
        "dependency_controls": [
            {"key": dep_key, "value": dep_value} for dep_key, dep_value in dependencies
        ],
        "context_id": context_id,
        "set_args": set_args,
        "out_dir": str(out_dir),
    }
    case["signature"] = _case_signature(case)
    return case


def build_baseline_cases(campaign_dir: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for topology in TOPOLOGY_VALUES:
        dependencies = [(TOPOLOGY_SELECTOR_KEY, topology)]
        for repeat in range(1, BASELINE_REPEATS + 1):
            cases.append(
                make_case(
                    campaign_dir,
                    phase="baseline",
                    category=topology,
                    variable_key=None,
                    variable_value=None,
                    dependencies=dependencies,
                    topology=topology,
                    case_suffix=f"A{repeat:02d}",
                    subroot="baselines",
                )
            )
    return cases


def build_pilot_cases(campaign_dir: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for repeat in (1, 2):
        cases.append(
            make_case(
                campaign_dir,
                phase="pilot",
                category="default_aa",
                variable_key=None,
                variable_value=None,
                dependencies=[(TOPOLOGY_SELECTOR_KEY, "default")],
                topology="default",
                case_suffix=f"A{repeat:02d}",
                subroot="pilot",
            )
        )
    for value in (65, 97):
        dependencies = build_dependency_controls("wizard/spanwise_paths", value)
        cases.append(
            make_case(
                campaign_dir,
                phase="pilot",
                category="known_effect",
                variable_key="wizard/spanwise_paths",
                variable_value=value,
                dependencies=dependencies,
                topology="default",
                subroot="pilot",
            )
        )
    for topology in ("default", "hi"):
        cases.append(
            make_case(
                campaign_dir,
                phase="pilot",
                category="topology_switch",
                variable_key=TOPOLOGY_SELECTOR_KEY,
                variable_value=topology,
                dependencies=[],
                topology=topology,
                subroot="pilot",
            )
        )
    for value in (False, True):
        cases.append(
            make_case(
                campaign_dir,
                phase="pilot",
                category="negative_control",
                variable_key="row/low_memory_usage",
                variable_value=value,
                dependencies=[(TOPOLOGY_SELECTOR_KEY, "default")],
                topology="default",
                subroot="pilot",
            )
        )
    return cases


def build_control_cases(
    campaign_dir: Path,
    *,
    hoh_anchor: list[tuple[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for key in sorted(GENERAL_CONTROL_KEYS):
        if key in GENERAL_HOH_KEYS and hoh_anchor is None:
            blocked.append(
                {
                    "case_id": f"cases/blocked/{_safe_name(key)}",
                    "phase": "cases",
                    "category": "GENERAL_HOH",
                    "topology": "hoh",
                    "variable_key": key,
                    "variable_value": None,
                    "dependency_controls": [],
                    "context_id": "blocked_hoh",
                    "set_args": [],
                    "out_dir": "",
                    "signature": "",
                    "status": "blocked",
                    "error": "BLOCKED_TOPOLOGY_BASELINE",
                    "run_summary": None,
                }
            )
            continue
        spec = CONTROL_REGISTRY[key]
        topology = _topology_for_key(key)
        for value in generate_test_values(spec):
            dependencies = build_dependency_controls(key, value)
            if key in GENERAL_HOH_KEYS and hoh_anchor:
                existing = dict(dependencies)
                for anchor_key, anchor_value in hoh_anchor:
                    if anchor_key != key and anchor_key not in existing:
                        dependencies.append((anchor_key, anchor_value))
                dependencies = sorted(
                    dependencies,
                    key=lambda item: (
                        STAGE_ORDER[CONTROL_REGISTRY[item[0]].stage],
                        CONTROL_DEPENDENCY_DEPTH.get(item[0], 0),
                        item[0],
                    ),
                )
            cases.append(
                make_case(
                    campaign_dir,
                    phase="cases",
                    category=_control_group(key),
                    variable_key=key,
                    variable_value=value,
                    dependencies=dependencies,
                    topology=topology if key != TOPOLOGY_SELECTOR_KEY else str(value),
                )
            )

    # 目标解析 smoke：不重复全族，仅用代表值验证 outlet/trailing accessor。
    smoke_specs = (
        (
            "interface/streamwise_cell_width",
            generate_test_values(CONTROL_REGISTRY["interface/streamwise_cell_width"])[0],
            "row:#1/interface:outlet",
            "outlet_interface",
        ),
        (
            "stagnation-point/constant_cells_percent",
            generate_test_values(CONTROL_REGISTRY["stagnation-point/constant_cells_percent"])[0],
            "row:#1/blade:#1/stagnation-point:trailing",
            "trailing_stagnation",
        ),
    )
    for key, value, target, suffix in smoke_specs:
        cases.append(
            make_case(
                campaign_dir,
                phase="cases",
                category="TARGET_SMOKE",
                variable_key=key,
                variable_value=value,
                dependencies=build_dependency_controls(key, value),
                topology="default",
                target=target,
                case_suffix=suffix,
            )
        )
    return cases, blocked


def build_context_baselines(
    campaign_dir: Path,
    cases: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    unique: dict[str, list[tuple[str, Any]]] = {}
    topology_by_context: dict[str, str | None] = {}
    for case in cases:
        dependencies = [
            (item["key"], item["value"])
            for item in case.get("dependency_controls", [])
        ]
        context_id = case["context_id"]
        unique.setdefault(context_id, dependencies)
        topology_by_context.setdefault(context_id, case.get("topology"))
    baselines: list[dict[str, Any]] = []
    for context_id, dependencies in sorted(unique.items()):
        baselines.append(
            make_case(
                campaign_dir,
                phase="context_baseline",
                category="context",
                variable_key=None,
                variable_value=None,
                dependencies=dependencies,
                topology=topology_by_context[context_id],
                case_suffix=context_id,
                subroot="context_baselines",
            )
        )
    return baselines


def _mesh_command(
    case: dict[str, Any],
    *,
    timeout_seconds: int,
    igg_executable: str | None,
) -> list[str]:
    command = [
        sys.executable,
        "-B",
        str(MESH_PY),
        str(GEOMETRY_PATH),
        "--out",
        case["out_dir"],
        "--timeout",
        str(timeout_seconds),
        "--mesh-fingerprint",
    ]
    if igg_executable:
        command.extend(["--igg", igg_executable])
    for assignment in case.get("set_args", []):
        command.extend(["--set", assignment])
    return command


def _extract_result_facts(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not summary:
        return {
            "generation_success": False,
            "structurally_valid": False,
            "quality_pass": False,
            "fingerprint": None,
            "negative_cells": None,
            "overlapping_status": None,
            "quality_status": None,
            "number_of_points": None,
        }
    autogrid = summary.get("autogrid", {}) or {}
    outputs = set((autogrid.get("outputs") or {}).keys())
    fingerprint = summary.get("mesh_fingerprint") or autogrid.get("mesh_fingerprint")
    quality = summary.get("quality") or {}
    metrics = quality.get("metrics", {}) or {}
    metadata = quality.get("metadata", {}) or {}
    quality_result = quality.get("result", {}) or {}
    negative_cells = metrics.get("negative_cells")
    overlap = metadata.get("overlapping_status")
    validity = str(metadata.get("mesh_validity") or "").upper()
    generation_success = (
        autogrid.get("returncode") == 0
        and REQUIRED_OUTPUTS.issubset(outputs)
        and isinstance(fingerprint, dict)
        and bool(fingerprint.get("comparison_sha256"))
    )
    validity_ok = (
        validity in {"OK", "VALID"}
        or ("VALID" in validity and "INVALID" not in validity)
    )
    structurally_valid = (
        generation_success
        and negative_cells == 0
        and overlap == "NO_OVERLAP"
        and validity_ok
    )
    return {
        "generation_success": generation_success,
        "structurally_valid": structurally_valid,
        "quality_pass": quality_result.get("status") == "PASS",
        "fingerprint": (fingerprint or {}).get("comparison_sha256"),
        "coordinate_fingerprint": (fingerprint or {}).get("aggregate_sha256"),
        "negative_cells": negative_cells,
        "overlapping_status": overlap,
        "mesh_validity": metadata.get("mesh_validity"),
        "quality_status": quality_result.get("status"),
        "number_of_points": metrics.get("number_of_points"),
        "grid_levels": metrics.get("grid_levels"),
    }


def execute_case(
    case: dict[str, Any],
    *,
    timeout_seconds: int,
    igg_executable: str | None,
) -> dict[str, Any]:
    started = time.time()
    out_dir = Path(case["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    command = _mesh_command(
        case,
        timeout_seconds=timeout_seconds,
        igg_executable=igg_executable,
    )
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=False,
            check=False,
            timeout=timeout_seconds + 120,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        returncode = completed.returncode
        outer_error = None
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
        returncode = 1
        outer_error = f"mesh.py 外层超时（{timeout_seconds + 120} 秒）"
    except OSError as exc:
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}"
        returncode = 1
        outer_error = stderr
    (out_dir / "runner_stdout.log").write_text(stdout, encoding="utf-8")
    (out_dir / "runner_stderr.log").write_text(stderr, encoding="utf-8")
    summary = _load_json(out_dir / "run_summary.json")
    facts = _extract_result_facts(summary)
    error = outer_error
    if error is None and summary:
        error = (summary.get("autogrid") or {}).get("error")
    if error is None and returncode != 0:
        error = stderr.strip().splitlines()[-1] if stderr.strip() else f"返回码 {returncode}"
    result = {
        **case,
        "command": command,
        "returncode": returncode,
        "status": "success" if returncode == 0 and facts["generation_success"] else "failed",
        "error": error,
        "duration_seconds": round(time.time() - started, 3),
        "run_summary": summary,
        **facts,
    }
    _write_json(out_dir / "campaign_case_result.json", result)
    return result


def _is_infrastructure_failure(result: dict[str, Any]) -> bool:
    if result.get("status") != "failed":
        return False
    text = str(result.get("error") or "").lower()
    markers = (
        "license",
        "许可证",
        "timeout",
        "超时",
        "cannot start",
        "无法启动",
        "access is denied",
        "no mesh outputs",
    )
    return any(marker in text for marker in markers)


def execute_batch(
    cases: list[dict[str, Any]],
    *,
    results_file: Path,
    max_workers: int,
    timeout_seconds: int,
    igg_executable: str | None,
    resume: bool,
    dry_run: bool,
) -> list[dict[str, Any]]:
    if dry_run:
        for case in cases:
            print(subprocess.list2cmdline(_mesh_command(
                case,
                timeout_seconds=timeout_seconds,
                igg_executable=igg_executable,
            )))
        return []

    previous = _load_json(results_file, [])
    if not isinstance(previous, list):
        previous = []
    reusable = {
        item.get("case_id"): item
        for item in previous
        if (
            resume
            and item.get("status") == "success"
            and item.get("signature")
            and item.get("signature") == next(
                (
                    case.get("signature")
                    for case in cases
                    if case.get("case_id") == item.get("case_id")
                ),
                None,
            )
        )
    }
    results_by_id: dict[str, dict[str, Any]] = dict(reusable)
    pending = [case for case in cases if case["case_id"] not in reusable]
    print(
        f"执行 {len(cases)} 例：复用 {len(reusable)}，待运行 {len(pending)}，"
        f"并发 {max_workers}"
    )
    completed_count = 0
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    execute_case,
                    case,
                    timeout_seconds=timeout_seconds,
                    igg_executable=igg_executable,
                ): case
                for case in pending
            }
            for future in concurrent.futures.as_completed(futures):
                case = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        **case,
                        "status": "failed",
                        "returncode": 1,
                        "error": f"{type(exc).__name__}: {exc}",
                        "run_summary": None,
                        **_extract_result_facts(None),
                    }
                results_by_id[case["case_id"]] = result
                completed_count += 1
                ordered = [results_by_id[c["case_id"]] for c in cases if c["case_id"] in results_by_id]
                _write_json(results_file, ordered)
                if (
                    completed_count <= 5
                    or completed_count % 10 == 0
                    or completed_count == len(pending)
                ):
                    successes = sum(item.get("status") == "success" for item in results_by_id.values())
                    print(
                        f"[{completed_count}/{len(pending)}] {case['case_id']} -> "
                        f"{result.get('status')}；累计成功 {successes}"
                    )

    # 只对许可证、超时、启动失败等基础设施问题进行一次串行重试。
    retry_cases = [
        case
        for case in cases
        if _is_infrastructure_failure(results_by_id.get(case["case_id"], {}))
    ]
    for case in retry_cases:
        print(f"串行重试基础设施失败：{case['case_id']}")
        retry_result = execute_case(
            case,
            timeout_seconds=timeout_seconds,
            igg_executable=igg_executable,
        )
        retry_result["retried_serially"] = True
        results_by_id[case["case_id"]] = retry_result
        _write_json(
            results_file,
            [results_by_id[c["case_id"]] for c in cases if c["case_id"] in results_by_id],
        )
    return [results_by_id[c["case_id"]] for c in cases if c["case_id"] in results_by_id]


def validate_pilot(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_category[result["category"]].append(result)
    failures: list[str] = []
    for result in results:
        if result.get("status") != "success":
            failures.append(f"{result['case_id']} 未成功：{result.get('error')}")

    aa_hashes = {item.get("fingerprint") for item in by_category["default_aa"]}
    if len(aa_hashes) != 1 or None in aa_hashes:
        failures.append("Default A/A 完整网格指纹不稳定")
    effect_hashes = {item.get("fingerprint") for item in by_category["known_effect"]}
    if len(effect_hashes) < 2:
        failures.append("已知有效 spanwise_paths 未产生不同网格指纹")
    topology_hashes = {item.get("fingerprint") for item in by_category["topology_switch"]}
    if len(topology_hashes) < 2:
        failures.append("Default 与 H&I 拓扑未产生不同网格指纹")
    negative_hashes = {item.get("fingerprint") for item in by_category["negative_control"]}
    if len(negative_hashes) != 1 or None in negative_hashes:
        failures.append("low_memory_usage 负对照改变了网格指纹或指纹缺失")

    conclusion = {
        "accepted": not failures,
        "failures": failures,
        "default_aa_fingerprints": sorted(str(item) for item in aa_hashes),
        "known_effect_fingerprints": sorted(str(item) for item in effect_hashes),
        "topology_fingerprints": sorted(str(item) for item in topology_hashes),
        "negative_control_fingerprints": sorted(str(item) for item in negative_hashes),
    }
    if failures:
        raise RuntimeError("pilot 门控失败：" + "；".join(failures))
    return conclusion


def _quality_score(result: dict[str, Any]) -> tuple[int, int, int, float]:
    summary = result.get("run_summary") or {}
    metrics = ((summary.get("quality") or {}).get("metrics") or {})
    negative = result.get("negative_cells")
    min_skewness = metrics.get("min_skewness_angle")
    return (
        0 if result.get("structurally_valid") else 1,
        int(negative) if isinstance(negative, (int, float)) else 10**12,
        0 if result.get("quality_pass") else 1,
        -float(min_skewness) if isinstance(min_skewness, (int, float)) else 0.0,
    )


def determine_hoh_anchor(
    campaign_dir: Path,
    baseline_results: list[dict[str, Any]],
    *,
    max_workers: int,
    timeout_seconds: int,
    igg_executable: str | None,
    resume: bool,
    dry_run: bool,
) -> tuple[list[tuple[str, Any]] | None, dict[str, Any]]:
    hoh_baselines = [
        result for result in baseline_results if result.get("topology") == "hoh"
    ]
    valid_baselines = [
        result for result in hoh_baselines if result.get("structurally_valid")
    ]
    if valid_baselines:
        gate = {
            "status": "valid_default_anchor",
            "anchor_controls": [],
            "baseline_case": min(valid_baselines, key=_quality_score)["case_id"],
        }
        _write_json(campaign_dir / "topology_rescue" / "topology_gate.json", gate)
        return [], gate
    if dry_run:
        return [], {"status": "dry_run"}

    single_cases: list[dict[str, Any]] = []
    for key in HOH_RESCUE_KEYS:
        for value in generate_test_values(CONTROL_REGISTRY[key])[:2]:
            dependencies = force_topology_dependency(
                build_dependency_controls(key, value),
                "hoh",
            )
            single_cases.append(
                make_case(
                    campaign_dir,
                    phase="topology_rescue",
                    category="single",
                    variable_key=key,
                    variable_value=value,
                    dependencies=dependencies,
                    topology="hoh",
                    subroot="topology_rescue",
                )
            )
    single_results = execute_batch(
        single_cases,
        results_file=campaign_dir / "topology_rescue" / "single_results.json",
        max_workers=max_workers,
        timeout_seconds=timeout_seconds,
        igg_executable=igg_executable,
        resume=resume,
        dry_run=False,
    )
    valid_singles = [
        result for result in single_results if result.get("structurally_valid")
    ]
    all_results = list(single_results)
    if not valid_singles:
        ranked = sorted(
            [result for result in single_results if result.get("status") == "success"],
            key=_quality_score,
        )
        distinct_keys: list[str] = []
        value_options: dict[str, list[Any]] = defaultdict(list)
        for result in ranked:
            key = result.get("variable_key")
            if key and key not in distinct_keys:
                distinct_keys.append(key)
            if key and result.get("variable_value") not in value_options[key]:
                value_options[key].append(result.get("variable_value"))
        distinct_keys = distinct_keys[:2]
        combination_cases: list[dict[str, Any]] = []
        if len(distinct_keys) == 2:
            first_key, second_key = distinct_keys
            for first_value in value_options[first_key][:2]:
                for second_value in value_options[second_key][:2]:
                    dependencies = [(TOPOLOGY_SELECTOR_KEY, "hoh")]
                    dependencies.extend(
                        [(first_key, first_value), (second_key, second_value)]
                    )
                    suffix = (
                        f"{_safe_name(first_key)}_{_safe_name(first_value)}__"
                        f"{_safe_name(second_key)}_{_safe_name(second_value)}"
                    )
                    combination_cases.append(
                        make_case(
                            campaign_dir,
                            phase="topology_rescue",
                            category="combination",
                            variable_key=None,
                            variable_value=None,
                            dependencies=dependencies,
                            topology="hoh",
                            case_suffix=suffix,
                            subroot="topology_rescue",
                        )
                    )
        if combination_cases:
            combination_results = execute_batch(
                combination_cases,
                results_file=campaign_dir / "topology_rescue" / "combination_results.json",
                max_workers=min(max_workers, 4),
                timeout_seconds=timeout_seconds,
                igg_executable=igg_executable,
                resume=resume,
                dry_run=False,
            )
            all_results.extend(combination_results)

    valid = [result for result in all_results if result.get("structurally_valid")]
    if not valid:
        gate = {
            "status": "blocked",
            "anchor_controls": None,
            "reason": "通用参数有限修复后仍无结构有效 HOH 网格",
            "tested_cases": len(all_results),
            "best_case": min(all_results, key=_quality_score)["case_id"] if all_results else None,
        }
        _write_json(campaign_dir / "topology_rescue" / "topology_gate.json", gate)
        return None, gate

    best = min(valid, key=_quality_score)
    anchor = [
        (item["key"], item["value"])
        for item in best.get("dependency_controls", [])
        if item["key"] != TOPOLOGY_SELECTOR_KEY
    ]
    if best.get("variable_key"):
        anchor.append((best["variable_key"], best["variable_value"]))
    deduplicated = list(dict(anchor).items())
    gate = {
        "status": "repaired_anchor",
        "anchor_controls": [
            {"key": key, "value": value} for key, value in deduplicated
        ],
        "anchor_case": best["case_id"],
        "tested_cases": len(all_results),
    }
    _write_json(campaign_dir / "topology_rescue" / "topology_gate.json", gate)
    return deduplicated, gate


def build_adaptive_cases(
    campaign_dir: Path,
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对两值均成功但指纹相同的数值控制追加一个更宽安全值。"""

    cases_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    results_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        if case.get("variable_key") in GENERAL_CONTROL_KEYS:
            cases_by_key[case["variable_key"]].append(case)
    for result in results:
        if result.get("variable_key") in GENERAL_CONTROL_KEYS:
            results_by_key[result["variable_key"]].append(result)

    adaptive: list[dict[str, Any]] = []
    for key, key_results in sorted(results_by_key.items()):
        spec = CONTROL_REGISTRY[key]
        successful = [item for item in key_results if item.get("status") == "success"]
        fingerprints = {item.get("fingerprint") for item in successful}
        if (
            len(successful) < 2
            or len(fingerprints) != 1
            or None in fingerprints
            or spec.value_type in {"bool", "enum"}
        ):
            continue
        existing_values = [item.get("variable_value") for item in successful]
        if spec.value_type == "int":
            largest = max(int(value) for value in existing_values)
            candidate: Any = max(largest + 4, largest * 2)
            if "points" in key or "index" in key:
                candidate = 4 * ((int(candidate) - 1 + 3) // 4) + 1
            if spec.maximum is not None:
                candidate = min(int(spec.maximum), int(candidate))
        elif spec.value_type == "float":
            largest = max(float(value) for value in existing_values)
            candidate = largest * 2.0 if largest else 1.0
            if spec.maximum is not None:
                candidate = min(float(spec.maximum), candidate)
        else:
            first = list(existing_values[-1])
            first[-1] = first[-1] * 1.5 if first[-1] else 1.0
            candidate = tuple(first)
        if candidate in existing_values:
            continue
        template = cases_by_key[key][0]
        dependencies = [
            (item["key"], item["value"])
            for item in template.get("dependency_controls", [])
        ]
        adaptive.append(
            make_case(
                campaign_dir,
                phase="adaptive",
                category=_control_group(key),
                variable_key=key,
                variable_value=candidate,
                dependencies=dependencies,
                topology=template.get("topology"),
                case_suffix=f"adaptive_{_format_value(candidate)}",
                subroot="cases",
            )
        )
    return adaptive


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def check_disk_capacity(
    campaign_dir: Path,
    pilot_results: list[dict[str, Any]],
    estimated_case_count: int,
) -> dict[str, Any]:
    successful_dirs = [
        Path(item["out_dir"])
        for item in pilot_results
        if item.get("status") == "success" and item.get("out_dir")
    ]
    sizes = [_directory_size(path) for path in successful_dirs]
    average = int(sum(sizes) / len(sizes)) if sizes else 0
    estimated = int(average * estimated_case_count * 1.2)
    free = shutil.disk_usage(campaign_dir.parent).free
    result = {
        "pilot_average_bytes": average,
        "estimated_case_count": estimated_case_count,
        "estimated_total_bytes_with_margin": estimated,
        "free_bytes": free,
        "sufficient": estimated == 0 or estimated < free * 0.9,
    }
    _write_json(campaign_dir / "disk_estimate.json", result)
    if not result["sufficient"]:
        raise RuntimeError(
            f"预计需要 {estimated / 1024**3:.1f} GiB，但仅剩 {free / 1024**3:.1f} GiB"
        )
    return result


def _run_analysis(campaign_dir: Path) -> int:
    analyzer = PROJECT_ROOT / "tests" / "test_analyze_results.py"
    completed = subprocess.run(
        [sys.executable, "-B", str(analyzer), str(campaign_dir)],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return completed.returncode


def _phase_alias(value: str) -> str:
    aliases = {
        "0": "all",
        "1": "baseline",
        "2": "cases",
        "3": "analyze",
    }
    return aliases.get(value, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        default="all",
        choices=(
            "audit",
            "pilot",
            "baseline",
            "cases",
            "analyze",
            "all",
            "0",
            "1",
            "2",
            "3",
        ),
    )
    parser.add_argument("--campaign-dir", type=Path)
    parser.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--igg")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--list-matrix",
        action="store_true",
        help="只打印案例与命令，不创建目录、不执行。",
    )
    args = parser.parse_args(argv)
    phase = _phase_alias(args.phase)
    if args.workers < 1 or args.workers > 32:
        parser.error("--workers 必须位于 1..32")
    if not GEOMETRY_PATH.exists():
        parser.error(f"找不到 Rotor37 几何：{GEOMETRY_PATH}")

    if args.list_matrix:
        virtual_root = Path("<campaign>")
        main_cases, _ = build_control_cases(virtual_root, hoh_anchor=[])
        context_cases = build_context_baselines(virtual_root, main_cases)
        all_cases = (
            build_pilot_cases(virtual_root)
            + build_baseline_cases(virtual_root)
            + context_cases
            + main_cases
        )
        counts = Counter(case["phase"] for case in all_cases)
        print(
            json.dumps(
                {
                    "registered": len(CONTROL_REGISTRY),
                    "general": len(GENERAL_CONTROL_KEYS),
                    "cases": len(all_cases),
                    "by_phase": dict(counts),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        for case in all_cases:
            print(case["case_id"])
            print(
                "  "
                + subprocess.list2cmdline(
                    _mesh_command(
                        case,
                        timeout_seconds=args.timeout,
                        igg_executable=args.igg,
                    )
                )
            )
        return 0

    campaign_dir = (
        args.campaign_dir.resolve()
        if args.campaign_dir
        else (RUNS_ROOT / _timestamp()).resolve()
    )
    campaign_dir.mkdir(parents=True, exist_ok=True)
    state_file = campaign_dir / "campaign_state.json"
    state = _load_json(
        state_file,
        {
            "schema_version": 2,
            "campaign_dir": str(campaign_dir),
            "geometry": str(GEOMETRY_PATH),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "completed_phases": [],
        },
    )
    resume = not args.no_resume

    def should_run(name: str) -> bool:
        return phase in {"all", name}

    if should_run("audit"):
        print("Phase audit：输出控制/API 清单")
        state["inventory"] = write_inventory(campaign_dir)
        if "audit" not in state["completed_phases"]:
            state["completed_phases"].append("audit")
        _write_json(state_file, state)

    pilot_results: list[dict[str, Any]] = _load_json(
        campaign_dir / "pilot_results.json", []
    )
    if should_run("pilot"):
        print("Phase pilot：验证指纹、已知效果和负对照")
        pilot_cases = build_pilot_cases(campaign_dir)
        pilot_results = execute_batch(
            pilot_cases,
            results_file=campaign_dir / "pilot_results.json",
            max_workers=min(args.workers, len(pilot_cases)),
            timeout_seconds=args.timeout,
            igg_executable=args.igg,
            resume=resume,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            state["pilot_gate"] = validate_pilot(pilot_results)
            if "pilot" not in state["completed_phases"]:
                state["completed_phases"].append("pilot")
            _write_json(state_file, state)

    baseline_results: list[dict[str, Any]] = _load_json(
        campaign_dir / "baseline_results.json", []
    )
    if should_run("baseline"):
        print("Phase baseline：三拓扑各 4 次 A/A")
        baseline_cases = build_baseline_cases(campaign_dir)
        baseline_results = execute_batch(
            baseline_cases,
            results_file=campaign_dir / "baseline_results.json",
            max_workers=min(args.workers, len(baseline_cases)),
            timeout_seconds=args.timeout,
            igg_executable=args.igg,
            resume=resume,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            state["baseline_count"] = len(baseline_results)
            if "baseline" not in state["completed_phases"]:
                state["completed_phases"].append("baseline")
            _write_json(state_file, state)

    if should_run("cases"):
        if not baseline_results and not args.dry_run:
            raise RuntimeError("执行 cases 前必须先完成 baseline")
        print("Phase cases：拓扑门控、上下文基线和 156 项参数")
        hoh_anchor, topology_gate = determine_hoh_anchor(
            campaign_dir,
            baseline_results,
            max_workers=args.workers,
            timeout_seconds=args.timeout,
            igg_executable=args.igg,
            resume=resume,
            dry_run=args.dry_run,
        )
        control_cases, blocked = build_control_cases(
            campaign_dir,
            hoh_anchor=hoh_anchor,
        )
        context_cases = build_context_baselines(campaign_dir, control_cases)
        if pilot_results:
            check_disk_capacity(
                campaign_dir,
                pilot_results,
                len(context_cases) + len(control_cases),
            )
        context_results = execute_batch(
            context_cases,
            results_file=campaign_dir / "context_baseline_results.json",
            max_workers=args.workers,
            timeout_seconds=args.timeout,
            igg_executable=args.igg,
            resume=resume,
            dry_run=args.dry_run,
        )
        case_results = execute_batch(
            control_cases,
            results_file=campaign_dir / "case_results.json",
            max_workers=args.workers,
            timeout_seconds=args.timeout,
            igg_executable=args.igg,
            resume=resume,
            dry_run=args.dry_run,
        )
        adaptive_cases = (
            []
            if args.dry_run
            else build_adaptive_cases(campaign_dir, control_cases, case_results)
        )
        adaptive_results = execute_batch(
            adaptive_cases,
            results_file=campaign_dir / "adaptive_results.json",
            max_workers=args.workers,
            timeout_seconds=args.timeout,
            igg_executable=args.igg,
            resume=resume,
            dry_run=args.dry_run,
        ) if adaptive_cases else []
        all_case_results = case_results + adaptive_results + blocked
        _write_json(campaign_dir / "all_case_results.json", all_case_results)
        state.update(
            {
                "topology_gate": topology_gate,
                "context_baseline_count": len(context_results),
                "case_count": len(all_case_results),
                "adaptive_case_count": len(adaptive_results),
            }
        )
        if not args.dry_run and "cases" not in state["completed_phases"]:
            state["completed_phases"].append("cases")
        _write_json(state_file, state)

    if should_run("analyze"):
        print("Phase analyze：生成数据驱动汇总")
        returncode = _run_analysis(campaign_dir)
        if returncode != 0:
            return returncode
        if "analyze" not in state["completed_phases"]:
            state["completed_phases"].append("analyze")
        _write_json(state_file, state)

    print(f"活动目录：{campaign_dir}")
    return 0


class CampaignRunnerUnitTests(unittest.TestCase):
    def test_full_matrix_covers_156_controls_with_comparable_variants(self) -> None:
        cases, blocked = build_control_cases(Path("<unit>"), hoh_anchor=[])
        self.assertFalse(blocked)
        by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for case in cases:
            if case.get("variable_key") in GENERAL_CONTROL_KEYS:
                by_key[case["variable_key"]].append(case)
        self.assertEqual(set(by_key), set(GENERAL_CONTROL_KEYS))
        for key, key_cases in by_key.items():
            contexts: dict[str, set[str]] = defaultdict(set)
            for case in key_cases:
                contexts[case["context_id"]].add(
                    json.dumps(case["variable_value"], sort_keys=True)
                )
            self.assertGreaterEqual(
                max(len(values) for values in contexts.values()),
                2,
                key,
            )

    def test_dependency_order_and_command_are_explicit(self) -> None:
        dependencies = build_dependency_controls(
            "stagnation-point/distribution_absolute_distance",
            1.0e-4,
        )
        keys = [key for key, _ in dependencies]
        self.assertLess(
            keys.index("stagnation-point/distribution_from_expansion_ratio"),
            keys.index("stagnation-point/distribution_type"),
        )
        case = make_case(
            Path("<unit>"),
            phase="cases",
            category="GENERAL_DEFAULT",
            variable_key="blade/b2b.default.streamwise_inlet_points",
            variable_value=17,
            dependencies=build_dependency_controls(
                "blade/b2b.default.streamwise_inlet_points", 17
            ),
            topology="default",
        )
        command = _mesh_command(case, timeout_seconds=1800, igg_executable=None)
        self.assertIn("--mesh-fingerprint", command)
        self.assertEqual(command.count("--set"), len(case["set_args"]))
        self.assertIn(
            "row:#1/blade:#1/b2b.default.streamwise_inlet_points=17",
            command,
        )

    def test_hoh_rescue_overrides_core_controls_default_topology(self) -> None:
        dependencies = force_topology_dependency(
            build_dependency_controls("row/flow_path.number", 65),
            "hoh",
        )
        self.assertIn((TOPOLOGY_SELECTOR_KEY, "hoh"), dependencies)
        self.assertNotIn((TOPOLOGY_SELECTOR_KEY, "default"), dependencies)

    def test_resume_reuses_matching_successful_case(self) -> None:
        case = {
            "case_id": "cases/unit",
            "signature": "same",
            "out_dir": "unused",
        }
        previous = [{**case, "status": "success", "generation_success": True}]
        with tempfile.TemporaryDirectory() as tmp:
            result_file = Path(tmp) / "results.json"
            _write_json(result_file, previous)
            with patch(__name__ + ".execute_case") as execute_mock:
                results = execute_batch(
                    [case],
                    results_file=result_file,
                    max_workers=1,
                    timeout_seconds=1,
                    igg_executable=None,
                    resume=True,
                    dry_run=False,
                )
        execute_mock.assert_not_called()
        self.assertEqual(results, previous)

    def test_pilot_rejects_unstable_aa_fingerprint(self) -> None:
        results = []
        for category, fingerprints in (
            ("default_aa", ("A", "B")),
            ("known_effect", ("C", "D")),
            ("topology_switch", ("E", "F")),
            ("negative_control", ("G", "G")),
        ):
            for index, fingerprint in enumerate(fingerprints):
                results.append(
                    {
                        "case_id": f"{category}/{index}",
                        "category": category,
                        "status": "success",
                        "fingerprint": fingerprint,
                    }
                )
        with self.assertRaisesRegex(RuntimeError, "A/A"):
            validate_pilot(results)


if __name__ == "__main__":
    raise SystemExit(main())
