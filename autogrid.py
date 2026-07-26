"""生成并执行 NUMECA AutoGrid 初始化脚本，收集网格运行产物。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import json
import shutil
import struct
import subprocess
import sys
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from controls import CONTROL_REGISTRY, MAPPED_SETTERS, _ALL_KNOWN_SETTERS, ResolvedControl


CONTROL_RESULT_MARKER = "AGMESH_CONTROL_RESULT:"
CONTROL_POST_RESULT_MARKER = "AGMESH_CONTROL_POST_RESULT:"
MESH_FINGERPRINT_FILE = "mesh_fingerprint.json"


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
    post_control_results: list[dict[str, Any]] = field(default_factory=list)
    mesh_fingerprint: dict[str, Any] | None = None

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
    mesh_fingerprint: bool = False,
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
            mesh_fingerprint=mesh_fingerprint,
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
            post_control_results=[],
            mesh_fingerprint=None,
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
    parsed_post_events = parse_control_results(stdout, marker=CONTROL_POST_RESULT_MARKER)
    control_results = merge_control_results(controls, parsed_events)
    post_control_results = merge_post_control_results(controls, parsed_post_events)
    fingerprint_data = None
    if (
        mesh_fingerprint
        and process_returncode == 0
        and "cgns" in outputs
    ):
        try:
            fingerprint_data = fingerprint_cgns_coordinates(
                outputs["cgns"],
                hdf5_dll=Path(command[0]).resolve().parent / "hdf5dll.dll",
            )
            (run_path / MESH_FINGERPRINT_FILE).write_text(
                json.dumps(
                    fingerprint_data,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            runner_error = (
                f"完整 CGNS 坐标指纹生成失败：{type(exc).__name__}: {exc}"
            )
    failed_control = next((event for event in parsed_events if event.get("status") == "failed"), None)
    script_error = _script_error(stderr)
    effective_returncode = process_returncode
    if effective_returncode == 0 and (failed_control is not None or script_error is not None):
        effective_returncode = 1
    if effective_returncode == 0 and mesh_fingerprint and fingerprint_data is None:
        effective_returncode = 1
        if runner_error is None:
            runner_error = runner_error or "已请求网格指纹，但未生成 mesh_fingerprint.json"
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
        post_control_results=post_control_results,
        mesh_fingerprint=fingerprint_data,
    )


def render_autogrid_script(
    *,
    geomturbo_path: str | Path,
    output_prefix: str = "mesh",
    use_row_wizard: bool = True,
    controls: Sequence[ResolvedControl | dict[str, Any]] = (),
    mesh_fingerprint: bool = False,
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
CONTROL_POST_RESULT_MARKER = {CONTROL_POST_RESULT_MARKER!r}
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


def _emit_control(
    control,
    status,
    readback=None,
    error=None,
    readback_before=None,
    readback_before_error=None,
):
    event = {{
        "id": control.get("id"),
        "key": control.get("key"),
        "target_path": control.get("target_path"),
        "requested": control.get("requested"),
        "project_value": control.get("project_value"),
        "status": status,
        "readback": readback,
        "error": error,
        "readback_before": readback_before,
        "readback_before_error": readback_before_error,
    }}
    print(CONTROL_RESULT_MARKER + json.dumps(event, sort_keys=True))


def _apply_control(control):
    try:
        target = _resolve_target(control)
        _check_topology(control, target)
        readback_before = None
        readback_before_error = None
        if control.get("getter"):
            try:
                readback_before = _readback(control, target)
            except Exception as getter_exc:
                readback_before_error = (
                    _safe_text(getter_exc.__class__.__name__) + u": " + _safe_text(getter_exc)
                )
        _invoke_control(control, target)
        readback_error = None
        try:
            readback = _readback(control, target)
        except Exception as getter_exc:
            readback = None
            readback_error = _safe_text(getter_exc.__class__.__name__) + u": " + _safe_text(getter_exc)
        _emit_control(
            control,
            "applied",
            readback,
            readback_error,
            readback_before,
            readback_before_error,
        )
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


def _emit_post_generation_readbacks():
    for control in CONTROL_PLAN:
        event = {{
            "id": control.get("id"),
            "key": control.get("key"),
            "target_path": control.get("target_path"),
            "requested": control.get("requested"),
            "project_value": control.get("project_value"),
            "status": "no_getter",
            "readback": None,
            "error": None,
        }}
        try:
            if control.get("getter"):
                target = _resolve_target(control)
                event["readback"] = _readback(control, target)
                event["status"] = "readback"
        except Exception as exc:
            event["status"] = "failed"
            event["error"] = _safe_text(exc.__class__.__name__) + u": " + _safe_text(exc)
        print(CONTROL_POST_RESULT_MARKER + json.dumps(event, sort_keys=True))


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

_emit_post_generation_readbacks()

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


def parse_control_results(
    stdout: str,
    *,
    marker: str = CONTROL_RESULT_MARKER,
) -> list[dict[str, Any]]:
    """从 IGG 标准输出中解析网格控制应用结果。"""

    results: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        marker_index = line.find(marker)
        if marker_index < 0:
            continue
        payload = line[marker_index + len(marker) :].strip()
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("id"):
            results.append(value)
    return results


def merge_post_control_results(
    controls: Sequence[ResolvedControl],
    events: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将生成后的 getter 回读与原控制计划合并。"""

    event_by_id = {str(event.get("id")): dict(event) for event in events}
    merged: list[dict[str, Any]] = []
    for control in controls:
        event = event_by_id.get(control.control_id)
        if event is None:
            merged.append(
                {
                    "id": control.control_id,
                    "key": control.key,
                    "target_path": control.target_path,
                    "status": "not_observed",
                    "readback": None,
                    "error": "未收到生成后控制回读标记",
                }
            )
        else:
            merged.append(event)
    return merged


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
                    "readback_before": event.get("readback_before"),
                    "readback_before_error": event.get("readback_before_error"),
                    "readback": event.get("readback"),
                    "error": event.get("error"),
                }
            )
        else:
            data.update(
                {
                    "status": "not_applied",
                    "readback_before": None,
                    "readback_before_error": None,
                    "readback": None,
                    "error": "未收到 AutoGrid 控制结果标记",
                }
            )
        merged.append(data)
    return merged


def fingerprint_cgns_coordinates(
    cgns_path: str | Path,
    *,
    hdf5_dll: str | Path,
) -> dict[str, Any]:
    """用厂商 HDF5 DLL 元数据定位并哈希 CGNS 中全部 block 坐标。

    AutoGrid 17.1 的 CGNS 坐标数据集是未压缩、连续存储的 Float64 数组。
    本函数只使用标准库 ``ctypes`` 和 AutoGrid 随附的 ``hdf5dll.dll``；
    DLL 仅用于可靠取得数据集维度和文件偏移，坐标字节由 Python 流式读取。
    """

    mesh_path = Path(cgns_path).resolve()
    dll_path = Path(hdf5_dll).resolve()
    if not mesh_path.exists():
        raise OSError(f"CGNS 文件不存在：{mesh_path}")
    if not dll_path.exists():
        raise OSError(f"找不到 AutoGrid HDF5 DLL：{dll_path}")

    dll_directory = None
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if callable(add_dll_directory):
        dll_directory = add_dll_directory(str(dll_path.parent))
    try:
        library = ctypes.CDLL(str(dll_path))
        _configure_hdf5_api(library)
        library.H5open()
        # 禁止“尝试打开普通数据集为 group”时向 stderr 打印 HDF5 error stack。
        if hasattr(library, "H5Eset_auto2"):
            library.H5Eset_auto2(0, None, None)
        file_id = library.H5Fopen(os.fsencode(str(mesh_path)), 0, 0)
        if file_id < 0:
            raise RuntimeError(f"H5Fopen 失败：{mesh_path}")
        try:
            blocks = _discover_cgns_coordinate_blocks(library, file_id, mesh_path)
        finally:
            library.H5Fclose(file_id)
    finally:
        if dll_directory is not None:
            dll_directory.close()

    if not blocks:
        raise RuntimeError("CGNS 中未找到 GridCoordinates/CoordinateX,Y,Z")
    normalized = json.dumps(
        blocks,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "algorithm": "sha256",
        "coordinate_capture": "CGNS/HDF5 contiguous CoordinateX,Y,Z",
        "metadata_reader": str(dll_path),
        "number_of_blocks": len(blocks),
        "total_block_points": sum(block["point_count"] for block in blocks),
        "total_block_cells": sum(block["cell_count"] for block in blocks),
        "aggregate_sha256": hashlib.sha256(normalized).hexdigest(),
        "blocks": blocks,
    }


def _configure_hdf5_api(library: Any) -> None:
    """声明本任务用到的最小 HDF5 1.8 C API。"""

    hid_t = ctypes.c_longlong
    hsize_t = ctypes.c_ulonglong
    library.H5open.argtypes = []
    library.H5open.restype = ctypes.c_int
    library.H5Fopen.argtypes = [ctypes.c_char_p, ctypes.c_uint, hid_t]
    library.H5Fopen.restype = hid_t
    library.H5Fclose.argtypes = [hid_t]
    library.H5Fclose.restype = ctypes.c_int
    library.H5Gopen2.argtypes = [hid_t, ctypes.c_char_p, hid_t]
    library.H5Gopen2.restype = hid_t
    library.H5Gclose.argtypes = [hid_t]
    library.H5Gclose.restype = ctypes.c_int
    library.H5Gget_num_objs.argtypes = [hid_t, ctypes.POINTER(hsize_t)]
    library.H5Gget_num_objs.restype = ctypes.c_int
    library.H5Gget_objname_by_idx.argtypes = [
        hid_t,
        hsize_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    library.H5Gget_objname_by_idx.restype = ctypes.c_ssize_t
    library.H5Dopen2.argtypes = [hid_t, ctypes.c_char_p, hid_t]
    library.H5Dopen2.restype = hid_t
    library.H5Dclose.argtypes = [hid_t]
    library.H5Dclose.restype = ctypes.c_int
    library.H5Dget_space.argtypes = [hid_t]
    library.H5Dget_space.restype = hid_t
    library.H5Dget_offset.argtypes = [hid_t]
    library.H5Dget_offset.restype = ctypes.c_ulonglong
    library.H5Dget_storage_size.argtypes = [hid_t]
    library.H5Dget_storage_size.restype = hsize_t
    library.H5Sget_simple_extent_ndims.argtypes = [hid_t]
    library.H5Sget_simple_extent_ndims.restype = ctypes.c_int
    library.H5Sget_simple_extent_dims.argtypes = [
        hid_t,
        ctypes.POINTER(hsize_t),
        ctypes.POINTER(hsize_t),
    ]
    library.H5Sget_simple_extent_dims.restype = ctypes.c_int
    library.H5Sclose.argtypes = [hid_t]
    library.H5Sclose.restype = ctypes.c_int
    if hasattr(library, "H5Eset_auto2"):
        library.H5Eset_auto2.argtypes = [hid_t, ctypes.c_void_p, ctypes.c_void_p]
        library.H5Eset_auto2.restype = ctypes.c_int


def _hdf_group_names(library: Any, group_id: int) -> list[str]:
    count = ctypes.c_ulonglong()
    if library.H5Gget_num_objs(group_id, ctypes.byref(count)) < 0:
        return []
    names: list[str] = []
    for index in range(int(count.value)):
        buffer = ctypes.create_string_buffer(8192)
        length = library.H5Gget_objname_by_idx(
            group_id,
            ctypes.c_ulonglong(index),
            buffer,
            ctypes.sizeof(buffer),
        )
        if length >= 0:
            names.append(buffer.value.decode("utf-8", errors="replace"))
    return names


def _open_hdf_group(library: Any, location_id: int, path: str) -> int | None:
    group_id = library.H5Gopen2(location_id, path.encode("utf-8"), 0)
    return int(group_id) if group_id >= 0 else None


def _hdf_dataset_info(
    library: Any,
    file_id: int,
    path: str,
) -> dict[str, Any] | None:
    dataset_id = library.H5Dopen2(file_id, path.encode("utf-8"), 0)
    if dataset_id < 0:
        return None
    try:
        space_id = library.H5Dget_space(dataset_id)
        if space_id < 0:
            return None
        try:
            rank = int(library.H5Sget_simple_extent_ndims(space_id))
            if rank < 1:
                return None
            dimensions = (ctypes.c_ulonglong * rank)()
            if library.H5Sget_simple_extent_dims(space_id, dimensions, None) < 0:
                return None
            shape = [int(dimensions[index]) for index in range(rank)]
        finally:
            library.H5Sclose(space_id)
        offset = int(library.H5Dget_offset(dataset_id))
        storage_bytes = int(library.H5Dget_storage_size(dataset_id))
    finally:
        library.H5Dclose(dataset_id)
    if offset == (1 << 64) - 1 or storage_bytes <= 0:
        return None
    return {
        "shape": shape,
        "offset": offset,
        "storage_bytes": storage_bytes,
    }


def _discover_cgns_coordinate_blocks(
    library: Any,
    file_id: int,
    mesh_path: Path,
) -> list[dict[str, Any]]:
    root_id = _open_hdf_group(library, file_id, "/")
    if root_id is None:
        raise RuntimeError("无法打开 CGNS HDF5 根 group")
    coordinate_blocks: list[dict[str, Any]] = []
    try:
        for base_name in _hdf_group_names(library, root_id):
            base_path = "/" + base_name
            base_id = _open_hdf_group(library, file_id, base_path)
            if base_id is None:
                continue
            try:
                for zone_name in _hdf_group_names(library, base_id):
                    zone_path = base_path + "/" + zone_name
                    zone_id = _open_hdf_group(library, file_id, zone_path)
                    if zone_id is None:
                        continue
                    try:
                        if "GridCoordinates" not in _hdf_group_names(library, zone_id):
                            continue
                    finally:
                        library.H5Gclose(zone_id)
                    axes: list[dict[str, Any]] = []
                    for axis in ("CoordinateX", "CoordinateY", "CoordinateZ"):
                        dataset_path = (
                            zone_path
                            + "/GridCoordinates/"
                            + axis
                            + "/ data"
                        )
                        info = _hdf_dataset_info(library, file_id, dataset_path)
                        if info is None:
                            axes = []
                            break
                        axes.append({"axis": axis, **info})
                    if axes:
                        coordinate_blocks.append(
                            _fingerprint_coordinate_block(
                                mesh_path,
                                index=len(coordinate_blocks) + 1,
                                base_name=base_name,
                                zone_name=zone_name,
                                axes=axes,
                            )
                        )
            finally:
                library.H5Gclose(base_id)
    finally:
        library.H5Gclose(root_id)
    coordinate_blocks.sort(key=lambda block: (block["base"], block["name"]))
    for index, block in enumerate(coordinate_blocks, start=1):
        block["index"] = index
    return coordinate_blocks


def _fingerprint_coordinate_block(
    mesh_path: Path,
    *,
    index: int,
    base_name: str,
    zone_name: str,
    axes: list[dict[str, Any]],
) -> dict[str, Any]:
    shapes = {tuple(axis["shape"]) for axis in axes}
    if len(shapes) != 1:
        raise RuntimeError(f"{zone_name} 的 X/Y/Z 坐标维度不一致")
    storage_shape = list(shapes.pop())
    if len(storage_shape) != 3:
        raise RuntimeError(f"{zone_name} 不是三维结构化 block：{storage_shape}")
    point_count = math_product(storage_shape)
    for axis in axes:
        if axis["storage_bytes"] != point_count * 8:
            raise RuntimeError(
                f"{zone_name}/{axis['axis']} 不是连续未压缩 Float64 数据集"
            )

    digest = hashlib.sha256()
    with mesh_path.open("rb") as stream:
        for axis in axes:
            digest.update(axis["axis"].encode("ascii"))
            stream.seek(axis["offset"])
            remaining = axis["storage_bytes"]
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError(f"读取 {zone_name}/{axis['axis']} 坐标时提前结束")
                digest.update(chunk)
                remaining -= len(chunk)
        probes = _read_coordinate_probes(stream, storage_shape, axes)

    # CGNS/HDF5 以 K,J,I 顺序报告 shape；对外统一记录 I,J,K。
    size = list(reversed(storage_shape))
    cell_count = math_product([max(value - 1, 0) for value in size])
    return {
        "index": index,
        "base": base_name,
        "name": zone_name,
        "size": size,
        "storage_shape": storage_shape,
        "point_count": point_count,
        "cell_count": cell_count,
        "coordinate_sha256": digest.hexdigest(),
        "coordinate_bytes": sum(axis["storage_bytes"] for axis in axes),
        "coordinate_capture": "CGNS/HDF5 contiguous CoordinateX,Y,Z",
        "samples": probes,
    }


def _read_coordinate_probes(
    stream: Any,
    storage_shape: list[int],
    axes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    size_i, size_j, size_k = reversed(storage_shape)
    center = (
        1 + (size_i - 1) // 2,
        1 + (size_j - 1) // 2,
        1 + (size_k - 1) // 2,
    )
    indices = {
        (i_value, j_value, k_value)
        for i_value in (1, size_i)
        for j_value in (1, size_j)
        for k_value in (1, size_k)
    }
    indices.add(center)
    for i_value in _fixed_probe_indices(size_i):
        indices.add((i_value, center[1], center[2]))
    for j_value in _fixed_probe_indices(size_j):
        indices.add((center[0], j_value, center[2]))
    for k_value in _fixed_probe_indices(size_k):
        indices.add((center[0], center[1], k_value))

    probes = []
    for i_value, j_value, k_value in sorted(indices):
        flat_index = (
            (k_value - 1) * size_j * size_i
            + (j_value - 1) * size_i
            + (i_value - 1)
        )
        xyz = []
        for axis in axes:
            stream.seek(axis["offset"] + flat_index * 8)
            raw = stream.read(8)
            if len(raw) != 8:
                raise OSError("读取固定位置坐标探针时提前结束")
            xyz.append(struct.unpack("<d", raw)[0])
        probes.append({"ijk": [i_value, j_value, k_value], "xyz": xyz})
    return probes


def _fixed_probe_indices(size: int) -> list[int]:
    return sorted(
        {
            1,
            size,
            1 + (size - 1) // 4,
            1 + (size - 1) // 2,
            1 + 3 * (size - 1) // 4,
        }
    )


def math_product(values: Sequence[int]) -> int:
    result = 1
    for value in values:
        result *= int(value)
    return result


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    """读取由 IGG 诊断脚本写出的 JSON 字典。"""

    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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
