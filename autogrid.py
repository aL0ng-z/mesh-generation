"""生成并执行 NUMECA AutoGrid 初始化脚本，收集网格运行产物。"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from controls import CONTROL_REGISTRY, MAPPED_SETTERS, _ALL_KNOWN_SETTERS, ResolvedControl


CONTROL_RESULT_MARKER = "AGMESH_CONTROL_RESULT:"


@dataclass(frozen=True)
class AutoGridRun:
    """记录一次 AutoGrid 执行的命令、产物与控制应用结果。"""

    command: list[str]
    returncode: int | None
    run_dir: str
    script: str
    outputs: dict[str, str]
    control_results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """将运行记录转换为可序列化字典。"""

        return asdict(self)


def run_autogrid_init(
    geomturbo_path: str | Path,
    run_dir: str | Path,
    *,
    igg_executable: str = "igg",
    output_prefix: str = "mesh",
    use_row_wizard: bool = True,
    dry_run: bool = False,
    timeout_seconds: int | None = None,
    controls: Sequence[ResolvedControl] = (),
) -> AutoGridRun:
    """生成 AutoGrid 脚本并按配置执行或仅进行 dry-run。"""

    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)

    geom_source = Path(geomturbo_path).resolve()
    geom_copy = run_path / "input.geomTurbo"
    if geom_source != geom_copy.resolve():
        shutil.copy2(geom_source, geom_copy)

    script_path = run_path / "autogrid_init.py"
    script_path.write_text(
        render_autogrid_script(
            geomturbo_path=geom_copy.resolve(),
            output_prefix=output_prefix,
            use_row_wizard=use_row_wizard,
            controls=controls,
        ),
        encoding="utf-8",
    )

    igg_resolved = resolve_igg(igg_executable)
    command = [
        igg_resolved or igg_executable,
        "-autogrid5",
        "-batch",
        "-script",
        str(script_path.resolve()),
    ]
    if dry_run:
        return AutoGridRun(
            command=command,
            returncode=None,
            run_dir=str(run_path),
            script=str(script_path),
            outputs={},
            control_results=[control.to_dict() for control in controls],
            error=None,
        )

    try:
        completed = subprocess.run(
            command,
            cwd=run_path.resolve(),
            text=False,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        stdout = _completed_text(completed.stdout)
        stderr = _completed_text(completed.stderr)
        process_returncode = completed.returncode
        runner_error = None
    except subprocess.TimeoutExpired as exc:
        stdout = _completed_text(exc.stdout)
        stderr = _completed_text(exc.stderr)
        process_returncode = 1
        runner_error = f"AutoGrid 超时（{timeout_seconds} 秒）"
        stderr = (stderr + "\n" + runner_error).strip()
    except OSError as exc:
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}"
        process_returncode = 1
        runner_error = f"无法启动 IGG：{exc}"
    (run_path / "stdout.log").write_text(stdout, encoding="utf-8")
    (run_path / "stderr.log").write_text(stderr, encoding="utf-8")
    outputs = collect_outputs(run_path, output_prefix)
    parsed_events = parse_control_results(stdout)
    control_results = merge_control_results(controls, parsed_events)
    failed_control = next((event for event in parsed_events if event.get("status") == "failed"), None)
    script_error = _script_error(stderr)
    effective_returncode = process_returncode
    if effective_returncode == 0 and (failed_control is not None or script_error is not None):
        effective_returncode = 1
    if runner_error is None and failed_control is not None:
        runner_error = failed_control.get("error") or "AutoGrid 控制应用失败"
    if runner_error is None and script_error is not None:
        runner_error = script_error
    return AutoGridRun(
        command=command,
        returncode=effective_returncode,
        run_dir=str(run_path),
        script=str(script_path),
        outputs={key: str(value) for key, value in outputs.items()},
        control_results=control_results,
        error=runner_error,
    )


def render_autogrid_script(
    *,
    geomturbo_path: str | Path,
    output_prefix: str = "mesh",
    use_row_wizard: bool = True,
    controls: Sequence[ResolvedControl | dict[str, Any]] = (),
) -> str:
    """渲染可由 IGG 执行的 AutoGrid Python 脚本文本。"""

    control_plan = _serialize_control_plan(controls)
    control_plan_json = json.dumps(control_plan, ensure_ascii=True, sort_keys=True)
    return f'''# -*- coding: utf-8 -*-
# 由 mesh.py 自动生成，目标版本仅限 NUMECA AutoGrid 17.1。
# 仅从 geomTurbo 初始化；不读取模板，不修改 .trb 文本。

import os
import json

GEOMTURBO_FILE = r"{Path(geomturbo_path)}"
OUTPUT_PREFIX = r"{output_prefix}"
USE_ROW_WIZARD = {bool(use_row_wizard)!r}
CONTROL_RESULT_MARKER = {CONTROL_RESULT_MARKER!r}
CONTROL_PLAN = json.loads({control_plan_json!r})


def _require_global(name):
    value = globals().get(name)
    if not callable(value):
        raise RuntimeError(u"缺少 AutoGrid 17.1 API：" + name)
    return value


def _entity_name(entity):
    getter = getattr(entity, "get_name", None)
    if not callable(getter):
        return None
    return getter()


def _ensure_str(value):
    """将 unicode API 值编码为 str（bytes），避免 Python 2 C 扩展报 Expecting string。"""
    try:
        unicode_type = unicode
    except NameError:
        return value
    if isinstance(value, unicode_type):
        return value.encode(\"utf-8\")
    if isinstance(value, tuple):
        return tuple(_ensure_str(v) for v in value)
    return value


def _safe_text(value):
    try:
        text_type = unicode
    except NameError:
        text_type = str
    if isinstance(value, text_type):
        return value
    if isinstance(value, str):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.decode("latin1", "replace")
    try:
        return text_type(value)
    except Exception:
        return text_type(repr(value))


def _require_entity(entity, label):
    if entity is None or entity == 0:
        raise RuntimeError(u"控制目标不存在：" + label)
    return entity


def _part(control, kind):
    for item in control.get("target", []):
        if item.get("kind") == kind:
            return item
    return None


def _row_target(control):
    item = _part(control, "row")
    if item is None:
        raise RuntimeError(u"控制目标缺少 row：" + control["key"])
    index = int(item["index"])
    if index < 1 or index > ROW_COUNT:
        raise RuntimeError(u"叶排索引超出范围：" + str(index))
    entity = _require_entity(_require_global("row")(index), "row:#" + str(index))
    actual_name = _entity_name(entity)
    if actual_name is not None and actual_name != item.get("name"):
        raise RuntimeError(u"叶排名称与静态解析不一致，索引 #" + str(index) + u"：" + str(actual_name))
    return entity


def _blade_target(control, row_entity):
    item = _part(control, "blade")
    if item is None:
        raise RuntimeError(u"控制目标缺少 blade：" + control["key"])
    entity = _require_entity(row_entity.blade(int(item["index"])), control.get("target_path", "blade"))
    actual_name = _entity_name(entity)
    if actual_name is not None and actual_name != item.get("name"):
        raise RuntimeError(u"叶片名称与静态解析不一致，索引 #" + str(item["index"]) + u"：" + str(actual_name))
    return entity


def _resolve_target(control):
    kind = control["target_kind"]
    if kind == "configuration":
        return None
    if kind == "existing-effect":
        item = _part(control, "existing-effect")
        index = int(item["index"])
        count = int(_require_global("a5_get_ZR_effect_number")())
        if index > count:
            raise RuntimeError(u"ZR 技术效果索引超出范围：" + str(index))
        return _require_entity(_require_global("technologicalEffectZR")(index), control.get("target_path", "existing-effect"))
    row_entity = _row_target(control)
    if kind == "row":
        return row_entity
    if kind == "wizard":
        return _require_entity(row_entity.row_wizard(), control.get("target_path", "wizard"))
    if kind == "acoustic-wizard":
        return _require_entity(row_entity.acoustic_wizard(), control.get("target_path", "acoustic-wizard"))
    if kind == "interface":
        name = _part(control, "interface")["name"]
        accessor = {{"inlet": "inlet", "outlet": "outlet", "outlet2": "outlet2"}}.get(name)
        if accessor is None:
            raise RuntimeError(u"不支持的接口选择器：" + str(name))
        method = getattr(row_entity, accessor, None)
        if not callable(method):
            raise RuntimeError(u"缺少 AutoGrid 17.1 接口 accessor：" + accessor)
        return _require_entity(method(), control.get("target_path", "interface"))
    if kind == "endwall":
        name = _part(control, "endwall")["name"]
        accessor = {{"hub": "hub_end_wall", "shroud": "shroud_end_wall"}}.get(name)
        method = getattr(row_entity, accessor, None)
        if not callable(method):
            raise RuntimeError(u"缺少 AutoGrid 17.1 端壁 accessor：" + str(accessor))
        return _require_entity(method(), control.get("target_path", "endwall"))
    if kind == "snubber":
        index = int(_part(control, "snubber")["index"])
        if index > int(row_entity.get_number_of_snubbers()):
            raise RuntimeError(u"Snubber 索引超出范围：" + str(index))
        return _require_entity(row_entity.snubber(index), control.get("target_path", "snubber"))
    if kind == "endwall-holes-line":
        endwall_name = _part(control, "endwall")["name"]
        endwall = row_entity.hub_end_wall() if endwall_name == "hub" else row_entity.shroud_end_wall()
        endwall = _require_entity(endwall, control.get("target_path", "endwall"))
        index = int(_part(control, kind)["index"])
        return _require_entity(endwall.holes_line(index), control.get("target_path", kind))
    blade_entity = _blade_target(control, row_entity)
    if kind == "blade":
        return blade_entity
    if kind in ("gap", "partial-gap", "fillet"):
        item = _part(control, kind)
        side = item["name"]
        accessors = {{
            "gap": {{"hub": "get_hub_gap", "shroud": "get_shroud_gap"}},
            "partial-gap": {{"hub": "get_hub_partial_gap", "shroud": "get_shroud_partial_gap"}},
            "fillet": {{"hub": "get_hub_fillet", "shroud": "get_shroud_fillet"}},
        }}
        accessor = accessors[kind].get(side)
        method = getattr(blade_entity, accessor, None)
        if not callable(method):
            raise RuntimeError(u"缺少 AutoGrid 17.1 accessor：" + str(accessor))
        return _require_entity(method(), control.get("target_path", kind))
    if kind == "blade-sheet":
        return _require_entity(blade_entity.sheet(), control.get("target_path", kind))
    if kind == "solid-body":
        _require_entity(blade_entity.solid_body(), control.get("target_path", kind))
        return blade_entity
    if kind == "lete-wizard":
        return _require_entity(blade_entity.wizard_le_te(), control.get("target_path", kind))
    if kind == "stagnation-point":
        name = _part(control, kind)["name"]
        accessor = "leadingEdgeControl" if name == "leading" else "trailingEdgeControl"
        return _require_entity(getattr(blade_entity, accessor)(), control.get("target_path", kind))
    if kind == "holes-line":
        index = int(_part(control, kind)["index"])
        if index > int(blade_entity.number_of_holes_lines()):
            raise RuntimeError(u"孔列索引超出范围：" + str(index))
        return _require_entity(blade_entity.holes_line(index), control.get("target_path", kind))
    if kind == "basin-hole":
        index = int(_part(control, kind)["index"])
        if index > int(blade_entity.number_of_basin_holes()):
            raise RuntimeError(u"Basin hole 索引超出范围：" + str(index))
        return _require_entity(blade_entity.basin_hole(index), control.get("target_path", kind))
    if kind == "pin-fins-line":
        index = int(_part(control, kind)["index"])
        channel = _require_entity(blade_entity.cooling_channel(), control.get("target_path", "cooling-channel"))
        pin_channel = _require_entity(channel.pinFinsChannel(1), control.get("target_path", "pin-fins-channel"))
        return _require_entity(pin_channel.pinFins_line(index), control.get("target_path", kind))
    raise RuntimeError(u"不支持的注册目标类型：" + str(kind))


def _check_topology(control, target):
    allowed = control.get("topologies") or []
    if not allowed:
        return
    getter = getattr(target, "get_b2b_topology_type", None)
    if not callable(getter):
        raise RuntimeError(u"控制缺少拓扑 getter：" + control["key"])
    raw = getter()
    topology = {{0: "default", 1: "hoh", 2: "user", 3: "hi"}}.get(raw, str(raw).lower())
    if topology not in allowed:
        raise RuntimeError(
            u"控制 " + control["key"] + u" 不适用于拓扑 " + str(topology)
        )


def _control_method(control, target):
    name = control["setter"]
    if control.get("setter_mode") == "interface_bool":
        return None
    method = _require_global(name) if target is None else getattr(target, name, None)
    if not callable(method):
        raise RuntimeError(u"缺少 AutoGrid 17.1 setter：" + name)
    return method


def _invoke_control(control, target):
    mode = control.get("setter_mode", "value")
    value = _ensure_str(control.get("api_value"))
    if mode == "interface_bool":
        if control["setter"] == "__bool_b2b_control__":
            name = "enable_b2b_control" if value else "disable_b2b_control"
        elif control["setter"] == "__bool_geometry_fixed__":
            name = "geometry_is_fixed" if value else "geometry_is_not_fixed"
        else:
            raise RuntimeError(u"未知接口布尔 setter：" + control["setter"])
        method = getattr(target, name, None)
        if not callable(method):
            raise RuntimeError(u"缺少 AutoGrid 17.1 setter：" + name)
        return method()
    if mode == "row_property_memory_use":
        # 绕过 AutoGrid 17.1 enable_low_memory_usage 的 int/string bug，
        # 直接调用全局 set_row_properties_ 并传入字符串值。
        func = _require_global("set_row_properties_")
        return func(target.impl, "memory_use", value)
    method = _control_method(control, target)
    if mode == "no_args":
        return method()
    if mode == "tuple_args":
        return method(*value)
    if mode == "row_accuracy_level":
        getter = getattr(target, "get_coarse_grid_level_target", None)
        current_target = getter() if callable(getter) else 250000
        return method(value, current_target)
    if mode == "row_accuracy_target":
        getter = getattr(target, "get_coarse_grid_level", None)
        current_level = getter() if callable(getter) else 4
        return method(current_level, value)
    return method(value)


def _readback(control, target):
    name = control.get("getter")
    if not name:
        return None
    getter = _require_global(name) if target is None else getattr(target, name, None)
    if not callable(getter):
        raise RuntimeError(u"缺少 AutoGrid 17.1 getter：" + name)
    return getter()


def _emit_control(control, status, readback=None, error=None):
    event = {{
        "id": control.get("id"),
        "key": control.get("key"),
        "target_path": control.get("target_path"),
        "requested": control.get("requested"),
        "project_value": control.get("project_value"),
        "status": status,
        "readback": readback,
        "error": error,
    }}
    print(CONTROL_RESULT_MARKER + json.dumps(event, sort_keys=True))


def _apply_control(control):
    try:
        target = _resolve_target(control)
        _check_topology(control, target)
        _invoke_control(control, target)
        readback_error = None
        try:
            readback = _readback(control, target)
        except Exception as getter_exc:
            readback = None
            readback_error = _safe_text(getter_exc.__class__.__name__) + u": " + _safe_text(getter_exc)
        _emit_control(control, "applied", readback, readback_error)
    except Exception as exc:
        message = _safe_text(exc.__class__.__name__) + u": " + _safe_text(exc)
        _emit_control(control, "failed", None, message)
        raise


def _apply_stage(stage):
    for control in CONTROL_PLAN:
        if control.get("stage") == stage:
            _apply_control(control)


def _generate_row_wizards():
    if not USE_ROW_WIZARD:
        return
    for index in range(1, ROW_COUNT + 1):
        row_entity = _require_entity(_require_global("row")(index), "row:#" + str(index))
        wizard = _require_entity(row_entity.row_wizard(), "row:#" + str(index) + "/wizard")
        generate = getattr(wizard, "generate", None)
        if not callable(generate):
            raise RuntimeError(u"缺少 AutoGrid 17.1 RowWizard.generate")
        generate()


_require_global("a5_new_project")(1)
_require_global("a5_init_new_project_from_a_geomTurbo_file")(GEOMTURBO_FILE)
ROW_COUNT = int(_require_global("a5_get_row_number")())
if ROW_COUNT < 1:
    raise RuntimeError(u"a5_get_row_number() 未返回有效叶排")

# 阶段顺序：configuration -> wizard setter -> RowWizard.generate。
_apply_stage("configuration")
_apply_stage("wizard")
_generate_row_wizards()

# wizard 完成后依次应用拓扑、分布、边界层、优化、接口和已有技术效果。
_apply_stage("topology")
_apply_stage("distribution")
_apply_stage("boundary_layer")
_apply_stage("optimization")
_apply_stage("interface")
_apply_stage("existing_effect")

_TRB_OUT = os.path.join(os.getcwd(), OUTPUT_PREFIX + ".trb")
_IGG_OUT = os.path.join(os.getcwd(), OUTPUT_PREFIX + ".igg")
_CGNS_OUT = os.path.join(os.getcwd(), OUTPUT_PREFIX + ".cgns")

# 预保存仅通过正式 API；.trb 永不做字符串修改。
print("Saving project before generation:", _TRB_OUT)
_require_global("a5_save_project")(_TRB_OUT)

_require_global("a5_generate_flow_paths")()
_require_global("a5_generate_b2b")()
_require_global("a5_generate_3d")()

_require_global("a5_save_project")(_TRB_OUT)
_require_global("a5_save_mesh")(_IGG_OUT)
_require_global("a5_export_CGNS_project")(_CGNS_OUT)
print("AutoGrid geomTurbo init script completed for", OUTPUT_PREFIX)
'''


def _serialize_control_plan(
    controls: Sequence[ResolvedControl | dict[str, Any]],
) -> list[dict[str, Any]]:
    """将解析后的控制项转换为脚本可消费的数据结构。"""

    serialized: list[dict[str, Any]] = []
    for control in controls:
        data = control.to_dict() if isinstance(control, ResolvedControl) else dict(control)
        key = data.get("key")
        spec = CONTROL_REGISTRY.get(str(key))
        if spec is None:
            raise ValueError(f"未知控制键：{key}")
        setter = data.get("setter")
        allowed_setters = {method for method in (spec.setter,) if method}
        allowed_setters.update(method for _, method in spec.setter_by_value)
        if setter not in allowed_setters or (
            setter not in MAPPED_SETTERS
            and setter not in _ALL_KNOWN_SETTERS
            and not str(setter).startswith("__bool_")
        ):
            raise ValueError(f"控制 {key} 使用了未注册 setter：{setter}")
        if spec.setter_by_value:
            data["setter_mode"] = "no_args"
        serialized.append(data)
    return serialized


def parse_control_results(stdout: str) -> list[dict[str, Any]]:
    """从 IGG 标准输出中解析网格控制应用结果。"""

    results: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        marker_index = line.find(CONTROL_RESULT_MARKER)
        if marker_index < 0:
            continue
        payload = line[marker_index + len(CONTROL_RESULT_MARKER) :].strip()
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("id"):
            results.append(value)
    return results


def merge_control_results(
    controls: Sequence[ResolvedControl],
    events: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将控制计划与实际执行结果合并为完整状态列表。"""

    event_by_id = {str(event.get("id")): dict(event) for event in events}
    merged: list[dict[str, Any]] = []
    for control in controls:
        data = control.to_dict()
        event = event_by_id.get(control.control_id)
        if event:
            data.update(
                {
                    "status": event.get("status", "failed"),
                    "readback": event.get("readback"),
                    "error": event.get("error"),
                }
            )
        else:
            data.update({"status": "not_applied", "readback": None, "error": "未收到 AutoGrid 控制结果标记"})
        merged.append(data)
    return merged


def _completed_text(value: str | bytes | None) -> str:
    """将子进程输出统一转换为文本。"""

    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-8", "gb18030"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", errors="replace")
    return value


def _script_error(stderr: str) -> str | None:
    """从 IGG 标准错误中提取有效的脚本错误信息。"""

    if not re.search(r"(?:Traceback \(most recent call last\)|SyntaxError:|RuntimeError:|ValueError:)", stderr):
        return None
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "AutoGrid Python 脚本执行失败"


def collect_outputs(run_dir: str | Path, output_prefix: str = "mesh") -> dict[str, Path]:
    """收集运行目录中已生成的 AutoGrid 网格及报告文件。"""

    run_path = Path(run_dir)
    suffixes = {
        "igg": ".igg",
        "cgns": ".cgns",
        "trb": ".trb",
        "bcs": ".bcs",
        "info": ".info",
        "geom": ".geom",
        "geomturbo": ".geomTurbo",
        "quality_report": ".qualityReport",
    }
    outputs: dict[str, Path] = {}
    for key, suffix in suffixes.items():
        candidate = run_path / f"{output_prefix}{suffix}"
        if candidate.exists() and candidate.stat().st_size > 0:
            outputs[key] = candidate
    return outputs


def resolve_igg(executable: str = "igg") -> str | None:
    """解析 IGG 可执行文件路径，必要时搜索常见安装目录。"""

    if os.path.sep in executable or (os.path.altsep and os.path.altsep in executable):
        path = Path(executable)
        return str(path) if path.exists() else None
    resolved = shutil.which(executable)
    if resolved:
        return resolved
    if executable.lower() not in {"igg", "igg.exe", "iggx86_64", "iggx86_64.exe"}:
        return None
    for root in _candidate_roots():
        candidate = root / "bin64" / "iggx86_64.exe"
        if candidate.exists():
            return str(candidate)
        candidate = root / "bin64" / "igg.exe"
        if candidate.exists():
            return str(candidate)
        candidate = root / "bin" / "igg"
        if candidate.exists():
            return str(candidate)
    return None


def _candidate_roots() -> list[Path]:
    """返回用于搜索 NUMECA 安装的候选根目录。"""

    env = os.environ
    roots: list[Path] = []
    for name in ("NUMECA_ROOT", "NUMECA_HOME", "FINE_ROOT", "FINE_HOME", "FINE171_ROOT"):
        if env.get(name):
            roots.append(Path(env[name]))
    if sys.platform.startswith("win"):
        program_data = Path(env.get("ProgramData", r"C:\ProgramData"))
        roots.extend([program_data / "NUMECA" / "fine171", program_data / "NUMECA"])
    else:
        roots.extend([Path("/opt/numeca"), Path("/usr/local/numeca"), Path("/opt/cadence")])

    expanded: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        candidates = [root]
        if root.exists():
            try:
                candidates.extend(
                    child
                    for child in root.iterdir()
                    if child.is_dir()
                    and any(token in child.name.lower() for token in ("fine", "numeca", "fidelity", "autogrid"))
                )
            except OSError:
                pass
        for candidate in candidates:
            key = str(candidate).lower()
            if key not in seen:
                expanded.append(candidate)
                seen.add(key)
    return expanded
