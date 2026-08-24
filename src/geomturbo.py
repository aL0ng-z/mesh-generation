"""解析 ``.geomTurbo`` 文件并提取叶轮机械几何拓扑摘要。"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, TextIO


MAX_PHYSICAL_LINE_CHARS = 1024 * 1024
MAX_IDENTIFIER_CHARS = 512
MAX_NESTING_DEPTH = 128
MAX_ROWS = 256
MAX_BLADES_PER_ROW = 64


class GeomTurboParseError(ValueError):
    """表示输入超出安全解析边界或无法按 geomTurbo 文本处理。"""

    def __init__(self, message: str, *, details: Mapping[str, Any]) -> None:
        self.details = dict(details)
        super().__init__(message)


@dataclass(frozen=True)
class BladeInfo:
    """描述单个叶片实体及其间隙、圆角等拓扑信息。"""

    name: str
    number_of_blades: int | None = None
    has_tip_gap: bool = False
    gap_sides: tuple[str, ...] = ()
    partial_gap_sides: tuple[str, ...] = ()
    fillet_sides: tuple[str, ...] = ()


@dataclass(frozen=True)
class RowInfo:
    """描述一个叶排包含的叶片实体及周期性信息。"""

    name: str
    periodicity: int | None = None
    blades: list[BladeInfo] = field(default_factory=list)
    has_tip_gap: bool = False

    @property
    def main_blades(self) -> int | None:
        """返回主叶片数量，缺失时回退到叶排周期数。"""

        if self.blades and self.blades[0].number_of_blades is not None:
            return self.blades[0].number_of_blades
        return self.periodicity

    @property
    def has_splitter(self) -> bool:
        """判断叶排中是否包含分流叶片。"""

        return len(self.blades) > 1 or any("spl" in blade.name.lower() for blade in self.blades)


@dataclass(frozen=True)
class GeomTurboSummary:
    """汇总一个 ``.geomTurbo`` 文件的几何与叶排信息。"""

    path: str
    version: str | None
    units: str | None
    units_factor: float | None
    row_count: int
    rows: list[RowInfo]

    @property
    def multi_row(self) -> bool:
        """判断几何是否包含多个叶排。"""

        return self.row_count > 1

    @property
    def has_splitter(self) -> bool:
        """判断任一叶排是否包含分流叶片。"""

        return any(row.has_splitter for row in self.rows)

    @property
    def has_tip_gap(self) -> bool:
        """判断任一叶排是否包含叶尖间隙。"""

        return any(row.has_tip_gap for row in self.rows)

    def to_dict(self) -> dict[str, Any]:
        """将几何摘要转换为可序列化字典。"""

        data = asdict(self)
        data["multi_row"] = self.multi_row
        data["has_splitter"] = self.has_splitter
        data["has_tip_gap"] = self.has_tip_gap
        for row_data, row in zip(data["rows"], self.rows):
            row_data["main_blades"] = row.main_blades
            row_data["has_splitter"] = row.has_splitter
        return data


def parse_geomturbo(path: str | Path) -> GeomTurboSummary:
    """读取 ``.geomTurbo`` 文件并返回结构化几何摘要。"""

    geom_path = Path(path)
    with geom_path.open("r", encoding="utf-8", errors="replace") as stream:
        version, units, units_factor, rows = _parse_lines(_iter_bounded_lines(stream))
    return GeomTurboSummary(
        path=str(geom_path),
        version=version,
        units=units,
        units_factor=units_factor,
        row_count=len(rows),
        rows=rows,
    )


def _iter_bounded_lines(stream: TextIO) -> Iterator[str]:
    """逐行读取并限制单条物理行，避免异常输入形成超大字符串。"""

    line_number = 0
    while True:
        raw_line = stream.readline(MAX_PHYSICAL_LINE_CHARS + 1)
        if raw_line == "":
            return
        line_number += 1
        if len(raw_line) > MAX_PHYSICAL_LINE_CHARS:
            raise GeomTurboParseError(
                f"geomTurbo 第 {line_number} 行超过允许的 "
                f"{MAX_PHYSICAL_LINE_CHARS} 字符",
                details={"max_line_chars": MAX_PHYSICAL_LINE_CHARS},
            )
        yield raw_line


def _parse_lines(lines: Iterable[str]) -> tuple[str | None, str | None, float | None, list[RowInfo]]:
    """单遍解析几何文件的元数据、叶排及叶片拓扑。"""

    version: str | None = None
    units: str | None = None
    units_factor_text: str | None = None
    rows: list[RowInfo] = []
    stack: list[str] = []
    current_row: dict[str, Any] | None = None
    current_blade: dict[str, Any] | None = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        key, value = _split_key_value(line)
        _validate_identifier(key, "键名")
        key_upper = key.upper()
        if value:
            if key_upper == "VERSION" and version is None:
                version = _validate_identifier(value, "VERSION")
            elif key_upper == "UNITS" and units is None:
                units = _validate_identifier(value, "UNITS")
            elif key_upper == "UNITS-FACTOR" and units_factor_text is None:
                units_factor_text = _validate_identifier(value, "UNITS-FACTOR")

        begin = re.match(r"NI_BEGIN\s+(\S+)(?:\s+(\S+))?", line, flags=re.IGNORECASE)
        if begin:
            block = _validate_identifier(begin.group(1), "块名").lower()
            block_arg = _validate_identifier(begin.group(2) or "", "块参数").lower()
            if len(stack) >= MAX_NESTING_DEPTH:
                raise GeomTurboParseError(
                    f"geomTurbo 嵌套深度超过允许的 {MAX_NESTING_DEPTH} 层",
                    details={"max_nesting_depth": MAX_NESTING_DEPTH},
                )
            stack.append(block)
            if block == "nirow":
                current_row = {
                    "name": f"row_{len(rows) + 1}",
                    "periodicity": None,
                    "blades": [],
                    "has_tip_gap": False,
                }
            elif block == "niblade" and current_row is not None:
                current_blade = {
                    "name": f"blade_{len(current_row['blades']) + 1}",
                    "number_of_blades": None,
                    "has_tip_gap": False,
                    "gap_sides": [],
                    "partial_gap_sides": [],
                    "fillet_sides": [],
                }
            elif block in {"nitipgap", "nishroudgap", "nihubgap"}:
                if current_blade is not None:
                    side = "hub" if block == "nihubgap" else "shroud"
                    _append_unique(current_blade["gap_sides"], side)
                    current_blade["has_tip_gap"] = side == "shroud" or current_blade["has_tip_gap"]
                if current_row is not None:
                    current_row["has_tip_gap"] = True
            elif block in {"nitippartialgap", "nishroudpartialgap", "nihubpartialgap"}:
                if current_blade is not None:
                    side = "hub" if block == "nihubpartialgap" else "shroud"
                    _append_unique(current_blade["partial_gap_sides"], side)
            elif block in {"nitipfillet", "nishroudfillet", "nihubfillet"}:
                if current_blade is not None:
                    side = "hub" if block == "nihubfillet" else "shroud"
                    _append_unique(current_blade["fillet_sides"], side)
            elif block == "ninonaxisurfaces" and block_arg == "tip_gap" and current_row is not None:
                current_row["has_tip_gap"] = True
            continue

        end = re.match(r"NI_END\s+(\S+)", line, flags=re.IGNORECASE)
        if end:
            block = _validate_identifier(end.group(1), "块名").lower()
            # 部分厂商版本把首行 ``GEOMETRY TURBO`` 作为隐式根块，末尾却
            # 使用显式 ``NI_END GEOMTURBO``；该唯一兼容形式不占用解析栈。
            if block == "geomturbo" and not stack:
                continue
            if not stack or stack[-1] != block:
                raise GeomTurboParseError(
                    "geomTurbo 的 NI_BEGIN/NI_END 块结构不匹配",
                    details={"reason": "mismatched_block"},
                )
            if block == "niblade" and current_row is not None and current_blade is not None:
                if len(current_row["blades"]) >= MAX_BLADES_PER_ROW:
                    raise GeomTurboParseError(
                        f"单个叶排的叶片实体超过允许的 {MAX_BLADES_PER_ROW} 个",
                        details={"max_blades_per_row": MAX_BLADES_PER_ROW},
                    )
                current_row["blades"].append(
                    BladeInfo(
                        name=current_blade["name"],
                        number_of_blades=current_blade["number_of_blades"],
                        has_tip_gap=current_blade["has_tip_gap"],
                        gap_sides=_unique_tuple(current_blade["gap_sides"]),
                        partial_gap_sides=_unique_tuple(current_blade["partial_gap_sides"]),
                        fillet_sides=_unique_tuple(current_blade["fillet_sides"]),
                    )
                )
                current_blade = None
            elif block == "nirow" and current_row is not None:
                if len(rows) >= MAX_ROWS:
                    raise GeomTurboParseError(
                        f"geomTurbo 叶排数量超过允许的 {MAX_ROWS} 个",
                        details={"max_rows": MAX_ROWS},
                    )
                blade_tip_gap = any(blade.has_tip_gap for blade in current_row["blades"])
                rows.append(
                    RowInfo(
                        name=current_row["name"],
                        periodicity=current_row["periodicity"],
                        blades=current_row["blades"],
                        has_tip_gap=bool(current_row["has_tip_gap"] or blade_tip_gap),
                    )
                )
                current_row = None
                current_blade = None
            if stack:
                stack.pop()
            continue

        if current_row is None:
            continue

        key_lower = key.lower()
        if key_lower == "name":
            current_block = stack[-1] if stack else ""
            if current_blade is not None and current_block == "niblade":
                current_blade["name"] = _validate_identifier(value, "叶片名称")
            elif current_blade is None and current_block == "nirow":
                current_row["name"] = _validate_identifier(value, "叶排名称")
        elif key_lower == "periodicity":
            current_row["periodicity"] = _to_int(value)
        elif key_lower == "number_of_blades" and current_blade is not None:
            current_blade["number_of_blades"] = _to_int(value)

    if stack:
        raise GeomTurboParseError(
            "geomTurbo 存在未闭合的 NI_BEGIN 块",
            details={"reason": "unclosed_block"},
        )
    return version, units, _to_float(units_factor_text), rows


def _split_key_value(line: str) -> tuple[str, str]:
    """将一行几何定义拆分为键和值。"""

    parts = line.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


def _validate_identifier(value: str, label: str) -> str:
    """限制会进入解析状态的 token 与名称长度。"""

    if len(value) > MAX_IDENTIFIER_CHARS:
        raise GeomTurboParseError(
            f"geomTurbo {label}超过允许的 {MAX_IDENTIFIER_CHARS} 字符",
            details={"max_identifier_chars": MAX_IDENTIFIER_CHARS},
        )
    return value


def _append_unique(values: list[str], value: str) -> None:
    """只保存首次出现的侧别，使重复技术块不扩张解析状态。"""

    if value not in values:
        values.append(value)


def _to_float(value: str | None) -> float | None:
    """尽可能将文本首项转换为浮点数，失败时返回空值。"""

    if value is None:
        return None
    try:
        number = float(value.split()[0])
    except (IndexError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value: str) -> int | None:
    """尽可能将文本转换为整数，失败时返回空值。"""

    try:
        number = float(value.split()[0])
        return int(number) if math.isfinite(number) else None
    except (IndexError, OverflowError, ValueError):
        return None


def _unique_tuple(values: list[str]) -> tuple[str, ...]:
    """按原顺序去重并返回不可变元组。"""

    return tuple(dict.fromkeys(values))
