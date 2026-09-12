from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from controls import CONTROL_REGISTRY, ControlValidationError, enumerate_control_targets
from geomturbo import BladeInfo, GeomTurboSummary, RowInfo, parse_geomturbo
from mesh_app.config import Settings
from mesh_app.control_service import ControlService, target_to_selector
from mesh_app.db import Database
from mesh_app.sessions import SessionService, ServiceError, dump_json, utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GEOMETRY_TEXT = """\
GEOMETRY TURBO
VERSION 5.6
UNITS METER
UNITS-FACTOR 1.0
NI_BEGIN nirow
NAME Rotor
PERIODICITY 36
NI_BEGIN niblade
NAME MainBlade
NUMBER_OF_BLADES 36
NI_BEGIN nitipgap
NI_END nitipgap
NI_END niblade
NI_END nirow
NI_BEGIN nirow
NAME Stator
PERIODICITY 50
NI_BEGIN niblade
NAME StatorBlade
NUMBER_OF_BLADES 50
NI_END niblade
NI_END nirow
"""


def make_services(tmp_path: Path) -> tuple[Settings, Database, SessionService, ControlService]:
    settings = Settings.from_env({"MESH_DATA_DIR": str(tmp_path / "data")}, project_root=PROJECT_ROOT)
    settings.ensure_directories()
    database = Database(settings.database_path, settings.migrations_dir)
    database.migrate()
    return settings, database, SessionService(database), ControlService(database, settings.data_dir)


def create_ready_baseline(
    settings: Settings,
    database: Database,
    sessions: SessionService,
) -> tuple[dict[str, object], str]:
    provisional = "case-a"
    geometry_path = settings.geometry_dir / provisional / "source.geomTurbo"
    geometry_path.parent.mkdir(parents=True, exist_ok=True)
    geometry_path.write_text(GEOMETRY_TEXT, encoding="utf-8")
    relative = geometry_path.relative_to(settings.data_dir).as_posix()
    summary = parse_geomturbo(geometry_path).to_dict()
    summary["path"] = relative
    detail = sessions.create_session(
        title="两级压气机控制测试",
        expert_name=None,
        source_filename="case.geomTurbo",
        geometry_sha256=hashlib.sha256(GEOMETRY_TEXT.encode()).hexdigest(),
        geometry_relative_path=relative,
        geometry_summary=summary,
    )
    baseline_id = str(detail["runs"][0]["id"])
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE runs SET status = 'RUNNING', updated_at = ? WHERE id = ?",
            (utc_now(), baseline_id),
        )
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE runs SET status = 'SUCCEEDED', updated_at = ?, finished_at = ? WHERE id = ?",
            (utc_now(), utc_now(), baseline_id),
        )
    return detail, baseline_id


def _promote_to_succeeded(database: Database, run_id: str) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute("UPDATE runs SET status = 'RUNNING' WHERE id = ?", (run_id,))
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE runs SET status = 'SUCCEEDED', updated_at = ?, finished_at = ? WHERE id = ?",
            (utc_now(), utc_now(), run_id),
        )


def test_public_target_enumeration_uses_precise_geometry_entities() -> None:
    geometry = GeomTurboSummary(
        path="synthetic.geomTurbo",
        version="5.6",
        units="METER",
        units_factor=1.0,
        row_count=2,
        rows=[
            RowInfo(
                name="Rotor",
                periodicity=36,
                blades=[
                    BladeInfo(
                        name="MainBlade",
                        number_of_blades=36,
                        has_tip_gap=True,
                        gap_sides=("shroud",),
                    ),
                    BladeInfo(name="Splitter", number_of_blades=36),
                ],
                has_tip_gap=True,
            ),
            RowInfo(
                name="Stator",
                periodicity=50,
                blades=[BladeInfo(name="StatorBlade", number_of_blades=50)],
            ),
        ],
    )

    rows = enumerate_control_targets(geometry, target_kind="row")
    assert [target_to_selector(target) for target in rows] == ["row:#1", "row:#2"]
    blades = enumerate_control_targets(geometry, control_key="blade/b2b.topology")
    assert [target_to_selector(target) for target in blades] == [
        "row:#1/blade:#1",
        "row:#1/blade:#2",
        "row:#2/blade:#1",
    ]
    gaps = enumerate_control_targets(geometry, control_key="gap/spanwise_points")
    assert [target_to_selector(target) for target in gaps] == ["row:#1/blade:#1/gap:#1"]


def test_unresolved_effects_are_excluded_by_default_and_opt_in_is_explicit() -> None:
    geometry = GeomTurboSummary(
        path="synthetic.geomTurbo",
        version=None,
        units=None,
        units_factor=None,
        row_count=0,
        rows=[],
    )
    assert enumerate_control_targets(
        geometry, control_key="existing-effect/maximum_expansion"
    ) == []
    unresolved = enumerate_control_targets(
        geometry,
        control_key="existing-effect/maximum_expansion",
        include_unresolved=True,
    )
    assert len(unresolved) == 99
    assert target_to_selector(unresolved[0]) == "existing-effect:#1"
    assert target_to_selector(unresolved[-1]) == "existing-effect:#99"
    with pytest.raises(ControlValidationError, match="未知控制目标类型"):
        enumerate_control_targets(geometry, target_kind="不存在的目标")


def test_control_state_and_preview_enforce_prerequisites_and_clear_dependencies(tmp_path: Path) -> None:
    settings, database, sessions, controls = make_services(tmp_path)
    detail, baseline_id = create_ready_baseline(settings, database, sessions)
    session_id = str(detail["id"])

    state = controls.get_control_state(session_id, parent_run_id=baseline_id)
    target_points = next(
        item
        for item in state["controls"]
        if item["key"] == "row/target_points" and item["selector"] == "row:#1"
    )
    assert target_points["availability"] == "LOCKED"
    assert target_points["reason"] == CONTROL_REGISTRY["row/target_points"].unsupported_reason
    assert target_points["can_clear"] is False
    skewness = next(
        item
        for item in state["controls"]
        if item["key"] == "row/optimization.skewness" and item["selector"] == "row:#1"
    )
    assert skewness["availability"] == "LOCKED"
    assert "row/optimization.steps>0" in skewness["reason"]

    target_only = controls.preview(
        session_id,
        parent_run_id=baseline_id,
        changes=[
            {"key": "row/target_points", "selector": "row:#1", "op": "set", "value": 500000}
        ],
    )
    assert target_only["valid"] is False
    assert target_only["errors"][0]["code"] == "INVALID_CONTROL_CHANGE"

    target_valid = controls.preview(
        session_id,
        parent_run_id=baseline_id,
        changes=[
            {"key": "row/mesh_level", "selector": "row:#1", "op": "set", "value": "user"},
            {"key": "row/target_points", "selector": "row:#1", "op": "set", "value": 500000},
        ],
    )
    assert target_valid["valid"] is False

    # 启用规则只要求优化步数为正：100/300 不再被判缺依赖。
    for steps_value in (100, 300):
        positive_steps = controls.preview(
            session_id,
            parent_run_id=baseline_id,
            changes=[
                {"key": "row/optimization.steps", "selector": "row:#1", "op": "set", "value": steps_value},
                {
                    "key": "row/optimization.skewness",
                    "selector": "row:#1",
                    "op": "set",
                    "value": "yes",
                },
            ],
        )
        assert positive_steps["valid"] is True

    zero_steps = controls.preview(
        session_id,
        parent_run_id=baseline_id,
        changes=[
            {"key": "row/optimization.steps", "selector": "row:#1", "op": "set", "value": 0},
            {
                "key": "row/optimization.skewness",
                "selector": "row:#1",
                "op": "set",
                "value": "yes",
            },
        ],
    )
    assert zero_steps["valid"] is False
    assert "steps>0" in zero_steps["errors"][0]["message"]

    valid_optimization = controls.preview(
        session_id,
        parent_run_id=baseline_id,
        changes=[
            {"key": "row/optimization.steps", "selector": "row:#1", "op": "set", "value": 200},
            {
                "key": "row/optimization.skewness",
                "selector": "row:#1",
                "op": "set",
                "value": "yes",
            },
        ],
    )
    assert valid_optimization["valid"] is True
    child = sessions.create_child_run(
        session_id=session_id,
        parent_run_id=baseline_id,
        request_id="optimization-child",
        expected_version=1,
        control_snapshot=valid_optimization["snapshot"],
        control_delta=valid_optimization["delta"],
    )
    child_id = str(child["id"])
    with database.transaction(immediate=True) as connection:
        connection.execute("UPDATE runs SET status = 'RUNNING' WHERE id = ?", (child_id,))
    with database.transaction(immediate=True) as connection:
        connection.execute("UPDATE runs SET status = 'SUCCEEDED' WHERE id = ?", (child_id,))

    # 200→100 仍满足启用规则，不清除子项。
    preserved = controls.preview(
        session_id,
        parent_run_id=child_id,
        changes=[
            {"key": "row/optimization.steps", "selector": "row:#1", "op": "set", "value": 100}
        ],
    )
    assert preserved["valid"] is True
    assert preserved["required_clears"] == []

    # 变为零才触发清除。
    zeroed = controls.preview(
        session_id,
        parent_run_id=child_id,
        changes=[
            {"key": "row/optimization.steps", "selector": "row:#1", "op": "set", "value": 0}
        ],
    )
    assert zeroed["valid"] is True
    assert zeroed["required_clears"] == [
        {"key": "row/optimization.skewness", "selector": "row:#1", "op": "clear"}
    ]

    # 清除前置项同样触发清除。
    cleared = controls.preview(
        session_id,
        parent_run_id=child_id,
        changes=[
            {"key": "row/optimization.steps", "selector": "row:#1", "op": "clear"}
        ],
    )
    assert cleared["valid"] is True
    assert cleared["required_clears"] == [
        {"key": "row/optimization.skewness", "selector": "row:#1", "op": "clear"}
    ]


def test_effective_availability_unlocks_in_draft_and_relocks_on_revoke(tmp_path: Path) -> None:
    settings, database, sessions, controls = make_services(tmp_path)
    detail, baseline_id = create_ready_baseline(settings, database, sessions)
    session_id = str(detail["id"])

    def entry_of(preview: dict[str, object], key: str, selector: str) -> dict[str, object]:
        for item in preview["effective_availability"]:
            if item["key"] == key and item["selector"] == selector:
                return item
        raise AssertionError(f"effective_availability 缺少 {key} / {selector}")

    # 空草稿：与父快照目录一致，子项保持锁定。
    empty = controls.preview(session_id, parent_run_id=baseline_id, changes=[])
    assert empty["valid"] is True
    assert entry_of(empty, "row/optimization.steps", "row:#1")["availability"] == "EDITABLE"
    assert entry_of(empty, "row/optimization.skewness", "row:#1")["availability"] == "LOCKED"

    # 同一草稿内设置前置项即解锁子项。
    unlocked = controls.preview(
        session_id,
        parent_run_id=baseline_id,
        changes=[{"key": "row/optimization.steps", "selector": "row:#1", "op": "set", "value": 100}],
    )
    assert unlocked["valid"] is True
    assert entry_of(unlocked, "row/optimization.skewness", "row:#1")["availability"] == "EDITABLE"
    assert entry_of(unlocked, "row/optimization.skewness", "row:#1")["reason"] is None

    # 可定位的预检错误响应同样返回有效可编辑状态，便于修正草稿。
    invalid = controls.preview(
        session_id,
        parent_run_id=baseline_id,
        changes=[{"key": "row/optimization.skewness", "selector": "row:#1", "op": "set", "value": "yes"}],
    )
    assert invalid["valid"] is False
    assert invalid["errors"][0]["code"] == "CONTROL_PREREQUISITE_NOT_MET"
    target_invalid = entry_of(invalid, "row/optimization.skewness", "row:#1")
    assert target_invalid["availability"] == "LOCKED"
    assert "row/optimization.steps>0" in target_invalid["reason"]

    # 把解锁后的组合落成一个父运行，再撤销前置项验证重新锁定与必要清除。
    seeded = controls.preview(
        session_id,
        parent_run_id=baseline_id,
        changes=[
            {"key": "row/optimization.steps", "selector": "row:#1", "op": "set", "value": 100},
            {"key": "row/optimization.skewness", "selector": "row:#1", "op": "set", "value": "yes"},
        ],
    )
    assert seeded["valid"] is True
    child = sessions.create_child_run(
        session_id=session_id,
        parent_run_id=baseline_id,
        request_id="availability-child",
        expected_version=1,
        control_snapshot=seeded["snapshot"],
        control_delta=seeded["delta"],
    )
    child_id = str(child["id"])
    _promote_to_succeeded(database, child_id)

    revoked = controls.preview(
        session_id,
        parent_run_id=child_id,
        changes=[{"key": "row/optimization.steps", "selector": "row:#1", "op": "clear"}],
    )
    assert revoked["valid"] is True
    assert revoked["required_clears"] == [
        {"key": "row/optimization.skewness", "selector": "row:#1", "op": "clear"}
    ]
    assert entry_of(revoked, "row/optimization.steps", "row:#1")["availability"] == "EDITABLE"
    target_revoked = entry_of(revoked, "row/optimization.skewness", "row:#1")
    assert target_revoked["availability"] == "LOCKED"
    assert "row/optimization.steps>0" in target_revoked["reason"]


def test_historical_target_points_can_be_cleared_but_cannot_be_inherited_or_retried(tmp_path: Path) -> None:
    settings, database, sessions, controls = make_services(tmp_path)
    detail, baseline_id = create_ready_baseline(settings, database, sessions)
    snapshot = {"schema_version": 1, "items": [
        {"key": "row/mesh_level", "selector": "row:#1", "value": "user"},
        {"key": "row/target_points", "selector": "row:#1", "value": 500000},
    ]}
    historical = sessions.create_child_run(
        session_id=detail["id"], parent_run_id=baseline_id, request_id="historical-target",
        expected_version=1, control_snapshot=snapshot, control_delta={"schema_version": 1, "items": []},
    )
    _promote_to_succeeded(database, historical["id"])
    state = controls.get_control_state(detail["id"], parent_run_id=historical["id"])
    target = next(item for item in state["controls"] if item["key"] == "row/target_points" and item["selector"] == "row:#1")
    assert target["availability"] == "LOCKED" and target["can_clear"] is True
    unchanged = controls.preview(detail["id"], parent_run_id=historical["id"], changes=[])
    assert unchanged["valid"] is False
    assert "row/target_points" in unchanged["errors"][0]["message"]
    cleared = controls.preview(detail["id"], parent_run_id=historical["id"], changes=[
        {"key": "row/target_points", "selector": "row:#1", "op": "clear"},
    ])
    assert cleared["valid"] is True
    assert all(item["key"] != "row/target_points" for item in cleared["snapshot"]["items"])
    failed = sessions.create_child_run(
        session_id=detail["id"], parent_run_id=historical["id"], request_id="historical-failed-target",
        expected_version=2, control_snapshot=snapshot, control_delta={"schema_version": 1, "items": []},
    )
    with database.transaction(immediate=True) as connection:
        connection.execute("UPDATE runs SET status = 'RUNNING' WHERE id = ?", (failed["id"],))
        connection.execute("UPDATE runs SET status = 'FAILED' WHERE id = ?", (failed["id"],))
    with pytest.raises(ServiceError) as error:
        sessions.retry_run(run_id=failed["id"], request_id="retry-target", expected_version=3)
    assert error.value.code == "CONTROL_VALIDATION_FAILED"


def test_throat_points_predicate_preserves_children(tmp_path: Path) -> None:
    settings, database, sessions, controls = make_services(tmp_path)
    detail, baseline_id = create_ready_baseline(settings, database, sessions)
    session_id = str(detail["id"])
    throat = controls.preview(
        session_id,
        parent_run_id=baseline_id,
        changes=[
            {
                "key": "blade/b2b.default.type",
                "selector": "row:#1/blade:#1",
                "op": "set",
                "value": "streamwise",
            },
            {
                "key": "blade/b2b.default.throat_points",
                "selector": "row:#1/blade:#1",
                "op": "set",
                "value": 9,
            },
            {
                "key": "blade/b2b.default.throat_projection_type",
                "selector": "row:#1/blade:#1",
                "op": "set",
                "value": 1,
            },
        ],
    )
    assert throat["valid"] is True
    child = sessions.create_child_run(
        session_id=session_id,
        parent_run_id=baseline_id,
        request_id="throat-child",
        expected_version=1,
        control_snapshot=throat["snapshot"],
        control_delta=throat["delta"],
    )
    child_id = str(child["id"])
    _promote_to_succeeded(database, child_id)

    # 9→7/11 仍满足启用规则，保留子项。
    for points in (7, 11):
        kept = controls.preview(
            session_id,
            parent_run_id=child_id,
            changes=[
                {
                    "key": "blade/b2b.default.throat_points",
                    "selector": "row:#1/blade:#1",
                    "op": "set",
                    "value": points,
                },
            ],
        )
        assert kept["valid"] is True
        assert kept["required_clears"] == []

    # 变为零才触发清除。
    removed = controls.preview(
        session_id,
        parent_run_id=child_id,
        changes=[
            {
                "key": "blade/b2b.default.throat_points",
                "selector": "row:#1/blade:#1",
                "op": "set",
                "value": 0,
            },
        ],
    )
    assert removed["valid"] is True
    assert removed["required_clears"] == [
        {
            "key": "blade/b2b.default.throat_projection_type",
            "selector": "row:#1/blade:#1",
            "op": "clear",
        }
    ]


def test_topology_switch_clears_conditional_children(tmp_path: Path) -> None:
    settings, database, sessions, controls = make_services(tmp_path)
    detail, baseline_id = create_ready_baseline(settings, database, sessions)
    session_id = str(detail["id"])
    seeded = controls.preview(
        session_id,
        parent_run_id=baseline_id,
        changes=[
            {
                "key": "blade/b2b.topology",
                "selector": "row:#1/blade:#1",
                "op": "set",
                "value": "default",
            },
            {
                "key": "blade/b2b.default.type",
                "selector": "row:#1/blade:#1",
                "op": "set",
                "value": "streamwise",
            },
            {
                "key": "blade/b2b.default.throat_points",
                "selector": "row:#1/blade:#1",
                "op": "set",
                "value": 9,
            },
        ],
    )
    assert seeded["valid"] is True
    child = sessions.create_child_run(
        session_id=session_id,
        parent_run_id=baseline_id,
        request_id="topology-child",
        expected_version=1,
        control_snapshot=seeded["snapshot"],
        control_delta=seeded["delta"],
    )
    child_id = str(child["id"])
    _promote_to_succeeded(database, child_id)

    switched = controls.preview(
        session_id,
        parent_run_id=child_id,
        changes=[
            {
                "key": "blade/b2b.topology",
                "selector": "row:#1/blade:#1",
                "op": "set",
                "value": "hi",
            },
        ],
    )
    assert switched["valid"] is True
    assert switched["required_clears"] == [
        {
            "key": "blade/b2b.default.throat_points",
            "selector": "row:#1/blade:#1",
            "op": "clear",
        },
        {
            "key": "blade/b2b.default.type",
            "selector": "row:#1/blade:#1",
            "op": "clear",
        },
    ]


def test_preview_rejects_name_wildcard_and_non_applicable_target(tmp_path: Path) -> None:
    settings, database, sessions, controls = make_services(tmp_path)
    detail, baseline_id = create_ready_baseline(settings, database, sessions)
    session_id = str(detail["id"])
    result = controls.preview(
        session_id,
        parent_run_id=baseline_id,
        changes=[
            {"key": "row/mesh_level", "selector": "row:Rotor", "op": "set", "value": "fine"}
        ],
    )
    assert result["valid"] is False
    assert "#N" in result["errors"][0]["message"]
