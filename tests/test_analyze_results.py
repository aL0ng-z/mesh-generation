"""Rotor37 通用网格控制参数验证结果分析器。

本模块只依据 campaign 保存的清单、案例结果、schema 3 网格指纹和
qualityReport 解析结果进行判定，不包含针对某次运行的硬编码结论。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import re
import sys
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


CLASSIFICATIONS = (
    "EFFECT_PASS",
    "EFFECT_VALID_WITH_QUALITY_WARNING",
    "EFFECT_INVALID_MESH_ONLY",
    "NO_MESH_EFFECT",
    "FAIL_READBACK",
    "FAIL_GENERATION",
    "BLOCKED_TOPOLOGY_BASELINE",
)

QUALITY_FIELDS = (
    "number_of_points",
    "grid_levels",
    "negative_cells",
    "min_skewness_angle",
    "max_expansion_ratio",
    "min_spanwise_skewness_angle",
    "max_spanwise_expansion_ratio",
    "max_aspect_ratio",
)


def load_json(path: Path, default: Any = None) -> Any:
    """读取 JSON；缺失或损坏时返回调用方指定的默认值。"""

    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def extract_quality(summary: dict[str, Any] | None) -> dict[str, Any]:
    """提取质量值、有效性元数据及临界 block/I/J/K。"""

    quality = (summary or {}).get("quality") or {}
    metrics = quality.get("metrics") or {}
    metadata = quality.get("metadata") or {}
    result = quality.get("result") or {}
    extracted = {field: metrics.get(field) for field in QUALITY_FIELDS}
    extracted.update(
        {
            "quality_status": result.get("status"),
            "quality_accepted": result.get("accepted"),
            "quality_reasons": result.get("reasons") or [],
            "mesh_validity": metadata.get("mesh_validity"),
            "overlapping_status": metadata.get("overlapping_status"),
            "generation_time": metadata.get("generation_time"),
            "generation_time_seconds": metadata.get("generation_time_seconds"),
        }
    )
    extracted["critical_locations"] = {
        key: value
        for key, value in metrics.items()
        if key.endswith("_critical_location") or key.endswith("_block")
    }
    return extracted


def extract_mesh_signature(summary: dict[str, Any] | None) -> dict[str, Any]:
    """取得主网格签名；兼容 schema 2 时仅产生明确标注的辅助签名。"""

    if not summary:
        return {
            "comparison_sha256": None,
            "coordinate_sha256": None,
            "source": "missing",
            "blocks": [],
        }
    autogrid = summary.get("autogrid") or {}
    fingerprint = summary.get("mesh_fingerprint") or autogrid.get("mesh_fingerprint")
    if isinstance(fingerprint, dict):
        blocks = fingerprint.get("blocks") or []
        return {
            "comparison_sha256": fingerprint.get("comparison_sha256"),
            "coordinate_sha256": fingerprint.get("aggregate_sha256"),
            "source": "schema3_complete_coordinates",
            "number_of_blocks": fingerprint.get("number_of_blocks"),
            "total_block_points": fingerprint.get("total_block_points"),
            "total_block_cells": fingerprint.get("total_block_cells"),
            "grid_levels": fingerprint.get("grid_levels"),
            "blocks": blocks,
            "probes": [
                {
                    "block": block.get("index"),
                    "name": block.get("name"),
                    "samples": block.get("samples") or [],
                }
                for block in blocks
            ],
        }

    # 旧摘要没有完整坐标指纹。若 CGNS 仍存在，计算文件哈希，但明确作为辅助证据。
    output_path = (autogrid.get("outputs") or {}).get("cgns")
    cgns_hash = sha256_file(Path(output_path)) if output_path else None
    quality = extract_quality(summary)
    legacy_payload = {
        "cgns_sha256": cgns_hash,
        "number_of_points": quality.get("number_of_points"),
        "grid_levels": quality.get("grid_levels"),
    }
    has_payload = any(value is not None for value in legacy_payload.values())
    auxiliary = (
        hashlib.sha256(
            json.dumps(
                legacy_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if has_payload
        else None
    )
    return {
        "comparison_sha256": auxiliary,
        "coordinate_sha256": None,
        "source": "legacy_auxiliary",
        "number_of_blocks": None,
        "total_block_points": quality.get("number_of_points"),
        "total_block_cells": None,
        "grid_levels": quality.get("grid_levels"),
        "blocks": [],
        "probes": [],
    }


def structurally_valid(summary: dict[str, Any] | None) -> bool:
    quality = extract_quality(summary)
    validity = str(quality.get("mesh_validity") or "").upper()
    validity_ok = (
        validity in {"OK", "VALID"}
        or ("VALID" in validity and "INVALID" not in validity)
    )
    return (
        quality.get("negative_cells") == 0
        and quality.get("overlapping_status") == "NO_OVERLAP"
        and validity_ok
    )


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
        return result if math.isfinite(result) else None
    return None


def values_equivalent(expected: Any, observed: Any) -> bool:
    """宽容比较 AutoGrid getter 的数值、布尔、tuple/list 和文本回读。"""

    if expected is None:
        return observed is None
    if isinstance(expected, (list, tuple)) and isinstance(observed, (list, tuple)):
        return len(expected) == len(observed) and all(
            values_equivalent(left, right) for left, right in zip(expected, observed)
        )
    expected_number = _numeric(expected)
    observed_number = _numeric(observed)
    if expected_number is not None and observed_number is not None:
        return math.isclose(expected_number, observed_number, rel_tol=1.0e-7, abs_tol=1.0e-10)
    return str(expected).strip().lower() == str(observed).strip().lower()


def find_control_readback(
    summary: dict[str, Any] | None,
    key: str | None,
) -> dict[str, Any]:
    """返回变量控制在 setter 前、setter 后及生成后的 getter 回读。"""

    if not summary or not key:
        return {
            "available": False,
            "status": "NO_SUMMARY",
            "expected": None,
            "before": None,
            "after": None,
            "post_generation": None,
            "matches_after": None,
            "matches_post_generation": None,
        }
    controls = summary.get("controls") or {}
    applied = controls.get("applied") or (summary.get("autogrid") or {}).get("control_results") or []
    post_events = controls.get("post_generation") or (summary.get("autogrid") or {}).get(
        "post_control_results"
    ) or []
    matching = [item for item in applied if item.get("key") == key]
    if not matching:
        return {
            "available": False,
            "status": "CONTROL_NOT_FOUND",
            "expected": None,
            "before": None,
            "after": None,
            "post_generation": None,
            "matches_after": None,
            "matches_post_generation": None,
        }
    event = matching[-1]
    control_id = event.get("id")
    post = next(
        (
            item
            for item in reversed(post_events)
            if item.get("id") == control_id or (
                control_id is None and item.get("key") == key
            )
        ),
        None,
    )
    getter = event.get("getter")
    expected = event.get("api_value", event.get("project_value", event.get("requested")))
    has_getter = bool(getter)
    after = event.get("readback")
    post_value = (post or {}).get("readback")
    return {
        "available": True,
        "has_getter": has_getter,
        "status": event.get("status"),
        "post_status": (post or {}).get("status"),
        "expected": expected,
        "before": event.get("readback_before"),
        "before_error": event.get("readback_before_error"),
        "after": after,
        "after_error": event.get("error"),
        "post_generation": post_value,
        "post_error": (post or {}).get("error"),
        "matches_after": values_equivalent(expected, after) if has_getter else None,
        "matches_post_generation": (
            values_equivalent(expected, post_value)
            if has_getter and post and post.get("status") == "readback"
            else None
        ),
    }


def infer_stable_readback_mapping(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """识别 getter 对公开枚举/布尔值采用整数代码的稳定一一映射。

    AutoGrid 17.1 的部分 setter 接受 ``yes/no`` 或枚举文本，但配套 getter
    返回 0/1 或枚举序号。只要 setter 后与生成后的回读一致、相同请求始终
    得到相同代码、不同请求得到不同代码，就把它视为可靠的规范化回读；
    被网格生成重置或多个请求坍缩到同一值的情况不会通过该判定。
    """

    eligible = [
        record
        for record in records
        if (record.get("readback") or {}).get("has_getter")
        and (record.get("readback") or {}).get("post_status") == "readback"
    ]
    by_requested: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in eligible:
        requested_key = json.dumps(
            record.get("variable_value"),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        by_requested[requested_key].append(record)
    if len(by_requested) < 2:
        return []

    mapping: list[dict[str, Any]] = []
    observed_values: list[Any] = []
    for requested_key, requested_records in sorted(by_requested.items()):
        first_record = requested_records[0]
        first_readback = first_record.get("readback") or {}
        observed = first_readback.get("after")
        post_value = first_readback.get("post_generation")
        if (
            observed is None
            or post_value is None
            or not values_equivalent(observed, post_value)
        ):
            return []
        for record in requested_records[1:]:
            readback = record.get("readback") or {}
            if (
                not values_equivalent(observed, readback.get("after"))
                or not values_equivalent(observed, readback.get("post_generation"))
            ):
                return []
        if any(values_equivalent(observed, previous) for previous in observed_values):
            return []
        observed_values.append(observed)
        mapping.append(
            {
                "requested": json.loads(requested_key),
                "observed": observed,
            }
        )
    return mapping


def readback_failed(record: dict[str, Any]) -> bool:
    """判断案例是否存在未被稳定类别映射解释的 getter 回读失败。"""

    readback = record.get("readback") or {}
    return bool(
        readback.get("has_getter")
        and not readback.get("stable_mapping")
        and (
            readback.get("matches_after") is False
            or readback.get("matches_post_generation") is False
        )
    )


def case_record(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("run_summary")
    quality = extract_quality(summary)
    signature = extract_mesh_signature(summary)
    readback = find_control_readback(summary, result.get("variable_key"))
    autogrid = (summary or {}).get("autogrid") or {}
    outputs = set((autogrid.get("outputs") or {}).keys())
    generation_success = bool(
        (
            autogrid.get("returncode") == 0
            and {"igg", "cgns", "trb", "quality_report"}.issubset(outputs)
            and signature.get("comparison_sha256")
        )
        if summary
        else result.get("generation_success", result.get("status") == "success")
    )
    record = {
        "case_id": result.get("case_id"),
        "phase": result.get("phase"),
        "category": result.get("category"),
        "topology": result.get("topology"),
        "variable_key": result.get("variable_key"),
        "variable_value": result.get("variable_value"),
        "context_id": result.get("context_id"),
        "status": result.get("status"),
        "generation_success": generation_success,
        "structurally_valid": bool(
            structurally_valid(summary)
            if summary
            else result.get("structurally_valid")
        ),
        "quality_pass": bool(
            quality.get("quality_status") == "PASS"
            if summary
            else result.get("quality_pass")
        ),
        "error": result.get("error"),
        "duration_seconds": result.get("duration_seconds"),
        "out_dir": result.get("out_dir"),
        "command": result.get("command"),
        "fingerprint": signature.get("comparison_sha256"),
        "coordinate_fingerprint": signature.get("coordinate_sha256"),
        "fingerprint_source": signature.get("source"),
        "number_of_blocks": signature.get("number_of_blocks"),
        "total_block_points": signature.get("total_block_points"),
        "total_block_cells": signature.get("total_block_cells"),
        "blocks": signature.get("blocks"),
        "probes": signature.get("probes"),
        "readback": readback,
        **quality,
    }
    return record


def compare_pair(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """比较同一依赖上下文内两个成功变体的主网格证据。"""

    changed = (
        left.get("fingerprint") is not None
        and right.get("fingerprint") is not None
        and left.get("fingerprint") != right.get("fingerprint")
    )
    left_blocks = [
        (block.get("name"), tuple(block.get("size") or ()))
        for block in left.get("blocks") or []
    ]
    right_blocks = [
        (block.get("name"), tuple(block.get("size") or ()))
        for block in right.get("blocks") or []
    ]
    topology_or_size_changed = any(
        (
            left.get("number_of_blocks") != right.get("number_of_blocks"),
            left.get("total_block_points") != right.get("total_block_points"),
            left.get("total_block_cells") != right.get("total_block_cells"),
            left.get("grid_levels") != right.get("grid_levels"),
            left_blocks != right_blocks,
        )
    )
    quality_deltas = {}
    for field in QUALITY_FIELDS:
        left_value = _numeric(left.get(field))
        right_value = _numeric(right.get(field))
        if left_value is not None and right_value is not None:
            quality_deltas[field] = right_value - left_value
    return {
        "left_case": left.get("case_id"),
        "right_case": right.get("case_id"),
        "left_value": left.get("variable_value"),
        "right_value": right.get("variable_value"),
        "context_id": left.get("context_id"),
        "mesh_changed": changed,
        "topology_or_size_changed": topology_or_size_changed,
        "coordinate_only_changed": changed and not topology_or_size_changed,
        "both_structurally_valid": bool(
            left.get("structurally_valid") and right.get("structurally_valid")
        ),
        "both_quality_pass": bool(left.get("quality_pass") and right.get("quality_pass")),
        "quality_deltas": quality_deltas,
    }


def classify_control(
    inventory_row: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """依据同上下文变体比较，把一个候选控制归入计划规定的最终类别。"""

    key = inventory_row["key"]
    if not inventory_row.get("general_candidate"):
        return {
            "key": key,
            "group": inventory_row.get("general_group"),
            "classification": "EXCLUDED",
            "reason": inventory_row.get("exclusion_reason") or "不在通用候选集合",
            "case_count": 0,
            "success_count": 0,
            "comparable_pair_count": 0,
            "effect_pair_count": 0,
            "records": [],
            "pairs": [],
        }

    relevant = [
        record
        for record in records
        if record.get("variable_key") == key and record.get("category") != "TARGET_SMOKE"
    ]
    stable_readback_mapping: list[dict[str, Any]] = []
    if relevant and all(record.get("status") == "blocked" for record in relevant):
        classification = "BLOCKED_TOPOLOGY_BASELINE"
        reason = "HOH 在 Rotor37 上未获得结构有效的通用锚点"
        pairs: list[dict[str, Any]] = []
    else:
        successful = [
            record
            for record in relevant
            if record.get("generation_success") and record.get("fingerprint")
        ]
        by_context: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in successful:
            by_context[str(record.get("context_id"))].append(record)
        pairs = []
        for context_records in by_context.values():
            distinct_values = {
                json.dumps(record.get("variable_value"), sort_keys=True, default=str)
                for record in context_records
            }
            if len(distinct_values) < 2:
                continue
            for left, right in itertools.combinations(context_records, 2):
                if values_equivalent(left.get("variable_value"), right.get("variable_value")):
                    continue
                pairs.append(compare_pair(left, right))

        getter_records = [
            record
            for record in successful
            if (record.get("readback") or {}).get("has_getter")
        ]
        stable_readback_mapping = infer_stable_readback_mapping(getter_records)
        if stable_readback_mapping:
            for record in getter_records:
                (record.get("readback") or {})["stable_mapping"] = True
        reliable_readbacks = [
            record
            for record in getter_records
            if (record.get("readback") or {}).get("matches_after") is True
            and (
                (record.get("readback") or {}).get("matches_post_generation") is True
                or (record.get("readback") or {}).get("post_status") == "no_getter"
            )
        ]
        reliable_requested_values = {
            json.dumps(
                record.get("variable_value"),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            for record in reliable_readbacks
        }
        readback_verified = bool(
            stable_readback_mapping or len(reliable_requested_values) >= 2
        )
        effect_pairs = [pair for pair in pairs if pair["mesh_changed"]]
        if not successful:
            classification = "FAIL_GENERATION"
            reason = "没有成功且具有网格指纹的变体"
        elif getter_records and not readback_verified:
            classification = "FAIL_READBACK"
            reason = "不足两个请求值在 setter 后及生成后获得可靠 getter 回读"
        elif not pairs:
            classification = "FAIL_GENERATION"
            reason = "不足两个相同依赖上下文下的成功变体，无法判定效果"
        elif not effect_pairs:
            classification = "NO_MESH_EFFECT"
            reason = "setter 成功，但所有同上下文变体的完整网格指纹相同"
        elif any(
            pair["both_structurally_valid"] and pair["both_quality_pass"]
            for pair in effect_pairs
        ):
            classification = "EFFECT_PASS"
            reason = "完整网格指纹改变，且存在两端均结构有效、质量 PASS 的变体对"
        elif any(pair["both_structurally_valid"] for pair in effect_pairs):
            classification = "EFFECT_VALID_WITH_QUALITY_WARNING"
            reason = "完整网格指纹改变且结构有效，但至少一端未通过质量硬阈值"
        else:
            classification = "EFFECT_INVALID_MESH_ONLY"
            reason = "仅在包含负体积、重叠或 validity 异常的网格上观察到变化"

    successes = [record for record in relevant if record.get("generation_success")]
    effect_pairs = [pair for pair in pairs if pair.get("mesh_changed")]
    return {
        "key": key,
        "description": inventory_row.get("description"),
        "group": inventory_row.get("general_group"),
        "target_kind": inventory_row.get("target_kind"),
        "value_type": inventory_row.get("value_type"),
        "topologies": inventory_row.get("topologies"),
        "classification": classification,
        "reason": reason,
        "case_count": len(relevant),
        "success_count": len(successes),
        "valid_count": sum(bool(record.get("structurally_valid")) for record in successes),
        "quality_pass_count": sum(bool(record.get("quality_pass")) for record in successes),
        "readback_failure_count": sum(readback_failed(record) for record in successes),
        "stable_readback_mapping": stable_readback_mapping,
        "comparable_pair_count": len(pairs),
        "effect_pair_count": len(effect_pairs),
        "topology_or_size_effect_count": sum(
            bool(pair.get("topology_or_size_changed")) for pair in effect_pairs
        ),
        "coordinate_only_effect_count": sum(
            bool(pair.get("coordinate_only_changed")) for pair in effect_pairs
        ),
        "values_tested": [record.get("variable_value") for record in relevant],
        "records": relevant,
        "pairs": pairs,
    }


def analyze_baselines(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_topology: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_topology[str(record.get("topology"))].append(record)
    result: dict[str, Any] = {}
    for topology in ("default", "hoh", "hi"):
        items = by_topology.get(topology, [])
        successful = [item for item in items if item.get("generation_success")]
        hashes = {item.get("fingerprint") for item in successful}
        coordinate_hashes = {item.get("coordinate_fingerprint") for item in successful}
        quality_vectors = {
            tuple(item.get(field) for field in QUALITY_FIELDS)
            for item in successful
        }
        result[topology] = {
            "case_count": len(items),
            "success_count": len(successful),
            "fingerprint_repeatable": len(hashes) == 1 and None not in hashes,
            "coordinate_repeatable": len(coordinate_hashes) == 1 and None not in coordinate_hashes,
            "quality_repeatable": len(quality_vectors) == 1,
            "fingerprints": sorted(str(value) for value in hashes),
            "valid_count": sum(bool(item.get("structurally_valid")) for item in successful),
            "quality_pass_count": sum(bool(item.get("quality_pass")) for item in successful),
            "points": sorted(
                {item.get("number_of_points") for item in successful if item.get("number_of_points") is not None}
            ),
            "blocks": sorted(
                {item.get("number_of_blocks") for item in successful if item.get("number_of_blocks") is not None}
            ),
        }
    return result


def failure_cluster(record: dict[str, Any]) -> str:
    text = str(record.get("error") or "")
    summary = record.get("readback") or {}
    text += " " + str(summary.get("after_error") or "") + " " + str(summary.get("post_error") or "")
    lowered = text.lower()
    if record.get("status") == "blocked":
        return "拓扑基线门控"
    if "timeout" in lowered or "超时" in text:
        return "超时"
    if "license" in lowered or "许可证" in text:
        return "许可证/基础设施"
    if (
        "getter" in lowered
        or record.get("classification") == "FAIL_READBACK"
        or readback_failed(record)
    ):
        return "getter 回读"
    if "assert" in lowered:
        return "AutoGrid 断言"
    if "topolog" in lowered or "拓扑" in text:
        return "拓扑生成"
    if "negative" in lowered or (record.get("negative_cells") or 0) > 0:
        return "负体积"
    if record.get("overlapping_status") == "OVERLAP":
        return "网格重叠"
    if "not found" in lowered or "未匹配" in text or "不存在" in text:
        return "目标解析"
    if "runtimeerror" in lowered or "traceback" in lowered:
        return "脚本/API 运行时"
    return "其他"


def _flatten_case_row(record: dict[str, Any]) -> dict[str, Any]:
    readback = record.get("readback") or {}
    return {
        "case_id": record.get("case_id"),
        "category": record.get("category"),
        "topology": record.get("topology"),
        "variable_key": record.get("variable_key"),
        "variable_value": json.dumps(record.get("variable_value"), ensure_ascii=False),
        "context_id": record.get("context_id"),
        "status": record.get("status"),
        "generation_success": record.get("generation_success"),
        "structurally_valid": record.get("structurally_valid"),
        "quality_pass": record.get("quality_pass"),
        "fingerprint": record.get("fingerprint"),
        "coordinate_fingerprint": record.get("coordinate_fingerprint"),
        "fingerprint_source": record.get("fingerprint_source"),
        "number_of_blocks": record.get("number_of_blocks"),
        "total_block_points": record.get("total_block_points"),
        "total_block_cells": record.get("total_block_cells"),
        **{field: record.get(field) for field in QUALITY_FIELDS},
        "mesh_validity": record.get("mesh_validity"),
        "overlapping_status": record.get("overlapping_status"),
        "readback_before": json.dumps(readback.get("before"), ensure_ascii=False),
        "readback_after": json.dumps(readback.get("after"), ensure_ascii=False),
        "readback_post_generation": json.dumps(
            readback.get("post_generation"), ensure_ascii=False
        ),
        "readback_matches_after": readback.get("matches_after"),
        "readback_matches_post_generation": readback.get("matches_post_generation"),
        "readback_stable_mapping": readback.get("stable_mapping"),
        "error": record.get("error"),
        "out_dir": record.get("out_dir"),
    }


def _markdown_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        cells = [
            str(value if value is not None else "")
            .replace("|", "\\|")
            .replace("\n", " ")
            for value in row
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render_markdown(analysis: dict[str, Any]) -> str:
    controls = analysis["controls"]
    cases = analysis["cases"]
    lines = [
        "# Rotor37 通用网格控制参数自动化验证报告",
        "",
        "> 本报告由保存的 schema 3 完整坐标指纹与 qualityReport 数据自动生成；",
        "> 工程解释和可迁移性判断见同目录 `agent_deep_analysis.md`。",
        "",
        "## 1. 覆盖与结论总览",
        "",
        f"- 注册控制：{analysis['inventory_counts']['registered']} 项",
        f"- 通用候选：{analysis['inventory_counts']['general']} 项",
        f"- 明确排除：{analysis['inventory_counts']['excluded']} 项",
        f"- 变量案例：{len(cases)} 个；成功生成：{sum(bool(item['generation_success']) for item in cases)} 个",
        "",
    ]
    counts = Counter(item["classification"] for item in controls)
    lines.extend(
        _markdown_table(
            ["最终分类", "控制数"],
            ((name, counts[name]) for name in sorted(counts)),
        )
    )
    lines.extend(["", "## 2. 三拓扑 A/A 重复性", ""])
    lines.extend(
        _markdown_table(
            [
                "拓扑",
                "成功/总数",
                "完整指纹重复",
                "坐标指纹重复",
                "结构有效",
                "质量 PASS",
                "点数",
                "block 数",
            ],
            (
                (
                    topology,
                    f"{item['success_count']}/{item['case_count']}",
                    item["fingerprint_repeatable"],
                    item["coordinate_repeatable"],
                    item["valid_count"],
                    item["quality_pass_count"],
                    item["points"],
                    item["blocks"],
                )
                for topology, item in analysis["baselines"].items()
            ),
        )
    )
    lines.extend(["", "## 3. 156 项通用候选逐项判定", ""])
    candidate_controls = [item for item in controls if item["classification"] != "EXCLUDED"]
    lines.extend(
        _markdown_table(
            [
                "控制键",
                "分组",
                "最终分类",
                "成功/案例",
                "可比对",
                "有效差异对",
                "尺寸/拓扑差异",
                "纯坐标差异",
                "说明",
            ],
            (
                (
                    f"`{item['key']}`",
                    item["group"],
                    item["classification"],
                    f"{item['success_count']}/{item['case_count']}",
                    item["comparable_pair_count"],
                    item["effect_pair_count"],
                    item["topology_or_size_effect_count"],
                    item["coordinate_only_effect_count"],
                    item["reason"],
                )
                for item in candidate_controls
            ),
        )
    )
    lines.extend(["", "## 4. 逐取值生成、指纹与质量结果", ""])
    lines.extend(
        _markdown_table(
            [
                "控制键",
                "值",
                "上下文",
                "生成",
                "结构",
                "质量",
                "点数",
                "层级",
                "负体积",
                "最小偏斜角",
                "最大增长率",
                "block/坐标指纹",
            ],
            (
                (
                    f"`{item.get('variable_key')}`",
                    json.dumps(item.get("variable_value"), ensure_ascii=False),
                    item.get("context_id"),
                    item.get("generation_success"),
                    item.get("structurally_valid"),
                    item.get("quality_status"),
                    item.get("number_of_points"),
                    item.get("grid_levels"),
                    item.get("negative_cells"),
                    item.get("min_skewness_angle"),
                    item.get("max_expansion_ratio"),
                    f"{item.get('number_of_blocks')}/{str(item.get('coordinate_fingerprint') or '')[:12]}",
                )
                for item in cases
                if item.get("category") != "TARGET_SMOKE"
            ),
        )
    )
    lines.extend(["", "## 5. 344 项支持与排除总表", ""])
    lines.extend(
        _markdown_table(
            ["控制键", "策展分组", "状态", "说明"],
            (
                (
                    f"`{item['key']}`",
                    item["group"],
                    item["classification"],
                    item["reason"],
                )
                for item in controls
            ),
        )
    )
    lines.extend(["", "## 6. 失败模式与质量敏感性", ""])
    lines.extend(
        _markdown_table(
            ["失败簇", "案例数"],
            sorted(analysis["failure_clusters"].items()),
        )
    )
    sensitive = [
        item
        for item in candidate_controls
        if item["classification"]
        in {
            "EFFECT_VALID_WITH_QUALITY_WARNING",
            "EFFECT_INVALID_MESH_ONLY",
        }
    ]
    lines.extend(["", "### 质量敏感控制", ""])
    lines.extend(
        _markdown_table(
            ["控制键", "分类", "结构有效/成功", "质量 PASS/成功"],
            (
                (
                    f"`{item['key']}`",
                    item["classification"],
                    f"{item['valid_count']}/{item['success_count']}",
                    f"{item['quality_pass_count']}/{item['success_count']}",
                )
                for item in sensitive
            ),
        )
    )
    lines.extend(["", "### 临界 block / I-J-K", ""])
    critical_rows = []
    for item in cases:
        for metric, location in (item.get("critical_locations") or {}).items():
            critical_rows.append(
                (
                    item.get("case_id"),
                    metric,
                    json.dumps(location, ensure_ascii=False),
                )
            )
    lines.extend(_markdown_table(["案例", "指标", "位置"], critical_rows))
    lines.append("")
    return "\n".join(lines)


def analyze_campaign(campaign_dir: Path) -> dict[str, Any]:
    inventory = load_json(campaign_dir / "inventory" / "supported_controls.json", [])
    if not inventory:
        raise RuntimeError("缺少 inventory/supported_controls.json；请先执行 audit 阶段")

    baseline_raw = load_json(campaign_dir / "baseline_results.json", [])
    context_raw = load_json(campaign_dir / "context_baseline_results.json", [])
    case_raw = load_json(campaign_dir / "all_case_results.json")
    if case_raw is None:
        case_raw = load_json(campaign_dir / "case_results.json", [])
    pilot_raw = load_json(campaign_dir / "pilot_results.json", [])

    baselines = [case_record(item) for item in baseline_raw]
    context_baselines = [case_record(item) for item in context_raw]
    cases = [case_record(item) for item in case_raw]
    pilots = [case_record(item) for item in pilot_raw]

    controls = [
        classify_control(row, cases)
        for row in sorted(inventory, key=lambda item: item["key"])
    ]
    failures = [
        item
        for item in cases
        if not item.get("generation_success")
        or not item.get("structurally_valid")
        or readback_failed(item)
    ]
    failure_clusters = Counter(failure_cluster(item) for item in failures)
    group_stats: dict[str, Counter[str]] = defaultdict(Counter)
    for item in controls:
        group_stats[str(item.get("group"))][item["classification"]] += 1

    negative_control = [
        item for item in pilots if item.get("category") == "negative_control"
    ]
    negative_hashes = {
        item.get("fingerprint")
        for item in negative_control
        if item.get("generation_success")
    }
    analysis = {
        "schema_version": 1,
        "campaign_dir": str(campaign_dir),
        "inventory_counts": {
            "registered": len(inventory),
            "general": sum(bool(row.get("general_candidate")) for row in inventory),
            "excluded": sum(not bool(row.get("general_candidate")) for row in inventory),
        },
        "baselines": analyze_baselines(baselines),
        "negative_control": {
            "case_count": len(negative_control),
            "fingerprints": sorted(str(value) for value in negative_hashes),
            "same_fingerprint": len(negative_hashes) == 1 and None not in negative_hashes,
        },
        "group_stats": {
            group: dict(sorted(counts.items()))
            for group, counts in sorted(group_stats.items())
        },
        "classification_counts": dict(
            sorted(Counter(item["classification"] for item in controls).items())
        ),
        "failure_clusters": dict(sorted(failure_clusters.items())),
        "controls": controls,
        "cases": cases,
        "context_baselines": context_baselines,
        "pilots": pilots,
    }
    return analysis


def write_outputs(campaign_dir: Path, analysis: dict[str, Any]) -> None:
    results_dir = campaign_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    write_json(results_dir / "automated_analysis.json", analysis)

    control_rows = []
    for item in analysis["controls"]:
        control_rows.append(
            {
                key: item.get(key)
                for key in (
                    "key",
                    "description",
                    "group",
                    "target_kind",
                    "value_type",
                    "topologies",
                    "classification",
                    "reason",
                    "case_count",
                    "success_count",
                    "valid_count",
                    "quality_pass_count",
                    "readback_failure_count",
                    "stable_readback_mapping",
                    "comparable_pair_count",
                    "effect_pair_count",
                    "topology_or_size_effect_count",
                    "coordinate_only_effect_count",
                )
            }
        )
    write_csv(
        results_dir / "control_results.csv",
        control_rows,
        list(control_rows[0]),
    )
    write_json(results_dir / "control_results.json", analysis["controls"])

    case_rows = [_flatten_case_row(item) for item in analysis["cases"]]
    if case_rows:
        write_csv(results_dir / "case_results.csv", case_rows, list(case_rows[0]))
    fingerprint_rows = [
        {
            "case_id": item.get("case_id"),
            "variable_key": item.get("variable_key"),
            "variable_value": json.dumps(item.get("variable_value"), ensure_ascii=False),
            "context_id": item.get("context_id"),
            "comparison_sha256": item.get("fingerprint"),
            "coordinate_sha256": item.get("coordinate_fingerprint"),
            "source": item.get("fingerprint_source"),
            "number_of_blocks": item.get("number_of_blocks"),
            "total_block_points": item.get("total_block_points"),
            "total_block_cells": item.get("total_block_cells"),
            "grid_levels": item.get("grid_levels"),
            "blocks": json.dumps(item.get("blocks"), ensure_ascii=False),
            "probes": json.dumps(item.get("probes"), ensure_ascii=False),
        }
        for item in analysis["cases"]
    ]
    if fingerprint_rows:
        write_csv(
            results_dir / "mesh_fingerprints.csv",
            fingerprint_rows,
            list(fingerprint_rows[0]),
        )
    quality_rows = [
        {
            "case_id": item.get("case_id"),
            "variable_key": item.get("variable_key"),
            "variable_value": json.dumps(item.get("variable_value"), ensure_ascii=False),
            **{field: item.get(field) for field in QUALITY_FIELDS},
            "quality_status": item.get("quality_status"),
            "mesh_validity": item.get("mesh_validity"),
            "overlapping_status": item.get("overlapping_status"),
            "critical_locations": json.dumps(
                item.get("critical_locations"), ensure_ascii=False
            ),
        }
        for item in analysis["cases"]
    ]
    if quality_rows:
        write_csv(
            results_dir / "quality_metrics.csv",
            quality_rows,
            list(quality_rows[0]),
        )

    report = render_markdown(analysis)
    (results_dir / "automated_analysis.md").write_text(report, encoding="utf-8")
    failed = [
        item
        for item in analysis["cases"]
        if not item.get("generation_success")
        or not item.get("structurally_valid")
        or readback_failed(item)
    ]
    failure_lines = ["# Rotor37 campaign 失败簇", ""]
    for cluster, count in analysis["failure_clusters"].items():
        failure_lines.extend([f"## {cluster}（{count}）", ""])
        for item in failed:
            if failure_cluster(item) == cluster:
                detail = str(item.get("error") or "")
                if not detail and readback_failed(item):
                    readback = item.get("readback") or {}
                    detail = (
                        "expected="
                        + json.dumps(readback.get("expected"), ensure_ascii=False)
                        + "；after="
                        + json.dumps(readback.get("after"), ensure_ascii=False)
                        + "；post="
                        + json.dumps(
                            readback.get("post_generation"),
                            ensure_ascii=False,
                        )
                    )
                if not detail and not item.get("structurally_valid"):
                    detail = (
                        f"validity={item.get('mesh_validity')}；"
                        f"overlap={item.get('overlapping_status')}；"
                        f"negative_cells={item.get('negative_cells')}"
                    )
                failure_lines.append(
                    f"- `{item.get('case_id')}`：{detail[:500]}"
                )
        failure_lines.append("")
    (results_dir / "failure_clusters.md").write_text(
        "\n".join(failure_lines), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    args = parser.parse_args(argv)
    campaign_dir = args.campaign_dir.resolve()
    try:
        analysis = analyze_campaign(campaign_dir)
        write_outputs(campaign_dir, analysis)
    except RuntimeError as exc:
        print(f"分析失败：{exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "campaign_dir": str(campaign_dir),
                "inventory_counts": analysis["inventory_counts"],
                "classification_counts": analysis["classification_counts"],
                "failure_clusters": analysis["failure_clusters"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


class AnalyzeResultsUnitTests(unittest.TestCase):
    def test_value_comparison_handles_numeric_bool_and_tuple(self) -> None:
        self.assertTrue(values_equivalent(True, 1))
        self.assertTrue(values_equivalent((1.0, 2.0), [1, 2]))
        self.assertTrue(values_equivalent(1.0, 1.0 + 1.0e-9))
        self.assertFalse(values_equivalent("default", "hoh"))

    def test_schema2_summary_remains_analyzable(self) -> None:
        summary = {
            "schema_version": 2,
            "autogrid": {"outputs": {}},
            "quality": {
                "metrics": {"number_of_points": 100, "grid_levels": 3},
                "metadata": {},
                "result": {"status": "PASS"},
            },
        }
        signature = extract_mesh_signature(summary)
        self.assertEqual(signature["source"], "legacy_auxiliary")
        self.assertIsNotNone(signature["comparison_sha256"])

    def test_complete_coordinate_change_is_primary_effect_evidence(self) -> None:
        inventory = {
            "key": "row/example",
            "general_candidate": True,
            "general_group": "GENERAL_CORE",
        }
        records = []
        for value, fingerprint in ((1, "A"), (2, "B")):
            records.append(
                {
                    "case_id": f"case/{value}",
                    "category": "GENERAL_CORE",
                    "variable_key": "row/example",
                    "variable_value": value,
                    "context_id": "same",
                    "generation_success": True,
                    "fingerprint": fingerprint,
                    "structurally_valid": True,
                    "quality_pass": True,
                    "readback": {"has_getter": False},
                    "blocks": [],
                }
            )
        result = classify_control(inventory, records)
        self.assertEqual(result["classification"], "EFFECT_PASS")
        self.assertEqual(result["effect_pair_count"], 1)

    def test_integer_coded_enum_readback_is_a_stable_mapping(self) -> None:
        inventory = {
            "key": "row/example",
            "general_candidate": True,
            "general_group": "GENERAL_CORE",
        }
        records = []
        for value, observed, fingerprint in (
            ("disabled", 0, "A"),
            ("enabled", 1, "B"),
        ):
            records.append(
                {
                    "case_id": f"case/{value}",
                    "category": "GENERAL_CORE",
                    "variable_key": "row/example",
                    "variable_value": value,
                    "context_id": "same",
                    "generation_success": True,
                    "fingerprint": fingerprint,
                    "structurally_valid": True,
                    "quality_pass": True,
                    "readback": {
                        "has_getter": True,
                        "matches_after": False,
                        "matches_post_generation": False,
                        "after": observed,
                        "post_generation": observed,
                        "post_status": "readback",
                    },
                    "blocks": [],
                }
            )
        result = classify_control(inventory, records)
        self.assertEqual(result["classification"], "EFFECT_PASS")
        self.assertEqual(
            result["stable_readback_mapping"],
            [
                {"requested": "disabled", "observed": 0},
                {"requested": "enabled", "observed": 1},
            ],
        )
        self.assertEqual(result["readback_failure_count"], 0)

    def test_single_default_readback_does_not_verify_multiple_variants(self) -> None:
        inventory = {
            "key": "row/example",
            "general_candidate": True,
            "general_group": "GENERAL_CORE",
        }
        records = []
        for value, matches in ((1, True), (2, False)):
            records.append(
                {
                    "case_id": f"case/{value}",
                    "category": "GENERAL_CORE",
                    "variable_key": "row/example",
                    "variable_value": value,
                    "context_id": "same",
                    "generation_success": True,
                    "fingerprint": "A",
                    "structurally_valid": True,
                    "quality_pass": True,
                    "readback": {
                        "has_getter": True,
                        "matches_after": matches,
                        "matches_post_generation": matches,
                        "after": 1,
                        "post_generation": 1,
                        "post_status": "readback",
                    },
                    "blocks": [],
                }
            )
        result = classify_control(inventory, records)
        self.assertEqual(result["classification"], "FAIL_READBACK")


if __name__ == "__main__":
    raise SystemExit(main())
