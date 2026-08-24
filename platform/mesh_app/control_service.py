"""控制目录、几何目标、依赖清除与提交预检。"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from controls import (
    CONDITIONAL_KEY_TOPOLOGY,
    CONTROL_PREREQUISITES,
    CONTROL_REGISTRY,
    ControlSpec,
    ControlValidationError,
    TargetEntity,
    enumerate_control_targets,
    list_control_specs,
    parse_control_assignment,
    resolve_control_requests,
)
from geomturbo import GeomTurboParseError, parse_geomturbo

from .db import Database
from .sessions import EMPTY_CONTROL_SNAPSHOT, ServiceError, dump_json, load_json


_PRECISE_SELECTOR = re.compile(r"^[a-z][a-z0-9-]*:#(?:[1-9][0-9]*)(?:/[a-z][a-z0-9-]*:#(?:[1-9][0-9]*))*$")


class ControlService:
    """把 ``src/`` 控制注册表转换为网页可用的稳定契约。"""

    def __init__(self, database: Database, data_dir: str | Path) -> None:
        self.database = database
        self.data_dir = Path(data_dir).resolve()

    def get_control_state(
        self,
        session_id: str,
        *,
        parent_run_id: str,
    ) -> dict[str, Any]:
        """返回按真实几何目标展开的分层控制目录。"""

        session, parent, geometry = self._context(session_id, parent_run_id)
        snapshot = load_json(parent["control_snapshot_json"], EMPTY_CONTROL_SNAPSHOT)
        values = _snapshot_map(snapshot)
        controls: list[dict[str, Any]] = []
        entities: set[str] = set()
        stages: set[str] = set()
        topologies: set[str] = set()
        for spec in list_control_specs():
            targets = enumerate_control_targets(geometry, control_key=spec.key)
            if not targets:
                controls.append(_control_item(spec, None, None, "NOT_APPLICABLE", _not_applicable_reason(spec)))
                continue
            for target in targets:
                selector = target_to_selector(target)
                identity = (spec.key, selector)
                value = values.get(identity)
                availability, reason = _availability(session, parent, spec, selector, values)
                controls.append(_control_item(spec, selector, value, availability, reason))
                entities.add(selector)
                stages.add(spec.stage)
                topologies.update(spec.topologies)
        return {
            "parent_run_id": parent_run_id,
            "controls": controls,
            "entities": sorted(entities),
            "stages": sorted(stages),
            "topologies": sorted(topologies),
        }

    def preview(
        self,
        session_id: str,
        *,
        parent_run_id: str,
        changes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """规范化草稿、验证目标和值，并计算必须清除的子控制。"""

        session, parent, geometry = self._context(session_id, parent_run_id)
        if session["status"] != "ACTIVE":
            raise ServiceError("SESSION_FROZEN", "会话已完成，控制已冻结", status_code=409)
        if parent["status"] != "SUCCEEDED":
            raise ServiceError("PARENT_RUN_NOT_SUCCEEDED", "只能基于成功运行预检控制", status_code=409)
        base_snapshot = load_json(parent["control_snapshot_json"], EMPTY_CONTROL_SNAPSHOT)
        base = _snapshot_map(base_snapshot)
        normalized: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for raw_change in changes:
            try:
                expanded = self._normalize_change(raw_change, geometry)
                for change in expanded:
                    identity = (change["key"], change["selector"])
                    if identity in seen:
                        raise ControlValidationError(
                            f"同一控制和目标在草稿中重复出现：{change['selector']}/{change['key']}"
                        )
                    seen.add(identity)
                    normalized.append(change)
            except (ControlValidationError, KeyError, TypeError, ValueError) as exc:
                errors.append(
                    {
                        "code": "INVALID_CONTROL_CHANGE",
                        "message": str(exc) or "控制变更格式无效",
                    }
                )

        if errors:
            return {
                "valid": False,
                "normalized_changes": normalized,
                "expanded_changes": normalized,
                "required_clears": [],
                "warnings": [],
                "errors": errors,
            }

        working = dict(base)
        for change in normalized:
            identity = (change["key"], change["selector"])
            if change["op"] == "clear":
                working.pop(identity, None)
            else:
                working[identity] = change["value"]

        required_clears = _required_clears(base, working, normalized)
        explicitly_cleared = {
            (item["key"], item["selector"])
            for item in normalized
            if item["op"] == "clear"
        }
        required_clears = [
            item for item in required_clears if (item["key"], item["selector"]) not in explicitly_cleared
        ]
        effective = dict(working)
        for item in required_clears:
            effective.pop((item["key"], item["selector"]), None)

        prerequisite_errors = _validate_prerequisites(effective)
        if prerequisite_errors:
            return {
                "valid": False,
                "normalized_changes": normalized,
                "expanded_changes": normalized,
                "required_clears": required_clears,
                "warnings": [],
                "errors": prerequisite_errors,
            }

        try:
            requests = [
                parse_control_assignment(_assignment_text(key, selector, value), source="web")
                for (key, selector), value in sorted(effective.items())
            ]
            resolved = resolve_control_requests(requests, geometry)
        except ControlValidationError as exc:
            return {
                "valid": False,
                "normalized_changes": normalized,
                "expanded_changes": normalized,
                "required_clears": required_clears,
                "warnings": [],
                "errors": [{"code": "CONTROL_VALIDATION_FAILED", "message": str(exc)}],
            }

        snapshot = _snapshot_from_map(effective)
        delta_items = list(normalized)
        delta_items.extend(item for item in required_clears if item not in delta_items)
        delta = {"schema_version": 1, "items": delta_items}
        warnings = []
        if required_clears:
            warnings.append(
                {
                    "code": "DEPENDENT_CONTROLS_REQUIRE_CLEAR",
                    "message": "父控制变化会使部分子控制失效，提交前需要确认清除",
                }
            )
        return {
            "valid": True,
            "normalized_changes": normalized,
            "expanded_changes": normalized,
            "required_clears": required_clears,
            "warnings": warnings,
            "errors": [],
            "resolved_controls": [item.to_dict() for item in resolved],
            "snapshot": snapshot,
            "delta": delta,
        }

    def prepare_run_controls(
        self,
        session_id: str,
        *,
        parent_run_id: str,
        changes: Sequence[Mapping[str, Any]],
        confirm_required_clears: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """为创建运行生成完整快照，并执行清除确认门。"""

        preview = self.preview(
            session_id,
            parent_run_id=parent_run_id,
            changes=changes,
        )
        if not preview["valid"]:
            raise ServiceError(
                "CONTROL_VALIDATION_FAILED",
                "控制草稿未通过预检，请修正后重试",
                status_code=422,
                details={"errors": preview["errors"]},
            )
        if preview["required_clears"] and not confirm_required_clears:
            raise ServiceError(
                "CONTROL_CLEAR_CONFIRMATION_REQUIRED",
                "父控制变化需要清除子控制，请确认清除后再提交",
                status_code=409,
                details={"required_clears": preview["required_clears"]},
            )
        return preview["snapshot"], preview["delta"], preview

    def _normalize_change(
        self,
        raw: Mapping[str, Any],
        geometry: Any,
    ) -> list[dict[str, Any]]:
        allowed = {"key", "selector", "op", "value"}
        extra = set(raw) - allowed
        if extra:
            raise ControlValidationError("控制变更包含未知字段：" + ", ".join(sorted(extra)))
        key = raw["key"]
        selector = raw["selector"]
        operation = raw["op"]
        if not isinstance(key, str) or key not in CONTROL_REGISTRY:
            raise ControlValidationError(f"未知控制键：{key}")
        if not isinstance(selector, str):
            raise ControlValidationError("控制 selector 必须是字符串")
        spec = CONTROL_REGISTRY[key]
        selectors = _expand_selector(spec, selector, geometry)
        if operation not in {"set", "clear"}:
            raise ControlValidationError("控制 op 仅接受 set 或 clear")
        if operation == "clear":
            if "value" in raw and raw["value"] is not None:
                raise ControlValidationError("clear 操作不能携带 value")
            return [{"key": key, "selector": item, "op": "clear"} for item in selectors]
        if "value" not in raw:
            raise ControlValidationError("set 操作必须携带 value")
        normalized: list[dict[str, Any]] = []
        for precise_selector in selectors:
            request = parse_control_assignment(
                _assignment_text(key, precise_selector, raw["value"]), source="web"
            )
            normalized.append(
                {
                    "key": key,
                    "selector": precise_selector,
                    "op": "set",
                    "value": request.value,
                }
            )
        return normalized

    def _context(
        self,
        session_id: str,
        parent_run_id: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row, Any]:
        with self.database.reading() as connection:
            session = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if session is None:
                raise ServiceError("SESSION_NOT_FOUND", "找不到指定会话", status_code=404)
            parent = connection.execute("SELECT * FROM runs WHERE id = ?", (parent_run_id,)).fetchone()
            if parent is None or parent["session_id"] != session_id:
                raise ServiceError("PARENT_RUN_NOT_FOUND", "父运行不属于当前会话", status_code=404)
        geometry_path = _safe_data_path(self.data_dir, str(session["geometry_relative_path"]))
        if not geometry_path.is_file():
            raise ServiceError(
                "GEOMETRY_FILE_MISSING",
                "会话的几何文件已缺失，请联系运维检查数据目录",
                status_code=500,
            )
        try:
            geometry = parse_geomturbo(geometry_path)
        except GeomTurboParseError as exc:
            raise ServiceError(
                "INVALID_GEOMTURBO",
                "会话几何文件超过安全解析边界，请联系运维检查数据",
                status_code=422,
                details=exc.details,
            ) from exc
        except OSError as exc:
            raise ServiceError("GEOMETRY_READ_FAILED", "无法读取会话几何文件", status_code=500) from exc
        return session, parent, geometry


def target_to_selector(target: tuple[TargetEntity, ...]) -> str:
    """将网格内核目标实体转换为网页只读的精确 ``#N`` 选择器。"""

    if not target:
        return "configuration"
    return "/".join(f"{entity.kind}:#{entity.index}" for entity in target)


def _expand_selector(spec: ControlSpec, selector: str, geometry: Any) -> list[str]:
    if not spec.hierarchy:
        if selector != "configuration":
            raise ControlValidationError(f"控制 {spec.key} 的 selector 必须是 configuration")
        return [selector]
    candidates = [target_to_selector(target) for target in enumerate_control_targets(geometry, control_key=spec.key)]
    if selector in candidates:
        return [selector]
    if selector.endswith(":*"):
        prefix_parts = selector.split("/")
        if any(part != prefix_parts[-1] and part.endswith(":*") for part in prefix_parts):
            raise ControlValidationError("批量目标只允许最后一级使用 *")
        wildcard_kind = prefix_parts[-1][:-2]
        if wildcard_kind != spec.hierarchy[-1]:
            raise ControlValidationError(f"控制 {spec.key} 的批量目标层级错误")
        prefix = "/".join(prefix_parts[:-1])
        matched = [
            item
            for item in candidates
            if (not prefix or item.startswith(prefix + "/"))
            and item.split("/")[-1].startswith(wildcard_kind + ":#")
        ]
        if matched:
            return matched
    if not _PRECISE_SELECTOR.fullmatch(selector):
        raise ControlValidationError("网页控制目标必须使用精确的 #N 索引")
    raise ControlValidationError(f"控制 {spec.key} 不适用于几何目标：{selector}")


def _control_item(
    spec: ControlSpec,
    selector: str | None,
    value: Any,
    availability: str,
    reason: str | None,
) -> dict[str, Any]:
    value_type = {
        "bool": "boolean",
        "int": "integer",
        "float": "number",
        "enum": "enum",
        "tuple_int": "string",
        "tuple_float": "string",
    }.get(spec.value_type, "string")
    options = [{"value": value, "label": str(value)} for value in spec.enum_values]
    return {
        "key": spec.key,
        "label": spec.description,
        "description": spec.description,
        "priority": spec.priority,
        "entity": spec.target_kind,
        "selector": selector or "/".join(f"{kind}:#N" for kind in spec.hierarchy),
        "stage": spec.stage,
        "topology": ",".join(spec.topologies) if spec.topologies else None,
        "availability": availability,
        "reason": reason,
        "value_type": value_type,
        "value": value,
        "inherited_value": None if value is not None else "由 AutoGrid 默认决定",
        "explicit": value is not None,
        "minimum": spec.minimum,
        "maximum": spec.maximum,
        "options": options,
    }


def _availability(
    session: sqlite3.Row,
    parent: sqlite3.Row,
    spec: ControlSpec,
    selector: str,
    values: Mapping[tuple[str, str], Any],
) -> tuple[str, str | None]:
    if session["status"] == "COMPLETED":
        return "LOCKED", "会话已完成，控制已冻结"
    if parent["status"] != "SUCCEEDED":
        return "LOCKED", "只能从成功运行创建控制分支"
    missing_prerequisites = _missing_prerequisites(spec.key, selector, values)
    if missing_prerequisites:
        requirements = "、".join(
            f"{key}={_render_value(expected)}" for key, expected in missing_prerequisites
        )
        return "LOCKED", f"需先显式设置 {requirements}"
    if spec.topologies:
        topology_value = values.get(("blade/b2b.topology", _selector_prefix(selector, 2)))
        if topology_value is not None and topology_value not in spec.topologies:
            return "NOT_APPLICABLE", f"当前叶片拓扑为 {topology_value}"
    return "EDITABLE", None


def _not_applicable_reason(spec: ControlSpec) -> str:
    return spec.not_applicable_when or "当前 geomTurbo 几何中没有可确认的适用实体"


def _snapshot_map(snapshot: Mapping[str, Any]) -> dict[tuple[str, str], Any]:
    if snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("items"), list):
        raise ServiceError("INVALID_CONTROL_SNAPSHOT", "数据库中的控制快照版本无效", status_code=500)
    result: dict[tuple[str, str], Any] = {}
    for item in snapshot["items"]:
        if not isinstance(item, Mapping):
            raise ServiceError("INVALID_CONTROL_SNAPSHOT", "数据库中的控制快照格式无效", status_code=500)
        result[(str(item.get("key")), str(item.get("selector")))] = item.get("value")
    return result


def _snapshot_from_map(values: Mapping[tuple[str, str], Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "items": [
            {"key": key, "selector": selector, "value": value}
            for (key, selector), value in sorted(values.items(), key=lambda item: (item[0][1], item[0][0]))
        ],
    }


def _required_clears(
    base: Mapping[tuple[str, str], Any],
    working: Mapping[tuple[str, str], Any],
    changes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    invalidated: set[tuple[str, str]] = set()
    changed_values = {
        (str(change["key"]), str(change["selector"])): (
            change.get("value") if change["op"] == "set" else None
        )
        for change in changes
    }
    for (changed_key, changed_selector), changed_value in changed_values.items():
        for (dependent_key, dependent_selector), _dependent_value in base.items():
            prerequisites = CONTROL_PREREQUISITES.get(dependent_key, ())
            for prerequisite_key, expected in prerequisites:
                if prerequisite_key != changed_key:
                    continue
                if _selectors_compatible(changed_selector, dependent_selector) and changed_value != expected:
                    invalidated.add((dependent_key, dependent_selector))
        if changed_key == "blade/b2b.topology":
            for (dependent_key, dependent_selector), _dependent_value in base.items():
                required_topology = CONDITIONAL_KEY_TOPOLOGY.get(dependent_key)
                if (
                    required_topology is not None
                    and required_topology != changed_value
                    and _selectors_compatible(changed_selector, dependent_selector)
                ):
                    invalidated.add((dependent_key, dependent_selector))
    return [
        {"key": key, "selector": selector, "op": "clear"}
        for key, selector in sorted(invalidated, key=lambda item: (item[1], item[0]))
        if (key, selector) in working
    ]


def _validate_prerequisites(
    values: Mapping[tuple[str, str], Any],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for (key, selector), _value in sorted(values.items(), key=lambda item: (item[0][1], item[0][0])):
        missing = _missing_prerequisites(key, selector, values)
        if not missing:
            continue
        requirements = "、".join(
            f"{prerequisite_key}={_render_value(expected)}"
            for prerequisite_key, expected in missing
        )
        errors.append(
            {
                "code": "CONTROL_PREREQUISITE_NOT_MET",
                "key": key,
                "selector": selector,
                "message": f"控制 {key} 需要先显式设置 {requirements}",
            }
        )
    return errors


def _missing_prerequisites(
    key: str,
    selector: str,
    values: Mapping[tuple[str, str], Any],
) -> list[tuple[str, Any]]:
    missing: list[tuple[str, Any]] = []
    for prerequisite_key, expected in CONTROL_PREREQUISITES.get(key, ()):
        candidates = [
            value
            for (candidate_key, candidate_selector), value in values.items()
            if candidate_key == prerequisite_key
            and _selectors_compatible(candidate_selector, selector)
        ]
        if not candidates or not any(value == expected for value in candidates):
            missing.append((prerequisite_key, expected))
    return missing


def _selectors_compatible(left: str, right: str) -> bool:
    if left == "configuration" or right == "configuration":
        return left == right
    left_parts = left.split("/")
    right_parts = right.split("/")
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def _selector_prefix(selector: str, length: int) -> str:
    if selector == "configuration":
        return selector
    return "/".join(selector.split("/")[:length])


def _assignment_text(key: str, selector: str, value: Any) -> str:
    spec = CONTROL_REGISTRY[key]
    local_key = key.split("/", 1)[1]
    if not spec.hierarchy:
        path = f"configuration/{local_key}"
    elif spec.scope == "wizard":
        path = f"{selector}/wizard/{local_key}"
    else:
        path = f"{selector}/{local_key}"
    return f"{path}={_render_value(value)}"


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return ",".join(_render_value(item) for item in value)
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _safe_data_path(data_dir: Path, relative_path: str) -> Path:
    normalized = relative_path.replace("\\", "/")
    candidate = (data_dir / normalized).resolve()
    try:
        candidate.relative_to(data_dir)
    except ValueError as exc:
        raise ServiceError("UNSAFE_PATH", "数据库中的几何路径越过数据目录", status_code=500) from exc
    return candidate


__all__ = ["ControlService", "target_to_selector"]
