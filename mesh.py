from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from autogrid import run_autogrid_init
from geomturbo import parse_geomturbo
from quality import summarize_quality


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an initial AutoGrid mesh directly from a .geomTurbo file."
    )
    parser.add_argument("geomturbo", help="Input .geomTurbo file.")
    parser.add_argument("--out", default=None, help="Run/output directory. Defaults to runs/<stem>_<timestamp>.")
    parser.add_argument("--igg", default=None, help="IGG executable or full path. Overrides .env.")
    parser.add_argument("--no-row-wizard", action="store_true", help="Skip optional AutoGrid row wizard calls.")
    parser.add_argument("--dry-run", action="store_true", help="Create scripts and reports without launching IGG.")
    parser.add_argument("--timeout", type=int, default=None, help="AutoGrid timeout in seconds.")
    args = parser.parse_args()

    geomturbo_path = Path(args.geomturbo)
    if not geomturbo_path.exists():
        print(f"ERROR: geomTurbo file not found: {geomturbo_path}", file=sys.stderr)
        return 2

    run_dir = Path(args.out) if args.out else _default_run_dir(geomturbo_path)
    run_dir.mkdir(parents=True, exist_ok=True)

    geometry = parse_geomturbo(geomturbo_path)
    env_settings = _load_env_file(Path(".env"))
    igg_executable = args.igg or env_settings.get("IGG_EXE") or env_settings.get("IGG_PATH") or "igg"

    autogrid_run = run_autogrid_init(
        geomturbo_path,
        run_dir,
        igg_executable=igg_executable,
        use_row_wizard=not args.no_row_wizard,
        dry_run=args.dry_run,
        timeout_seconds=args.timeout,
    )

    quality_summary: dict[str, Any] | None = None
    missing_mesh_outputs = not args.dry_run and autogrid_run.returncode == 0 and not autogrid_run.outputs
    if missing_mesh_outputs:
        quality_summary = {
            "metrics_source": None,
            "metrics": {},
            "result": {
                "status": "UNKNOWN",
                "accepted": False,
                "reasons": ["No AutoGrid mesh outputs found"],
            },
        }
    elif not args.dry_run and autogrid_run.returncode == 0:
        try:
            quality_summary = summarize_quality(autogrid_run.outputs)
        except Exception as exc:
            quality_summary = {
                "metrics_source": None,
                "metrics": {},
                "result": {
                    "status": "UNKNOWN",
                    "accepted": False,
                    "reasons": [f"{type(exc).__name__}: {exc}"],
                },
            }

    run_summary = {
        "schema_version": 1,
        "run_dir": str(run_dir),
        "geometry": geometry.to_dict(),
        "autogrid": autogrid_run.to_dict(),
        "quality": quality_summary,
    }
    _write_json(run_dir / "run_summary.json", run_summary)

    report = _render_report(geometry.to_dict(), autogrid_run.to_dict(), quality_summary)
    (run_dir / "report.md").write_text(report, encoding="utf-8")

    print(json.dumps(run_summary, indent=2, default=str))
    if autogrid_run.returncode not in (None, 0):
        return 1
    if missing_mesh_outputs:
        return 1
    return 0


def _default_run_dir(geomturbo_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("runs") / f"{geomturbo_path.stem}_{stamp}"


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _render_report(
    geometry: dict[str, Any],
    autogrid_run: dict[str, Any],
    quality_summary: dict[str, Any] | None,
) -> str:
    lines = [
        "# AutoGrid Initial Mesh Report",
        "",
        "## Geometry",
        "",
        f"- Source: `{geometry['path']}`",
        f"- Units: `{geometry.get('units')}`",
        f"- Row count: {geometry['row_count']}",
        f"- Multi-row: {geometry['multi_row']}",
        f"- Has splitter: {geometry['has_splitter']}",
        f"- Has tip gap: {geometry['has_tip_gap']}",
        "",
        "| Row | Periodicity | Main blades | Blade sections | Splitter | Tip gap |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in geometry["rows"]:
        lines.append(
            f"| {row['name']} | {row.get('periodicity')} | {row.get('main_blades')} | "
            f"{len(row.get('blades', []))} | {row.get('has_splitter')} | {row.get('has_tip_gap')} |"
        )
    lines.extend(
        [
            "",
            "## AutoGrid",
            "",
            f"- Return code: {autogrid_run.get('returncode')}",
            f"- Script: `{autogrid_run.get('script')}`",
            f"- Command: `{_command_line(autogrid_run.get('command', []))}`",
            "",
            "## Outputs",
            "",
        ]
    )
    outputs = autogrid_run.get("outputs", {})
    if outputs:
        for key, value in outputs.items():
            lines.append(f"- {key}: `{value}`")
    else:
        lines.append("- No mesh outputs recorded.")

    lines.extend(["", "## Quality", ""])
    if quality_summary:
        result = quality_summary.get("result", {})
        metrics = quality_summary.get("metrics", {})
        lines.extend(
            [
                f"- Source: {quality_summary.get('metrics_source')}",
                f"- Status: {result.get('status')}",
                f"- Accepted: {result.get('accepted')}",
                f"- Negative cells: {metrics.get('negative_cells')}",
                f"- Number of points: {metrics.get('number_of_points')}",
                f"- Min skewness: {metrics.get('min_skewness_angle')}",
                f"- Max expansion ratio: {metrics.get('max_expansion_ratio')}",
                f"- Max aspect ratio: {metrics.get('max_aspect_ratio')}",
            ]
        )
        reasons = result.get("reasons") or []
        if reasons:
            lines.append("- Reasons: " + "; ".join(reasons))
    else:
        lines.append("- Not evaluated.")
    return "\n".join(lines) + "\n"


def _command_line(command: list[str]) -> str:
    return " ".join(str(part) for part in command)


if __name__ == "__main__":
    raise SystemExit(main())
