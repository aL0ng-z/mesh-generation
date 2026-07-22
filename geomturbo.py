from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BladeInfo:
    name: str
    number_of_blades: int | None = None
    has_tip_gap: bool = False


@dataclass(frozen=True)
class RowInfo:
    name: str
    periodicity: int | None = None
    blades: list[BladeInfo] = field(default_factory=list)
    has_tip_gap: bool = False

    @property
    def main_blades(self) -> int | None:
        if self.blades and self.blades[0].number_of_blades is not None:
            return self.blades[0].number_of_blades
        return self.periodicity

    @property
    def has_splitter(self) -> bool:
        return len(self.blades) > 1 or any("spl" in blade.name.lower() for blade in self.blades)


@dataclass(frozen=True)
class GeomTurboSummary:
    path: str
    version: str | None
    units: str | None
    units_factor: float | None
    row_count: int
    rows: list[RowInfo]

    @property
    def multi_row(self) -> bool:
        return self.row_count > 1

    @property
    def has_splitter(self) -> bool:
        return any(row.has_splitter for row in self.rows)

    @property
    def has_tip_gap(self) -> bool:
        return any(row.has_tip_gap for row in self.rows)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["multi_row"] = self.multi_row
        data["has_splitter"] = self.has_splitter
        data["has_tip_gap"] = self.has_tip_gap
        for row_data, row in zip(data["rows"], self.rows):
            row_data["main_blades"] = row.main_blades
            row_data["has_splitter"] = row.has_splitter
        return data


def parse_geomturbo(path: str | Path) -> GeomTurboSummary:
    geom_path = Path(path)
    text = geom_path.read_text(encoding="utf-8", errors="replace")
    rows = _parse_rows(text)
    return GeomTurboSummary(
        path=str(geom_path),
        version=_first_value(text, "VERSION"),
        units=_first_value(text, "UNITS"),
        units_factor=_first_float_value(text, "UNITS-FACTOR"),
        row_count=len(rows),
        rows=rows,
    )


def _parse_rows(text: str) -> list[RowInfo]:
    rows: list[RowInfo] = []
    stack: list[str] = []
    current_row: dict[str, Any] | None = None
    current_blade: dict[str, Any] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        begin = re.match(r"NI_BEGIN\s+(\S+)(?:\s+(\S+))?", line, flags=re.IGNORECASE)
        if begin:
            block = begin.group(1).lower()
            block_arg = (begin.group(2) or "").lower()
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
                }
            elif block == "nitipgap":
                if current_blade is not None:
                    current_blade["has_tip_gap"] = True
                if current_row is not None:
                    current_row["has_tip_gap"] = True
            elif block == "ninonaxisurfaces" and block_arg == "tip_gap" and current_row is not None:
                current_row["has_tip_gap"] = True
            continue

        end = re.match(r"NI_END\s+(\S+)", line, flags=re.IGNORECASE)
        if end:
            block = end.group(1).lower()
            if block == "niblade" and current_row is not None and current_blade is not None:
                current_row["blades"].append(
                    BladeInfo(
                        name=current_blade["name"],
                        number_of_blades=current_blade["number_of_blades"],
                        has_tip_gap=current_blade["has_tip_gap"],
                    )
                )
                current_blade = None
            elif block == "nirow" and current_row is not None:
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

        key, value = _split_key_value(line)
        key_lower = key.lower()
        if key_lower == "name":
            current_block = stack[-1] if stack else ""
            if current_blade is not None and current_block == "niblade":
                current_blade["name"] = value
            elif current_blade is None and current_block == "nirow":
                current_row["name"] = value
        elif key_lower == "periodicity":
            current_row["periodicity"] = _to_int(value)
        elif key_lower == "number_of_blades" and current_blade is not None:
            current_blade["number_of_blades"] = _to_int(value)

    return rows


def _split_key_value(line: str) -> tuple[str, str]:
    parts = line.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


def _first_value(text: str, key: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(key)}\s+(.+?)\s*$", text, flags=re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _first_float_value(text: str, key: str) -> float | None:
    value = _first_value(text, key)
    if value is None:
        return None
    try:
        return float(value.split()[0])
    except ValueError:
        return None


def _to_int(value: str) -> int | None:
    try:
        return int(float(value.split()[0]))
    except (IndexError, ValueError):
        return None
