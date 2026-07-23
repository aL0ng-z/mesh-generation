from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


NUMBER_TEXT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
NUMBER = rf"({NUMBER_TEXT})"

HARD_LIMITS = {
    "negative_cells": 0,
    "min_grid_levels": 3,
    "min_skewness_angle": 15.0,
    "max_expansion_ratio": 3.0,
    "max_spanwise_deviation": 40.0,
    "max_spanwise_expansion_ratio": 2.0,
    "max_aspect_ratio": 15000.0,
}

CRITERIA = (
    "skewness_angle",
    "spanwise_skewness_angle",
    "spanwise_expansion_ratio",
    "aspect_ratio",
    "expansion_ratio",
    "wall_distance",
)

CRITICAL_EXTREME = {
    "skewness_angle": "minimum",
    "spanwise_skewness_angle": "minimum",
    "spanwise_expansion_ratio": "maximum",
    "aspect_ratio": "maximum",
    "expansion_ratio": "maximum",
    "wall_distance": "maximum",
}

LEGACY_CRITERION_PREFIX = {
    "skewness_angle": "skewness_angle",
    "spanwise_skewness_angle": "spanwise_skewness_angle",
    "spanwise_expansion_ratio": "spanwise_expansion_ratio",
    "aspect_ratio": "aspect_ratio",
    "expansion_ratio": "expansion_ratio",
    "wall_distance": "wall_distance",
}


@dataclass(frozen=True)
class QualityEvaluation:
    status: str
    accepted: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize_quality(
    outputs: dict[str, str],
    *,
    units_factor: float | None = None,
    units: str | None = None,
) -> dict[str, Any]:
    quality_report = outputs.get("quality_report")
    cgns = outputs.get("cgns")
    source_path = Path(quality_report or cgns) if quality_report or cgns else None
    if source_path is not None and (units_factor is None or units is None):
        discovered_units, discovered_factor = _discover_project_units(source_path)
        units = units or discovered_units
        units_factor = units_factor if units_factor is not None else discovered_factor

    if quality_report:
        model = parse_quality_report(quality_report, units_factor=units_factor, units=units)
        metrics_source = "quality_report"
    elif cgns:
        metrics = parse_embedded_cgns_quality(cgns)
        model = _model_from_flat_metrics(metrics, units_factor=units_factor, units=units)
        metrics_source = "embedded_cgns"
    else:
        return {
            "metrics_source": None,
            "metadata": _empty_metadata(),
            "project": _empty_project(units=units, units_factor=units_factor),
            "entities": [],
            "metrics": {},
            "result": QualityEvaluation("UNKNOWN", False, ["No quality source found"]).to_dict(),
        }

    metrics = model["metrics"]
    return {
        "metrics_source": metrics_source,
        "metadata": model["metadata"],
        "project": model["project"],
        "entities": model["entities"],
        "metrics": metrics,
        "result": evaluate_quality(metrics).to_dict(),
    }


def parse_quality_report(
    path: str | Path,
    *,
    units_factor: float | None = None,
    units: str | None = None,
) -> dict[str, Any]:
    """按 AutoGrid 17.1 报告章节解析完整质量模型。

    返回值同时包含 ``metadata/project/entities`` 和旧扁平 ``metrics``。
    为兼容旧调用，旧扁平字段也镜像在返回字典顶层。
    """

    report_path = Path(path)
    text = report_path.read_text(encoding="utf-8", errors="replace")
    if units_factor is None or units is None:
        discovered_units, discovered_factor = _discover_project_units(report_path)
        units = units or discovered_units
        units_factor = units_factor if units_factor is not None else discovered_factor

    metadata = _empty_metadata()
    project = _empty_project(units=units, units_factor=units_factor)
    entities: list[dict[str, Any]] = []
    project_rows: list[dict[str, Any]] = []
    current_project_row: dict[str, Any] | None = None
    current_entity: dict[str, Any] | None = None
    in_project_info = False
    in_quality_report = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            continue

        version_match = re.search(r"AUTOGRID\s+version\s+([^\s*]+)", stripped, flags=re.IGNORECASE)
        if version_match:
            metadata["autogrid_version"] = version_match.group(1)
            continue
        project_match = re.match(r"PROJECT\s*:\s*(.*)$", stripped, flags=re.IGNORECASE)
        if project_match:
            project["name"] = project_match.group(1).strip() or None
            continue
        template_match = re.match(r"TEMPLATE\s+FILE\s*:\s*(.*)$", stripped, flags=re.IGNORECASE)
        if template_match:
            project["template_path"] = template_match.group(1).strip() or None
            continue
        if re.match(r"NI_BEGIN\s+PROJECT_INFO", stripped, flags=re.IGNORECASE):
            in_project_info = True
            continue
        if re.match(r"NI_END\s+PROJECT_INFO", stripped, flags=re.IGNORECASE):
            if current_project_row is not None:
                project_rows.append(current_project_row)
                current_project_row = None
            in_project_info = False
            continue
        if "GRID QUALITY REPORT" in stripped.upper():
            in_quality_report = True
            continue

        if in_project_info:
            row_match = re.match(r"ROW\s+NAME\s*:\s*(.+?)\s*$", stripped, flags=re.IGNORECASE)
            if row_match:
                if current_project_row is not None:
                    project_rows.append(current_project_row)
                current_project_row = {
                    "name": row_match.group(1).strip(),
                    "main_blades": None,
                    "splitter_blades": None,
                    "number_of_points": None,
                    "layers": None,
                    "b2b_topology": None,
                }
                continue
            value_match = re.match(rf"NUMBER\s+OF\s+POINTS\s+({NUMBER_TEXT})", stripped, flags=re.IGNORECASE)
            if value_match:
                value = int(float(value_match.group(1)))
                if current_project_row is None:
                    project["number_of_points"] = value
                else:
                    current_project_row["number_of_points"] = value
                continue
            rows_match = re.match(rf"NUMBER\s+OF\s+ROWS\s+({NUMBER_TEXT})", stripped, flags=re.IGNORECASE)
            if rows_match and current_project_row is None:
                project["number_of_rows"] = int(float(rows_match.group(1)))
                continue
            if current_project_row is not None:
                project_field_patterns = (
                    ("main_blades", rf"NUMBER\s+OF\s+MAIN\s+BLADES\s+({NUMBER_TEXT})", int),
                    ("splitter_blades", rf"NUMBER\s+OF\s+SPLITTER\s+BLADES\s+({NUMBER_TEXT})", int),
                    ("layers", rf"NUMBER\s+OF\s+LAYERS\s+({NUMBER_TEXT})", int),
                    ("b2b_topology", r"BLADE\s+TO\s+BLADE\s+TOPOLOGY\s+(.+?)\s*$", str),
                )
                matched_project_field = False
                for field, pattern, converter in project_field_patterns:
                    match = re.match(pattern, stripped, flags=re.IGNORECASE)
                    if not match:
                        continue
                    current_project_row[field] = (
                        int(float(match.group(1))) if converter is int else match.group(1).strip()
                    )
                    matched_project_field = True
                    break
                if matched_project_field:
                    continue

        if not in_quality_report:
            continue

        date_match = re.match(r"Generation\s+Date\s*:\s*(.+?)\s*$", stripped, flags=re.IGNORECASE)
        if date_match:
            metadata["generation_date"] = date_match.group(1).strip()
            continue
        time_match = re.match(r"Generation\s+Time\s*:\s*(.+?)\s*$", stripped, flags=re.IGNORECASE)
        if time_match:
            raw_time = time_match.group(1).strip()
            metadata["generation_time"] = raw_time
            metadata["generation_time_seconds"] = _duration_seconds(raw_time)
            continue
        validity_match = re.match(r"Mesh\s+Validity\s*:\s*([^()]*)\s*(?:\((.*?)\))?\s*$", stripped, flags=re.IGNORECASE)
        if validity_match:
            metadata["mesh_validity"] = validity_match.group(1).strip() or None
            overlap_text = (validity_match.group(2) or "").strip() or None
            metadata["overlapping_message"] = overlap_text
            if overlap_text and re.search(r"\bNo\s+Overlapping\b", overlap_text, flags=re.IGNORECASE):
                metadata["overlapping_status"] = "NO_OVERLAP"
            elif overlap_text and re.search(r"Overlapping", overlap_text, flags=re.IGNORECASE):
                metadata["overlapping_status"] = "OVERLAP"
            else:
                metadata["overlapping_status"] = "UNKNOWN"
            continue

        section_match = re.match(r"(.+?)\s+Quality\s*$", stripped, flags=re.IGNORECASE)
        if section_match and "GRID QUALITY" not in stripped.upper():
            name = section_match.group(1).strip()
            scope = "entire_mesh" if name.lower() == "entire mesh" else "row"
            current_entity = _new_entity(scope=scope, name=name)
            entities.append(current_entity)
            continue
        if current_entity is None:
            continue

        if re.search(r"\bNo\s+Negative\s+Cell\b", stripped, flags=re.IGNORECASE):
            current_entity["negative_cells"] = 0
            continue
        negative_match = re.search(rf"\bNegative\s+Cells?\s*:?\s*({NUMBER_TEXT})", stripped, flags=re.IGNORECASE)
        if negative_match:
            current_entity["negative_cells"] = int(float(negative_match.group(1)))
            continue
        points_match = re.match(rf"Number\s+of\s+Points\s*:?\s*({NUMBER_TEXT})", stripped, flags=re.IGNORECASE)
        if points_match:
            current_entity["number_of_points"] = int(float(points_match.group(1)))
            continue
        levels_match = re.match(rf"Number\s+of\s+grid\s+levels\s*:?\s*({NUMBER_TEXT})", stripped, flags=re.IGNORECASE)
        if levels_match:
            current_entity["grid_levels"] = int(float(levels_match.group(1)))
            continue

        location = _parse_location_line(stripped)
        if location is not None:
            criterion_name, location_data = location
            criterion = current_entity["criteria"].setdefault(criterion_name, _empty_criterion())
            criterion["critical_location"] = location_data
            continue
        statistic = _parse_statistic_line(stripped)
        if statistic is not None:
            criterion_name, statistic_name, value = statistic
            criterion = current_entity["criteria"].setdefault(criterion_name, _empty_criterion())
            criterion[statistic_name] = value

    if current_project_row is not None:
        project_rows.append(current_project_row)
    project["rows"] = project_rows
    if project["number_of_rows"] is None and project_rows:
        project["number_of_rows"] = len(project_rows)

    for entity in entities:
        _finalize_entity_criteria(entity, units_factor=units_factor, units=units)
    _derive_entire_mesh_locations(entities)
    entire = next((entity for entity in entities if entity["scope"] == "entire_mesh"), None)
    metrics = _flatten_entity_metrics(entire) if entire is not None else {}
    model: dict[str, Any] = {
        "metadata": metadata,
        "project": project,
        "entities": entities,
        "metrics": metrics,
    }
    model.update(metrics)
    return model


def parse_embedded_cgns_quality(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_bytes().decode("latin1", errors="ignore")
    if "NIGridQuality" not in text:
        raise ValueError(f"No embedded NIGridQuality data found: {path}")

    metrics: dict[str, Any] = {
        "negative_cells": _last_int(text, r"NEGATIVE_CELLS\s+(\d+)"),
        "number_of_points": _last_int(text, r"NUMBER_OF_POINTS\s+(\d+)"),
        "grid_levels": _last_int(text, r"MULTIGRID_LEVEL\s+(\d+)"),
    }
    quality_map = {
        "NIGridQuality_skewness": ("skewness_angle", "min_skewness_angle", "max_skewness_angle", "avg_skewness_angle"),
        "NIGridQuality_span_skewness": (
            "spanwise_skewness_angle",
            "min_spanwise_skewness_angle",
            "max_spanwise_skewness_angle",
            "avg_spanwise_skewness_angle",
        ),
        "NIGridQuality_span_exp": (
            "spanwise_expansion_ratio",
            "min_spanwise_expansion_ratio",
            "max_spanwise_expansion_ratio",
            "avg_spanwise_expansion_ratio",
        ),
        "NIGridQuality_aspectRatio": ("aspect_ratio", "min_aspect_ratio", "max_aspect_ratio", "avg_aspect_ratio"),
        "NIGridQuality_expansionRatio": (
            "expansion_ratio",
            "min_expansion_ratio",
            "max_expansion_ratio",
            "avg_expansion_ratio",
        ),
        "NIGridQuality_wallDistance": ("wall_distance", "min_wall_distance", "max_wall_distance", "avg_wall_distance"),
    }
    for block_name, fields in quality_map.items():
        blocks = _quality_blocks(text, block_name)
        if not blocks:
            continue
        criterion_name, min_field, max_field, avg_field = fields
        block = blocks[-1]
        metrics[avg_field] = _first_float(block, r"\baverage\s+" + NUMBER)
        metrics[min_field] = _first_float(block, r"\bmin\s+" + NUMBER)
        metrics[max_field] = _first_float(block, r"\bmax\s+" + NUMBER)
        extreme = CRITICAL_EXTREME[criterion_name]
        location = _embedded_location(blocks, extreme=extreme)
        if location is not None:
            extreme_field = min_field if extreme == "minimum" else max_field
            metrics[f"{extreme_field}_critical_location"] = location
            metrics[f"{extreme_field}_block"] = location.get("block")
    _add_wall_uniformity(metrics)
    return {key: value for key, value in metrics.items() if value is not None}


def evaluate_quality(metrics: dict[str, Any]) -> QualityEvaluation:
    if "metrics" in metrics and "negative_cells" not in metrics and isinstance(metrics["metrics"], dict):
        metrics = metrics["metrics"]
    required = {
        "negative_cells",
        "number_of_points",
        "grid_levels",
        "min_skewness_angle",
        "max_expansion_ratio",
        "min_spanwise_skewness_angle",
        "max_spanwise_expansion_ratio",
        "max_aspect_ratio",
    }
    missing = sorted(field for field in required if field not in metrics or metrics[field] is None)
    if missing:
        return QualityEvaluation("UNKNOWN", False, [f"Missing metric: {field}" for field in missing])

    reasons: list[str] = []
    if metrics["negative_cells"] != HARD_LIMITS["negative_cells"]:
        reasons.append("Negative cells detected")
    if metrics["grid_levels"] < HARD_LIMITS["min_grid_levels"]:
        reasons.append("Insufficient grid levels")
    if metrics["min_skewness_angle"] < HARD_LIMITS["min_skewness_angle"]:
        reasons.append("Minimum skewness angle below hard limit")
    if metrics["max_expansion_ratio"] > HARD_LIMITS["max_expansion_ratio"]:
        reasons.append("Maximum expansion ratio above hard limit")
    if 180.0 - metrics["min_spanwise_skewness_angle"] > HARD_LIMITS["max_spanwise_deviation"]:
        reasons.append("Spanwise angular deviation above hard limit")
    if metrics["max_spanwise_expansion_ratio"] > HARD_LIMITS["max_spanwise_expansion_ratio"]:
        reasons.append("Spanwise expansion ratio above hard limit")
    if metrics["max_aspect_ratio"] > HARD_LIMITS["max_aspect_ratio"]:
        reasons.append("Maximum aspect ratio above hard limit")
    return QualityEvaluation("FAIL" if reasons else "PASS", not reasons, reasons)


def _new_entity(*, scope: str, name: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "name": name,
        "negative_cells": None,
        "number_of_points": None,
        "grid_levels": None,
        "criteria": {},
    }


def _empty_criterion() -> dict[str, Any]:
    return {"minimum": None, "maximum": None, "average": None, "critical_location": None}


def _empty_metadata() -> dict[str, Any]:
    return {
        "autogrid_version": None,
        "generation_date": None,
        "generation_time": None,
        "generation_time_seconds": None,
        "mesh_validity": None,
        "overlapping_status": "UNKNOWN",
        "overlapping_message": None,
    }


def _empty_project(*, units: str | None, units_factor: float | None) -> dict[str, Any]:
    return {
        "name": None,
        "template_path": None,
        "units": units,
        "units_factor": units_factor,
        "number_of_points": None,
        "number_of_rows": None,
        "rows": [],
    }


def _parse_statistic_line(line: str) -> tuple[str, str, float] | None:
    match = re.match(
        rf"(Minimal|Maximum|Maximal|Average|Minimum)\s+(.+?)\s*:\s*({NUMBER_TEXT})\s*$",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    statistic_word = match.group(1).lower()
    statistic = "minimum" if statistic_word in {"minimal", "minimum"} else "maximum" if statistic_word in {"maximal", "maximum"} else "average"
    criterion = _criterion_name(match.group(2))
    if criterion is None:
        return None
    return criterion, statistic, float(match.group(3))


def _parse_location_line(line: str) -> tuple[str, dict[str, Any]] | None:
    match = re.match(
        r"(Max|Min)\s+Location\s+(.+?)\s*:\s*(.+?)\s+I\s*,\s*J\s*,\s*K\s*:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    criterion = _criterion_name(match.group(2))
    if criterion is None:
        return None
    return criterion, {
        "extreme": CRITICAL_EXTREME[criterion],
        "reported_extreme": "maximum" if match.group(1).lower() == "max" else "minimum",
        "block": match.group(3).strip(),
        "i": int(match.group(4)),
        "j": int(match.group(5)),
        "k": int(match.group(6)),
    }


def _criterion_name(label: str) -> str | None:
    normalized = re.sub(r"\s+", " ", label.strip()).lower()
    aliases = {
        "skewness angle": "skewness_angle",
        "spanwise skewness angle": "spanwise_skewness_angle",
        "spanwise expansion ratio": "spanwise_expansion_ratio",
        "aspect ratio": "aspect_ratio",
        "expansion ratio": "expansion_ratio",
        "wall distance": "wall_distance",
    }
    return aliases.get(normalized)


def _finalize_entity_criteria(
    entity: dict[str, Any],
    *,
    units_factor: float | None,
    units: str | None,
) -> None:
    ordered: dict[str, Any] = {}
    for name in CRITERIA:
        criterion = entity["criteria"].get(name, _empty_criterion())
        location = criterion.get("critical_location")
        if location is not None:
            location["extreme"] = CRITICAL_EXTREME[name]
        if name == "wall_distance":
            criterion["unit"] = units or "project"
            criterion["si"] = {
                statistic: criterion.get(statistic) * units_factor
                if criterion.get(statistic) is not None and units_factor is not None
                else None
                for statistic in ("minimum", "maximum", "average")
            }
            criterion["si"]["unit"] = "m"
        ordered[name] = criterion
    entity["criteria"] = ordered


def _derive_entire_mesh_locations(entities: list[dict[str, Any]]) -> None:
    entire = next((entity for entity in entities if entity["scope"] == "entire_mesh"), None)
    rows = [entity for entity in entities if entity["scope"] == "row"]
    if entire is None:
        return
    for criterion_name in CRITERIA:
        criterion = entire["criteria"].get(criterion_name)
        if not criterion or criterion.get("critical_location") is not None:
            continue
        statistic = CRITICAL_EXTREME[criterion_name]
        global_value = criterion.get(statistic)
        if global_value is None:
            continue
        for row_entity in rows:
            row_criterion = row_entity["criteria"].get(criterion_name, {})
            row_value = row_criterion.get(statistic)
            location = row_criterion.get("critical_location")
            if row_value is None or location is None:
                continue
            if abs(row_value - global_value) <= max(1.0, abs(global_value)) * 1.0e-10:
                derived = deepcopy(location)
                derived["derived_from_scope"] = "row"
                derived["derived_from_name"] = row_entity["name"]
                criterion["critical_location"] = derived
                break


def _flatten_entity_metrics(entity: dict[str, Any] | None) -> dict[str, Any]:
    if entity is None:
        return {}
    metrics: dict[str, Any] = {}
    for field in ("negative_cells", "number_of_points", "grid_levels"):
        if entity.get(field) is not None:
            metrics[field] = entity[field]
    for criterion_name in CRITERIA:
        criterion = entity.get("criteria", {}).get(criterion_name, {})
        prefix = LEGACY_CRITERION_PREFIX[criterion_name]
        for statistic, short in (("minimum", "min"), ("maximum", "max"), ("average", "avg")):
            value = criterion.get(statistic)
            if value is not None:
                metrics[f"{short}_{prefix}"] = value
        location = criterion.get("critical_location")
        if location is not None:
            extreme_short = "min" if CRITICAL_EXTREME[criterion_name] == "minimum" else "max"
            field = f"{extreme_short}_{prefix}"
            metrics[f"{field}_critical_location"] = deepcopy(location)
            metrics[f"{field}_block"] = location.get("block")
    _add_wall_uniformity(metrics)
    return metrics


def _model_from_flat_metrics(
    metrics: dict[str, Any],
    *,
    units_factor: float | None,
    units: str | None,
) -> dict[str, Any]:
    entity = _new_entity(scope="entire_mesh", name="Entire Mesh")
    for field in ("negative_cells", "number_of_points", "grid_levels"):
        entity[field] = metrics.get(field)
    for criterion_name in CRITERIA:
        prefix = LEGACY_CRITERION_PREFIX[criterion_name]
        criterion = _empty_criterion()
        for statistic, short in (("minimum", "min"), ("maximum", "max"), ("average", "avg")):
            criterion[statistic] = metrics.get(f"{short}_{prefix}")
        extreme_short = "min" if CRITICAL_EXTREME[criterion_name] == "minimum" else "max"
        criterion["critical_location"] = metrics.get(f"{extreme_short}_{prefix}_critical_location")
        entity["criteria"][criterion_name] = criterion
    _finalize_entity_criteria(entity, units_factor=units_factor, units=units)
    return {
        "metadata": _empty_metadata(),
        "project": {
            **_empty_project(units=units, units_factor=units_factor),
            "number_of_points": metrics.get("number_of_points"),
        },
        "entities": [entity],
        "metrics": metrics,
    }


def _duration_seconds(value: str) -> int | None:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", value)
    if not match:
        return None
    hours, minutes, seconds = (int(field) for field in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _discover_project_units(source_path: Path) -> tuple[str | None, float | None]:
    candidates = (source_path.parent / "input.geomTurbo", source_path.with_suffix(".geomTurbo"))
    for candidate in candidates:
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8", errors="replace")
        units_match = re.search(r"^\s*UNITS\s+(.+?)\s*$", text, flags=re.IGNORECASE | re.MULTILINE)
        factor_match = re.search(rf"^\s*UNITS-FACTOR\s+({NUMBER_TEXT})", text, flags=re.IGNORECASE | re.MULTILINE)
        units = units_match.group(1).strip() if units_match else None
        factor = float(factor_match.group(1)) if factor_match else None
        return units, factor
    return None, None


def _quality_blocks(text: str, name: str) -> list[str]:
    pattern = rf"NI_BEGIN\s+{re.escape(name)}(?P<body>.*?)NI_END\s+NIGridQuality"
    return [match.group("body") for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL)]


def _embedded_location(blocks: list[str], *, extreme: str) -> dict[str, Any] | None:
    preferred_prefix = "min" if extreme == "minimum" else "max"
    for block in reversed(blocks):
        block_name = _first_text(block, rf"\b{preferred_prefix}Block\s+(\S+)")
        reported_extreme = extreme
        if not block_name and extreme == "minimum":
            # 17.1 的嵌入数据有时把“最差位置”统一写成 maxBlock。
            block_name = _first_text(block, r"\bmaxBlock\s+(\S+)")
            reported_extreme = "maximum"
        if not block_name or block_name.lower() == "undef":
            continue
        indices: dict[str, int | None] = {}
        for axis in ("I", "J", "K"):
            raw = _first_text(block, rf"\b{preferred_prefix}{axis}\s+(-?\d+)")
            if raw is None and extreme == "minimum":
                raw = _first_text(block, rf"\bmax{axis}\s+(-?\d+)")
            indices[axis.lower()] = int(raw) if raw is not None else None
        return {
            "extreme": extreme,
            "reported_extreme": reported_extreme,
            "block": block_name,
            **indices,
        }
    return None


def _first_float(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def _last_int(text: str, pattern: str) -> int | None:
    matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
    return int(matches[-1].group(1)) if matches else None


def _first_text(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _add_wall_uniformity(metrics: dict[str, Any]) -> None:
    min_wall = metrics.get("min_wall_distance")
    max_wall = metrics.get("max_wall_distance")
    if min_wall is not None and max_wall is not None and min_wall != 0:
        metrics["wall_distance_uniformity"] = max_wall / min_wall


__all__ = [
    "CRITERIA",
    "CRITICAL_EXTREME",
    "HARD_LIMITS",
    "QualityEvaluation",
    "evaluate_quality",
    "parse_embedded_cgns_quality",
    "parse_quality_report",
    "summarize_quality",
]
