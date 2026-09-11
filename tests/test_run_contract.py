"""批次②结果契约验收测试：独占运行目录、控制事件协议、Schema v4 与来源记录。

全部通过标准库 unittest 执行，使用 tests/fake_igg.py 作为 --igg 指向对象，
不依赖 NUMECA 环境。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
TESTS_DIR = ROOT / "tests"
for _directory in (SRC_DIR, TESTS_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import fake_igg  # noqa: E402
import test_analyze_results  # noqa: E402
from autogrid import LOCK_FILE_NAME, collect_outputs  # noqa: E402
from mesh import _write_json, source_signature  # noqa: E402

MESH_PY = SRC_DIR / "mesh.py"
GEOMETRY = ROOT / "geometries" / "fixtures" / "single_row.geomTurbo"

GRID_LEVELS_SET = "configuration/grid_levels=4"


def run_cli(args, *, cwd=None, env_extra=None, timeout=180):
    """以子进程方式运行 mesh.py CLI，返回 CompletedProcess。"""

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    if env_extra:
        env.update(env_extra)
    command = [sys.executable, "-B", str(MESH_PY), *args]
    return subprocess.run(
        command,
        cwd=str(cwd or ROOT),
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def load_summary(out_dir):
    return json.loads((out_dir / "run_summary.json").read_text(encoding="utf-8"))


class RunContractTests(unittest.TestCase):
    """覆盖 PLAN.md 批次②内核侧验收矩阵。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.igg = str(fake_igg.write_launcher(self.tmp))

    def tearDown(self):
        self._tmp.cleanup()

    def _behavior_env(self, behavior):
        return {fake_igg.BEHAVIOR_ENV: behavior}

    def test_default_run_dir_contains_uuid(self):
        completed = run_cli([str(GEOMETRY), "--dry-run"], cwd=self.tmp)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        summary = json.loads(completed.stdout)
        run_dir = summary["run_dir"]
        self.assertRegex(
            run_dir,
            r"runs[\\/]single_row_\d{8}_\d{6}_[0-9a-f]{32}$",
        )
        self.assertRegex(summary["run_id"], r"^[0-9a-f]{32}$")
        self.assertTrue((self.tmp / run_dir / LOCK_FILE_NAME).exists())

    def test_explicit_out_dir_rejects_existing_files_and_preserves_them(self):
        out = self.tmp / "reuse"
        out.mkdir()
        (out / "mesh.igg").write_bytes(b"old-igg-bytes")
        (out / "run_summary.json").write_text("{}", encoding="utf-8")
        completed = run_cli([str(GEOMETRY), "--out", str(out), "--dry-run"])
        self.assertEqual(completed.returncode, 2)
        self.assertIn("错误", completed.stderr.decode(errors="replace"))
        self.assertEqual((out / "mesh.igg").read_bytes(), b"old-igg-bytes")
        self.assertEqual((out / "run_summary.json").read_text(encoding="utf-8"), "{}")
        self.assertFalse((out / LOCK_FILE_NAME).exists())

    def test_explicit_out_dir_with_existing_lock_is_rejected(self):
        out = self.tmp / "locked"
        out.mkdir()
        (out / LOCK_FILE_NAME).write_text("{}", encoding="utf-8")
        completed = run_cli([str(GEOMETRY), "--out", str(out), "--dry-run"])
        self.assertEqual(completed.returncode, 2)
        self.assertIn("占用", completed.stderr.decode(errors="replace"))
        self.assertEqual((out / LOCK_FILE_NAME).read_text(encoding="utf-8"), "{}")

    def test_explicit_out_dir_accepts_worker_whitelist_logs(self):
        out = self.tmp / "whitelist"
        out.mkdir()
        (out / "worker.stdout.log").write_text("worker log", encoding="utf-8")
        (out / "worker.stderr.log").write_text("worker log", encoding="utf-8")
        completed = run_cli([str(GEOMETRY), "--out", str(out), "--dry-run"])
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertTrue((out / LOCK_FILE_NAME).exists())
        self.assertEqual(
            (out / "worker.stdout.log").read_text(encoding="utf-8"), "worker log"
        )

    def test_dry_run_occupied_dir_cannot_be_reused(self):
        out = self.tmp / "dry"
        first = run_cli([str(GEOMETRY), "--out", str(out), "--dry-run"])
        self.assertEqual(first.returncode, 0, first.stderr.decode(errors="replace"))
        second = run_cli([str(GEOMETRY), "--out", str(out), "--dry-run"])
        self.assertEqual(second.returncode, 2)
        self.assertIn("占用", second.stderr.decode(errors="replace"))
        self.assertTrue((out / LOCK_FILE_NAME).exists())

    def test_executed_dir_cannot_be_reused(self):
        out = self.tmp / "executed"
        args = [str(GEOMETRY), "--out", str(out), "--igg", self.igg, "--set", GRID_LEVELS_SET]
        first = run_cli(args, env_extra=self._behavior_env("normal"))
        self.assertEqual(first.returncode, 0, first.stderr.decode(errors="replace"))
        second = run_cli(args, env_extra=self._behavior_env("normal"))
        self.assertEqual(second.returncode, 2)
        self.assertIn("占用", second.stderr.decode(errors="replace"))
        self.assertTrue((out / "mesh.igg").exists())

    def test_concurrent_runs_on_same_out_dir_are_rejected(self):
        out = self.tmp / "race"
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env[fake_igg.BEHAVIOR_ENV] = "slow"
        command = [
            sys.executable,
            "-B",
            str(MESH_PY),
            str(GEOMETRY),
            "--out",
            str(out),
            "--igg",
            self.igg,
        ]
        first = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        second = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        first_out, first_err = first.communicate(timeout=180)
        second_out, second_err = second.communicate(timeout=180)
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 2])
        winner_out = first_out if first.returncode == 0 else second_out
        winner_summary = json.loads(winner_out)
        lock_data = json.loads((out / LOCK_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(lock_data["run_id"], winner_summary["run_id"])
        loser_err = first_err if first.returncode == 2 else second_err
        self.assertIn("占用", loser_err.decode(errors="replace"))

    def test_protocol_violations_fail_the_run(self):
        cases = {
            "corrupt_json": "corrupt_json_event",
            "duplicate_id": "duplicate_control_event",
            "unknown_id": "unknown_control_event",
            "wrong_target": "control_event_target_mismatch",
            "wrong_stage": "control_event_stage_mismatch",
            "no_events": "missing_control_event",
            "no_completion": "missing_completion_event",
            "wrong_run_id": "control_event_run_id_mismatch",
        }
        for behavior, expected_code in cases.items():
            with self.subTest(behavior=behavior):
                out = self.tmp / f"proto_{behavior}"
                completed = run_cli(
                    [
                        str(GEOMETRY),
                        "--out",
                        str(out),
                        "--igg",
                        self.igg,
                        "--set",
                        GRID_LEVELS_SET,
                    ],
                    env_extra=self._behavior_env(behavior),
                )
                self.assertEqual(
                    completed.returncode,
                    1,
                    completed.stderr.decode(errors="replace"),
                )
                summary = load_summary(out)
                codes = [
                    item["code"]
                    for item in summary["execution_evidence"]["protocol_errors"]
                ]
                self.assertIn(expected_code, codes)
                self.assertEqual(
                    summary["controls"]["verification"]["status"], "PROTOCOL_ERROR"
                )
                for item in summary["execution_evidence"]["protocol_errors"]:
                    self.assertIn("code", item)
                    self.assertIn("message", item)

    def test_no_events_also_records_missing_post_generation(self):
        out = self.tmp / "proto_no_post"
        completed = run_cli(
            [
                str(GEOMETRY),
                "--out",
                str(out),
                "--igg",
                self.igg,
                "--set",
                GRID_LEVELS_SET,
            ],
            env_extra=self._behavior_env("no_events"),
        )
        self.assertEqual(completed.returncode, 1)
        codes = {
            item["code"]
            for item in load_summary(out)["execution_evidence"]["protocol_errors"]
        }
        self.assertIn("missing_control_event", codes)
        self.assertIn("missing_post_generation_event", codes)

    def test_readback_error_is_recorded_and_run_still_succeeds(self):
        out = self.tmp / "readback_error"
        completed = run_cli(
            [
                str(GEOMETRY),
                "--out",
                str(out),
                "--igg",
                self.igg,
                "--set",
                GRID_LEVELS_SET,
            ],
            env_extra=self._behavior_env("readback_error"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        summary = load_summary(out)
        self.assertEqual(summary["execution_evidence"]["protocol_errors"], [])
        self.assertIsNotNone(summary["execution_evidence"]["completion_event"])
        verification = summary["controls"]["verification"]
        self.assertEqual(verification["status"], "COMPLETE")
        self.assertEqual(verification["results"][0]["verification"], "READBACK_ERROR")
        self.assertIn("getter", verification["results"][0]["error"])
        self.assertEqual(summary["quality"]["result"]["status"], "PASS")
        applied = summary["controls"]["applied"][0]
        self.assertEqual(applied["status"], "applied")

    def test_readback_mismatch_is_recorded_and_run_still_succeeds(self):
        out = self.tmp / "readback_mismatch"
        completed = run_cli(
            [
                str(GEOMETRY),
                "--out",
                str(out),
                "--igg",
                self.igg,
                "--set",
                GRID_LEVELS_SET,
            ],
            env_extra=self._behavior_env("readback_mismatch"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        summary = load_summary(out)
        verification = summary["controls"]["verification"]
        self.assertEqual(verification["status"], "COMPLETE")
        self.assertEqual(verification["results"][0]["verification"], "MISMATCH")

    def test_setter_failure_escalates_returncode(self):
        out = self.tmp / "setter_failure"
        completed = run_cli(
            [
                str(GEOMETRY),
                "--out",
                str(out),
                "--igg",
                self.igg,
                "--set",
                GRID_LEVELS_SET,
            ],
            env_extra=self._behavior_env("setter_failure"),
        )
        self.assertEqual(completed.returncode, 1)
        summary = load_summary(out)
        self.assertIn("fake setter failure", summary["autogrid"]["error"])
        verification = summary["controls"]["verification"]["results"][0]
        self.assertEqual(verification["verification"], "UNVERIFIABLE")

    def test_verification_uses_registry_normalization(self):
        out = self.tmp / "verify"
        completed = run_cli(
            [
                str(GEOMETRY),
                "--out",
                str(out),
                "--igg",
                self.igg,
                "--set",
                GRID_LEVELS_SET,
                "--set",
                "row:#1/wizard/grid_level=medium",
                "--set",
                "row:#1/optimization.multigrid=true",
                "--set",
                "row:#1/wizard/first_cell_width=1.0e-6",
                "--set",
                "row:#1/streamwise_weight=0.75,1.0,1.0",
            ],
            env_extra=self._behavior_env("normal"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        summary = load_summary(out)
        verification = summary["controls"]["verification"]
        self.assertEqual(verification["status"], "COMPLETE")
        expected = {
            "configuration/grid_levels": "VERIFIED",
            "wizard/grid_level": "VERIFIED",
            "row/optimization.multigrid": "VERIFIED",
            "wizard/first_cell_width": "VERIFIED",
            "row/streamwise_weight": "UNVERIFIABLE",
        }
        by_key = {item["key"]: item["verification"] for item in verification["results"]}
        self.assertEqual(by_key, expected)

    def test_no_controls_still_require_completion_event(self):
        for behavior, expected_code, expect_completion in (
            ("no_completion", 1, None),
            ("normal", 0, "completed"),
        ):
            with self.subTest(behavior=behavior):
                out = self.tmp / f"no_controls_{behavior}"
                completed = run_cli(
                    [str(GEOMETRY), "--out", str(out), "--igg", self.igg],
                    env_extra=self._behavior_env(behavior),
                )
                self.assertEqual(
                    completed.returncode,
                    expected_code,
                    completed.stderr.decode(errors="replace"),
                )
                summary = load_summary(out)
                self.assertEqual(
                    summary["controls"]["verification"]["status"],
                    "PROTOCOL_ERROR" if expect_completion is None else "NOT_REQUESTED",
                )
                if expect_completion is None:
                    codes = {
                        item["code"]
                        for item in summary["execution_evidence"]["protocol_errors"]
                    }
                    self.assertIn("missing_completion_event", codes)
                else:
                    self.assertEqual(summary["execution_evidence"]["protocol_errors"], [])
                    self.assertEqual(
                        summary["execution_evidence"]["completion_event"]["status"],
                        expect_completion,
                    )

    def test_schema_v4_sources_manifest_and_atomic_summary(self):
        out = self.tmp / "schema4"
        out.mkdir()
        (out / "worker.stdout.log").write_text("worker log", encoding="utf-8")
        (out / "worker.stderr.log").write_text("worker log", encoding="utf-8")
        run_id = "ab12cd34ef56ab12cd34ef56ab12cd34"
        completed = run_cli(
            [
                str(GEOMETRY),
                "--out",
                str(out),
                "--igg",
                self.igg,
                "--run-id",
                run_id,
                "--set",
                GRID_LEVELS_SET,
            ],
            env_extra=self._behavior_env("normal"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        summary = load_summary(out)
        self.assertEqual(summary["schema_version"], 4)
        self.assertEqual(summary["run_id"], run_id)
        datetime.fromisoformat(summary["created_at"])
        self.assertEqual(summary["controls"]["verification"]["status"], "COMPLETE")
        self.assertEqual(
            summary["controls"]["verification"]["results"][0]["verification"], "VERIFIED"
        )
        self.assertEqual(summary["execution_evidence"]["protocol_errors"], [])
        self.assertEqual(
            summary["execution_evidence"]["completion_event"],
            {"stage": "final", "run_id": run_id, "status": "completed"},
        )
        self.assertEqual(summary["quality"]["result"]["status"], "PASS")
        self.assertEqual(summary["quality_validation"]["status"], "VALID")
        self.assertEqual(summary["missing_mesh_outputs"], [])

        sources = summary["sources"]
        input_summary = sources["input_summary"]
        self.assertEqual(input_summary["path"], str(GEOMETRY))
        self.assertEqual(
            input_summary["sha256"], hashlib.sha256(GEOMETRY.read_bytes()).hexdigest()
        )
        self.assertEqual(input_summary["size_bytes"], GEOMETRY.stat().st_size)
        self.assertEqual(input_summary["version"], "5.4")
        self.assertEqual(input_summary["units"], "Meters")
        self.assertEqual(input_summary["units_factor"], 1.0)
        self.assertEqual(
            [item["path"] for item in sources["source_signature"]["files"]],
            [
                "src/mesh.py",
                "src/controls.py",
                "src/autogrid.py",
                "src/geomturbo.py",
                "src/quality.py",
            ],
        )
        for item in sources["source_signature"]["files"]:
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(sources["source_signature"]["algorithm"], r"sha256")
        commit = sources["git"]["commit"]
        self.assertTrue(commit is None or re.fullmatch(r"[0-9a-f]{40}", commit))
        self.assertIsInstance(sources["git"]["dirty"], bool)
        self.assertEqual(sources["generated_script"]["path"], "autogrid_init.py")
        self.assertEqual(
            sources["generated_script"]["sha256"],
            hashlib.sha256((out / "autogrid_init.py").read_bytes()).hexdigest(),
        )
        self.assertEqual(sources["control_registry"]["key_count"], 344)
        self.assertRegex(sources["control_registry"]["signature"], r"^[0-9a-f]{64}$")
        self.assertEqual(sources["quality_rules_version"], "1")
        self.assertEqual(sources["vendor_version"], "17.1-1")

        manifest = summary["manifest"]
        self.assertIn("generation", manifest["stages_completed"])
        self.assertIn("quality", manifest["stages_completed"])
        relative_paths = {item["relative_path"] for item in manifest["outputs"]}
        self.assertEqual(
            relative_paths,
            {"mesh.igg", "mesh.cgns", "mesh.trb", "mesh.qualityReport"},
        )
        for item in manifest["outputs"]:
            self.assertGreater(item["size_bytes"], 0)
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
        igg_entry = next(
            item for item in manifest["outputs"] if item["relative_path"] == "mesh.igg"
        )
        self.assertEqual(
            igg_entry["sha256"],
            hashlib.sha256((out / "mesh.igg").read_bytes()).hexdigest(),
        )
        # 白名单预存文件与宿主自写文件绝不进入产物清单。
        self.assertNotIn(
            "worker.stdout.log", {item["relative_path"] for item in manifest["outputs"]}
        )
        self.assertNotIn(
            "input.geomTurbo", {item["relative_path"] for item in manifest["outputs"]}
        )
        self.assertNotIn(
            "autogrid_init.py", {item["relative_path"] for item in manifest["outputs"]}
        )
        # 占用标记内容含 run_id 与创建时间。
        lock_data = json.loads((out / LOCK_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(lock_data["run_id"], run_id)
        self.assertIn("created_at", lock_data)
        # 摘要文件不包含 NaN/Infinity 字面量。
        summary_text = (out / "run_summary.json").read_text(encoding="utf-8")
        self.assertNotIn("NaN", summary_text)
        self.assertNotIn("Infinity", summary_text)

    def test_missing_outputs_are_recorded_separately(self):
        out = self.tmp / "no_outputs"
        completed = run_cli(
            [
                str(GEOMETRY),
                "--out",
                str(out),
                "--igg",
                self.igg,
                "--set",
                GRID_LEVELS_SET,
            ],
            env_extra=self._behavior_env("no_output_files"),
        )
        self.assertEqual(completed.returncode, 1)
        summary = load_summary(out)
        self.assertEqual(summary["missing_mesh_outputs"], ["igg", "cgns"])
        self.assertEqual(summary["quality"]["result"]["status"], "UNKNOWN")
        self.assertEqual(summary["quality_validation"]["status"], "UNKNOWN")
        self.assertEqual(summary["execution_evidence"]["protocol_errors"], [])

    def test_collect_outputs_snapshot_excludes_preexisting_files(self):
        directory = self.tmp / "snapshot"
        directory.mkdir()
        (directory / "mesh.igg").write_bytes(b"old")
        (directory / "mesh.cgns").write_bytes(b"new")
        with_snapshot = collect_outputs(
            directory, "mesh", snapshot=frozenset({"mesh.igg"})
        )
        self.assertEqual(set(with_snapshot), {"cgns"})
        without_snapshot = collect_outputs(directory, "mesh")
        self.assertEqual(set(without_snapshot), {"igg", "cgns"})

    def test_write_json_rejects_non_finite_numbers(self):
        with self.assertRaises(ValueError):
            _write_json(self.tmp / "nan.json", {"value": float("nan")})
        with self.assertRaises(ValueError):
            _write_json(self.tmp / "inf.json", {"value": float("inf")})
        self.assertFalse((self.tmp / "nan.json").exists())
        self.assertFalse((self.tmp / "inf.json").exists())

    def test_v3_historical_summary_remains_analyzable(self):
        v3_summary = {
            "schema_version": 3,
            "controls": {
                "applied": [
                    {
                        "id": "C0001",
                        "key": "row/optimization.steps",
                        "target_path": "row:#1",
                        "api_value": 200,
                        "requested": 200,
                        "project_value": 200,
                        "status": "applied",
                        "getter": "get_row_optimization_steps",
                        "readback": 200,
                        "readback_before": None,
                        "readback_before_error": None,
                        "error": None,
                    }
                ],
                "post_generation": [
                    {"id": "C0001", "status": "readback", "readback": 200}
                ],
            },
            "autogrid": {"outputs": {}, "returncode": 0},
            "mesh_fingerprint": None,
            "quality": {
                "metrics": {"number_of_points": 100},
                "metadata": {},
                "result": {"status": "PASS"},
            },
        }
        readback = test_analyze_results.find_control_readback(
            v3_summary, "row/optimization.steps"
        )
        self.assertTrue(readback["available"])
        self.assertTrue(readback["matches_after"])
        quality = test_analyze_results.extract_quality(v3_summary)
        self.assertEqual(quality["quality_status"], "PASS")
        record = test_analyze_results.case_record(
            {
                "case_id": "v3-case",
                "variable_key": "row/optimization.steps",
                "variable_value": 200,
                "run_summary": v3_summary,
            }
        )
        self.assertEqual(record["case_id"], "v3-case")
        self.assertIsNotNone(record["quality_status"])

    def test_source_signature_matches_legacy_algorithm(self):
        files = [SRC_DIR / "mesh.py", SRC_DIR / "autogrid.py"]
        legacy_payload = [
            (str(path.relative_to(ROOT)), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in files
            if path.exists()
        ]
        legacy = hashlib.sha256(
            json.dumps(legacy_payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.assertEqual(source_signature(files, root=ROOT), legacy)


if __name__ == "__main__":
    unittest.main()
