from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AutoGridRun:
    command: list[str]
    returncode: int | None
    run_dir: str
    script: str
    outputs: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_autogrid_init(
    geomturbo_path: str | Path,
    run_dir: str | Path,
    *,
    igg_executable: str = "igg",
    output_prefix: str = "mesh",
    use_row_wizard: bool = True,
    dry_run: bool = False,
    timeout_seconds: int | None = None,
) -> AutoGridRun:
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)

    geom_source = Path(geomturbo_path).resolve()
    geom_copy = run_path / "input.geomTurbo"
    if geom_source != geom_copy.resolve():
        shutil.copy2(geom_source, geom_copy)

    script_path = run_path / "autogrid_init.py"
    script_path.write_text(
        render_autogrid_script(
            geomturbo_path=geom_copy.resolve(),
            output_prefix=output_prefix,
            use_row_wizard=use_row_wizard,
        ),
        encoding="utf-8",
    )

    igg_resolved = resolve_igg(igg_executable)
    command = [
        igg_resolved or igg_executable,
        "-autogrid5",
        "-batch",
        "-script",
        str(script_path.resolve()),
    ]
    if dry_run:
        return AutoGridRun(command=command, returncode=None, run_dir=str(run_path), script=str(script_path), outputs={})

    completed = subprocess.run(
        command,
        cwd=run_path.resolve(),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    (run_path / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    (run_path / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    outputs = collect_outputs(run_path, output_prefix)
    return AutoGridRun(
        command=command,
        returncode=completed.returncode,
        run_dir=str(run_path),
        script=str(script_path),
        outputs={key: str(value) for key, value in outputs.items()},
    )


def render_autogrid_script(
    *,
    geomturbo_path: str | Path,
    output_prefix: str = "mesh",
    use_row_wizard: bool = True,
) -> str:
    return f'''# Auto-generated for NUMECA AutoGrid5 / IGG batch execution.
# Starts from geomTurbo only; no .trb template and no manual mesh-parameter overrides.

import os

GEOMTURBO_FILE = r"{Path(geomturbo_path)}"
OUTPUT_PREFIX = r"{output_prefix}"
USE_ROW_WIZARD = {bool(use_row_wizard)!r}


def _call_first(names, args=(), required=True):
    for name in names:
        func = globals().get(name)
        if callable(func):
            print("Calling", name)
            return func(*args)
    message = "No available AutoGrid API among: " + ", ".join(names)
    if required:
        raise RuntimeError(message)
    print(message)
    return None


def _try_method(obj, names, args=()):
    if obj is None:
        return None
    for name in names:
        method = getattr(obj, name, None)
        if callable(method):
            try:
                print("Calling method", name)
                return method(*args)
            except Exception as exc:
                print("Method failed", name, exc)
    return None


def _try_row_wizard():
    if not USE_ROW_WIZARD:
        return
    row_getter = globals().get("row")
    if not callable(row_getter):
        print("No row() accessor; skipping row wizard.")
        return
    try:
        row_count = _guess_row_count()
    except Exception:
        row_count = 1
    for index in range(1, row_count + 1):
        try:
            row_object = row_getter(index)
        except Exception as exc:
            print("Could not access row", index, "skipping row wizard:", exc)
            continue
        wizard = _try_method(row_object, ["row_wizard", "get_row_wizard", "wizard"])
        if wizard is None:
            print("No row wizard object for row", index)
            continue
        _try_method(wizard, ["generate", "apply", "compute"])


def _guess_row_count():
    row_getter = globals().get("row")
    if not callable(row_getter):
        return 1
    count = 0
    for index in range(1, 100):
        try:
            value = row_getter(index)
        except Exception:
            break
        if value is None:
            break
        count = index
    return max(count, 1)


_call_first(["a5_new_project", "a5_create_project", "a5_reset_project"], (1,), required=False)
_call_first(
    [
        "a5_init_new_project_from_a_geomTurbo_file",
        "a5_init_from_geomTurbo_file",
        "a5_open_geomturbo",
        "a5_import_geomturbo",
        "a5_initialize_from_geomturbo",
    ],
    (GEOMTURBO_FILE,),
)
_try_row_wizard()

_TRB_OUT = os.path.join(os.getcwd(), OUTPUT_PREFIX + ".trb")
_IGG_OUT = os.path.join(os.getcwd(), OUTPUT_PREFIX + ".igg")
_CGNS_OUT = os.path.join(os.getcwd(), OUTPUT_PREFIX + ".cgns")

# AutoGrid derives the .qualityReport file name from the saved project path.
# Saving only after 3D generation leaves the report name undefined in batch mode.
print("Saving project before generation:", _TRB_OUT)
_call_first(["a5_save_project"], (_TRB_OUT,), required=False)

_call_first(["a5_generate_b2b_grid", "a5_generate_b2b", "a5_compute_b2b"])
_call_first(["a5_generate_3d", "a5_generate_3d_grid", "a5_generate_mesh", "a5_compute_mesh"])

_call_first(["a5_save_project", "a5_save_template"], (_TRB_OUT,), required=False)
_call_first(["a5_save_mesh"], (_IGG_OUT,))
_call_first(["a5_export_CGNS_project", "a5_export_unstructured_CGNS"], (_CGNS_OUT,), required=False)
print("AutoGrid geomTurbo init script completed for", OUTPUT_PREFIX)
'''


def collect_outputs(run_dir: str | Path, output_prefix: str = "mesh") -> dict[str, Path]:
    run_path = Path(run_dir)
    suffixes = {
        "igg": ".igg",
        "cgns": ".cgns",
        "trb": ".trb",
        "bcs": ".bcs",
        "info": ".info",
        "geom": ".geom",
        "geomturbo": ".geomTurbo",
        "quality_report": ".qualityReport",
    }
    outputs: dict[str, Path] = {}
    for key, suffix in suffixes.items():
        candidate = run_path / f"{output_prefix}{suffix}"
        if candidate.exists() and candidate.stat().st_size > 0:
            outputs[key] = candidate
    return outputs


def resolve_igg(executable: str = "igg") -> str | None:
    if os.path.sep in executable or (os.path.altsep and os.path.altsep in executable):
        path = Path(executable)
        return str(path) if path.exists() else None
    resolved = shutil.which(executable)
    if resolved:
        return resolved
    if executable.lower() not in {"igg", "igg.exe", "iggx86_64", "iggx86_64.exe"}:
        return None
    for root in _candidate_roots():
        candidate = root / "bin64" / "iggx86_64.exe"
        if candidate.exists():
            return str(candidate)
        candidate = root / "bin64" / "igg.exe"
        if candidate.exists():
            return str(candidate)
        candidate = root / "bin" / "igg"
        if candidate.exists():
            return str(candidate)
    return None


def _candidate_roots() -> list[Path]:
    env = os.environ
    roots: list[Path] = []
    for name in ("NUMECA_ROOT", "NUMECA_HOME", "FINE_ROOT", "FINE_HOME", "FINE171_ROOT"):
        if env.get(name):
            roots.append(Path(env[name]))
    if sys.platform.startswith("win"):
        program_data = Path(env.get("ProgramData", r"C:\ProgramData"))
        roots.extend([program_data / "NUMECA" / "fine171", program_data / "NUMECA"])
    else:
        roots.extend([Path("/opt/numeca"), Path("/usr/local/numeca"), Path("/opt/cadence")])

    expanded: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        candidates = [root]
        if root.exists():
            try:
                candidates.extend(
                    child
                    for child in root.iterdir()
                    if child.is_dir()
                    and any(token in child.name.lower() for token in ("fine", "numeca", "fidelity", "autogrid"))
                )
            except OSError:
                pass
        for candidate in candidates:
            key = str(candidate).lower()
            if key not in seen:
                expanded.append(candidate)
                seen.add(key)
    return expanded
