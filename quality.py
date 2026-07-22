from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


NUMBER = r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)"

HARD_LIMITS = {
    "negative_cells": 0,
    "min_grid_levels": 3,
    "min_skewness_angle": 15.0,
    "max_expansion_ratio": 3.0,
    "max_spanwise_deviation": 40.0,
    "max_spanwise_expansion_ratio": 2.0,
    "max_aspect_ratio": 15000.0,
}


@dataclass(frozen=True)
class QualityEvaluation:
    status: str
    accepted: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize_quality(outputs: dict[str, str]) -> dict[str, Any]:
    metrics_source = None
    metrics: dict[str, Any] = {}
    quality_report = outputs.get("quality_report")
    cgns = outputs.get("cgns")
    if quality_report:
        metrics_source = "quality_report"
        metrics = parse_quality_report(Path(quality_report))
    elif cgns:
        metrics_source = "embedded_cgns"
        metrics = parse_embedded_cgns_quality(Path(cgns))
    else:
        return {
            "metrics_source": None,
            "metrics": {},
            "result": QualityEvaluation("UNKNOWN", False, ["No quality source found"]).to_dict(),
        }

    result = evaluate_quality(metrics)
    summary = {
        "metrics_source": metrics_source,
        "metrics": metrics,
        "result": result.to_dict(),
    }
    return summary


def parse_quality_report(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    metrics: dict[str, Any] = {}
    if re.search(r"\bNo\s+Negative\s+Cell\b", text, flags=re.IGNORECASE):
        metrics["negative_cells"] = 0
    else:
        match = re.search(rf"\bNegative\s+Cells?\s*:?\s*{NUMBER}", text, flags=re.IGNORECASE)
        if match:
            metrics["negative_cells"] = int(float(match.group(1)))
    field_patterns = {
        "number_of_points": rf"Number\s+of\s+Points\s*:?\s*{NUMBER}",
        "grid_levels": rf"Number\s+of\s+grid\s+levels\s*:?\s*{NUMBER}",
        "min_skewness_angle": rf"Minimal\s+Skewness\s+Angle\s*:?\s*{NUMBER}",
        "avg_skewness_angle": rf"Average\s+Skewness\s+Angle\s*:?\s*{NUMBER}",
        "max_expansion_ratio": rf"Maximal\s+Expansion\s+Ratio\s*:?\s*{NUMBER}",
        "avg_expansion_ratio": rf"Average\s+Expansion\s+Ratio\s*:?\s*{NUMBER}",
        "min_spanwise_skewness_angle": rf"Minimal\s+Spanwise\s+Skewness\s+Angle\s*:?\s*{NUMBER}",
        "max_spanwise_expansion_ratio": rf"Maximal\s+Spanwise\s+Expansion\s+Ratio\s*:?\s*{NUMBER}",
        "max_aspect_ratio": rf"Maximal\s+Aspect\s+Ratio\s*:?\s*{NUMBER}",
        "avg_aspect_ratio": rf"Average\s+Aspect\s+Ratio\s*:?\s*{NUMBER}",
        "min_wall_distance": rf"Minimal\s+wall\s+Distance\s*:?\s*{NUMBER}",
        "max_wall_distance": rf"Maximal\s+wall\s+Distance\s*:?\s*{NUMBER}",
    }
    for field, pattern in field_patterns.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = float(match.group(1))
            metrics[field] = int(value) if field in {"number_of_points", "grid_levels"} else value
    _add_wall_uniformity(metrics)
    return metrics


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
        "NIGridQuality_skewness": ("min_skewness_angle", "max_skewness_angle", "avg_skewness_angle"),
        "NIGridQuality_span_skewness": (
            "min_spanwise_skewness_angle",
            "max_spanwise_skewness_angle",
            "avg_spanwise_skewness_angle",
        ),
        "NIGridQuality_span_exp": (
            "min_spanwise_expansion_ratio",
            "max_spanwise_expansion_ratio",
            "avg_spanwise_expansion_ratio",
        ),
        "NIGridQuality_aspectRatio": ("min_aspect_ratio", "max_aspect_ratio", "avg_aspect_ratio"),
        "NIGridQuality_expansionRatio": (
            "min_expansion_ratio",
            "max_expansion_ratio",
            "avg_expansion_ratio",
        ),
        "NIGridQuality_wallDistance": ("min_wall_distance", "max_wall_distance", "avg_wall_distance"),
    }
    for block_name, fields in quality_map.items():
        blocks = _quality_blocks(text, block_name)
        if not blocks:
            continue
        block = blocks[-1]
        min_field, max_field, avg_field = fields
        metrics[avg_field] = _first_float(block, r"\baverage\s+" + NUMBER)
        metrics[min_field] = _first_float(block, r"\bmin\s+" + NUMBER)
        metrics[max_field] = _first_float(block, r"\bmax\s+" + NUMBER)
        for location_block in reversed(blocks):
            block_location = _first_text(location_block, r"\bmaxBlock\s+(\S+)")
            if block_location and block_location.lower() != "undef":
                metrics[f"{max_field}_block"] = block_location
                break
    _add_wall_uniformity(metrics)
    return {key: value for key, value in metrics.items() if value is not None}


def evaluate_quality(metrics: dict[str, Any]) -> QualityEvaluation:
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
    missing = sorted(field for field in required if field not in metrics)
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


def _quality_blocks(text: str, name: str) -> list[str]:
    pattern = rf"NI_BEGIN\s+{re.escape(name)}(?P<body>.*?)NI_END\s+NIGridQuality"
    return [match.group("body") for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL)]


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
    if min_wall and max_wall:
        metrics["wall_distance_uniformity"] = max_wall / min_wall
