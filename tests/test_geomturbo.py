from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from geomturbo import (  # noqa: E402
    MAX_BLADES_PER_ROW,
    MAX_IDENTIFIER_CHARS,
    MAX_NESTING_DEPTH,
    MAX_PHYSICAL_LINE_CHARS,
    MAX_ROWS,
    GeomTurboParseError,
    parse_geomturbo,
)


class GeomTurboParserTests(unittest.TestCase):
    def test_parser_streams_file_without_read_text(self) -> None:
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("不应整文件读取")) as read_text:
            summary = parse_geomturbo(ROOT / "geometries" / "Rotor37.geomTurbo")

        read_text.assert_not_called()
        self.assertEqual(summary.version, "5.4")
        self.assertEqual(summary.units, "Meters")
        self.assertEqual(summary.units_factor, 1.0)
        self.assertEqual(summary.row_count, 1)

    def test_parser_rejects_an_abnormally_long_physical_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            geometry = Path(temp_dir) / "long-line.geomTurbo"
            geometry.write_text("X" * (MAX_PHYSICAL_LINE_CHARS + 1), encoding="utf-8")

            with self.assertRaisesRegex(GeomTurboParseError, "第 1 行超过允许"):
                parse_geomturbo(geometry)

    def test_parser_bounds_accumulated_structure(self) -> None:
        cases = {
            "nesting": "NI_BEGIN block\n" * (MAX_NESTING_DEPTH + 1),
            "rows": "NI_BEGIN nirow\nNI_END nirow\n" * (MAX_ROWS + 1),
            "blades": (
                "NI_BEGIN nirow\n"
                + "NI_BEGIN niblade\nNI_END niblade\n" * (MAX_BLADES_PER_ROW + 1)
                + "NI_END nirow\n"
            ),
            "identifier": f"NI_BEGIN {'X' * (MAX_IDENTIFIER_CHARS + 1)}\n",
            "mismatch": "NI_BEGIN nirow\nNI_END niblade\n",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            for name, content in cases.items():
                with self.subTest(name=name):
                    geometry = Path(temp_dir) / f"{name}.geomTurbo"
                    geometry.write_text(content, encoding="utf-8")
                    with self.assertRaises(GeomTurboParseError):
                        parse_geomturbo(geometry)

    def test_non_finite_numeric_fields_degrade_to_none(self) -> None:
        content = """GEOMETRY TURBO
UNITS-FACTOR nan
NI_BEGIN nirow
PERIODICITY 1e309
NI_BEGIN niblade
NUMBER_OF_BLADES 1e309
NI_END niblade
NI_END nirow
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            geometry = Path(temp_dir) / "non-finite.geomTurbo"
            geometry.write_text(content, encoding="utf-8")
            summary = parse_geomturbo(geometry)

        self.assertIsNone(summary.units_factor)
        self.assertIsNone(summary.rows[0].periodicity)
        self.assertIsNone(summary.rows[0].blades[0].number_of_blades)

    def test_rotor37_summary(self) -> None:
        summary = parse_geomturbo(ROOT / "geometries" / "Rotor37.geomTurbo")

        self.assertEqual(summary.row_count, 1)
        self.assertFalse(summary.multi_row)
        self.assertFalse(summary.has_splitter)
        self.assertTrue(summary.has_tip_gap)
        self.assertEqual(summary.rows[0].name, "row 1")
        self.assertEqual(summary.rows[0].main_blades, 36)
        self.assertEqual(summary.rows[0].periodicity, 36)
        self.assertEqual(summary.rows[0].blades[0].gap_sides, ("shroud",))

    def test_wp100_summary(self) -> None:
        summary = parse_geomturbo(ROOT / "geometries" / "WP100_comp.geomTurbo")

        self.assertEqual(summary.row_count, 3)
        self.assertTrue(summary.multi_row)
        self.assertTrue(summary.has_splitter)
        self.assertTrue(summary.has_tip_gap)
        self.assertEqual([row.name for row in summary.rows], ["impeller", "diffuser_radial", "diffuser_axial"])
        self.assertEqual(summary.rows[0].main_blades, 11)
        self.assertTrue(summary.rows[0].has_splitter)
        self.assertEqual(len(summary.rows[0].blades), 2)
        self.assertFalse(summary.rows[1].has_splitter)
        self.assertEqual(summary.units_factor, 0.001)
        self.assertEqual(summary.rows[0].blades[0].gap_sides, ("shroud",))

    def test_ori1_existing_fillet_side_is_discovered(self) -> None:
        summary = parse_geomturbo(ROOT / "geometries" / "ori1.geomTurbo")

        rotor = summary.rows[1]
        self.assertEqual(rotor.name, "Rotor")
        self.assertEqual(rotor.blades[0].gap_sides, ("shroud",))
        self.assertEqual(rotor.blades[0].fillet_sides, ("hub",))


if __name__ == "__main__":
    unittest.main()
