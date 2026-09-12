"""质量指标验证：非法数值不得 PASS 的回归测试（CR-03）。

覆盖 PLAN.md 补充验收的完整质量矩阵：NaN、±Inf、指数溢出、零/负点数、
小数及布尔计数、非法角度/比例、负壁面距离、8 个必填字段逐一缺失，
以及硬阈值等号行为。每个非法用例同时断言 reasons 含字段路径、
accepted=False；涉及非有限值的对外结构断言转换为 null。
"""

from __future__ import annotations

import math
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SOURCE_DIR))

from quality import (  # noqa: E402
    QualityEvaluation,
    evaluate_quality,
    parse_embedded_cgns_quality,
    parse_quality_report,
    summarize_quality,
)


REQUIRED_FIELDS = (
    "negative_cells",
    "number_of_points",
    "grid_levels",
    "min_skewness_angle",
    "max_expansion_ratio",
    "min_spanwise_skewness_angle",
    "max_spanwise_expansion_ratio",
    "max_aspect_ratio",
)

VALID_REPORT = """\
AUTOGRID version 17.1

NI_BEGIN PROJECT_INFO
PROJECT : UnitQuality
ROW NAME : row 1
NUMBER OF POINTS 1000
NUMBER OF ROWS 1
NUMBER OF MAIN BLADES 17
NUMBER OF SPLITTER BLADES 0
NUMBER OF LAYERS 3
BLADE TO BLADE TOPOLOGY default
NI_END PROJECT_INFO

GRID QUALITY REPORT
Generation Date : 2026-09-01
Generation Time : 00:01:30
Mesh Validity : OK (No Overlapping)

Entire Mesh Quality
Number of Negative Cells : 0
Number of Points : 1000
Number of grid levels : 4
Minimal Skewness Angle : 25.0
Maximal Expansion Ratio : 2.0
Minimal Spanwise Skewness Angle : 150.0
Maximal Spanwise Expansion Ratio : 1.5
Maximal Aspect Ratio : 1000.0
"""

VALID_CGNS = """\
NI_BEGIN NIGridQuality
NEGATIVE_CELLS 0
NUMBER_OF_POINTS 12345
MULTIGRID_LEVEL 3
NI_BEGIN NIGridQuality_skewness
average 25.0
min 18.0
max 40.0
NI_END NIGridQuality
NI_BEGIN NIGridQuality_span_skewness
average 150.0
min 145.0
max 155.0
NI_END NIGridQuality
NI_BEGIN NIGridQuality_span_exp
average 1.2
min 1.1
max 1.5
NI_END NIGridQuality
NI_BEGIN NIGridQuality_aspectRatio
average 500.0
min 10.0
max 1000.0
NI_END NIGridQuality
NI_BEGIN NIGridQuality_expansionRatio
average 1.6
min 1.2
max 2.0
NI_END NIGridQuality
NI_BEGIN NIGridQuality_wallDistance
average 0.01
min 0.005
max 0.02
NI_END NIGridQuality
NI_END NIGridQuality
"""


def _valid_metrics(**overrides: Any) -> dict[str, Any]:
    """返回一份满足全部硬阈值与物理域的合法指标基线。"""

    metrics: dict[str, Any] = {
        "negative_cells": 0,
        "number_of_points": 1000,
        "grid_levels": 4,
        "min_skewness_angle": 25.0,
        "max_expansion_ratio": 2.0,
        "min_spanwise_skewness_angle": 150.0,
        "max_spanwise_expansion_ratio": 1.5,
        "max_aspect_ratio": 1000.0,
    }
    metrics.update(overrides)
    return metrics


def _temp_text_file(test_case: unittest.TestCase, text: str, name: str) -> str:
    """写入临时文本文件并在测试结束时清理。"""

    directory = tempfile.mkdtemp(prefix="mesh_quality_test_")
    test_case.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return str(path)


class QualityValidationTests(unittest.TestCase):
    """直接调用 evaluate_quality() 的非法指标回归。"""

    def _evaluation(self, **overrides: Any) -> QualityEvaluation:
        return evaluate_quality(_valid_metrics(**overrides))

    def _assert_invalid(self, result: QualityEvaluation, path: str, problem: str) -> None:
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(result.accepted)
        self.assertIn(f"{path}: {problem}", result.reasons)

    def test_valid_metrics_pass(self) -> None:
        result = self._evaluation()
        self.assertEqual(result.status, "PASS")
        self.assertTrue(result.accepted)
        self.assertEqual(result.reasons, [])

    def test_nan_expansion_ratio_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(max_expansion_ratio=math.nan),
            "quality.metrics.max_expansion_ratio",
            "非有限数值",
        )

    def test_positive_infinity_skewness_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(min_skewness_angle=math.inf),
            "quality.metrics.min_skewness_angle",
            "非有限数值",
        )

    def test_negative_infinity_aspect_ratio_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(max_aspect_ratio=-math.inf),
            "quality.metrics.max_aspect_ratio",
            "非有限数值",
        )

    def test_exponential_overflow_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(max_expansion_ratio=float("1e309")),
            "quality.metrics.max_expansion_ratio",
            "非有限数值",
        )

    def test_zero_points_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(number_of_points=0),
            "quality.metrics.number_of_points",
            "计数必须为正整数",
        )

    def test_negative_points_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(number_of_points=-5),
            "quality.metrics.number_of_points",
            "计数必须为正整数",
        )

    def test_zero_grid_levels_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(grid_levels=0),
            "quality.metrics.grid_levels",
            "计数必须为正整数",
        )

    def test_negative_cells_count_must_be_nonnegative(self) -> None:
        self._assert_invalid(
            self._evaluation(negative_cells=-1),
            "quality.metrics.negative_cells",
            "计数必须为非负整数",
        )

    def test_decimal_count_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(number_of_points=3.0),
            "quality.metrics.number_of_points",
            "计数必须为整数",
        )

    def test_boolean_count_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(number_of_points=True),
            "quality.metrics.number_of_points",
            "布尔值不是合法计数",
        )

    def test_boolean_negative_cells_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(negative_cells=False),
            "quality.metrics.negative_cells",
            "布尔值不是合法计数",
        )

    def test_angle_above_180_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(min_skewness_angle=181.0),
            "quality.metrics.min_skewness_angle",
            "角度必须在 [0, 180] 内",
        )

    def test_negative_angle_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(min_skewness_angle=-5.0),
            "quality.metrics.min_skewness_angle",
            "角度必须在 [0, 180] 内",
        )

    def test_negative_ratio_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(max_expansion_ratio=-2.0),
            "quality.metrics.max_expansion_ratio",
            "比例必须为正",
        )

    def test_zero_ratio_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(max_expansion_ratio=0.0),
            "quality.metrics.max_expansion_ratio",
            "比例必须为正",
        )

    def test_negative_wall_distance_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(min_wall_distance=-1.0),
            "quality.metrics.min_wall_distance",
            "壁面距离必须非负",
        )

    def test_boolean_real_statistic_is_unknown(self) -> None:
        self._assert_invalid(
            self._evaluation(max_expansion_ratio=True),
            "quality.metrics.max_expansion_ratio",
            "非实数",
        )

    def test_missing_required_fields_are_unknown(self) -> None:
        baseline = _valid_metrics()
        for field in REQUIRED_FIELDS:
            with self.subTest(field=field):
                metrics = dict(baseline)
                del metrics[field]
                result = evaluate_quality(metrics)
                self.assertEqual(result.status, "UNKNOWN")
                self.assertFalse(result.accepted)
                self.assertIn(f"quality.metrics.{field}: 缺失", result.reasons)

    def test_hard_limit_equality_still_passes(self) -> None:
        self.assertEqual(self._evaluation(max_expansion_ratio=3.0).status, "PASS")
        self.assertEqual(self._evaluation(grid_levels=3).status, "PASS")
        self.assertEqual(self._evaluation(min_skewness_angle=15.0).status, "PASS")


class QualityReportParseTests(unittest.TestCase):
    """parse_quality_report() 产出的模型不再静默截断计数。"""

    def test_valid_report_model_passes(self) -> None:
        path = _temp_text_file(self, VALID_REPORT, "mesh.qualityReport")
        model = parse_quality_report(path)
        self.assertEqual(model["metrics"]["number_of_points"], 1000)
        self.assertIsInstance(model["metrics"]["number_of_points"], int)
        result = evaluate_quality(model["metrics"])
        self.assertEqual(result.status, "PASS")
        self.assertTrue(result.accepted)
        summary = summarize_quality({"quality_report": path})
        self.assertEqual(summary["result"]["status"], "PASS")
        self.assertEqual(summary["quality_validation"]["status"], "VALID")
        self.assertEqual(summary["quality_validation"]["reasons"], [])

    def test_report_decimal_count_is_not_truncated(self) -> None:
        text = VALID_REPORT.replace("Number of Points : 1000", "Number of Points : 3.0")
        path = _temp_text_file(self, text, "mesh.qualityReport")
        model = parse_quality_report(path)
        self.assertEqual(model["metrics"]["number_of_points"], 3.0)
        result = evaluate_quality(model["metrics"])
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(result.accepted)
        self.assertIn(
            "quality.metrics.number_of_points: 计数必须为整数", result.reasons
        )

    def test_report_overflow_statistic_is_null_and_invalid(self) -> None:
        text = VALID_REPORT.replace(
            "Maximal Expansion Ratio : 2.0", "Maximal Expansion Ratio : 1e309"
        )
        path = _temp_text_file(self, text, "mesh.qualityReport")
        model = parse_quality_report(path)
        result = evaluate_quality(model["metrics"])
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(result.accepted)
        self.assertIn(
            "quality.metrics.max_expansion_ratio: 非有限数值", result.reasons
        )
        summary = summarize_quality({"quality_report": path})
        self.assertIsNone(summary["metrics"]["max_expansion_ratio"])
        self.assertEqual(summary["result"]["status"], "UNKNOWN")
        validation = summary["quality_validation"]
        self.assertEqual(validation["status"], "INVALID")
        self.assertIn(
            {"field": "quality.metrics.max_expansion_ratio", "problem": "非有限数值"},
            validation["reasons"],
        )
        self.assertIn("Maximal Expansion Ratio : 1e309", summary["raw"])

    def test_optional_statistic_invalid_tokens_are_not_discarded(self) -> None:
        for token in ("NaN", "Inf", "-Inf", "1e999", "invalid", ""):
            with self.subTest(token=token):
                path = _temp_text_file(
                    self, VALID_REPORT + f"Average Wall Distance : {token}\n", "mesh.qualityReport"
                )
                summary = summarize_quality({"quality_report": path}, units_factor=0.001)
                self.assertEqual(summary["result"]["status"], "UNKNOWN")
                self.assertFalse(summary["result"]["accepted"])
                self.assertIsNone(summary["metrics"]["avg_wall_distance"])
                self.assertTrue(any(
                    reason["field"] == "quality.metrics.avg_wall_distance"
                    for reason in summary["quality_validation"]["reasons"]
                ))
                json.dumps(summary, allow_nan=False)

    def test_all_row_criteria_and_counts_are_validated(self) -> None:
        cases = (
            ("Average Skewness Angle : 181", "criteria.skewness_angle.average"),
            ("Minimal Spanwise Skewness Angle : -1", "criteria.spanwise_skewness_angle.minimum"),
            ("Maximal Spanwise Expansion Ratio : 0", "criteria.spanwise_expansion_ratio.maximum"),
            ("Average Aspect Ratio : -1", "criteria.aspect_ratio.average"),
            ("Average Expansion Ratio : NaN", "criteria.expansion_ratio.average"),
            ("Average Wall Distance : 1e999", "criteria.wall_distance.average"),
            ("Minimal Wall Distance : invalid", "criteria.wall_distance.minimum"),
            ("Number of Negative Cells : -1", "negative_cells"),
            ("Number of Points : Inf", "number_of_points"),
            ("Number of grid levels : 3.5", "grid_levels"),
        )
        for line, field in cases:
            with self.subTest(line=line):
                path = _temp_text_file(
                    self, VALID_REPORT + f"row 1 Quality\n{line}\n", "mesh.qualityReport"
                )
                model = parse_quality_report(path)
                self.assertEqual(evaluate_quality(model).status, "UNKNOWN")
                summary = summarize_quality({"quality_report": path})
                self.assertEqual(summary["result"]["status"], "UNKNOWN")
                self.assertFalse(summary["result"]["accepted"])
                self.assertIn(
                    f"quality.entities[1].{field}",
                    [reason["field"] for reason in summary["quality_validation"]["reasons"]],
                )
                json.dumps(summary, allow_nan=False)

    def test_missing_and_invalid_fields_are_both_reported(self) -> None:
        path = _temp_text_file(
            self, VALID_REPORT.replace("Number of Points : 1000\n", "")
            + "Average Wall Distance : invalid\n", "mesh.qualityReport"
        )
        reasons = summarize_quality({"quality_report": path})["quality_validation"]["reasons"]
        self.assertIn({"field": "quality.metrics.number_of_points", "problem": "缺失"}, reasons)
        self.assertIn({
            "field": "quality.metrics.avg_wall_distance", "problem": "无法解析数值: 'invalid'"
        }, reasons)

    def test_project_row_invalid_count_is_not_discarded(self) -> None:
        path = _temp_text_file(
            self, VALID_REPORT.replace("NUMBER OF MAIN BLADES 17", "NUMBER OF MAIN BLADES bad"),
            "mesh.qualityReport",
        )
        summary = summarize_quality({"quality_report": path})
        self.assertFalse(summary["result"]["accepted"])
        self.assertIn({
            "field": "quality.project.rows[0].main_blades", "problem": "无法解析数值: 'bad'"
        }, summary["quality_validation"]["reasons"])
        self.assertIsNone(summary["project"]["rows"][0]["main_blades"])


class CgnsQualityParseTests(unittest.TestCase):
    """parse_embedded_cgns_quality() 的 CGNS 降级解析入口。"""

    def test_valid_cgns_metrics_pass(self) -> None:
        path = _temp_text_file(self, VALID_CGNS, "mesh.cgns")
        metrics = parse_embedded_cgns_quality(path)
        self.assertEqual(metrics["number_of_points"], 12345)
        self.assertIsInstance(metrics["number_of_points"], int)
        result = evaluate_quality(metrics)
        self.assertEqual(result.status, "PASS")
        self.assertTrue(result.accepted)
        summary = summarize_quality({"cgns": path})
        self.assertEqual(summary["result"]["status"], "PASS")
        self.assertEqual(summary["quality_validation"]["status"], "VALID")

    def test_cgns_decimal_count_is_not_truncated(self) -> None:
        text = VALID_CGNS.replace("NUMBER_OF_POINTS 12345", "NUMBER_OF_POINTS 3.0")
        path = _temp_text_file(self, text, "mesh.cgns")
        metrics = parse_embedded_cgns_quality(path)
        self.assertEqual(metrics["number_of_points"], 3.0)
        result = evaluate_quality(metrics)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(result.accepted)
        self.assertIn(
            "quality.metrics.number_of_points: 计数必须为整数", result.reasons
        )

    def test_cgns_overflow_statistic_is_null_and_invalid(self) -> None:
        text = VALID_CGNS.replace("max 2.0", "max 1e309")
        path = _temp_text_file(self, text, "mesh.cgns")
        metrics = parse_embedded_cgns_quality(path)
        result = evaluate_quality(metrics)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(result.accepted)
        self.assertIn(
            "quality.metrics.max_expansion_ratio: 非有限数值", result.reasons
        )
        summary = summarize_quality({"cgns": path})
        self.assertIsNone(summary["metrics"]["max_expansion_ratio"])
        self.assertEqual(summary["quality_validation"]["status"], "INVALID")
        self.assertIn(
            {"field": "quality.metrics.max_expansion_ratio", "problem": "非有限数值"},
            summary["quality_validation"]["reasons"],
        )

    def test_optional_cgns_statistic_and_count_invalid_tokens_are_retained(self) -> None:
        for token in ("NaN", "Inf", "1e999", "bad", ""):
            for original, replacement, field in (
                ("average 0.01", f"average {token}", "avg_wall_distance"),
                ("NUMBER_OF_POINTS 12345", f"NUMBER_OF_POINTS {token}", "number_of_points"),
            ):
                with self.subTest(token=token, field=field):
                    path = _temp_text_file(self, VALID_CGNS.replace(original, replacement), "mesh.cgns")
                    summary = summarize_quality({"cgns": path})
                    self.assertEqual(summary["result"]["status"], "UNKNOWN")
                    self.assertFalse(summary["result"]["accepted"])
                    self.assertIsNone(summary["metrics"][field])
                    self.assertTrue(any(
                        reason["field"] == f"quality.metrics.{field}"
                        for reason in summary["quality_validation"]["reasons"]
                    ))
                    json.dumps(summary, allow_nan=False)


class QualitySourceSelectionTests(unittest.TestCase):
    """summarize_quality() 的数据源优先级与无来源行为。"""

    def test_summarize_without_quality_source_is_unknown(self) -> None:
        summary = summarize_quality({})
        self.assertEqual(summary["metrics_source"], None)
        self.assertEqual(summary["metrics"], {})
        result = summary["result"]
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reasons"], ["No quality source found"])
        self.assertEqual(summary["quality_validation"]["status"], "UNKNOWN")
        self.assertEqual(
            summary["quality_validation"]["reasons"],
            [{"field": "quality", "problem": "No quality source found"}],
        )
        self.assertIsNone(summary["raw"])

    def test_quality_report_takes_priority_over_cgns(self) -> None:
        report_path = _temp_text_file(self, VALID_REPORT, "mesh.qualityReport")
        cgns_path = _temp_text_file(self, VALID_CGNS, "mesh.cgns")
        summary = summarize_quality(
            {"quality_report": report_path, "cgns": cgns_path}
        )
        self.assertEqual(summary["metrics_source"], "quality_report")
        self.assertEqual(summary["result"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
