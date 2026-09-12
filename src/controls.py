"""定义、解析、校验并解析 AutoGrid 17.1 网格控制项。"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


PRIORITIES = ("P0", "P1", "P2")
STAGES = (
    "configuration",
    "wizard",
    "topology",
    "distribution",
    "boundary_layer",
    "optimization",
    "interface",
    "existing_effect",
)
STAGE_ORDER = {stage: index for index, stage in enumerate(STAGES)}


class ControlValidationError(ValueError):
    """控制表达式、实体选择或数值校验失败。"""


@dataclass(frozen=True)
class ControlSpec:
    """AutoGrid 17.1 纯网格控制的静态数据契约。"""

    key: str
    description: str
    scope: str
    target_kind: str
    hierarchy: tuple[str, ...]
    value_type: str
    setter: str | None
    getter: str | None
    priority: str
    stage: str
    enum_values: tuple[str, ...] = ()
    value_map: tuple[tuple[Any, Any], ...] = ()
    setter_by_value: tuple[tuple[Any, str], ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    tuple_length: int | None = None
    si_length: bool = False
    topologies: tuple[str, ...] = ()
    not_applicable_when: str | None = None
    setter_mode: str = "value"
    unsupported_reason: str | None = None

    @property
    def supports_readback(self) -> bool:
        """判断该控制项是否支持通过 getter 回读。"""

        return self.getter is not None

    def map_api_value(self, value: Any) -> Any:
        """将公开控制值映射为 AutoGrid API 所需值。"""

        for public_value, api_value in self.value_map:
            if value == public_value:
                return api_value
        return value

    def setter_for_value(self, value: Any) -> str | None:
        """根据控制值选择对应的 AutoGrid setter。"""

        for public_value, method in self.setter_by_value:
            if value == public_value:
                return method
        return self.setter

    def to_dict(self) -> dict[str, Any]:
        """将控制规格转换为可序列化字典。"""

        data = asdict(self)
        data["supports_readback"] = self.supports_readback
        data["unit"] = "m" if self.si_length else None
        return data


@dataclass(frozen=True)
class EntitySelector:
    """描述控制表达式中的实体类型、匹配方式和值。"""

    kind: str
    mode: str
    value: str | int

    @property
    def specificity(self) -> int:
        """返回选择器的匹配精确度。"""

        return 0 if self.mode == "wildcard" else 1

    def canonical(self) -> str:
        """返回选择器的规范化文本表示。"""

        if self.mode == "wildcard":
            return f"{self.kind}:*"
        if self.mode == "index":
            return f"{self.kind}:#{self.value}"
        return f"{self.kind}:{self.value}"

    def to_dict(self) -> dict[str, Any]:
        """将实体选择器转换为可序列化字典。"""

        return asdict(self)


@dataclass(frozen=True)
class ControlRequest:
    """表示用户提交且已经通过基础解析的控制请求。"""

    raw: str
    key: str
    selectors: tuple[EntitySelector, ...]
    value: Any
    spec: ControlSpec
    source: str = "--set"

    @property
    def selector_path(self) -> str:
        """返回控制请求的规范化实体路径。"""

        if self.spec.scope == "configuration":
            return "configuration"
        path = "/".join(selector.canonical() for selector in self.selectors)
        if self.spec.scope == "wizard":
            return f"{path}/wizard"
        return path

    @property
    def specificity(self) -> int:
        """返回请求中所有实体选择器的总精确度。"""

        return sum(selector.specificity for selector in self.selectors)

    def to_dict(self) -> dict[str, Any]:
        """将控制请求转换为可序列化字典。"""

        return {
            "raw": self.raw,
            "key": self.key,
            "selector": self.selector_path,
            "value": self.value,
            "source": self.source,
            "priority": self.spec.priority,
        }


@dataclass(frozen=True)
class TargetEntity:
    """表示几何中可应用控制的具体目标实体。"""

    kind: str
    index: int | None
    name: str

    def to_dict(self) -> dict[str, Any]:
        """将目标实体转换为可序列化字典。"""

        return {"kind": self.kind, "index": self.index, "name": self.name}


@dataclass(frozen=True)
class ResolvedControl:
    """表示已展开通配符并绑定具体实体的控制项。"""

    control_id: str
    key: str
    target: tuple[TargetEntity, ...]
    requested_value: Any
    project_value: Any
    api_value: Any
    setter: str
    getter: str | None
    setter_mode: str
    stage: str
    priority: str
    scope: str
    target_kind: str
    topologies: tuple[str, ...]
    source: str

    @property
    def target_path(self) -> str:
        """返回已解析目标实体的规范化路径。"""

        return "/".join(
            f"{part.kind}:#{part.index}" if part.index is not None else f"{part.kind}:{part.name}"
            for part in self.target
        ) or "configuration"

    def to_dict(self) -> dict[str, Any]:
        """将已解析控制项转换为可序列化字典。"""

        return {
            "id": self.control_id,
            "key": self.key,
            "target": [part.to_dict() for part in self.target],
            "target_path": self.target_path,
            "requested": self.requested_value,
            "project_value": self.project_value,
            "api_value": self.api_value,
            "setter": self.setter,
            "getter": self.getter,
            "setter_mode": self.setter_mode,
            "stage": self.stage,
            "priority": self.priority,
            "scope": self.scope,
            "target_kind": self.target_kind,
            "topologies": list(self.topologies),
            "source": self.source,
            "status": "planned",
            "readback": None,
            "error": None,
        }


_SPECS: list[ControlSpec] = []


def _add(
    key: str,
    description: str,
    *,
    scope: str,
    target_kind: str,
    hierarchy: tuple[str, ...],
    value_type: str,
    setter: str | None,
    getter: str | None,
    priority: str,
    stage: str,
    enum_values: tuple[str, ...] = (),
    value_map: tuple[tuple[Any, Any], ...] = (),
    setter_by_value: tuple[tuple[Any, str], ...] = (),
    minimum: float | None = None,
    maximum: float | None = None,
    tuple_length: int | None = None,
    si_length: bool = False,
    topologies: tuple[str, ...] = (),
    not_applicable_when: str | None = None,
    setter_mode: str = "value",
    unsupported_reason: str | None = None,
) -> None:
    """向内部注册表添加一条完整控制规格。"""

    _SPECS.append(
        ControlSpec(
            key=key,
            description=description,
            scope=scope,
            target_kind=target_kind,
            hierarchy=hierarchy,
            value_type=value_type,
            setter=setter,
            getter=getter,
            priority=priority,
            stage=stage,
            enum_values=enum_values,
            value_map=value_map,
            setter_by_value=setter_by_value,
            minimum=minimum,
            maximum=maximum,
            tuple_length=tuple_length,
            si_length=si_length,
            topologies=topologies,
            not_applicable_when=not_applicable_when,
            setter_mode=setter_mode,
            unsupported_reason=unsupported_reason,
        )
    )


def _direct(
    key: str,
    method: str,
    description: str,
    *,
    target_kind: str,
    hierarchy: tuple[str, ...],
    value_type: str = "float",
    priority: str = "P2",
    stage: str = "distribution",
    minimum: float | None = None,
    maximum: float | None = None,
    si_length: bool = False,
    topologies: tuple[str, ...] = (),
    getter: str | None = "auto",
    scope: str | None = None,
    setter_mode: str = "value",
) -> None:
    """注册可直接映射到单个 API 方法的控制规格。"""

    resolved_getter = method.replace("set_", "get_", 1) if getter == "auto" else getter
    _add(
        key,
        description,
        scope=scope or (target_kind if target_kind in {"row", "blade", "gap", "partial-gap", "fillet", "interface", "endwall"} else "existing-effect"),
        target_kind=target_kind,
        hierarchy=hierarchy,
        value_type=value_type,
        setter=method,
        getter=resolved_getter,
        priority=priority,
        stage=stage,
        minimum=minimum,
        maximum=maximum,
        si_length=si_length,
        topologies=topologies,
        setter_mode=setter_mode,
    )


# 全局配置与入口/出口 bulb：均不改变物理几何。
_direct(
    "configuration/grid_levels",
    "a5_set_configuration_number_of_grid_levels",
    "多重网格层级数",
    target_kind="configuration",
    hierarchy=(),
    value_type="int",
    priority="P0",
    stage="configuration",
    minimum=1,
    maximum=9,
    getter="a5_get_configuration_number_of_grid_levels",
    scope="configuration",
)
_direct(
    "configuration/support_curve_control_points",
    "a5_set_support_curve_control_pts",
    "支撑曲线控制点数",
    target_kind="configuration",
    hierarchy=(),
    value_type="int",
    stage="configuration",
    minimum=2,
    maximum=10001,
    getter="a5_get_support_curve_control_pts",
    scope="configuration",
)
for _side in ("inlet", "outlet"):
    _add(
        f"configuration/{_side}_bulb.topology",
        f"{_side} bulb 拓扑",
        scope="configuration",
        target_kind="configuration",
        hierarchy=(),
        value_type="enum",
        setter=None,
        getter=f"get_{_side}_bulb_topology",
        priority="P2",
        stage="topology",
        enum_values=("sharp", "rounded", "radial"),
        setter_by_value=tuple(
            (value, f"set_{_side}_bulb_{value}_topology") for value in ("sharp", "rounded", "radial")
        ),
    )
    for _suffix, _label in (
        ("streamwise_points", "流向点数"),
        ("h_streamwise_points", "H 块流向点数"),
        ("spanwise_points", "展向点数"),
        ("c_block_points", "C 块点数"),
        ("radial_points", "径向点数"),
        ("singular_line", "奇异线点数"),
        ("smoothing_steps", "光顺步数"),
        ("butterfly_smoothing_steps", "蝶形区光顺步数"),
    ):
        _api_suffix = {
            "streamwise_points": "streamwise_number_of_points",
            "h_streamwise_points": "H_streamwise_number_of_points",
            "spanwise_points": "spanwise_number_of_points",
            "c_block_points": "C_block_number_of_points",
            "radial_points": "radial_number_of_points",
            "singular_line": "singular_line",
            "smoothing_steps": "smoothing_steps",
            "butterfly_smoothing_steps": "butterfly_smoothing_steps",
        }[_suffix]
        _direct(
            f"configuration/{_side}_bulb.{_suffix}",
            f"set_{_side}_bulb_{_api_suffix}",
            f"{_side} bulb {_label}",
            target_kind="configuration",
            hierarchy=(),
            value_type="int",
            stage="distribution" if "points" in _suffix or _suffix == "singular_line" else "optimization",
            minimum=0 if "steps" in _suffix else 1,
            maximum=10001,
            getter=f"get_{_side}_bulb_{_api_suffix}",
            scope="configuration",
        )


# RowWizard：仅保留网格控制；hub/shroud 截断、gap/fillet 尺寸等几何项在审计表中排除。
_add(
    "wizard/grid_level",
    "RowWizard 网格级别",
    scope="wizard",
    target_kind="wizard",
    hierarchy=("row",),
    value_type="enum",
    setter="set_grid_level",
    getter="get_grid_level",
    priority="P0",
    stage="wizard",
    enum_values=("coarse", "medium", "fine", "user"),
    value_map=(("coarse", 1), ("medium", 2), ("fine", 3), ("user", 4)),
)
for _key, _method, _getter, _label, _kind, _minimum, _maximum, _si in (
    ("spanwise_paths", "set_flow_path_number", "get_flow_path_number", "展向 flow paths", "int", 3, 10001, False),
    ("far_field_spanwise_paths", "set_flow_path_number_far_field", "get_flow_path_number_far_field", "远场展向 flow paths", "int", 3, 10001, False),
    ("far_field_constant_cells_percent", "set_cst_cell_number_far_field", "get_cst_cell_number_far_field", "远场常值单元比例", "int", 0, 100, False),
    ("full_matching", "set_full_matching_topology", "get_full_matching_topology", "全匹配拓扑", "bool", None, None, False),
    ("first_cell_width", "set_row_cell_width_at_wall", "get_row_cell_width_at_wall", "首层单元宽度", "float", 0, None, True),
    ("blade_tip_rounded_topology", "set_blade_tip_rounded_topology", "get_blade_tip_rounded_topology", "圆钝叶尖拓扑", "bool", None, None, False),
):
    _add(
        f"wizard/{_key}",
        _label,
        scope="wizard",
        target_kind="wizard",
        hierarchy=("row",),
        value_type=_kind,
        setter=_method,
        getter=_getter,
        priority="P0" if _key in {"spanwise_paths", "first_cell_width"} else "P1",
        stage="wizard",
        minimum=_minimum,
        maximum=_maximum,
        si_length=_si,
    )
for _key, _method, _label, _kind, _minimum, _si in (
    ("acoustic.max_span_cell_size", "set_max_span_cell_size", "声学区最大展向单元尺寸", "float", 0, True),
    ("acoustic.max_far_field_span_cell_size", "set_max_span_cell_size_in_far_field", "声学远场最大展向单元尺寸", "float", 0, True),
    ("acoustic.max_b2b_cell_size", "set_max_B2B_cell_size", "声学 B2B 最大单元尺寸", "float", 0, True),
    ("acoustic.max_stream_cell_size", "set_max_stream_cell_size_upstream_downstream", "声学上下游最大流向单元尺寸", "float", 0, True),
    ("acoustic.max_bulb_stream_cell_size", "set_max_stream_cell_size_in_bulb", "声学 bulb 最大流向单元尺寸", "float", 0, True),
    ("acoustic.far_field_reference_layer", "set_far_field_reference_layer", "声学远场参考层", "int", 1, False),
):
    _add(
        f"wizard/{_key}",
        _label,
        scope="wizard",
        target_kind="acoustic-wizard",
        hierarchy=("row",),
        value_type=_kind,
        setter=_method,
        getter=_method.replace("set_", "get_", 1),
        priority="P2",
        stage="wizard",
        minimum=_minimum,
        maximum=10001 if _kind == "int" else None,
        si_length=_si,
        not_applicable_when="目标行不是声学行",
    )


# 行级网格密度、flow-path 与优化控制。
_add(
    "row/mesh_level",
    "行网格级别（coarse/medium/fine/user）",
    scope="row",
    target_kind="row",
    hierarchy=("row",),
    value_type="enum",
    setter="set_coarse_grid_level",
    getter="get_coarse_grid_level",
    priority="P0",
    stage="wizard",
    enum_values=("coarse", "medium", "fine", "user"),
    value_map=(("coarse", 1), ("medium", 2), ("fine", 3), ("user", 4)),
    setter_mode="row_accuracy_level",
)
_add(
    "row/target_points",
    "user 网格级别目标点数",
    scope="row",
    target_kind="row",
    hierarchy=("row",),
    value_type="int",
    setter="set_coarse_grid_level",
    getter="get_coarse_grid_level_target",
    priority="P0",
    stage="wizard",
    minimum=100,
    maximum=2_000_000_000,
    setter_mode="row_accuracy_target",
    unsupported_reason="目标点数控制暂时停用：当前 AutoGrid 17.1 生成路径不能兑现目标点数，请使用网格级别、展向或 B2B 点数控制。",
)
_add(
    "row/streamwise_weight",
    "入口/叶片/出口流向权重",
    scope="row",
    target_kind="row",
    hierarchy=("row",),
    value_type="tuple_float",
    setter="set_streamwise_weight",
    getter=None,
    priority="P1",
    stage="distribution",
    tuple_length=3,
    minimum=0,
    setter_mode="tuple_args",
)
for _key, _method, _label, _kind, _priority, _stage, _minimum, _maximum in (
    ("upstream.relaxation", "set_upstream_block_relaxation", "上游块聚集松弛", "int", "P2", "distribution", 0, 1),
    ("downstream.relaxation", "set_downstream_block_relaxation", "下游块聚集松弛", "int", "P2", "distribution", 0, 1),
    ("downstream.before_nozzle_relaxation", "set_downstream_block_relaxation_before_nozzle", "喷嘴前下游块松弛", "int", "P2", "distribution", 0, 1),
    ("upstream.untwist", "set_untwist_upstream_block", "上游块去扭曲", "bool", "P2", "topology", None, None),
    ("downstream.untwist", "set_untwist_downstream_block", "下游块去扭曲", "bool", "P2", "topology", None, None),
    ("upstream.untwist_location", "set_untwist_upstream_block_stream_location", "上游去扭曲流向位置", "float", "P2", "distribution", 0, 1),
    ("downstream.untwist_location", "set_untwist_downstream_block_stream_location", "下游去扭曲流向位置", "float", "P2", "distribution", 0, 1),
    ("gap.hub_interpolation", "set_hub_gap_interpolation", "hub gap 插值", "bool", "P1", "distribution", None, None),
    ("gap.shroud_interpolation", "set_shroud_gap_interpolation", "shroud gap 插值", "bool", "P1", "distribution", None, None),
    ("gap.hub_interpolation_location", "set_hub_gap_interpolation_span_location", "hub gap 插值展向位置", "float", "P1", "distribution", 0, 1),
    ("gap.shroud_interpolation_location", "set_shroud_gap_interpolation_span_location", "shroud gap 插值展向位置", "float", "P1", "distribution", 0, 1),
    ("enforce_blade_wall_cell_width", "set_enforce_cell_width_at_blade_wall", "强制叶片壁面单元宽度", "bool", "P1", "boundary_layer", None, None),
    ("bladeless_mesh", "set_bladeless_mesh", "无叶片通道网格", "bool", "P2", "topology", None, None),
    ("span_interpolation", "set_span_interpolation", "展向插值间距", "float", "P1", "distribution", 0, 1),
    ("clustering", "set_clustering", "行级展向聚集", "float", "P1", "distribution", 0, None),
    ("optimization.steps", "set_row_optimization_steps", "普通优化步数", "int", "P0", "optimization", 0, 100000),
    ("optimization.gap_steps", "set_row_optimization_steps_in_gap", "gap 优化步数", "int", "P0", "optimization", 0, 100000),
    ("optimization.full_multigrid_steps", "set_row_full_multigrid_optimization_steps", "全多重网格优化步数", "int", "P1", "optimization", 0, 100000),
    ("optimization.boundary_steps", "set_row_bnd_optimization_steps", "边界优化步数", "int", "P1", "optimization", 0, 100000),
    ("optimization.straight_boundary", "set_row_straight_bnd_control", "直边界控制", "int", "P1", "optimization", 0, 1),
    ("optimization.freeze_skin", "set_row_optimization_freeze_skin_mesh", "冻结 skin 网格", "bool", "P1", "optimization", None, None),
    ("optimization.orthogonality", "set_row_optimization_orthogonality_control", "正交性优化权重", "float", "P1", "optimization", 0, 1),
    ("optimization.gap_orthogonality", "set_row_optimization_orthogonality_control_in_gap", "gap 正交性优化权重", "float", "P1", "optimization", 0, 1),
    ("optimization.wake", "set_row_optimization_wake_control", "尾迹正交性优化权重", "float", "P1", "optimization", 0, 1),
    ("optimization.nmb", "set_row_optimization_nmb_control", "NMB 优化权重", "float", "P1", "optimization", 0, 1),
    ("flow_path.number", "set_row_flow_path_number", "flow path 数量", "int", "P0", "distribution", 3, 10001),
    ("flow_path.hub_clustering", "set_flow_path_control_hub_clustering", "hub 端 flow path 聚集", "float", "P1", "distribution", 0, None),
    ("flow_path.shroud_clustering", "set_flow_path_control_shroud_clustering", "shroud 端 flow path 聚集", "float", "P1", "distribution", 0, None),
    ("flow_path.constant_cells", "set_flow_path_control_cst_cells_number", "flow path 常值单元数", "int", "P1", "distribution", 0, 10001),
    ("flow_path.control_points", "set_flow_path_control_control_point_number", "flow path 控制点数", "int", "P1", "distribution", 2, 10001),
    ("flow_path.intermediate_points", "set_flow_path_control_intermediate_point_number", "flow path 中间点数", "int", "P1", "distribution", 0, 10001),
    ("flow_path.smoothing_steps", "set_flow_path_control_smoothing_steps", "flow path 光顺步数", "int", "P1", "optimization", 0, 100000),
    ("flow_path.distribution_smoothing_steps", "set_flow_path_control_distribution_smoothing_steps", "flow path 分布光顺步数", "int", "P1", "optimization", 0, 100000),
):
    _direct(
        f"row/{_key}",
        _method,
        _label,
        target_kind="row",
        hierarchy=("row",),
        value_type=_kind,
        priority=_priority,
        stage=_stage,
        minimum=_minimum,
        maximum=_maximum,
    )
for _key, _method, _getter, _label in (
    ("optimization.skewness", "set_row_optimization_skewness_control", "get_row_optimization_type", "偏斜优化模式"),
    ("optimization.gap_skewness", "set_row_optimization_skewness_control_in_gap", "get_row_optimization_type_in_gap", "gap 偏斜优化模式"),
):
    _add(
        f"row/{_key}",
        _label,
        scope="row",
        target_kind="row",
        hierarchy=("row",),
        value_type="enum",
        setter=_method,
        getter=_getter,
        priority="P1",
        stage="optimization",
        enum_values=("no", "medium", "yes"),
        value_map=(("no", 0), ("medium", 1), ("yes", 2)),
    )
_add(
    "row/optimization.multigrid",
    "启用优化多重网格",
    scope="row",
    target_kind="row",
    hierarchy=("row",),
    value_type="bool",
    setter="set_row_optimization_multigrid_control",
    getter="get_row_optimization_multigrid_control",
    priority="P1",
    stage="optimization",
    value_map=((True, "yes"), (False, "no")),
)


# 行级低内存模式。
# 注意：AutoGrid 17.1 的 enable_low_memory_usage()/disable_low_memory_usage()
# 内部调用 set_row_properties_(impl, "memory_use", 1/0)，但底层 C 函数期望字符串
# 值导致 "TypeError: Expecting string"。通过 row_property_memory_use setter_mode
# 直接调用 set_row_properties_ 并传入 "1"/"0" 字符串绕过。
_add(
    "row/low_memory_usage",
    "低内存占用模式",
    scope="row",
    target_kind="row",
    hierarchy=("row",),
    value_type="bool",
    setter="set_row_properties_",
    getter=None,
    priority="P2",
    stage="wizard",
    value_map=((True, "1"), (False, "0")),
    setter_mode="row_property_memory_use",
)


# 叶片 B2B 拓扑和点分布。方法名全部来自 fine171/_python/_autogrid/Autogrid.py。
_add(
    "blade/b2b.topology",
    "B2B 拓扑类型",
    scope="blade",
    target_kind="blade",
    hierarchy=("row", "blade"),
    value_type="enum",
    setter="set_b2b_topology_type",
    getter="get_b2b_topology_type",
    priority="P2",
    stage="topology",
    enum_values=("default", "hoh", "user", "hi"),
    value_map=(("default", 0), ("hoh", 1), ("user", 2), ("hi", 3)),
)
for _key, _method, _getter, _label, _values, _mapping in (
    ("b2b.default.type", "set_b2b_default_topology_type", "get_b2b_default_topology_type", "Default 拓扑走向", ("streamwise", "rounded_azimuthal", "rounded_streamwise"), (("streamwise", 0), ("rounded_azimuthal", 1), ("rounded_streamwise", 2))),
    ("b2b.default.periodicity", "set_b2b_default_topology_periodicity_type", "get_b2b_default_topology_periodicity_type", "Default 周期面匹配", ("non_matching", "matching"), (("non_matching", 0), ("matching", 1))),
    ("b2b.default.inlet_stagger", "set_b2b_default_topology_staggered_inlet_angle", "get_b2b_default_topology_inlet_staggered_angle", "入口 stagger 类型", ("normal", "low", "high"), (("normal", 0), ("low", 1), ("high", 2))),
    ("b2b.default.outlet_stagger", "set_b2b_default_topology_staggered_outlet_angle", "get_b2b_default_topology_outlet_staggered_angle", "出口 stagger 类型", ("normal", "low", "high"), (("normal", 0), ("low", 1), ("high", 2))),
):
    _add(
        f"blade/{_key}",
        _label,
        scope="blade",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="enum",
        setter=_method,
        getter=_getter,
        priority="P2",
        stage="topology",
        enum_values=_values,
        value_map=_mapping,
        topologies=("default",),
    )
for _key, _method, _label in (
    ("b2b.default.high_stagger_optimization", "set_b2b_default_topology_high_staggered_optimization", "高 stagger 优化"),
    ("b2b.default.high_stagger_detection", "set_b2b_default_topology_high_staggered_detection", "高 stagger 自动检测"),
    ("b2b.default.leading_edge_zcst", "set_b2b_default_topology_leading_edge_zcstline", "前缘 Z 常值线"),
    ("b2b.default.trailing_edge_zcst", "set_b2b_default_topology_trailing_edge_zcstline", "尾缘 Z 常值线"),
    ("b2b.default.free_outlet_angle", "set_b2b_default_topology_free_outlet_angle", "自由出口角"),
    ("b2b.default.free_inlet_angle", "set_b2b_default_topology_free_inlet_angle", "自由入口角"),
    ("b2b.default.fix_outlet_angle", "set_b2b_default_topology_fix_outlet_angle", "固定出口角"),
    ("b2b.default.fix_inlet_angle", "set_b2b_default_topology_fix_inlet_angle", "固定入口角"),
    ("b2b.default.fix_outlet_mesh", "set_b2b_default_topology_fix_outlet_mesh", "固定出口网格"),
    ("b2b.default.fix_inlet_mesh", "set_b2b_default_topology_fix_inlet_mesh", "固定入口网格"),
    ("b2b.default.wake_control", "set_b2b_default_topology_wake_control", "尾迹控制"),
    ("b2b.default.wake_prolongation", "set_b2b_default_topology_wake_control_prolongation", "尾迹延伸"),
):
    _direct(
        f"blade/{_key}",
        _method,
        _label,
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="bool",
        priority="P1" if "wake" in _key else "P2",
        stage="topology",
        topologies=("default",),
    )
_DEFAULT_POINT_METHODS = {
    "azimuthal_inlet_points": "azimutal_inlet",
    "azimuthal_outlet_points": "azimutal_outlet",
    "azimuthal_inlet_up_points": "azimutal_inlet_up",
    "azimuthal_outlet_up_points": "azimutal_outlet_up",
    "azimuthal_inlet_down_points": "azimutal_inlet_down",
    "azimuthal_outlet_down_points": "azimutal_outlet_down",
    "streamwise_inlet_points": "streamwise_inlet",
    "streamwise_outlet_points": "streamwise_outlet",
    "streamwise_suction_points": "streamwise_blade_upper_side",
    "streamwise_pressure_points": "streamwise_blade_lower_side",
    "boundary_layer_points": "in_boundary_layer",
    "boundary_layer_gap_points": "in_boundary_layer_of_gaps",
    "leading_edge_index": "leading_edge_index",
    "trailing_edge_index": "trailing_edge_index",
}
for _key, _suffix in _DEFAULT_POINT_METHODS.items():
    _direct(
        f"blade/b2b.default.{_key}",
        f"set_b2b_default_topology_grid_point_number_{_suffix}",
        f"Default 拓扑 {_key}",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="int",
        priority="P1",
        stage="distribution",
        minimum=0 if _key.endswith("index") else 2,
        maximum=10001,
        topologies=("default",),
    )
for _key, _suffix, _label, _si, _minimum, _maximum in (
    ("cell_width_at_wall", "cell_width_at_wall", "壁面首层宽度", True, 0, None),
    ("hub_cell_width_at_wall", "cell_width_at_wall_at_hub", "hub 端首层宽度", True, 0, None),
    ("shroud_cell_width_at_wall", "cell_width_at_wall_at_shroud", "shroud 端首层宽度", True, 0, None),
    ("boundary_layer_width", "bnd_layer_width", "边界层厚度", True, 0, None),
    ("trailing_edge_cell_width", "cell_width_at_trailing_edge", "尾缘单元宽度", True, 0, None),
    ("leading_edge_cell_width", "cell_width_at_leading_edge", "前缘单元宽度", True, 0, None),
    ("boundary_layer_expansion", "expansion_ratio_in_bnd_layer", "边界层增长率", False, 1, None),
    ("skin_max_expansion", "skin_block_expansion_ratio_control", "skin 块最大增长率", False, 1, None),
):
    _direct(
        f"blade/b2b.default.{_key}",
        f"set_b2b_default_topology_{_suffix}",
        _label,
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="float",
        priority="P1",
        stage="boundary_layer",
        minimum=_minimum,
        maximum=_maximum,
        si_length=_si,
        topologies=("default",),
    )
# wall_width_interpolation 的 API 期望 int（0=禁用, 1=启用）。
_direct(
    "blade/b2b.default.wall_width_interpolation",
    "set_b2b_default_topology_cell_width_at_wall_interpolation",
    "壁面宽度插值",
    target_kind="blade",
    hierarchy=("row", "blade"),
    value_type="int",
    priority="P1",
    stage="boundary_layer",
    minimum=0,
    maximum=1,
    topologies=("default",),
)
for _key, _method, _label, _kind, _minimum, _maximum in (
    ("throat_points", "set_b2b_default_topology_throat_control", "喉部点数", "int", 0, 10001),
    ("throat_projection_type", "set_b2b_default_topology_throat_projection_type", "喉部投影类型", "int", 0, 10),
    ("throat_inlet_relaxation", "set_b2b_default_topology_throat_projection_inlet_relaxation", "喉部入口松弛", "int", 0, 1),
    ("throat_outlet_relaxation", "set_b2b_default_topology_throat_projection_outlet_relaxation", "喉部出口松弛", "int", 0, 1),
    ("outlet_angle", "set_b2b_default_topology_outlet_angle", "出口网格角", "float", -180, 180),
    ("inlet_angle", "set_b2b_default_topology_inlet_angle", "入口网格角", "float", -180, 180),
    ("wake_deviation_angle", "set_b2b_default_topology_wake_control_deviation_angle", "尾迹偏转角", "float", -180, 180),
    ("intersection_quality", "set_b2b_default_topology_intersection_quality", "交线质量控制", "int", 0, None),
    ("intersection_law", "set_b2b_default_topology_intersection_law", "交线分布律", "int", 0, 20),
    ("intersection_control_points", "set_b2b_default_topology_intersection_control_point_number", "交线控制点数", "int", 2, 10001),
    ("intersection_precision_ratio", "set_b2b_intersection_precision_check_ratio", "交线精度检查比", "float", 0, None),
    ("chord_control_points", "set_b2b_default_topology_chord_control_points_number", "弦向控制点数", "int", 2, 10001),
    ("blade_reference_angle", "set_b2b_blade_reference_angle", "叶片参考角", "float", -360, 360),
):
    _direct(
        f"blade/b2b.default.{_key}",
        _method,
        _label,
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type=_kind,
        priority="P2",
        stage="distribution",
        minimum=_minimum,
        maximum=_maximum,
        topologies=("default",),
    )


# HOH 控制。
for _side in ("inlet", "outlet"):
    _add(
        f"blade/b2b.hoh.{_side}_extension",
        f"HOH {_side} 延伸块",
        scope="blade",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="bool",
        setter=None,
        getter=f"get_b2b_hoh_topology_{_side}_extension",
        priority="P2",
        stage="topology",
        setter_by_value=(
            (True, f"set_b2b_hoh_topology_enable_{_side}_extension"),
            (False, f"set_b2b_hoh_topology_disable_{_side}_extension"),
        ),
        topologies=("hoh",),
    )
    _add(
        f"blade/b2b.hoh.{_side}_extension_type",
        f"HOH {_side} 延伸块类型",
        scope="blade",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="enum",
        setter=None,
        getter=f"get_b2b_hoh_topology_{_side}_H_extension_type",
        priority="P2",
        stage="topology",
        enum_values=("i", "h"),
        setter_by_value=(
            ("i", f"set_b2b_hoh_topology_{_side}_I_extension_type"),
            ("h", f"set_b2b_hoh_topology_{_side}_H_extension_type"),
        ),
        topologies=("hoh",),
    )
    # extension_location 的 getter 需要额外参数，禁用读回；setter 仍可正常执行。
    _direct(
        f"blade/b2b.hoh.{_side}_extension_location",
        f"set_b2b_hoh_topology_{_side}_extension_location",
        f"HOH {_side} extension_location",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="float",
        priority="P2",
        stage="distribution",
        minimum=0,
        maximum=1,
        topologies=("hoh",),
        getter=None,
    )
    _direct(
        f"blade/b2b.hoh.{_side}_extension_streamwise_points",
        f"set_b2b_hoh_topology_{_side}_extension_streamwise_npts",
        f"HOH {_side} extension_streamwise_points",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="int",
        priority="P2",
        stage="distribution",
        minimum=2,
        maximum=10001,
        topologies=("hoh",),
    )
for _key, _suffix, _label in (
    ("boundary_layer_points", "npts_in_boundary_layer", "边界层点数"),
    ("around_boundary_layer_points", "npts_around_boundary_layer", "边界层周围点数"),
    ("blade_side_points", "suction_and_pressure_side_npts", "吸力/压力面点数"),
    ("h_inlet_azimuthal_points_1", "H_inlet_azimuthal_npts_1", "入口 H1 方位点数"),
    ("h_inlet_azimuthal_points_2", "H_inlet_azimuthal_npts_2", "入口 H2 方位点数"),
    ("h_inlet_azimuthal_points_3", "H_inlet_azimuthal_npts_3", "入口 H3 方位点数"),
    ("i_inlet_azimuthal_points", "I_inlet_azimuthal_npts", "入口 I 方位点数"),
    ("h_outlet_azimuthal_points_1", "H_outlet_azimuthal_npts_1", "出口 H1 方位点数"),
    ("h_outlet_azimuthal_points_2", "H_outlet_azimuthal_npts_2", "出口 H2 方位点数"),
    ("h_outlet_azimuthal_points_3", "H_outlet_azimuthal_npts_3", "出口 H3 方位点数"),
    ("i_outlet_azimuthal_points", "I_outlet_azimuthal_npts", "出口 I 方位点数"),
    ("i_inlet_periodic_points", "I_inlet_periodic_npts", "入口 I 周期点数"),
    ("i_outlet_periodic_points", "I_outlet_periodic_npts", "出口 I 周期点数"),
    ("gap_azimuthal_o_points", "gap_azimuthal_O_number_of_points", "gap O 块方位点数"),
    ("gap_azimuthal_h_points", "gap_azimuthal_H_number_of_points", "gap H 块方位点数"),
    ("gap_streamwise_h_points", "gap_streamwise_H_number_of_points", "gap H 块流向点数"),
):
    _direct(
        f"blade/b2b.hoh.{_key}",
        f"set_b2b_hoh_topology_{_suffix}",
        f"HOH {_label}",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="int",
        priority="P2",
        stage="distribution",
        minimum=2,
        maximum=10001,
        topologies=("hoh",),
    )
_add(
    "blade/b2b.hoh.gap_matching",
    "HOH gap 与主通道匹配",
    scope="blade",
    target_kind="blade",
    hierarchy=("row", "blade"),
    value_type="bool",
    setter=None,
    getter="get_b2b_hoh_topology_gap_matching_with_main_channel",
    priority="P2",
    stage="topology",
    setter_by_value=((True, "set_b2b_hoh_topology_gap_matching_with_main_channel"), (False, "set_b2b_hoh_topology_gap_non_matching_with_main_channel")),
    topologies=("hoh",),
)
for _key, _suffix in (
    ("gap_leading_addition", "gap_d1_d2_addition"),
    ("gap_leading_ratio", "gap_d1_d2_ratio"),
    ("gap_trailing_addition", "gap_d3_d4_addition"),
    ("gap_trailing_ratio", "gap_d3_d4_ratio"),
):
    _direct(
        f"blade/b2b.hoh.{_key}",
        f"set_b2b_hoh_topology_{_suffix}",
        f"HOH {_key}",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="float",
        priority="P2",
        stage="distribution",
        minimum=0,
        topologies=("hoh",),
    )
for _edge in ("leading", "trailing"):
    _add(
        f"blade/b2b.hoh.{_edge}_edge_control_type",
        f"HOH {_edge} edge 分布控制类型",
        scope="blade",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="enum",
        setter=None,
        getter=f"get_b2b_hoh_{_edge}_edge_control_type",
        priority="P2",
        stage="distribution",
        enum_values=("none", "absolute_distance", "relative_distance", "cell_length"),
        setter_by_value=tuple(
            (value, f"set_b2b_hoh_{_edge}_edge_control_type_{value}")
            for value in ("none", "absolute_distance", "relative_distance", "cell_length")
        ),
        topologies=("hoh",),
    )
    for _kind, _si, _minimum in (("absolute_distance", True, 0), ("relative_distance", False, 0), ("cell_length", True, 0)):
        # 17.1 的 trailing cell_length C 绑定只接受 int，且 getter 返回 None；
        # leading cell_length 则是正常的 SI float。显式保留这一厂商 API 差异。
        _trailing_integer_cell_length = _edge == "trailing" and _kind == "cell_length"
        _value_type = "int" if _trailing_integer_cell_length else "float"
        _direct(
            f"blade/b2b.hoh.{_edge}_edge_{_kind}",
            f"set_b2b_hoh_{_edge}_edge_control_{_kind}",
            f"HOH {_edge} edge {_kind}",
            target_kind="blade",
            hierarchy=("row", "blade"),
            value_type=_value_type,
            priority="P2",
            stage="distribution",
            minimum=_minimum,
            si_length=_si and not _trailing_integer_cell_length,
            topologies=("hoh",),
        )
for _key, _method, _kind, _minimum, _maximum, _si in (
    ("blade_distribution_smoothing_steps", "set_b2b_hoh_blade_points_distribution_smoothing_steps", "int", 0, 100000, False),
    ("wake_clustering", "set_b2b_hoh_wake_clustering", "float", 0, None, False),
    ("boundary_layer_factor", "set_b2b_mesh_control_bnd_layer_factor", "float", 0, None, False),
    ("boundary_layer_cell_width", "set_b2b_mesh_control_bnd_layer_cell_width", "float", 0, None, True),
):
    _direct(
        f"blade/b2b.hoh.{_key}",
        _method,
        f"HOH {_key}",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type=_kind,
        priority="P2",
        stage="optimization" if "smoothing" in _key else "boundary_layer",
        minimum=_minimum,
        maximum=_maximum,
        si_length=_si,
        topologies=("hoh",),
    )


# H&I 拓扑控制。
for _key, _suffix, _kind, _label in (
    ("h_full", "H_full", "bool", "完整 H 块"),
    ("h_inlet", "H_inlet", "bool", "入口 H 块"),
    ("h_outlet", "H_outlet", "bool", "出口 H 块"),
    ("skin_block", "skin_block", "bool", "skin 块"),
):
    _direct(
        f"blade/b2b.hi.{_key}",
        f"set_b2b_HI_topology_{_suffix}",
        f"H&I {_label}",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type=_kind,
        priority="P2",
        stage="topology",
        topologies=("hi",),
    )
_HI_POINTS = {
    "streamwise_blade_inlet_pressure_points": "streamwise_blade_inlet_down",
    "streamwise_blade_pressure_points": "streamwise_blade_down",
    "streamwise_previous_blade_suction_points": "streamwise_blade_lower_side",
    "streamwise_blade_outlet_pressure_points": "streamwise_blade_outlet_down",
    "streamwise_blade_inlet_suction_points": "streamwise_blade_inlet_up",
    "streamwise_blade_suction_points": "streamwise_blade_up",
    "streamwise_blade_outlet_suction_points": "streamwise_blade_outlet_up",
    "azimuthal_inlet_points": "azimutal_inlet",
    "azimuthal_outlet_points": "azimutal_outlet",
    "azimuthal_inlet_up_points": "azimutal_inlet_up",
    "azimuthal_outlet_up_points": "azimutal_outlet_up",
    "leading_edge_index": "leading_edge_index",
    "trailing_edge_index": "trailing_edge_index",
}
for _key, _suffix in _HI_POINTS.items():
    _direct(
        f"blade/b2b.hi.{_key}",
        f"set_b2b_HI_topology_grid_point_number_{_suffix}",
        f"H&I {_key}",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type="int",
        priority="P2",
        stage="distribution",
        minimum=0 if _key.endswith("index") else 2,
        maximum=10001,
        topologies=("hi",),
        getter=None if _key in {"leading_edge_index", "trailing_edge_index"} else "auto",
    )
for _key, _method, _kind in (
    ("automatic_clustering_relaxation", "set_b2b_HI_topology_automatic_clustering_relaxation", "bool"),
    ("clustering_relaxation", "set_b2b_HI_topology_clustering_relaxation", "float"),
):
    _direct(
        f"blade/b2b.hi.{_key}",
        _method,
        f"H&I {_key}",
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type=_kind,
        priority="P2",
        stage="distribution",
        minimum=0 if _kind == "float" else None,
        maximum=1 if _kind == "float" else None,
        topologies=("hi",),
    )


# gap / partial-gap / fillet：只开放拓扑、点数、聚集与网格宽度。
_add(
    "gap/topology",
    "gap 网格拓扑",
    scope="gap",
    target_kind="gap",
    hierarchy=("row", "blade", "gap"),
    value_type="enum",
    setter=None,
    getter="get_topology_type",
    priority="P2",
    stage="topology",
    enum_values=("ho", "o", "o2h"),
    setter_by_value=(("ho", "set_topology_HO"), ("o", "set_topology_O"), ("o2h", "set_topology_O2H")),
)
for _key, _method, _label, _kind, _priority, _minimum, _maximum, _si in (
    ("clustering", "set_clustering", "gap 聚集", "float", "P1", 0, None, False),
    ("constant_cells", "set_constant_cell_number", "gap 常值单元数", "int", "P2", 0, 10001, False),
    ("spanwise_points", "set_number_of_points_in_spanwise_direction", "gap 展向点数", "int", "P0", 2, 10001, False),
):
    _direct(
        f"gap/{_key}",
        _method,
        _label,
        target_kind="gap",
        hierarchy=("row", "blade", "gap"),
        value_type=_kind,
        priority=_priority,
        stage="distribution",
        minimum=_minimum,
        maximum=_maximum,
        si_length=_si,
    )
for _key, _method, _label, _kind, _minimum, _maximum, _si in (
    ("leading_edge_points", "set_number_of_points_at_leading_edge", "partial-gap 前缘点数", "int", 2, 10001, False),
    ("trailing_edge_points", "set_number_of_points_at_trailing_edge", "partial-gap 尾缘点数", "int", 2, 10001, False),
    ("streamwise_cell_width", "set_streamwise_cell_width", "partial-gap 流向单元宽度", "float", 0, None, True),
    ("spanwise_cell_width", "set_spanwise_cell_width", "partial-gap 展向单元宽度", "float", 0, None, True),
    ("clustering_relaxation", "set_clustering_relaxation", "partial-gap 聚集松弛", "float", 0, 1, False),
    ("constant_cells", "set_constant_cell_number", "partial-gap 常值单元数", "int", 0, 10001, False),
    ("spanwise_points", "set_number_of_points_in_spanwise_direction", "partial-gap 展向点数", "int", 2, 10001, False),
):
    _direct(
        f"partial-gap/{_key}",
        _method,
        _label,
        target_kind="partial-gap",
        hierarchy=("row", "blade", "partial-gap"),
        value_type=_kind,
        priority="P2",
        stage="distribution",
        minimum=_minimum,
        maximum=_maximum,
        si_length=_si,
    )
for _key, _method, _label, _kind, _minimum, _maximum in (
    ("clustering", "set_clustering", "fillet 聚集", "float", 0, None),
    ("leading_edge_clustering", "set_clustering_at_leading_edge", "fillet 前缘聚集", "float", 0, None),
    ("trailing_edge_clustering", "set_clustering_at_trailing_edge", "fillet 尾缘聚集", "float", 0, None),
    ("spanwise_height_clustering", "set_clustering_from_spanwise_channel_height", "fillet 通道高度聚集", "float", 0, None),
    ("constant_cells", "set_constant_cell_number", "fillet 常值单元数", "int", 0, 10001),
    ("spanwise_points", "set_number_of_points_in_spanwise_direction", "fillet 展向点数", "int", 2, 10001),
    ("butterfly_topology", "set_butterfly_topology", "fillet 蝶形拓扑", "bool", None, None),
    ("butterfly_radial_points", "set_butterfly_radial_number_of_points", "fillet 蝶形径向点数", "int", 2, 10001),
):
    _direct(
        f"fillet/{_key}",
        _method,
        _label,
        target_kind="fillet",
        hierarchy=("row", "blade", "fillet"),
        value_type=_kind,
        priority="P2",
        stage="topology" if _key == "butterfly_topology" else "distribution",
        minimum=_minimum,
        maximum=_maximum,
        getter=None if _key == "spanwise_height_clustering" else "auto",
    )


# 行接口控制。interface 选择器取 inlet、outlet 或 outlet2。
for _key, _method, _getter, _label, _kind, _minimum, _maximum, _si in (
    ("streamwise_points", "streamwise_number_of_points", "get_streamwise_number_of_points", "接口流向点数", "int", 2, 10001, False),
    ("streamwise_index", "streamwise_index", "get_streamwise_index", "接口流向索引", "int", 0, 10001, False),
    ("b2b_control", "__bool_b2b_control__", "get_b2b_control", "接口 B2B 控制", "bool", None, None, False),
    ("geometry_fixed", "__bool_geometry_fixed__", "get_geometry_is_fixed", "固定接口几何", "bool", None, None, False),
    ("streamwise_cell_width", "cell_width_in_streamwise_direction", "get_cell_width_in_streamwise_direction", "接口流向单元宽度", "float", 0, None, True),
    ("clustering_relaxation_factor", "set_clustering_relaxation_factor", "get_clustering_relaxation_factor", "接口聚集松弛因子", "float", 0, 1, False),
    ("relative_location", "set_relative_location", "get_relative_location", "接口相对位置", "float", 0, 1, False),
    ("z_cst", "set_z_cst_shape", "get_z_cst_value", "接口 Z 常值形状", "float", None, None, False),
    ("r_cst", "set_r_cst_shape", "get_r_cst_value", "接口 R 常值形状", "float", 0, None, True),
):
    _direct(
        f"interface/{_key}",
        _method,
        _label,
        target_kind="interface",
        hierarchy=("row", "interface"),
        value_type=_kind,
        priority="P1" if _key in {"streamwise_points", "b2b_control", "streamwise_cell_width"} else "P2",
        stage="interface",
        minimum=_minimum,
        maximum=_maximum,
        si_length=_si,
        getter=_getter,
        setter_mode="interface_bool" if _method.startswith("__bool_") else "value",
    )
_add(
    "interface/clustering_relaxation_location",
    "接口聚集松弛位置",
    scope="interface",
    target_kind="interface",
    hierarchy=("row", "interface"),
    value_type="enum",
    setter="set_clustering_relaxation_location",
    getter="get_clustering_relaxation_location",
    priority="P2",
    stage="interface",
    enum_values=("none", "hub", "shroud", "mid_span"),
    value_map=(("none", "None"), ("hub", "Hub"), ("shroud", "Shroud"), ("mid_span", "Mid_Span")),
)
_add(
    "interface/shape",
    "接口形状",
    scope="interface",
    target_kind="interface",
    hierarchy=("row", "interface"),
    value_type="enum",
    setter=None,
    getter="get_shape",
    priority="P2",
    stage="interface",
    enum_values=("linear", "curvilinear", "user", "default"),
    setter_by_value=(("linear", "set_linear_shape"), ("curvilinear", "set_curvilinear_shape"), ("user", "set_user_defined_shape"), ("default", "set_default_shape")),
)
_add(
    "interface/reference_frame",
    "接口参考系",
    scope="interface",
    target_kind="interface",
    hierarchy=("row", "interface"),
    value_type="enum",
    setter=None,
    getter="get_reference_frame",
    priority="P2",
    stage="interface",
    enum_values=("relative", "absolute"),
    setter_by_value=(("relative", "set_reference_frame_relative"), ("absolute", "set_reference_frame_absolute")),
)


# 已存在的 endwall、snubber、孔/针肋等效果，只开放点数、聚集和优化参数。
for _key, _method, _label, _kind, _minimum, _maximum in (
    ("spanwise_points", "set_number_of_spanwise_points", "端壁展向点数", "int", 2, 10001),
    ("connected_layers", "set_number_of_connected_layers", "端壁连接层数", "int", 1, 10001),
    ("optimization_steps", "set_number_of_optimization_steps", "端壁优化步数", "int", 0, 100000),
    ("generation_type", "set_generation_type", "端壁网格生成类型", "int", 0, 20),
):
    _direct(
        f"endwall/{_key}",
        _method,
        _label,
        target_kind="endwall",
        hierarchy=("row", "endwall"),
        value_type=_kind,
        priority="P2",
        stage="existing_effect",
        minimum=_minimum,
        maximum=_maximum,
    )
for _key, _method, _label, _kind, _minimum, _maximum in (
    ("clustering", "set_clustering", "snubber 聚集", "float", 0, None),
    ("skin_expansion", "set_skin_expansion_ratio", "snubber skin 增长率", "float", 1, None),
    ("leading_edge_relative_control", "set_leading_edge_relative_control_distance", "snubber 前缘相对控制距离", "float", 0, 1),
    ("trailing_edge_relative_control", "set_trailing_edge_relative_control_distance", "snubber 尾缘相对控制距离", "float", 0, 1),
    ("spanwise_index", "set_spanwise_index", "snubber 展向索引", "int", 0, 10001),
    ("skin_points", "set_skin_number_of_points", "snubber skin 点数", "int", 2, 10001),
    ("upstream_points", "set_upstream_number_of_points", "snubber 上游点数", "int", 2, 10001),
    ("downstream_points", "set_downstream_number_of_points", "snubber 下游点数", "int", 2, 10001),
    ("spanwise_points", "set_spanwise_number_of_points", "snubber 展向点数", "int", 2, 10001),
    ("fillet_butterfly_radial_points", "set_fillet_butterfly_radial_number_of_points", "snubber fillet 蝶形径向点数", "int", 2, 10001),
):
    _direct(
        f"snubber/{_key}",
        _method,
        _label,
        target_kind="snubber",
        hierarchy=("row", "snubber"),
        value_type=_kind,
        priority="P2",
        stage="existing_effect",
        minimum=_minimum,
        maximum=_maximum,
    )
for _key, _method, _label in (
    ("leading_edge_points", "set_npts_near_leading_edge", "blade sheet 前缘附近点数"),
    ("trailing_edge_points", "set_npts_near_trailing_edge", "blade sheet 尾缘附近点数"),
):
    _direct(
        f"blade-sheet/{_key}",
        _method,
        _label,
        target_kind="blade-sheet",
        hierarchy=("row", "blade", "blade-sheet"),
        value_type="int",
        priority="P2",
        stage="existing_effect",
        minimum=2,
        maximum=10001,
    )
for _key, _method, _kind, _label, _minimum, _maximum, _si in (
    ("distribution_cell_length", "set_distribution_cell_length", "float", "停滞点分布单元长度", 0, None, True),
    ("distribution_absolute_distance", "set_distribution_absolute_distance", "float", "停滞点绝对控制距离", 0, None, True),
    ("distribution_relative_distance", "set_distribution_relative_distance", "float", "停滞点相对控制距离", 0, 1, False),
    ("constant_cells_percent", "set_percentage_cst_cell", "float", "停滞点常值单元比例", 0, 100, False),
    ("parametric_location", "set_parametric_location", "float", "停滞点参数位置", 0, 1, False),
):
    _direct(
        f"stagnation-point/{_key}",
        _method,
        _label,
        target_kind="stagnation-point",
        hierarchy=("row", "blade", "stagnation-point"),
        value_type=_kind,
        priority="P2",
        stage="existing_effect",
        minimum=_minimum,
        maximum=_maximum,
        si_length=_si,
    )
_add(
    "stagnation-point/distribution_type",
    "停滞点分布控制类型",
    scope="existing-effect",
    target_kind="stagnation-point",
    hierarchy=("row", "blade", "stagnation-point"),
    value_type="enum",
    setter=None,
    getter="get_distribution_type",
    priority="P2",
    stage="existing_effect",
    enum_values=("absolute_distance", "relative_distance", "cell_length"),
    setter_by_value=tuple((value, f"set_distribution_type_{value}") for value in ("absolute_distance", "relative_distance", "cell_length")),
)
_add(
    "stagnation-point/distribution_from_expansion_ratio",
    "停滞点膨胀比分布模式",
    scope="existing-effect",
    target_kind="stagnation-point",
    hierarchy=("row", "blade", "stagnation-point"),
    value_type="bool",
    setter=None,
    getter="get_distribution_type",
    priority="P2",
    stage="existing_effect",
    setter_by_value=(
        (True, "enable_distribution_from_expansion_ratio"),
        (False, "disable_distribution_from_expansion_ratio"),
    ),
)
_add(
    "stagnation-point/desired_expansion_ratio",
    "停滞点目标膨胀比",
    scope="existing-effect",
    target_kind="stagnation-point",
    hierarchy=("row", "blade", "stagnation-point"),
    value_type="float",
    setter="desired_expansion_ratio",
    getter=None,
    priority="P2",
    stage="existing_effect",
    minimum=1.0,
    setter_mode="value",
)
_EXISTING_LINE_FIELDS = (
    ("boundary_layer_points", "set_number_of_points_in_boundary_layer", "int", 2, 10001),
    ("streamwise_points", "set_number_of_points_streamwise", "int", 2, 10001),
    ("spanwise_points", "set_number_of_points_spanwise", "int", 2, 10001),
    ("streamwise_left_points", "set_number_of_points_streamwise_left", "int", 2, 10001),
    ("streamwise_right_points", "set_number_of_points_streamwise_right", "int", 2, 10001),
    ("spanwise_up_points", "set_number_of_points_spanwise_up", "int", 2, 10001),
    ("spanwise_down_points", "set_number_of_points_spanwise_down", "int", 2, 10001),
    ("inside_optimization_steps", "set_number_of_optimization_steps_inside_holes", "int", 0, 100000),
    ("around_optimization_steps", "set_number_of_optimization_steps_arround_holes", "int", 0, 100000),
    ("upstream_wake_length", "set_upstream_wake_length", "float", 0, None),
    ("downstream_wake_length", "set_downstream_wake_length", "float", 0, None),
    ("preserved_lower_layers", "set_preserved_layers_on_lower_side", "int", 0, 10001),
    ("preserved_upper_layers", "set_preserved_layers_on_upper_side", "int", 0, 10001),
    ("intersection_tolerance", "set_intersection_tolerance", "float", 0, None),
)
for _target_kind in ("holes-line", "endwall-holes-line", "pin-fins-line"):
    _hierarchy = {
        "holes-line": ("row", "blade", "holes-line"),
        "endwall-holes-line": ("row", "endwall", "endwall-holes-line"),
        "pin-fins-line": ("row", "blade", "pin-fins-line"),
    }[_target_kind]
    for _key, _method, _kind, _minimum, _maximum in _EXISTING_LINE_FIELDS:
        if _target_kind == "endwall-holes-line" and _key == "spanwise_points":
            continue
        _direct(
            f"{_target_kind}/{_key}",
            _method,
            f"{_target_kind} {_key}",
            target_kind=_target_kind,
            hierarchy=_hierarchy,
            value_type=_kind,
            priority="P2",
            stage="existing_effect",
            minimum=_minimum,
            maximum=_maximum,
        )
for _key, _method, _kind, _minimum, _maximum in (
    ("optimization_steps", "set_optimization_steps", "int", 0, 100000),
    ("streamwise_resolution", "set_streamwise_mesh_resolution", "int", 2, 10001),
    ("boundary_optimization_steps", "set_boundary_optimization_steps", "int", 0, 100000),
    ("hole_side_points", "set_number_of_points_on_hole_side", "int", 2, 10001),
    ("boundary_layer_points", "set_number_of_points_in_bnd_layer", "int", 2, 10001),
):
    _direct(
        f"basin-hole/{_key}",
        _method,
        f"basin hole {_key}",
        target_kind="basin-hole",
        hierarchy=("row", "blade", "basin-hole"),
        value_type=_kind,
        priority="P2",
        stage="existing_effect",
        minimum=_minimum,
        maximum=_maximum,
    )
for _key, _method, _kind, _minimum, _maximum in (
    ("maximum_expansion", "set_maximum_expansion_ratio", "float", 1, None),
    ("general_maximum_expansion", "set_maximum_expansion_ratio_general", "float", 1, None),
    ("boundary_layer_maximum_expansion", "set_maximum_expansion_ratio_in_boundary_layer", "float", 1, None),
    ("boundary_maximum_expansion", "set_maximum_expansion_ratio_along_boundary", "float", 1, None),
    ("clustering_relaxation_angle", "set_clustering_relaxation_angle", "float", 0, 180),
    ("solid_wall_clustering", "set_solid_wall_clustering", "float", 0, None),
    ("smoothing_steps", "set_number_of_smoothing_steps", "int", 0, 100000),
    ("constant_cells_percent", "set_constant_cells_percentage", "float", 0, 100),
    ("radial_expansion", "set_radial_expansion", "float", 1, None),
    ("far_field_smoothing_steps", "set_smoothing_steps_far_field", "int", 0, 100000),
    ("theta_deviation_propagation", "set_theta_deviation_propagation", "float", 0, 180),
    ("azimuthal_points", "set_azimuthal_number_of_points", "int", 2, 10001),
    ("periodic_fnmb_row_connection", "set_periodic_fnmb_row_connexion", "bool", None, None),
    ("periodic_fnmb_rs_connection", "set_periodic_fnmb_connexion_at_rs", "bool", None, None),
    ("periodic_rs_connection", "set_periodic_connexion_at_rs", "bool", None, None),
    ("matching_rs_connection", "set_matching_connexion_at_rs", "bool", None, None),
    ("h_topology_corners", "set_H_topology_around_corners", "bool", None, None),
    ("h_topology_thin_films", "set_H_topology_around_thin_films", "bool", None, None),
):
    _direct(
        f"existing-effect/{_key}",
        _method,
        f"已有 ZR 技术效果 {_key}",
        target_kind="existing-effect",
        hierarchy=("existing-effect",),
        value_type=_kind,
        priority="P2",
        stage="existing_effect",
        minimum=_minimum,
        maximum=_maximum,
    )


# 既有 solid body 的纯网格保持/分布控制。
_add(
    "solid-body/streamwise_distribution",
    "solid body 流向点分布方式",
    scope="existing-effect",
    target_kind="solid-body",
    hierarchy=("row", "blade", "solid-body"),
    value_type="enum",
    setter=None,
    getter="get_solid_body_streamwise_distribution_type",
    priority="P2",
    stage="existing_effect",
    enum_values=("same_as_blade", "adapted"),
    setter_by_value=(
        ("same_as_blade", "set_solid_body_streamwise_distribution_type_same_as_blade"),
        ("adapted", "set_solid_body_streamwise_distribution_type_adapted"),
    ),
)
for _key, _method, _label, _kind, _minimum, _maximum in (
    ("azimuthal_points", "set_solid_body_number_of_points_azimutal", "solid body 方位点数", "int", 2, 10001),
    ("b2b_relaxation", "set_solid_body_B2B_mesh_relaxation", "solid body B2B 松弛", "float", 0, 1),
    ("keep_blade_mesh", "set_solid_body_keep_blade_solid_mesh", "保留叶片固体网格", "bool", None, None),
    ("keep_mesh_around_skin", "set_solid_body_keep_mesh_around_skin", "保留 skin 周围网格", "bool", None, None),
    ("keep_cooling_channel_mesh", "set_solid_body_keep_mesh_in_cooling_channel", "保留冷却通道网格", "bool", None, None),
    ("keep_skin_mesh", "set_solid_body_keep_skin_mesh", "保留 skin 网格", "bool", None, None),
):
    _direct(
        f"solid-body/{_key}",
        _method,
        _label,
        target_kind="solid-body",
        hierarchy=("row", "blade", "solid-body"),
        value_type=_kind,
        priority="P2",
        stage="existing_effect",
        minimum=_minimum,
        maximum=_maximum,
    )


# 前缘/尾缘网格处理不修改输入几何，只改变拓扑解释和点分布。
for _key, _method, _label, _kind, _minimum, _maximum in (
    ("leading_blunt", "set_blunt_treatment_at_leading_edge", "前缘 blunt 处理", "bool", None, None),
    ("trailing_blunt", "set_blunt_treatment_at_trailing_edge", "尾缘 blunt 处理", "bool", None, None),
    ("leading_sharp", "set_sharp_treatment_at_leading_edge", "前缘 sharp 处理", "bool", None, None),
    ("trailing_sharp", "set_sharp_treatment_at_trailing_edge", "尾缘 sharp 处理", "bool", None, None),
    ("trailing_rounded", "set_rounded_treatment_at_trailing_edge", "尾缘 rounded 处理", "bool", None, None),
    ("leading_blend", "set_blend_treatment_at_leading_edge", "前缘 blend 权重", "int", 0, 1),
    ("trailing_blend", "set_blend_treatment_at_trailing_edge", "尾缘 blend 权重", "int", 0, 1),
):
    _direct(
        f"blade/edge_treatment.{_key}",
        _method,
        _label,
        target_kind="blade",
        hierarchy=("row", "blade"),
        value_type=_kind,
        priority="P2",
        stage="topology",
        minimum=_minimum,
        maximum=_maximum,
    )


# bypass/nozzle 内部拓扑控制。
for _key, _method, _getter, _label, _kind, _minimum, _maximum in (
    ("topology", "set_by_pass_configuration_topologyType", "get_by_pass_configuration_topologyType", "bypass 拓扑（0=H, 1=C）", "int", 0, 1),
    ("boundary_layer_relative_width", "set_by_pass_configuration_Bnd_layer_Width", "get_by_pass_configuration_Bnd_layer_Width", "bypass 边界层相对厚度", "float", 0, 1),
    ("nozzle_index", "set_by_pass_configuration_nozzle_index", "get_by_pass_configuration_nozzle_index", "bypass nozzle 网格索引", "int", 0, 10001),
    ("clustering", "set_by_pass_configuration_clustering", "get_by_pass_configuration_clustering", "bypass 聚集", "float", 0, None),
    ("spanwise_points", "set_by_pass_configuration_numberOfSpanwisePoints", "get_by_pass_configuration_numberOfSpanwisePoints", "bypass 展向点数", "int", 2, 10001),
    ("streamwise_points", "set_by_pass_configuration_numberOfStreamwisePoints", "get_by_pass_configuration_numberOfStreamwisePoints", "bypass 流向点数", "int", 2, 10001),
    ("relative_control_distance", "set_by_pass_configuration_relativeControlDistance", "get_by_pass_configuration_relativeControlDistance", "bypass 相对控制距离", "float", 0, 1),
    ("upstream_points", "set_by_pass_configuration_nup", "get_by_pass_configuration_nup", "bypass 上游点数", "int", 2, 10001),
    ("downstream_points", "set_by_pass_configuration_ndown", "get_by_pass_configuration_ndown", "bypass 下游点数", "int", 2, 10001),
    ("inlet_distribution_relaxation", "set_by_pass_configuration_relax_inlet_distribution", "get_by_pass_configuration_relax_inlet_distribution", "bypass 入口分布松弛", "float", 0, 1),
):
    _add(
        f"configuration/bypass.{_key}",
        _label,
        scope="configuration",
        target_kind="configuration",
        hierarchy=(),
        value_type=_kind,
        setter=_method,
        getter=_getter,
        priority="P2",
        stage="topology" if _key == "topology" else "distribution",
        minimum=_minimum,
        maximum=_maximum,
        not_applicable_when="项目不是 bypass/nozzle 拓扑",
    )


# WizardLETE：既有前/尾缘层的点分布和光顺控制。
for _key, _method, _label, _kind, _minimum, _maximum in (
    ("hub_clustering", "set_layer_hub_clustering", "LETE 层 hub 聚集", "float", 0, None),
    ("shroud_clustering", "set_layer_shroud_clustering", "LETE 层 shroud 聚集", "float", 0, None),
    ("layers", "set_layer_number", "LETE 层数", "int", 1, 10001),
    ("control_points", "set_layer_number_of_control_points", "LETE 层控制点数", "int", 2, 10001),
    ("constant_cells", "set_layer_number_of_constant_cells", "LETE 层常值单元数", "int", 0, 10001),
    ("hub_expansion", "set_hub_expansion", "LETE hub 增长率", "float", 1, None),
    ("shroud_expansion", "set_shroud_expansion", "LETE shroud 增长率", "float", 1, None),
    ("leading_chord_tolerance", "set_chord_tolerance_at_le", "LETE 前缘弦长容差", "float", 0, 1),
    ("trailing_chord_tolerance", "set_chord_tolerance_at_te", "LETE 尾缘弦长容差", "float", 0, 1),
    ("iteration_steps", "set_iteration_steps", "LETE 迭代步数", "int", 0, 100000),
):
    _direct(
        f"lete-wizard/{_key}",
        _method,
        _label,
        target_kind="lete-wizard",
        hierarchy=("row", "blade", "lete-wizard"),
        value_type=_kind,
        priority="P2",
        stage="existing_effect",
        minimum=_minimum,
        maximum=_maximum,
    )
_add(
    "lete-wizard/blade_type",
    "LETE 叶片角度类型",
    scope="existing-effect",
    target_kind="lete-wizard",
    hierarchy=("row", "blade", "lete-wizard"),
    value_type="enum",
    setter=None,
    getter="get_blade_type",
    priority="P2",
    stage="existing_effect",
    enum_values=("normal", "very_low_angle", "very_high_angle"),
    setter_by_value=(
        ("normal", "set_blade_normal_type"),
        ("very_low_angle", "set_blade_very_low_angle_type"),
        ("very_high_angle", "set_blade_very_high_angle_type"),
    ),
)


# endwall holes line 在通用 holes-line 基础上额外提供上下聚集和方位点数。
for _key, _method, _label, _kind, _minimum, _maximum in (
    ("up_clustering", "set_up_clustering_relaxation", "端壁孔线上侧聚集", "float", 0, None),
    ("down_clustering", "set_down_clustering_relaxation", "端壁孔线下侧聚集", "float", 0, None),
    ("azimuthal_points", "set_number_of_points_azimutal", "端壁孔线方位点数", "int", 2, 10001),
):
    _direct(
        f"endwall-holes-line/{_key}",
        _method,
        _label,
        target_kind="endwall-holes-line",
        hierarchy=("row", "endwall", "endwall-holes-line"),
        value_type=_kind,
        priority="P2",
        stage="existing_effect",
        minimum=_minimum,
        maximum=_maximum,
    )


# TechnologicalEffectZR 的专用边界层分布开关无 getter，setter 成功即 applied。
_direct(
    "existing-effect/special_boundary_layer_distribution",
    "set_special_distribution_for_boundary_layer",
    "已有 ZR 技术效果采用专用边界层分布",
    target_kind="existing-effect",
    hierarchy=("existing-effect",),
    value_type="bool",
    priority="P2",
    stage="existing_effect",
    getter=None,
)


CONTROL_REGISTRY: dict[str, ControlSpec] = {spec.key: spec for spec in _SPECS}
if len(CONTROL_REGISTRY) != len(_SPECS):
    raise RuntimeError("控制注册表存在重复键")
for _spec_item in CONTROL_REGISTRY.values():
    if _spec_item.priority not in PRIORITIES:
        raise RuntimeError(f"控制 {_spec_item.key} 的优先级无效")
    if _spec_item.stage not in STAGES:
        raise RuntimeError(f"控制 {_spec_item.key} 的应用阶段无效")
    if _spec_item.value_type not in {"bool", "int", "float", "enum", "tuple_int", "tuple_float"}:
        raise RuntimeError(f"控制 {_spec_item.key} 的类型无效")
    if not _spec_item.setter and not _spec_item.setter_by_value:
        raise RuntimeError(f"控制 {_spec_item.key} 未指定 setter")


# 控制项过滤器：按拓扑依赖关系分组，用于验证活动中的用例筛选。
# 键名与 PLAN.md "参数分层" 章节对齐。
TOPOLOGY_SELECTOR_KEY = "blade/b2b.topology"

COMMON_CORE_KEYS: frozenset[str] = frozenset(
    key for key, spec in CONTROL_REGISTRY.items()
    if not spec.topologies and key != TOPOLOGY_SELECTOR_KEY
)
COMMON_TOPOLOGY_KEYS: frozenset[str] = frozenset({TOPOLOGY_SELECTOR_KEY})
TOPOLOGY_DEFAULT_KEYS: frozenset[str] = frozenset(
    key for key, spec in CONTROL_REGISTRY.items()
    if spec.topologies == ("default",)
)
TOPOLOGY_HOH_KEYS: frozenset[str] = frozenset(
    key for key, spec in CONTROL_REGISTRY.items()
    if spec.topologies == ("hoh",)
)
TOPOLOGY_HI_KEYS: frozenset[str] = frozenset(
    key for key, spec in CONTROL_REGISTRY.items()
    if spec.topologies == ("hi",)
)
ALL_CONDITIONAL_KEYS: frozenset[str] = TOPOLOGY_DEFAULT_KEYS | TOPOLOGY_HOH_KEYS | TOPOLOGY_HI_KEYS
COMMON_KEYS: frozenset[str] = COMMON_CORE_KEYS | COMMON_TOPOLOGY_KEYS | ALL_CONDITIONAL_KEYS

# 拓扑值 -> 对应条件键集合的映射。
TOPOLOGY_KEY_MAP: dict[str, frozenset[str]] = {
    "default": TOPOLOGY_DEFAULT_KEYS,
    "hoh": TOPOLOGY_HOH_KEYS,
    "hi": TOPOLOGY_HI_KEYS,
}
# 条件键 -> 所需拓扑值的反向查找。
CONDITIONAL_KEY_TOPOLOGY: dict[str, str] = {}
for _topo, _keys in TOPOLOGY_KEY_MAP.items():
    for _key in _keys:
        CONDITIONAL_KEY_TOPOLOGY[_key] = _topo


# 通用控制策展集合。
#
# 注意：
# - 上面的 TOPOLOGY_*_KEYS / ALL_CONDITIONAL_KEYS 是完整运行时集合，负责拓扑
#   自动注入与冲突检测，绝不能因验证活动的筛选而缩减。
# - GENERAL_* 只回答“哪些控制适合作为常规叶轮机械网格控制进行 Rotor37
#   实机验证”，因此使用显式白名单，避免未来新增几何专用控制时被自动纳入。
GENERAL_CONFIGURATION_KEYS: frozenset[str] = frozenset(
    {
        "configuration/grid_levels",
        "configuration/support_curve_control_points",
    }
)
GENERAL_WIZARD_KEYS: frozenset[str] = frozenset(
    {
        "wizard/first_cell_width",
        "wizard/full_matching",
        "wizard/grid_level",
        "wizard/spanwise_paths",
    }
)
GENERAL_ROW_KEYS: frozenset[str] = frozenset(
    {
        "row/clustering",
        "row/downstream.relaxation",
        "row/downstream.untwist",
        "row/downstream.untwist_location",
        "row/enforce_blade_wall_cell_width",
        "row/flow_path.constant_cells",
        "row/flow_path.control_points",
        "row/flow_path.distribution_smoothing_steps",
        "row/flow_path.hub_clustering",
        "row/flow_path.intermediate_points",
        "row/flow_path.number",
        "row/flow_path.shroud_clustering",
        "row/flow_path.smoothing_steps",
        "row/mesh_level",
        "row/optimization.boundary_steps",
        "row/optimization.freeze_skin",
        "row/optimization.full_multigrid_steps",
        "row/optimization.multigrid",
        "row/optimization.nmb",
        "row/optimization.orthogonality",
        "row/optimization.skewness",
        "row/optimization.steps",
        "row/optimization.straight_boundary",
        "row/optimization.wake",
        "row/span_interpolation",
        "row/streamwise_weight",
        "row/target_points",
        "row/upstream.relaxation",
        "row/upstream.untwist",
        "row/upstream.untwist_location",
    }
)
GENERAL_EDGE_TREATMENT_KEYS: frozenset[str] = frozenset(
    {
        "blade/edge_treatment.leading_blend",
        "blade/edge_treatment.leading_blunt",
        "blade/edge_treatment.leading_sharp",
        "blade/edge_treatment.trailing_blend",
        "blade/edge_treatment.trailing_blunt",
        "blade/edge_treatment.trailing_rounded",
        "blade/edge_treatment.trailing_sharp",
    }
)
GENERAL_INTERFACE_KEYS: frozenset[str] = frozenset(
    {
        "interface/streamwise_cell_width",
    }
)
GENERAL_STAGNATION_POINT_KEYS: frozenset[str] = frozenset(
    {
        "stagnation-point/constant_cells_percent",
        "stagnation-point/desired_expansion_ratio",
        "stagnation-point/distribution_absolute_distance",
        "stagnation-point/distribution_cell_length",
        "stagnation-point/distribution_from_expansion_ratio",
        "stagnation-point/distribution_relative_distance",
        "stagnation-point/distribution_type",
        "stagnation-point/parametric_location",
    }
)

GENERAL_CORE_KEYS: frozenset[str] = (
    GENERAL_CONFIGURATION_KEYS
    | GENERAL_WIZARD_KEYS
    | GENERAL_ROW_KEYS
    | GENERAL_EDGE_TREATMENT_KEYS
    | GENERAL_INTERFACE_KEYS
    | GENERAL_STAGNATION_POINT_KEYS
)
GENERAL_TOPOLOGY_KEYS: frozenset[str] = frozenset({TOPOLOGY_SELECTOR_KEY})
GENERAL_DEFAULT_KEYS: frozenset[str] = TOPOLOGY_DEFAULT_KEYS - frozenset(
    {
        "blade/b2b.default.boundary_layer_gap_points",
    }
)
GENERAL_HOH_KEYS: frozenset[str] = TOPOLOGY_HOH_KEYS - frozenset(
    {
        "blade/b2b.hoh.gap_azimuthal_h_points",
        "blade/b2b.hoh.gap_azimuthal_o_points",
        "blade/b2b.hoh.gap_leading_addition",
        "blade/b2b.hoh.gap_leading_ratio",
        "blade/b2b.hoh.gap_matching",
        "blade/b2b.hoh.gap_streamwise_h_points",
        "blade/b2b.hoh.gap_trailing_addition",
        "blade/b2b.hoh.gap_trailing_ratio",
    }
)
GENERAL_HI_KEYS: frozenset[str] = TOPOLOGY_HI_KEYS
GENERAL_CONTROL_KEYS: frozenset[str] = (
    GENERAL_CORE_KEYS
    | GENERAL_TOPOLOGY_KEYS
    | GENERAL_DEFAULT_KEYS
    | GENERAL_HOH_KEYS
    | GENERAL_HI_KEYS
)


def _general_control_exclusion_reason(key: str, spec: ControlSpec) -> str:
    """返回未进入通用验证集合的逐键排除理由。"""

    if key == "row/low_memory_usage":
        return "运行资源策略，不应改变最终网格"
    if key == "row/bladeless_mesh":
        return "仅适用于无叶片通道，会改变物理计算域"
    if key == "row/downstream.before_nozzle_relaxation":
        return "依赖 nozzle 几何结构"
    if key.startswith("row/gap.") or key.startswith("row/optimization.gap_"):
        return "依赖实际 gap 几何结构"
    if key.startswith("configuration/bypass."):
        return "依赖 bypass/nozzle 项目结构"
    if key.startswith("configuration/inlet_bulb.") or key.startswith("configuration/outlet_bulb."):
        return "依赖 inlet/outlet bulb 几何结构"
    if key.startswith("wizard/far_field_"):
        return "依赖远场几何结构"
    if key == "wizard/blade_tip_rounded_topology":
        return "依赖圆钝叶尖几何结构"
    if key == "blade/b2b.default.boundary_layer_gap_points" or key.startswith("blade/b2b.hoh.gap_"):
        return "依赖实际 gap 几何结构"
    if spec.target_kind == "interface":
        return "依赖内部转静接口或会改变接口物理几何；Rotor37 通用入口/出口仅保留流向单元宽度"

    target_reasons = {
        "acoustic-wizard": "依赖声学行与远场结构",
        "basin-hole": "依赖既有 basin-hole 实体",
        "blade-sheet": "依赖既有 blade-sheet 实体",
        "endwall": "依赖既有端壁技术效果实体及显式生成动作",
        "endwall-holes-line": "依赖既有端壁孔列实体",
        "existing-effect": "依赖既有 ZR/3D 技术效果实体及激活动作",
        "fillet": "依赖实际 fillet 几何结构",
        "gap": "依赖实际 gap 几何结构",
        "holes-line": "依赖既有叶片孔列实体",
        "lete-wizard": "依赖 LETE wizard 的显式 generate 激活动作",
        "partial-gap": "依赖实际 partial-gap 几何结构",
        "pin-fins-line": "依赖既有冷却通道与 pin-fins 实体",
        "snubber": "依赖实际 snubber 几何结构",
        "solid-body": "依赖既有叶片固体域",
    }
    if spec.target_kind in target_reasons:
        return target_reasons[spec.target_kind]
    return "不满足 Rotor37 通用控制筛选条件"


GENERAL_CONTROL_EXCLUSIONS: dict[str, str] = {
    key: _general_control_exclusion_reason(key, spec)
    for key, spec in CONTROL_REGISTRY.items()
    if key not in GENERAL_CONTROL_KEYS
}

# 已确认存在于 17.1 API、但因依赖特定几何实体而不纳入 Rotor37 通用
# campaign 的纯网格 API。该清单用于扩展 API 审计报告，不参与运行时调用。
GENERAL_API_ONLY_EXCLUSIONS: dict[str, str] = {
    "EndWall.enable_multigrid_optimization": "依赖既有端壁技术效果实体",
    "EndWall.disable_multigrid_optimization": "依赖既有端壁技术效果实体",
    "HolesLine.enable_skewness_control_inside_holes": "依赖既有叶片孔列实体",
    "HolesLine.disable_skewness_control_inside_holes": "依赖既有叶片孔列实体",
    "HolesLine.enable_skewness_control_arround_holes": "依赖既有叶片孔列实体",
    "HolesLine.disable_skewness_control_arround_holes": "依赖既有叶片孔列实体",
    "EndWallHolesLine.enable_skewness_control_inside_holes": "依赖既有端壁孔列实体",
    "EndWallHolesLine.disable_skewness_control_inside_holes": "依赖既有端壁孔列实体",
    "EndWallHolesLine.enable_skewness_control_arround_holes": "依赖既有端壁孔列实体",
    "EndWallHolesLine.disable_skewness_control_arround_holes": "依赖既有端壁孔列实体",
    "PinFinsLine.enable_skewness_control_inside_holes": "依赖既有冷却通道与 pin-fins 实体",
    "PinFinsLine.disable_skewness_control_inside_holes": "依赖既有冷却通道与 pin-fins 实体",
    "PinFinsLine.enable_skewness_control_arround_holes": "依赖既有冷却通道与 pin-fins 实体",
    "PinFinsLine.disable_skewness_control_arround_holes": "依赖既有冷却通道与 pin-fins 实体",
}

# 仅描述“已显式提供多个控制时”的依赖和值要求；除 B2B topology 外，
# 普通 CLI 不会据此自动注入控制。campaign runner 使用该映射构造匹配上下文，
# resolve_control_requests() 则使用键依赖保证同阶段调用顺序正确。
# 值为 ">N" 字符串时表示启用谓词（显式值大于 N 才满足，如 optimization.steps
# 必须为正），其余值按等值判断；具体实验取值（200 步、9 个喉部点等）由
# campaign 采样矩阵决定，不作为通用前置条件。
CONTROL_PREREQUISITES: dict[str, tuple[tuple[str, Any], ...]] = {
    "blade/edge_treatment.leading_blend": (
        ("blade/edge_treatment.leading_blunt", True),
    ),
    "blade/edge_treatment.trailing_blend": (
        ("blade/edge_treatment.trailing_blunt", True),
    ),
    "row/downstream.untwist_location": (("row/downstream.untwist", True),),
    "row/upstream.untwist_location": (("row/upstream.untwist", True),),
    "row/target_points": (("row/mesh_level", "user"),),
    "row/optimization.freeze_skin": (("row/optimization.steps", ">0"),),
    "row/optimization.full_multigrid_steps": (("row/optimization.multigrid", True),),
    "row/optimization.nmb": (
        ("blade/b2b.default.periodicity", "non_matching"),
        ("row/optimization.steps", ">0"),
    ),
    "row/optimization.orthogonality": (("row/optimization.steps", ">0"),),
    "row/optimization.skewness": (("row/optimization.steps", ">0"),),
    "row/optimization.wake": (
        ("blade/b2b.default.wake_control", True),
        ("row/optimization.steps", ">0"),
    ),
    "blade/b2b.default.azimuthal_inlet_down_points": (
        ("blade/b2b.default.type", "rounded_azimuthal"),
    ),
    "blade/b2b.default.azimuthal_inlet_points": (
        ("blade/b2b.default.type", "rounded_azimuthal"),
    ),
    "blade/b2b.default.azimuthal_inlet_up_points": (
        ("blade/b2b.default.type", "rounded_azimuthal"),
    ),
    "blade/b2b.default.azimuthal_outlet_down_points": (
        ("blade/b2b.default.type", "rounded_azimuthal"),
    ),
    "blade/b2b.default.azimuthal_outlet_points": (
        ("blade/b2b.default.type", "rounded_azimuthal"),
    ),
    "blade/b2b.default.azimuthal_outlet_up_points": (
        ("blade/b2b.default.type", "rounded_azimuthal"),
    ),
    "blade/b2b.default.fix_inlet_angle": (("blade/b2b.default.type", "streamwise"),),
    "blade/b2b.default.fix_inlet_mesh": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.free_inlet_angle", False),
    ),
    "blade/b2b.default.fix_outlet_angle": (("blade/b2b.default.type", "streamwise"),),
    "blade/b2b.default.fix_outlet_mesh": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.free_outlet_angle", False),
    ),
    "blade/b2b.default.free_inlet_angle": (("blade/b2b.default.type", "streamwise"),),
    "blade/b2b.default.free_outlet_angle": (("blade/b2b.default.type", "streamwise"),),
    "blade/b2b.default.high_stagger_detection": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.high_stagger_optimization", True),
    ),
    "blade/b2b.default.high_stagger_optimization": (
        ("blade/b2b.default.type", "streamwise"),
    ),
    "blade/b2b.default.inlet_angle": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.free_inlet_angle", False),
    ),
    "blade/b2b.default.inlet_stagger": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.high_stagger_optimization", True),
        ("blade/b2b.default.high_stagger_detection", False),
    ),
    "blade/b2b.default.outlet_angle": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.free_outlet_angle", False),
    ),
    "blade/b2b.default.outlet_stagger": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.high_stagger_optimization", True),
        ("blade/b2b.default.high_stagger_detection", False),
    ),
    "blade/b2b.default.periodicity": (("blade/b2b.default.type", "streamwise"),),
    "blade/b2b.default.streamwise_inlet_points": (
        ("blade/b2b.default.type", "streamwise"),
    ),
    "blade/b2b.default.streamwise_outlet_points": (
        ("blade/b2b.default.type", "streamwise"),
    ),
    "blade/b2b.default.streamwise_pressure_points": (
        ("blade/b2b.default.type", "streamwise"),
    ),
    "blade/b2b.default.streamwise_suction_points": (
        ("blade/b2b.default.type", "streamwise"),
    ),
    "blade/b2b.default.throat_points": (("blade/b2b.default.type", "streamwise"),),
    "blade/b2b.default.throat_projection_type": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.throat_points", ">0"),
    ),
    "blade/b2b.default.throat_inlet_relaxation": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.throat_points", ">0"),
    ),
    "blade/b2b.default.throat_outlet_relaxation": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.throat_points", ">0"),
    ),
    "blade/b2b.default.wake_control": (("blade/b2b.default.type", "streamwise"),),
    "blade/b2b.default.wake_deviation_angle": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.wake_control", True),
    ),
    "blade/b2b.default.wake_prolongation": (
        ("blade/b2b.default.type", "streamwise"),
        ("blade/b2b.default.wake_control", True),
    ),
    "blade/b2b.hoh.inlet_extension_location": (
        ("blade/b2b.hoh.inlet_extension", True),
    ),
    "blade/b2b.hoh.inlet_extension_streamwise_points": (
        ("blade/b2b.hoh.inlet_extension", True),
    ),
    "blade/b2b.hoh.inlet_extension_type": (
        ("blade/b2b.hoh.inlet_extension", True),
    ),
    "blade/b2b.hoh.outlet_extension_location": (
        ("blade/b2b.hoh.outlet_extension", True),
    ),
    "blade/b2b.hoh.outlet_extension_streamwise_points": (
        ("blade/b2b.hoh.outlet_extension", True),
    ),
    "blade/b2b.hoh.outlet_extension_type": (
        ("blade/b2b.hoh.outlet_extension", True),
    ),
    "blade/b2b.hoh.leading_edge_absolute_distance": (
        ("blade/b2b.hoh.leading_edge_control_type", "absolute_distance"),
    ),
    "blade/b2b.hoh.leading_edge_cell_length": (
        ("blade/b2b.hoh.leading_edge_control_type", "cell_length"),
    ),
    "blade/b2b.hoh.leading_edge_relative_distance": (
        ("blade/b2b.hoh.leading_edge_control_type", "relative_distance"),
    ),
    "blade/b2b.hoh.trailing_edge_absolute_distance": (
        ("blade/b2b.hoh.trailing_edge_control_type", "absolute_distance"),
    ),
    "blade/b2b.hoh.trailing_edge_cell_length": (
        ("blade/b2b.hoh.trailing_edge_control_type", "cell_length"),
    ),
    "blade/b2b.hoh.trailing_edge_relative_distance": (
        ("blade/b2b.hoh.trailing_edge_control_type", "relative_distance"),
    ),
    "blade/b2b.hi.clustering_relaxation": (
        ("blade/b2b.hi.automatic_clustering_relaxation", False),
    ),
    "blade/b2b.hi.h_inlet": (("blade/b2b.hi.h_full", False),),
    "blade/b2b.hi.h_outlet": (("blade/b2b.hi.h_full", False),),
    "blade/b2b.hi.streamwise_blade_inlet_pressure_points": (
        ("blade/b2b.hi.h_inlet", True),
    ),
    "blade/b2b.hi.streamwise_blade_inlet_suction_points": (
        ("blade/b2b.hi.h_inlet", True),
    ),
    "blade/b2b.hi.streamwise_blade_outlet_pressure_points": (
        ("blade/b2b.hi.h_outlet", True),
    ),
    "blade/b2b.hi.streamwise_blade_outlet_suction_points": (
        ("blade/b2b.hi.h_outlet", True),
    ),
    "blade/b2b.hi.streamwise_blade_pressure_points": (
        ("blade/b2b.hi.h_full", True),
    ),
    "blade/b2b.hi.streamwise_blade_suction_points": (
        ("blade/b2b.hi.h_full", True),
    ),
    "blade/b2b.hi.streamwise_previous_blade_suction_points": (
        ("blade/b2b.hi.h_full", True),
    ),
    "stagnation-point/desired_expansion_ratio": (
        ("stagnation-point/distribution_from_expansion_ratio", True),
    ),
    "stagnation-point/distribution_absolute_distance": (
        ("stagnation-point/distribution_from_expansion_ratio", False),
        ("stagnation-point/distribution_type", "absolute_distance"),
    ),
    "stagnation-point/distribution_cell_length": (
        ("stagnation-point/distribution_from_expansion_ratio", False),
        ("stagnation-point/distribution_type", "cell_length"),
    ),
    "stagnation-point/distribution_relative_distance": (
        ("stagnation-point/distribution_from_expansion_ratio", False),
        ("stagnation-point/distribution_type", "relative_distance"),
    ),
    "stagnation-point/distribution_type": (
        ("stagnation-point/distribution_from_expansion_ratio", False),
    ),
}


def prerequisite_satisfied(expected: Any, value: Any) -> bool:
    """判断显式控制值是否满足前置条件。

    expected 为 ``">N"`` 字符串时按数值比较，表示启用谓词（如
    ``optimization.steps > 0``、``throat_points > 0``）；其余值（布尔、
    枚举、具体数值）按等值判断。缺失或已清除的值（None）不满足任何前置条件。
    """

    if value is None:
        return False
    if isinstance(expected, str) and expected.startswith(">"):
        return value > int(expected[1:])
    return value == expected


def _control_dependency_depth(key: str, trail: tuple[str, ...] = ()) -> int:
    """计算控制键在同阶段调用中的依赖深度，并检测循环依赖。"""

    if key in trail:
        cycle = " -> ".join(trail + (key,))
        raise RuntimeError(f"控制前置条件存在循环：{cycle}")
    prerequisites = CONTROL_PREREQUISITES.get(key, ())
    if not prerequisites:
        return 0
    same_stage_depths = [
        _control_dependency_depth(prerequisite_key, trail + (key,))
        for prerequisite_key, _ in prerequisites
        if (
            prerequisite_key in CONTROL_REGISTRY
            and CONTROL_REGISTRY[prerequisite_key].stage == CONTROL_REGISTRY[key].stage
        )
    ]
    return 1 + max(same_stage_depths, default=-1)


CONTROL_DEPENDENCY_DEPTH: dict[str, int] = {
    key: _control_dependency_depth(key) for key in CONTROL_REGISTRY
}

if GENERAL_CONTROL_KEYS | frozenset(GENERAL_CONTROL_EXCLUSIONS) != frozenset(CONTROL_REGISTRY):
    raise RuntimeError("通用控制集合与排除集合未完整覆盖注册表")
if GENERAL_CONTROL_KEYS & frozenset(GENERAL_CONTROL_EXCLUSIONS):
    raise RuntimeError("通用控制集合与排除集合存在重叠")
for _dependent_key, _prerequisites in CONTROL_PREREQUISITES.items():
    if _dependent_key not in CONTROL_REGISTRY:
        raise RuntimeError(f"未知依赖控制键：{_dependent_key}")
    for _prerequisite_key, _required_value in _prerequisites:
        if _prerequisite_key not in CONTROL_REGISTRY:
            raise RuntimeError(f"未知前置控制键：{_prerequisite_key}")
        if STAGE_ORDER[CONTROL_REGISTRY[_prerequisite_key].stage] > STAGE_ORDER[CONTROL_REGISTRY[_dependent_key].stage]:
            raise RuntimeError(
                f"前置控制 {_prerequisite_key} 的阶段晚于 {_dependent_key}"
            )


# setter 审计：映射项来自注册表，排除规则只用于审计，绝不参与运行时调用。
CONTROL_TARGET_OWNERS: dict[str, tuple[str, ...]] = {
    "configuration": ("",),
    "row": ("Row",),
    "blade": ("Blade",),
    "gap": ("Gap",),
    "partial-gap": ("PartialGap",),
    "fillet": ("Fillet",),
    "snubber": ("Snubber",),
    "blade-sheet": ("BladeSheet",),
    "stagnation-point": ("StagnationPoint",),
    "interface": ("RSInterface",),
    "holes-line": ("HolesLine",),
    "basin-hole": ("BasinHole",),
    "endwall-holes-line": ("EndWallHolesLine",),
    "endwall": ("EndWall",),
    "pin-fins-line": ("PinFinsLine",),
    "wizard": ("RowWizard",),
    "lete-wizard": ("WizardLETE",),
    "acoustic-wizard": ("RowAcousticWizard",),
    "existing-effect": ("TechnologicalEffectZR", "TechnologicalEffect3D"),
    # solid_body() 仅用于确认实体已存在；这些 setter/getter 位于 Blade。
    "solid-body": ("Blade",),
}

MAPPED_SETTERS: dict[str, tuple[str, ...]] = {}
MAPPED_SETTERS_BY_OWNER: dict[tuple[str, str], tuple[str, ...]] = {}
for _spec_item in CONTROL_REGISTRY.values():
    for _method in tuple(method for method in (_spec_item.setter,) if method) + tuple(
        method for _, method in _spec_item.setter_by_value
    ):
        MAPPED_SETTERS[_method] = tuple(sorted(set(MAPPED_SETTERS.get(_method, ()) + (_spec_item.key,))))
        for _owner in CONTROL_TARGET_OWNERS[_spec_item.target_kind]:
            _owner_key = (_owner, _method)
            MAPPED_SETTERS_BY_OWNER[_owner_key] = tuple(
                sorted(set(MAPPED_SETTERS_BY_OWNER.get(_owner_key, ()) + (_spec_item.key,)))
            )

_ALL_KNOWN_SETTERS: set[str] = set()
for _spec_item in CONTROL_REGISTRY.values():
    if _spec_item.setter:
        _ALL_KNOWN_SETTERS.add(_spec_item.setter)
    for _, _method in _spec_item.setter_by_value:
        _ALL_KNOWN_SETTERS.add(_method)

# 已知的运行时全局 helper 函数（C 扩展暴露，源码中无 def 定义，
# 但在 AutoGrid Python 环境中可直接调用）。
_RUNTIME_GLOBALS: frozenset[str] = frozenset({"set_row_properties_"})


EXCLUDED_SETTERS: dict[str, str] = {
    "a5_set_configuration_units": "项目单位属性，不是网格控制",
    "a5_set_cascade_project": "物理项目类型",
    "set_turbomachinery_axis": "物理几何坐标系",
    "a5_set_import_geometry_rotation_axis": "导入几何变换",
    "Row.set_periodicity": "周期数会改变物理计算域",
    "Row.set_number_of_periodicity_geometry": "几何周期数",
    "Row.set_rotation_speed": "物理转速",
    "Row.set_number_of_meshed_passages": "计算域通道数量",
    "Row.set_number_of_meshed_passage": "旧别名：计算域通道数量",
    "Row.set_swap_blade_patches_name": "边界命名而非网格控制",
    "Blade.set_name": "实体命名",
    "Row.set_name": "实体命名",
    "Gap.set_width_at_leading_edge": "物理 gap 尺寸",
    "Gap.set_width_at_trailing_edge": "物理 gap 尺寸",
    "PartialGap.set_chord_length_at_leading_edge": "物理 partial-gap 几何",
    "PartialGap.set_chord_length_at_trailing_edge": "物理 partial-gap 几何",
    "PartialGap.set_width_at_leading_edge": "物理 partial-gap 几何",
    "PartialGap.set_width_at_trailing_edge": "物理 partial-gap 几何",
    "PartialGap.set_cylinder_diameter": "物理 partial-gap 几何",
    "PartialGap.set_cylinder_axis": "物理 partial-gap 几何",
    "PartialGap.set_cylinder_origin": "物理 partial-gap 几何",
    "Fillet.set_radius_at_leading_edge": "物理 fillet 尺寸",
    "Fillet.set_radius_at_trailing_edge": "物理 fillet 尺寸",
    "Fillet.set_minimum_angle": "物理 fillet 几何定义",
    "Fillet.set_create_geometry": "创建几何",
    "Fillet.set_constant_radius": "物理 fillet 几何定义",
    "RowWizard.set_reset_hub_shroud": "重建物理端壁",
    "RowWizard.set_R_hub": "物理通道截断位置",
    "RowWizard.set_R_shroud": "物理通道截断位置",
    "RowWizard.set_R_far_field": "物理远场范围",
    "RowWizard.set_hub_location": "重复别名：物理通道截断位置",
    "RowWizard.set_shroud_location": "重复别名：物理通道截断位置",
    "RowWizard.set_upstream_channel_location": "物理域入口位置",
    "RowWizard.set_downstream_channel_location": "物理域出口位置",
    "RowWizard.set_hub_gap_width_at_leading_edge": "物理 gap 尺寸",
    "RowWizard.set_hub_gap_width_at_trailing_edge": "物理 gap 尺寸",
    "RowWizard.set_tip_gap_width_at_leading_edge": "物理 gap 尺寸",
    "RowWizard.set_tip_gap_width_at_trailing_edge": "物理 gap 尺寸",
    "RSInterface.set_name": "实体命名",
    "RSInterface.set_external_curve": "外部曲面链接",
    "EndWall.set_width": "物理端壁效果尺寸",
    "TechnologicalEffectZR.set_rotating_boundaries_rotation_speed": "物理转速",
}
EXCLUDED_SETTERS.update(
    {
        "set_active_control_layer_index": "交互式活动层选择，不是持久网格参数",
        "Row.set_row_interpolation_spacing": "重复旧别名；规范键为 row/span_interpolation",
        "Row.set_non_axisymmetric_hub": "非轴对称物理端壁映射",
        "Row.set_non_axisymmetric_hub_projection_type_face_normal": "物理端壁投影/变形",
        "Row.set_non_axisymmetric_hub_projection_type_spanwise_grid_line": "物理端壁投影/变形",
        "Row.set_non_axisymmetric_shroud": "非轴对称物理端壁映射",
        "Row.set_non_axisymmetric_shroud_projection_type_face_normal": "物理端壁投影/变形",
        "Row.set_non_axisymmetric_shroud_projection_type_spanwise_grid_line": "物理端壁投影/变形",
        "Row.set_non_axisymmetric_tip_gap": "非轴对称物理 gap 映射",
        "Blade.set_solid_body_configuration": "固体物理域配置",
        "Blade.set_solid_body_geometry_from_geomTurbo_file": "替换固体物理几何",
        "Blade.set_geometry_control_points_redistribution": "输入几何重分布",
        "Blade.set_geometry_control_points_redistribution_npts_at_le": "输入几何重分布",
        "Blade.set_geometry_control_points_redistribution_npts_on_middle": "输入几何重分布",
        "Blade.set_geometry_control_points_redistribution_cst_cells_on_middle": "输入几何重分布",
        "Blade.set_geometry_control_points_redistribution_npts_at_te": "输入几何重分布",
        "Blade.set_geometry_control_points_redistribution_spacing_at_le": "输入几何重分布",
        "Blade.set_geometry_control_points_redistribution_spacing_at_te": "输入几何重分布",
        "Blade.set_expansion_treatment": "叶片物理几何扩展处理",
        "Blade.set_shroud_treatment": "shroud 物理几何扩展处理",
        "Blade.set_hub_treatment": "hub 物理几何扩展处理",
        "Blade.set_b2b_default_topology_enable_high_staggered_optimization": "重复便捷别名；使用 blade/b2b.default.high_stagger_optimization",
        "Blade.set_b2b_default_topology_disable_high_staggered_optimization": "重复便捷别名；使用 blade/b2b.default.high_stagger_optimization",
        "Blade.set_b2b_default_topology_disable_high_staggered_detection": "重复便捷别名；使用 blade/b2b.default.high_stagger_detection",
        "Blade.set_b2b_default_topology_enable_high_staggered_detection": "重复便捷别名；使用 blade/b2b.default.high_stagger_detection",
        "Blade.set_b2b_default_topology_enable_leading_edge_zcstline": "重复便捷别名；使用 blade/b2b.default.leading_edge_zcst",
        "Blade.set_b2b_default_topology_disable_leading_edge_zcstline": "重复便捷别名；使用 blade/b2b.default.leading_edge_zcst",
        "Blade.set_b2b_default_topology_enable_trailing_edge_zcstline": "重复便捷别名；使用 blade/b2b.default.trailing_edge_zcst",
        "Blade.set_b2b_default_topology_disable_trailing_edge_zcstline": "重复便捷别名；使用 blade/b2b.default.trailing_edge_zcst",
        "Blade.set_b2b_default_topology_enable_wake_control": "重复便捷别名；使用 blade/b2b.default.wake_control",
        "Blade.set_b2b_default_topology_disable_wake_control": "重复便捷别名；使用 blade/b2b.default.wake_control",
        "Blade.set_b2b_default_topology_enable_wake_prolongation": "重复便捷别名；使用 blade/b2b.default.wake_prolongation",
        "Gap.set_non_axisymmetric_hub": "非轴对称物理 gap 映射",
        "BladeSheet.set_distance_from_leading_edge": "已有 sheet 的物理位置",
        "BladeSheet.set_distance_from_trailing_edge": "已有 sheet 的物理位置",
        "TechnologicalEffectZR.set_parameters": "重复聚合接口；使用逐项 existing-effect 控制",
        "TechnologicalEffectZR.set_tolerance": "几何连接容差",
        "FlowPathSubdivision.set_end_parameter": "低层重复接口；使用 interface/relative_location",
        "BasicCurve.set_discretisation": "输入几何曲线离散，不属于生成后网格控制",
        "BasicCurve.set_rotating_property": "物理旋转属性",
        "HolesLine.set_location_to_blade_upper_side": "孔实体物理位置",
        "HolesLine.set_location_to_blade_lower_side": "孔实体物理位置",
        "HolesLine.set_hole_line_shape_link_to_next_hole_line_shape": "实体曲面链接",
        "HolesLine.set_hole_line_shape_link_to_previous_hole_line_shape": "实体曲面链接",
        "BasinHole.set_parametric_azimutal_deviation": "孔实体物理位置",
        "EndWallHolesLine.set_hole_line_shape_link_to_next_hole_line_shape": "实体曲面链接",
        "EndWallHolesLine.set_hole_line_shape_link_to_previous_hole_line_shape": "实体曲面链接",
        "PinFins.set_diameter2": "针肋物理尺寸",
        "PinFinsLine.set_diameter2": "针肋物理尺寸",
        "PinFinsLine.set_hole_line_shape_link_to_next_hole_line_shape": "实体曲面链接",
        "PinFinsLine.set_hole_line_shape_link_to_previous_hole_line_shape": "实体曲面链接",
        "RowWizard.set_hub_cut_relative_value": "重复别名：物理通道截断位置",
        "RowWizard.set_tip_cut_relative_value": "重复别名：物理通道截断位置",
        "RowWizard.set_expansion_cst_cell_percentage_number": "重复旧别名；使用 wizard/far_field_constant_cells_percent",
        "RowWizard.set_expansion_number_of_layer": "重复旧别名；使用 wizard/far_field_spanwise_paths",
        "RowWizard.set_number_of_layer": "重复旧别名；使用 wizard/spanwise_paths",
        "RowWizard.set_cst_cell_percentage_number": "legacy 空操作接口",
        "WizardLETE.set_active_layer": "交互式活动层选择，不是持久网格参数",
    }
)

AUDIT_EXCLUSION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|\.)set_.*(?:display|graphics|camera|color)", re.I), "仅显示"),
    (re.compile(r"(?:^|\.)set_.*(?:geometry|surface|axis|origin|location|diameter|radius|width|height|heigth|angle)$", re.I), "物理几何或实体位置/尺寸"),
    (re.compile(r"(?:^|\.)set_.*(?:repair|repetition|sewing|stitch|data_reduction)", re.I), "几何修复、重复或变形"),
    (re.compile(r"(?:^|\.)set_.*_demo$", re.I), "demo 接口"),
    (re.compile(r"(?:^|\.)set_(?:name|type)$", re.I), "命名或物理实体类型"),
    (re.compile(r"(?:^|\.)(?:add|delete|remove|unset|create|load|link|move|paste|copy)_", re.I), "创建/删除实体、几何链接或交互操作"),
    (re.compile(r"(?:^|\.)set_.*(?:p1[xy]|p2[xy]|p3[xy]|p4[xy]|coordinate)", re.I), "物理几何坐标"),
    (re.compile(r"(?:^|\.)set_(?:x|y|z|r|theta|u|v|x2|y2|z2|r2|theta2)_", re.I), "物理实体位置"),
    (re.compile(r"(?:^|\.)set_.*(?:gap_width|fillet|solid_body_shape)", re.I), "物理 gap/fillet/固体几何"),
    (re.compile(r"(?:^|\.)set_(?:circular|rectangular|oval|quadrilateral|trailing_edge_.*)_shape", re.I), "物理实体形状"),
    (re.compile(r"(?:^|\.)set_(?:holes|pinfins)_number", re.I), "创建或删除实体"),
    (re.compile(r"(?:^|\.)set_.*(?:streamwise|spanwise)_location", re.I), "物理实体位置"),
    (re.compile(r"(?:^|\.)set_.*(?:depth|orientation)", re.I), "物理实体尺寸或方向"),
    (re.compile(r"(?:^|\.)set_.*(?:demo|legacy)", re.I), "demo/legacy 接口"),
)


def audit_setter(owner: str, method: str) -> dict[str, Any]:
    """返回一个 17.1 setter 的映射或明确排除理由。"""

    qualified = f"{owner}.{method}" if owner else method
    mapped = MAPPED_SETTERS_BY_OWNER.get((owner, method))
    if mapped:
        return {"setter": qualified, "status": "mapped", "control_keys": list(mapped), "reason": None}
    if qualified in EXCLUDED_SETTERS:
        return {"setter": qualified, "status": "excluded", "control_keys": [], "reason": EXCLUDED_SETTERS[qualified]}
    if method in EXCLUDED_SETTERS:
        return {"setter": qualified, "status": "excluded", "control_keys": [], "reason": EXCLUDED_SETTERS[method]}
    for pattern, reason in AUDIT_EXCLUSION_RULES:
        if pattern.search(qualified):
            return {"setter": qualified, "status": "excluded", "control_keys": [], "reason": reason}
    return {"setter": qualified, "status": "unaudited", "control_keys": [], "reason": "未纳入 AutoGrid 17.1 审计"}


def audit_autogrid_source(path: str | Path) -> list[dict[str, Any]]:
    """审计正式 17.1 ``Autogrid.py`` 中每个 ``set_*``/``a5_set_*`` 方法。"""

    text = Path(path).read_text(encoding="latin1")
    owner = ""
    results: list[dict[str, Any]] = []
    for line in text.splitlines():
        class_match = re.match(r"class\s+(\w+)", line)
        if class_match:
            owner = class_match.group(1)
        method_match = re.match(r"(\s*)def\s+((?:a5_)?set_\w+)\s*\(", line)
        if not method_match:
            continue
        if not method_match.group(1):
            owner = ""
        results.append(audit_setter(owner, method_match.group(2)))
    return results


def audit_control_bindings(path: str | Path) -> list[dict[str, Any]]:
    """反向核验注册表中的 setter/getter 是否存在于正式 17.1 API。"""

    text = Path(path).read_text(encoding="latin1")
    methods_by_owner: dict[str, set[str]] = {}
    owner = ""
    for line in text.splitlines():
        class_match = re.match(r"class\s+(\w+)", line)
        if class_match:
            owner = class_match.group(1)
        method_match = re.match(r"(\s*)def\s+(\w+)\s*\(", line)
        if not method_match:
            continue
        if not method_match.group(1):
            owner = ""
        methods_by_owner.setdefault(owner, set()).add(method_match.group(2))

    results: list[dict[str, Any]] = []
    for spec in sorted(CONTROL_REGISTRY.values(), key=lambda item: item.key):
        bindings: list[tuple[str, str]] = []
        if spec.setter:
            bindings.append(("setter", spec.setter))
        bindings.extend(("setter_by_value", method) for _, method in spec.setter_by_value)
        if spec.getter:
            bindings.append(("getter", spec.getter))
        for role, method in bindings:
            expected_owners = CONTROL_TARGET_OWNERS[spec.target_kind]
            synthetic = method.startswith("__bool_")
            available_owners = tuple(
                candidate for candidate in expected_owners if method in methods_by_owner.get(candidate, set())
            )
            # 模块级函数（包括 C 扩展暴露的全局 helper，不通过 def 定义）
            # 在源码中不可追踪，但在 AutoGrid 运行时环境中可用。
            if not available_owners:
                if method in methods_by_owner.get("", set()) or method in _RUNTIME_GLOBALS:
                    available_owners = ("<module>",)
            results.append(
                {
                    "control_key": spec.key,
                    "role": role,
                    "method": method,
                    "expected_owners": list(expected_owners),
                    "available_owners": list(available_owners),
                    "status": "synthetic" if synthetic else ("available" if available_owners else "missing"),
                }
            )
    return results


def parse_control_assignment(raw: str, *, source: str = "--set") -> ControlRequest:
    """解析并严格校验一条 ``--set`` 表达式。"""

    if "=" not in raw:
        raise ControlValidationError(f"控制表达式缺少 '='：{raw}")
    lhs, raw_value = raw.split("=", 1)
    lhs = lhs.strip()
    raw_value = raw_value.strip()
    if not lhs or not raw_value:
        raise ControlValidationError(f"控制表达式的键和值都不能为空：{raw}")
    parts = lhs.split("/")
    if len(parts) < 2:
        raise ControlValidationError(f"控制路径至少应包含作用域和键：{lhs}")
    local_key = parts[-1]
    selector_parts = parts[:-1]
    selectors: tuple[EntitySelector, ...]
    if selector_parts == ["configuration"]:
        key = f"configuration/{local_key}"
        selectors = ()
    else:
        is_wizard = selector_parts[-1] == "wizard"
        if is_wizard:
            selector_parts = selector_parts[:-1]
            key = f"wizard/{local_key}"
        else:
            if not selector_parts or ":" not in selector_parts[-1]:
                raise ControlValidationError(
                    f"无法识别控制作用域：{lhs}；实体名含 '/' 或 '=' 时必须改用 #N 索引"
                )
            target_kind = selector_parts[-1].split(":", 1)[0]
            key = f"{target_kind}/{local_key}"
        selectors = tuple(_parse_selector(part) for part in selector_parts)
    spec = CONTROL_REGISTRY.get(key)
    if spec is None:
        raise ControlValidationError(f"未知控制键：{key}")
    if tuple(selector.kind for selector in selectors) != spec.hierarchy:
        expected = "/".join(f"{kind}:..." for kind in spec.hierarchy) or "configuration"
        if spec.scope == "wizard":
            expected += "/wizard"
        raise ControlValidationError(f"控制 {key} 的选择器层级错误；应为 {expected}")
    value = parse_control_value(raw_value, spec)
    return ControlRequest(raw=raw, key=key, selectors=selectors, value=value, spec=spec, source=source)


def parse_control_assignments(assignments: Iterable[str]) -> list[ControlRequest]:
    """批量解析控制表达式并拒绝重复定义。"""

    requests = [parse_control_assignment(raw) for raw in assignments]
    seen: dict[tuple[str, tuple[EntitySelector, ...]], ControlRequest] = {}
    for request in requests:
        identity = (request.key, request.selectors)
        if identity in seen:
            raise ControlValidationError(
                f"同一作用域和控制键重复定义：{request.selector_path}/{request.key.split('/', 1)[1]}"
            )
        seen[identity] = request
    return requests


def parse_control_value(raw_value: str, spec: ControlSpec) -> Any:
    """按控制规格解析文本值并校验类型与范围。"""

    value_type = spec.value_type
    try:
        if value_type == "bool":
            lowered = raw_value.lower()
            if lowered not in {"true", "false", "1", "0"}:
                raise ControlValidationError(f"{spec.key} 仅接受 true/false 或 1/0")
            value: Any = lowered in {"true", "1"}
        elif value_type == "int":
            if not re.fullmatch(r"[+-]?\d+", raw_value):
                raise ControlValidationError(f"{spec.key} 需要整数")
            value = int(raw_value)
        elif value_type == "float":
            value = float(raw_value)
            if not math.isfinite(value):
                raise ControlValidationError(f"{spec.key} 需要有限浮点数")
        elif value_type == "enum":
            if raw_value not in spec.enum_values:
                allowed = ", ".join(spec.enum_values)
                raise ControlValidationError(f"{spec.key} 仅接受：{allowed}")
            value = raw_value
        elif value_type in {"tuple_int", "tuple_float"}:
            fields = [field.strip() for field in raw_value.split(",")]
            if spec.tuple_length is not None and len(fields) != spec.tuple_length:
                raise ControlValidationError(f"{spec.key} 需要 {spec.tuple_length} 个逗号分隔值")
            if value_type == "tuple_int":
                if any(not re.fullmatch(r"[+-]?\d+", field) for field in fields):
                    raise ControlValidationError(f"{spec.key} 的元组元素必须为整数")
                value = tuple(int(field) for field in fields)
            else:
                value = tuple(float(field) for field in fields)
                if any(not math.isfinite(field) for field in value):
                    raise ControlValidationError(f"{spec.key} 的元组元素必须为有限浮点数")
        else:
            raise ControlValidationError(f"{spec.key} 使用了不支持的类型：{value_type}")
    except ValueError as exc:
        if isinstance(exc, ControlValidationError):
            raise
        raise ControlValidationError(f"{spec.key} 的值无法解析：{raw_value}") from exc
    _validate_range(value, spec)
    return value


def validate_wizard_compatibility(requests: Sequence[ControlRequest], *, use_row_wizard: bool) -> None:
    """校验 RowWizard 开关是否与向导阶段控制项兼容。"""

    if use_row_wizard:
        return
    wizard_keys = sorted({request.key for request in requests if request.spec.scope == "wizard"})
    if wizard_keys:
        raise ControlValidationError("--no-row-wizard 不能与 wizard 控制同时使用：" + ", ".join(wizard_keys))


def convert_si_length(value: Any, units_factor: float | None) -> Any:
    """将以米输入的长度转换为几何项目单位。"""

    if units_factor is None or units_factor <= 0:
        raise ControlValidationError("几何文件缺少有效 UNITS-FACTOR，无法执行 SI 长度换算")
    if isinstance(value, tuple):
        return tuple(float(item) / units_factor for item in value)
    return float(value) / units_factor


def resolve_control_requests(
    requests: Sequence[ControlRequest],
    geometry: Any,
) -> list[ResolvedControl]:
    """按几何实体展开 wildcard，并以最精确选择器覆盖 wildcard。

    同时自动注入拓扑选择器、检测跨拓扑冲突，并按拓扑依赖排序。
    """

    units_factor = getattr(geometry, "units_factor", None)

    # Step 1: wildcard 展开。
    candidates_by_identity: dict[tuple[str, tuple[TargetEntity, ...]], list[ControlRequest]] = {}
    for request in requests:
        if request.spec.unsupported_reason:
            raise ControlValidationError(f"{request.key}：{request.spec.unsupported_reason}")
        targets = _candidate_targets(request.spec, geometry)
        matches = [target for target in targets if _target_matches(request.selectors, target)]
        if not matches:
            raise ControlValidationError(f"控制 {request.key} 未匹配到实体：{request.selector_path}")
        for target in matches:
            candidates_by_identity.setdefault((request.key, target), []).append(request)

    # Step 2: 拓扑自动注入与跨拓扑冲突检测。
    _ensure_topology_selectors(candidates_by_identity)

    # Step 3: 精确选择器优先。
    chosen: list[tuple[ControlRequest, tuple[TargetEntity, ...]]] = []
    for (key, target), candidates in candidates_by_identity.items():
        max_specificity = max(candidate.specificity for candidate in candidates)
        winners = [candidate for candidate in candidates if candidate.specificity == max_specificity]
        values = {_hashable_value(candidate.value) for candidate in winners}
        if len(values) > 1:
            paths = ", ".join(sorted(candidate.selector_path for candidate in winners))
            raise ControlValidationError(f"控制 {key} 对同一实体存在等优先级冲突：{paths}")
        chosen.append((sorted(winners, key=lambda item: item.selector_path)[0], target))

    # Step 4: 阶段内拓扑依赖排序——b2b.topology 永远先于其条件子参数。
    chosen.sort(key=_topology_sort_key)

    # Step 5: 生成已解析控制列表。
    resolved: list[ResolvedControl] = []
    for index, (request, target) in enumerate(chosen, start=1):
        spec = request.spec
        if spec.si_length:
            project_value = convert_si_length(request.value, units_factor)
            if spec.value_type in ("int", "tuple_int"):
                if isinstance(project_value, tuple):
                    project_value = tuple(int(v) for v in project_value)
                else:
                    project_value = int(project_value)
        else:
            project_value = request.value
        api_value = spec.map_api_value(project_value)
        setter = spec.setter_for_value(request.value)
        if setter is None:
            raise ControlValidationError(f"控制 {spec.key} 无法解析到 AutoGrid setter")
        resolved.append(
            ResolvedControl(
                control_id=f"C{index:04d}",
                key=spec.key,
                target=target,
                requested_value=request.value,
                project_value=project_value,
                api_value=api_value,
                setter=setter,
                getter=spec.getter,
                setter_mode=spec.setter_mode,
                stage=spec.stage,
                priority=spec.priority,
                scope=spec.scope,
                target_kind=spec.target_kind,
                topologies=spec.topologies,
                source=request.source,
            )
        )
    return resolved


def _topology_sort_key(
    item: tuple[ControlRequest, tuple[TargetEntity, ...]],
) -> tuple[int, int, int, str, str]:
    """排序键：阶段 → 拓扑选择器 → 依赖深度 → 目标 → 键名。

    确保 blade/b2b.topology 在同一阶段、同一目标内永远先于
    b2b.default.* / b2b.hoh.* / b2b.hi.* 等条件子参数，同时保证
    type/mode/enable 等已显式提供的前置控制先于依赖值。
    """
    request, target = item
    stage_order = STAGE_ORDER[request.spec.stage]
    target_path = "/".join(part.name for part in target)
    # 拓扑选择器优先级最高（0），其他控制为 1。
    is_topo_selector = 0 if request.key == TOPOLOGY_SELECTOR_KEY else 1
    dependency_depth = CONTROL_DEPENDENCY_DEPTH.get(request.key, 0)
    return (stage_order, is_topo_selector, dependency_depth, target_path, request.key)


def _ensure_topology_selectors(
    candidates_by_identity: dict[tuple[str, tuple[TargetEntity, ...]], list[ControlRequest]],
) -> None:
    """为拓扑条件参数自动注入 b2b.topology，并检测跨拓扑冲突。

    规则：
    - 若同一叶片使用了不同拓扑族的条件参数 → 报错。
    - 若已显式设置 b2b.topology 但与条件参数不匹配 → 报错。
    - 若未显式设置 b2b.topology 但条件参数可唯一确定拓扑 → 自动注入。
    """
    blade_topo_needs: dict[tuple[TargetEntity, ...], str] = {}
    blade_explicit_topo: dict[tuple[TargetEntity, ...], str] = {}

    for (key, target), candidates in candidates_by_identity.items():
        # 只关心 blade 级拓扑相关控制。
        blade_target = _blade_part(target)
        if blade_target is None:
            continue
        if key == TOPOLOGY_SELECTOR_KEY:
            winner = max(candidates, key=lambda c: c.specificity)
            blade_explicit_topo[target] = str(winner.value)
        elif key in CONDITIONAL_KEY_TOPOLOGY:
            required = CONDITIONAL_KEY_TOPOLOGY[key]
            if target in blade_topo_needs and blade_topo_needs[target] != required:
                raise ControlValidationError(
                    f"同一叶片存在跨拓扑冲突：同时使用了 {blade_topo_needs[target]} "
                    f"和 {required} 拓扑的条件参数；请明确选择一种拓扑"
                )
            blade_topo_needs[target] = required

    # 逐一校验和注入。
    for target, required_topo in blade_topo_needs.items():
        if target in blade_explicit_topo:
            if blade_explicit_topo[target] != required_topo:
                raise ControlValidationError(
                    f"拓扑冲突：显式设置 b2b.topology={blade_explicit_topo[target]}，"
                    f"但条件参数要求 {required_topo} 拓扑"
                )
            continue

        # 自动注入隐式 b2b.topology。
        topo_spec = CONTROL_REGISTRY[TOPOLOGY_SELECTOR_KEY]
        selectors = tuple(
            EntitySelector(kind=entity.kind, mode="index", value=entity.index)
            for entity in target
            if entity.index is not None
        )
        implicit_req = ControlRequest(
            raw=f"auto:b2b.topology={required_topo}",
            key=TOPOLOGY_SELECTOR_KEY,
            selectors=selectors,
            value=required_topo,
            spec=topo_spec,
            source="auto-inject",
        )
        candidates_by_identity[(TOPOLOGY_SELECTOR_KEY, target)] = [implicit_req]


def _blade_part(target: tuple[TargetEntity, ...]) -> TargetEntity | None:
    """返回目标中的 blade 实体，若不存在则返回 None。"""
    for entity in target:
        if entity.kind == "blade":
            return entity
    return None


def list_control_specs(priority: str | None = None) -> list[ControlSpec]:
    """按优先级筛选并返回已注册的控制规格。"""

    if priority is not None:
        normalized = priority.upper()
        if normalized not in PRIORITIES:
            raise ControlValidationError("优先级仅接受 P0、P1 或 P2")
        return [spec for spec in sorted(CONTROL_REGISTRY.values(), key=lambda item: item.key) if spec.priority == normalized]
    return sorted(CONTROL_REGISTRY.values(), key=lambda item: (PRIORITIES.index(item.priority), item.key))


def describe_control(key: str) -> ControlSpec:
    """返回指定键的控制规格，不存在时抛出校验错误。"""

    try:
        return CONTROL_REGISTRY[key]
    except KeyError as exc:
        raise ControlValidationError(f"未知控制键：{key}") from exc


def enumerate_control_targets(
    geometry: Any,
    *,
    control_key: str | None = None,
    target_kind: str | None = None,
    include_unresolved: bool = False,
) -> list[tuple[TargetEntity, ...]]:
    """枚举几何中可供控制项使用的稳定、精确目标。

    网页和其他调用方可通过本接口复用 CLI 的几何适用性逻辑，而无需
    复制 ``_candidate_targets`` 的叶排、叶片、间隙和端壁规则。默认不返回
    geomTurbo 无法可靠计数的既有技术效果占位符；需要兼容 CLI 的 ``#1``～
    ``#99`` 候选时可显式设置 ``include_unresolved=True``。
    """

    if control_key is not None:
        specs = [describe_control(control_key)]
    else:
        specs = list_control_specs()

    known_target_kinds = {spec.target_kind for spec in CONTROL_REGISTRY.values()}
    if target_kind is not None and target_kind not in known_target_kinds:
        raise ControlValidationError(f"未知控制目标类型：{target_kind}")

    unique_targets: dict[tuple[TargetEntity, ...], None] = {}
    for spec in specs:
        if target_kind is not None and spec.target_kind != target_kind:
            continue
        for target in _candidate_targets(spec, geometry):
            if not include_unresolved and _is_unresolved_target(target):
                continue
            unique_targets.setdefault(target, None)

    def sort_key(target: tuple[TargetEntity, ...]) -> tuple[Any, ...]:
        return (
            len(target),
            tuple(
                (entity.kind, entity.index if entity.index is not None else 0, entity.name)
                for entity in target
            ),
        )

    return sorted(unique_targets, key=sort_key)


def _is_unresolved_target(target: tuple[TargetEntity, ...]) -> bool:
    """判断目标是否只是 geomTurbo 无法确认数量的静态索引占位符。"""

    if not target:
        return False
    leaf = target[-1]
    return leaf.index is not None and leaf.name == f"#{leaf.index}"


def _parse_selector(raw: str) -> EntitySelector:
    """解析单个实体选择器表达式。"""

    if ":" not in raw:
        raise ControlValidationError(
            f"无效实体选择器：{raw}；实体名含 '/' 或 '=' 时必须改用 #N 索引"
        )
    kind, value = raw.split(":", 1)
    if not kind or not value:
        raise ControlValidationError(f"无效实体选择器：{raw}")
    if value == "*":
        return EntitySelector(kind=kind, mode="wildcard", value="*")
    if value.startswith("#"):
        index_text = value[1:]
        if not index_text.isdigit() or int(index_text) < 1:
            raise ControlValidationError(f"索引必须是从 1 开始的 #N：{raw}")
        return EntitySelector(kind=kind, mode="index", value=int(index_text))
    return EntitySelector(kind=kind, mode="name", value=value)


def _validate_range(value: Any, spec: ControlSpec) -> None:
    """校验标量或元组值是否位于控制规格允许范围内。"""

    values = value if isinstance(value, tuple) else (value,)
    if spec.value_type in {"bool", "enum"}:
        return
    for item in values:
        if spec.minimum is not None and item < spec.minimum:
            raise ControlValidationError(f"{spec.key} 不得小于 {spec.minimum}")
        if spec.maximum is not None and item > spec.maximum:
            raise ControlValidationError(f"{spec.key} 不得大于 {spec.maximum}")


def _candidate_targets(spec: ControlSpec, geometry: Any) -> list[tuple[TargetEntity, ...]]:
    """根据几何拓扑枚举控制规格可能作用的目标实体。"""

    if not spec.hierarchy:
        return [()]
    if spec.hierarchy == ("existing-effect",):
        return [(TargetEntity("existing-effect", index, f"#{index}"),) for index in range(1, 100)]
    rows = list(getattr(geometry, "rows", ()))
    targets: list[tuple[TargetEntity, ...]] = []
    for row_index, row_info in enumerate(rows, start=1):
        row_target = (TargetEntity("row", row_index, str(row_info.name)),)
        if spec.hierarchy == ("row",):
            targets.append(row_target)
            continue
        if spec.hierarchy == ("row", "interface"):
            for interface_index, name in enumerate(("inlet", "outlet", "outlet2"), start=1):
                targets.append(row_target + (TargetEntity("interface", interface_index, name),))
            continue
        if spec.hierarchy == ("row", "endwall"):
            for endwall_index, name in enumerate(("hub", "shroud"), start=1):
                targets.append(row_target + (TargetEntity("endwall", endwall_index, name),))
            continue
        if spec.hierarchy == ("row", "endwall", "endwall-holes-line"):
            for endwall_index, name in enumerate(("hub", "shroud"), start=1):
                endwall_target = row_target + (TargetEntity("endwall", endwall_index, name),)
                targets.extend(_indexed_unknown_targets(endwall_target, "endwall-holes-line"))
            continue
        if spec.hierarchy == ("row", "snubber"):
            targets.extend(_indexed_unknown_targets(row_target, "snubber"))
            continue
        if spec.hierarchy == ("row", "existing-effect"):
            targets.extend(_indexed_unknown_targets(row_target, "existing-effect"))
            continue
        for blade_index, blade_info in enumerate(getattr(row_info, "blades", ()), start=1):
            blade_target = row_target + (TargetEntity("blade", blade_index, str(blade_info.name)),)
            if spec.hierarchy == ("row", "blade"):
                targets.append(blade_target)
                continue
            leaf_kind = spec.hierarchy[-1]
            if leaf_kind in {"gap", "partial-gap", "fillet"}:
                attribute = {"gap": "gap_sides", "partial-gap": "partial_gap_sides", "fillet": "fillet_sides"}[leaf_kind]
                sides = list(getattr(blade_info, attribute, ()))
                if leaf_kind == "gap" and not sides and getattr(blade_info, "has_tip_gap", False):
                    sides = ["shroud"]
                for side_index, side in enumerate(sides, start=1):
                    targets.append(blade_target + (TargetEntity(leaf_kind, side_index, str(side)),))
            elif leaf_kind in {"blade-sheet", "solid-body", "lete-wizard"}:
                targets.append(blade_target + (TargetEntity(leaf_kind, 1, leaf_kind),))
            elif leaf_kind == "stagnation-point":
                targets.append(blade_target + (TargetEntity(leaf_kind, 1, "leading"),))
                targets.append(blade_target + (TargetEntity(leaf_kind, 2, "trailing"),))
            else:
                targets.extend(_indexed_unknown_targets(blade_target, leaf_kind))
    return targets


def _indexed_unknown_targets(prefix: tuple[TargetEntity, ...], kind: str) -> list[tuple[TargetEntity, ...]]:
    """为几何文件无法计数的既有效果创建静态索引候选。"""

    # geomTurbo 不携带这些已有技术效果的可靠数量。保留 #1..#99 的静态候选，
    # AutoGrid 脚本会以正式 accessor 严格确认实体是否真实存在。
    return [prefix + (TargetEntity(kind, index, f"#{index}"),) for index in range(1, 100)]


def _target_matches(
    selectors: tuple[EntitySelector, ...],
    target: tuple[TargetEntity, ...],
) -> bool:
    """判断一组选择器是否与具体目标实体路径匹配。"""

    if len(selectors) != len(target):
        return False
    for selector, entity in zip(selectors, target):
        if selector.kind != entity.kind:
            return False
        if selector.mode == "wildcard":
            continue
        if selector.mode == "index" and selector.value != entity.index:
            return False
        if selector.mode == "name" and selector.value != entity.name:
            return False
    return True


def _hashable_value(value: Any) -> Any:
    """将列表值转换为可参与冲突检测的可哈希形式。"""

    return tuple(value) if isinstance(value, list) else value


__all__ = [
    "ALL_CONDITIONAL_KEYS",
    "AUDIT_EXCLUSION_RULES",
    "COMMON_CORE_KEYS",
    "COMMON_KEYS",
    "COMMON_TOPOLOGY_KEYS",
    "CONDITIONAL_KEY_TOPOLOGY",
    "CONTROL_DEPENDENCY_DEPTH",
    "CONTROL_PREREQUISITES",
    "CONTROL_REGISTRY",
    "CONTROL_TARGET_OWNERS",
    "EXCLUDED_SETTERS",
    "GENERAL_API_ONLY_EXCLUSIONS",
    "GENERAL_CONFIGURATION_KEYS",
    "GENERAL_CONTROL_EXCLUSIONS",
    "GENERAL_CONTROL_KEYS",
    "GENERAL_CORE_KEYS",
    "GENERAL_DEFAULT_KEYS",
    "GENERAL_EDGE_TREATMENT_KEYS",
    "GENERAL_HI_KEYS",
    "GENERAL_HOH_KEYS",
    "GENERAL_INTERFACE_KEYS",
    "GENERAL_ROW_KEYS",
    "GENERAL_STAGNATION_POINT_KEYS",
    "GENERAL_TOPOLOGY_KEYS",
    "GENERAL_WIZARD_KEYS",
    "MAPPED_SETTERS",
    "MAPPED_SETTERS_BY_OWNER",
    "PRIORITIES",
    "STAGES",
    "TOPOLOGY_DEFAULT_KEYS",
    "TOPOLOGY_HI_KEYS",
    "TOPOLOGY_HOH_KEYS",
    "TOPOLOGY_KEY_MAP",
    "TOPOLOGY_SELECTOR_KEY",
    "ControlRequest",
    "ControlSpec",
    "ControlValidationError",
    "EntitySelector",
    "ResolvedControl",
    "TargetEntity",
    "_ALL_KNOWN_SETTERS",
    "_ensure_topology_selectors",
    "_topology_sort_key",
    "audit_autogrid_source",
    "audit_control_bindings",
    "audit_setter",
    "convert_si_length",
    "describe_control",
    "enumerate_control_targets",
    "list_control_specs",
    "parse_control_assignment",
    "parse_control_assignments",
    "parse_control_value",
    "prerequisite_satisfied",
    "resolve_control_requests",
    "validate_wizard_compatibility",
]
