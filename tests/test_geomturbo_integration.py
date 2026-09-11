"""依赖真实工程几何文件的集成测试。

默认读取仓库根 ``geometries/`` 目录下的真实几何文件；可用环境变量
``GEOMTURBO_TEST_GEOMETRIES_DIR`` 指向其他目录。文件缺失时相应测试跳过。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

GEOMETRIES_DIR = Path(
    os.environ.get("GEOMTURBO_TEST_GEOMETRIES_DIR", ROOT / "geometries")
)

from geomturbo import parse_geomturbo  # noqa: E402


def _require_geometry(filename: str):
    """缺少真实几何文件时跳过集成测试。"""

    path = GEOMETRIES_DIR / filename
    return unittest.skipUnless(
        path.is_file(),
        f"缺少真实几何 {path}；请将 .geomTurbo 样例放入 geometries/ 目录"
        "（或通过环境变量 GEOMTURBO_TEST_GEOMETRIES_DIR 指定目录）后再运行集成测试",
    )


class GeomTurboIntegrationTests(unittest.TestCase):
    """使用真实工程几何的集成测试，缺失文件时明确跳过。"""

    @_require_geometry("Rotor37.geomTurbo")
    def test_rotor37_summary(self) -> None:
        summary = parse_geomturbo(GEOMETRIES_DIR / "Rotor37.geomTurbo")

        self.assertEqual(summary.row_count, 1)
        self.assertFalse(summary.multi_row)
        self.assertFalse(summary.has_splitter)
        self.assertTrue(summary.has_tip_gap)
        self.assertEqual(summary.rows[0].name, "row 1")
        self.assertEqual(summary.rows[0].main_blades, 36)
        self.assertEqual(summary.rows[0].periodicity, 36)
        self.assertEqual(summary.rows[0].blades[0].gap_sides, ("shroud",))

    @_require_geometry("WP100_comp.geomTurbo")
    def test_wp100_summary(self) -> None:
        summary = parse_geomturbo(GEOMETRIES_DIR / "WP100_comp.geomTurbo")

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

    @_require_geometry("ori1.geomTurbo")
    def test_ori1_existing_fillet_side_is_discovered(self) -> None:
        summary = parse_geomturbo(GEOMETRIES_DIR / "ori1.geomTurbo")

        rotor = summary.rows[1]
        self.assertEqual(rotor.name, "Rotor")
        self.assertEqual(rotor.blades[0].gap_sides, ("shroud",))
        self.assertEqual(rotor.blades[0].fillet_sides, ("hub",))


if __name__ == "__main__":
    unittest.main()
