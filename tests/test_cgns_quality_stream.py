"""CGNS 质量降级解析的分块流式扫描一致性测试（CR-07）。

parse_embedded_cgns_quality 改为分块扫描后，其解析结果必须与旧整文件
``read_bytes`` 实现一致。本文件保留旧实现作为逐项对照基准，用合成文本
样本覆盖正常片段、跨块标记、多个外层片段、二进制噪声与缺失数据。
"""

from __future__ import annotations

import re
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
    CRITICAL_EXTREME,
    NUMBER,
    _add_wall_uniformity,
    _embedded_location,
    _first_float,
    _last_count,
    _quality_blocks,
    parse_embedded_cgns_quality,
)

VALID_CGNS = """\
NI_BEGIN NIGridQuality
NEGATIVE_CELLS 0
NUMBER_OF_POINTS 12345
MULTIGRID_LEVEL 3
NI_BEGIN NIGridQuality_skewness
average 25.0
min 18.0
max 40.0
minBlock Row1
minI 12
minJ 3
minK 1
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
maxBlock Row2
maxI 7
maxJ 8
maxK 9
NI_END NIGridQuality
NI_END NIGridQuality
"""


def _reference_parse(path: str | Path) -> dict[str, Any]:
    """旧实现基准：整文件 read_bytes 后执行与流式版相同的提取逻辑。"""

    text = Path(path).read_bytes().decode("latin1", errors="ignore")
    if "NIGridQuality" not in text:
        raise ValueError(f"No embedded NIGridQuality data found: {path}")
    metrics: dict[str, Any] = {
        "negative_cells": _last_count(text, rf"NEGATIVE_CELLS\s+{NUMBER}"),
        "number_of_points": _last_count(text, rf"NUMBER_OF_POINTS\s+{NUMBER}"),
        "grid_levels": _last_count(text, rf"MULTIGRID_LEVEL\s+{NUMBER}"),
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


def _temp_file(test_case: unittest.TestCase, data: bytes, name: str) -> str:
    directory = tempfile.mkdtemp(prefix="mesh_cgns_stream_")
    test_case.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    path = Path(directory) / name
    path.write_bytes(data)
    return str(path)


class CgnsStreamingParseTests(unittest.TestCase):
    """流式分块扫描与旧整读实现的逐项一致性。"""

    def test_streaming_matches_reference_on_valid_sample(self) -> None:
        path = _temp_file(self, VALID_CGNS.encode("latin1"), "mesh.cgns")
        expected = _reference_parse(path)
        parsed = parse_embedded_cgns_quality(path)
        self.assertEqual(parsed, expected)
        self.assertEqual(parsed["number_of_points"], 12345)
        self.assertEqual(parsed["min_skewness_angle"], 18.0)
        self.assertEqual(parsed["min_skewness_angle_critical_location"]["block"], "Row1")
        self.assertEqual(parsed["max_wall_distance_critical_location"]["i"], 7)
        self.assertIn("wall_distance_uniformity", parsed)

    def test_small_chunks_match_reference_on_all_boundaries(self) -> None:
        path = _temp_file(self, VALID_CGNS.encode("latin1"), "mesh.cgns")
        expected = _reference_parse(path)
        for chunk_bytes in (1, 2, 7, 31, 64):
            with self.subTest(chunk_bytes=chunk_bytes):
                parsed = parse_embedded_cgns_quality(path, _chunk_bytes=chunk_bytes)
                self.assertEqual(parsed, expected)

    def test_markers_straddling_default_chunk_boundary(self) -> None:
        # 构造约 4 MiB 文本，使外层开始、内层开始与末尾 NI_END 均跨 1 MiB 块边界。
        chunk = 1 << 20
        section = VALID_CGNS.strip()
        outer_begin = "NI_BEGIN NIGridQuality\n"
        inner_begin = "NI_BEGIN NIGridQuality_skewness\n"
        outer_end = "NI_END NIGridQuality\n"
        prefix = "X" * (chunk - 6)  # 外层开始标记从 chunk-6 处开始，跨入下一块
        # 内层 skewness 标记跨第 2 块边界：计算其前导长度。
        head = outer_begin + "NEGATIVE_CELLS 0\nNUMBER_OF_POINTS 12345\nMULTIGRID_LEVEL 3\n"
        body_after_skewness_begin = section[len(outer_begin) + len(head) + len(inner_begin):]
        lead = len(prefix) + len(outer_begin) + len(head)
        pad_inner = (chunk - 6 - lead) % chunk
        # 末尾 NI_END 跨下一块边界。
        before_end = len(prefix) + len(outer_begin) + len(head) + pad_inner + len(inner_begin) + len(body_after_skewness_begin)
        pad_end = (chunk - 3 - before_end) % chunk
        text = (
            prefix + outer_begin + head
            + "\n" * pad_inner
            + inner_begin + body_after_skewness_begin
            + "\n" * pad_end
            + outer_end
        )
        data = text.encode("latin1")
        path = _temp_file(self, data, "straddle.cgns")
        expected = _reference_parse(path)
        parsed = parse_embedded_cgns_quality(path)
        self.assertEqual(parsed, expected)
        self.assertGreater(len(data), 3 * chunk)

    def test_multiple_outer_sections_use_last_blocks(self) -> None:
        second = VALID_CGNS.replace("average 25.0", "average 33.0").replace(
            "min 18.0", "min 22.0"
        )
        path = _temp_file(self, (VALID_CGNS + "\n" + second).encode("latin1"), "mesh.cgns")
        expected = _reference_parse(path)
        parsed = parse_embedded_cgns_quality(path)
        self.assertEqual(parsed, expected)
        # 两个外层片段都保留，最后一个块的统计生效。
        self.assertEqual(parsed["avg_skewness_angle"], 33.0)
        self.assertEqual(parsed["min_skewness_angle"], 22.0)

    def test_stray_counts_outside_sections_still_take_last(self) -> None:
        # 旧实现整文件扫描，片段外的计数也参与“最后一次匹配”语义。
        text = "NEGATIVE_CELLS 7\n" + VALID_CGNS + "\nNEGATIVE_CELLS 2\n"
        path = _temp_file(self, text.encode("latin1"), "mesh.cgns")
        self.assertEqual(parse_embedded_cgns_quality(path), _reference_parse(path))
        self.assertEqual(parse_embedded_cgns_quality(path)["negative_cells"], 2)

    def test_binary_noise_around_section_matches_reference(self) -> None:
        noise = bytes(range(256)) * 32
        data = noise + VALID_CGNS.encode("latin1") + noise
        path = _temp_file(self, data, "mesh.cgns")
        expected = _reference_parse(path)
        for chunk_bytes in (13, 1 << 20):
            with self.subTest(chunk_bytes=chunk_bytes):
                self.assertEqual(parse_embedded_cgns_quality(path, _chunk_bytes=chunk_bytes), expected)

    def test_missing_quality_data_raises_same_error(self) -> None:
        path = _temp_file(self, b"just some mesh text without markers", "mesh.cgns")
        with self.assertRaises(ValueError) as streamed:
            parse_embedded_cgns_quality(path)
        with self.assertRaises(ValueError) as legacy:
            _reference_parse(path)
        self.assertEqual(str(streamed.exception), str(legacy.exception))

    def test_unterminated_section_keeps_available_counts(self) -> None:
        # 未闭合外层片段：旧实现找不到完整块但仍能提取片段内计数。
        text = "NI_BEGIN NIGridQuality\nNEGATIVE_CELLS 0\nNUMBER_OF_POINTS 777\nMULTIGRID_LEVEL 4\n"
        path = _temp_file(self, text.encode("latin1"), "mesh.cgns")
        self.assertEqual(parse_embedded_cgns_quality(path), _reference_parse(path))
        parsed = parse_embedded_cgns_quality(path)
        self.assertEqual(parsed["number_of_points"], 777)
        self.assertNotIn("min_skewness_angle", parsed)


if __name__ == "__main__":
    unittest.main()
