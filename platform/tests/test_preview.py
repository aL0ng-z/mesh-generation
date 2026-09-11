"""HDF5 CGNS 到 vtk.js VTP 预览的几何与缓存测试。"""

from __future__ import annotations

import asyncio
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import h5py
import httpx
import numpy as np
import pytest

import mesh_app.api as api_module
import mesh_app.preview as preview_module
from mesh_app.api import _PreviewConversionGate
from mesh_app.config import Settings
from mesh_app.preview import (
    PreviewError,
    PreviewService,
    PreviewUnavailableError,
    build_manifest,
    detect_cgns_format,
    generate_block_vtp,
    get_or_create_slice,
    prepare_preview,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _synthetic_cgns(path: Path, size: tuple[int, int, int] = (4, 3, 2)) -> Path:
    ni, nj, nk = size
    i, j, k = np.meshgrid(
        np.arange(ni, dtype=np.float64),
        np.arange(nj, dtype=np.float64),
        np.arange(nk, dtype=np.float64),
        indexing="ij",
    )
    # CGNS/HDF5 数据集物理 shape 为 K,J,I。
    coordinates = (i.transpose(2, 1, 0), (10 * j).transpose(2, 1, 0), (100 * k).transpose(2, 1, 0))
    with h5py.File(path, "w") as handle:
        base = handle.create_group("Base")
        base.attrs["label"] = np.bytes_("CGNSBase_t")
        zone = base.create_group("Rotor Block")
        zone.attrs["label"] = np.bytes_("Zone_t")
        zone.create_dataset(" data", data=np.asarray([[ni, ni - 1, 0], [nj, nj - 1, 0], [nk, nk - 1, 0]]))
        grid = zone.create_group("GridCoordinates")
        grid.attrs["label"] = np.bytes_("GridCoordinates_t")
        for name, values in zip(("CoordinateX", "CoordinateY", "CoordinateZ"), coordinates):
            coordinate = grid.create_group(name)
            coordinate.attrs["label"] = np.bytes_("DataArray_t")
            coordinate.create_dataset(" data", data=values)
    return path


def _piece(path: Path) -> ET.Element:
    return ET.parse(path).getroot().find("./PolyData/Piece")  # type: ignore[return-value]


def _points(path: Path) -> np.ndarray:
    data = ET.parse(path).getroot().find("./PolyData/Piece/Points/DataArray")
    assert data is not None and data.text
    values = np.fromstring(data.text, sep=" ")
    return values.reshape((-1, 3))


def test_manifest_uses_ijk_dimensions_bounds_and_capabilities(tmp_path: Path) -> None:
    cgns = _synthetic_cgns(tmp_path / "mesh.cgns", (4, 3, 2))
    assert detect_cgns_format(cgns) == "HDF5"
    manifest = build_manifest(cgns)
    assert manifest["status"] == "READY"
    assert manifest["available"] is True
    assert manifest["capabilities"]["slice_index_base"] == 0
    assert len(manifest["blocks"]) == 1
    block = manifest["blocks"][0]
    assert block["id"] == "b0001"
    assert block["name"] == "Rotor Block"
    assert block["size"] == [4, 3, 2]
    assert block["dimensions"] == {"I": 4, "J": 3, "K": 2}
    assert block["index_ranges"] == {"I": [0, 3], "J": [0, 2], "K": [0, 1]}
    assert block["bounds"] == {"x": [0.0, 3.0], "y": [0.0, 20.0], "z": [0.0, 100.0]}
    assert block["point_count"] == 24
    assert block["cell_count"] == 6


def test_prepare_generates_true_boundary_surface_and_wireframe(tmp_path: Path) -> None:
    size = (4, 3, 2)
    cgns = _synthetic_cgns(tmp_path / "mesh.cgns", size)
    cache = tmp_path / "cache"
    manifest = prepare_preview(cgns, cache)
    block = manifest["blocks"][0]
    surface = cache / block["assets"]["surface"]
    wireframe = cache / block["assets"]["wireframe"]
    assert surface.is_file() and wireframe.is_file()

    surface_piece = _piece(surface)
    expected_polygons = 2 * (
        (size[1] - 1) * (size[2] - 1)
        + (size[0] - 1) * (size[2] - 1)
        + (size[0] - 1) * (size[1] - 1)
    )
    # K=2 时所有点都在真实边界上。
    assert int(surface_piece.attrib["NumberOfPoints"]) == np.prod(size)
    assert int(surface_piece.attrib["NumberOfPolys"]) == expected_polygons
    assert np.allclose(_points(surface).min(axis=0), [0, 0, 0])
    assert np.allclose(_points(surface).max(axis=0), [3, 20, 100])

    ni, nj, nk = size
    expected_lines = (
        (ni - 1) * (nj * nk - max(nj - 2, 0) * max(nk - 2, 0))
        + (nj - 1) * (ni * nk - max(ni - 2, 0) * max(nk - 2, 0))
        + (nk - 1) * (ni * nj - max(ni - 2, 0) * max(nj - 2, 0))
    )
    wire_piece = _piece(wireframe)
    assert int(wire_piece.attrib["NumberOfLines"]) == expected_lines
    assert int(wire_piece.attrib["NumberOfPolys"]) == 0
    assert not list(cache.rglob("*.tmp"))


@pytest.mark.parametrize(
    ("axis", "index", "expected_points", "fixed_coordinate"),
    [
        ("I", 0, 3 * 2, (0, 0.0)),
        ("I", 3, 3 * 2, (0, 3.0)),
        ("J", 0, 4 * 2, (1, 0.0)),
        ("J", 2, 4 * 2, (1, 20.0)),
        ("K", 0, 4 * 3, (2, 0.0)),
        ("K", 1, 4 * 3, (2, 100.0)),
    ],
)
def test_ijk_first_last_slice_and_atomic_cache(
    tmp_path: Path,
    axis: str,
    index: int,
    expected_points: int,
    fixed_coordinate: tuple[int, float],
) -> None:
    cgns = _synthetic_cgns(tmp_path / "mesh.cgns", (4, 3, 2))
    cache = tmp_path / "cache"
    first = get_or_create_slice(cgns, cache, "b0001", axis, index)
    before = first.stat().st_mtime_ns
    second = get_or_create_slice(cgns, cache, "b0001", axis, index)
    assert second == first
    assert second.stat().st_mtime_ns == before
    points = _points(first)
    assert len(points) == expected_points
    assert np.allclose(points[:, fixed_coordinate[0]], fixed_coordinate[1])
    assert not list(cache.rglob("*.tmp"))


def test_slice_validates_axis_index_and_supports_zero_based_block_index(tmp_path: Path) -> None:
    cgns = _synthetic_cgns(tmp_path / "mesh.cgns")
    cache = tmp_path / "cache"
    assert get_or_create_slice(cgns, cache, 0, "I", 0).is_file()
    with pytest.raises(PreviewError, match="切片轴") as axis_error:
        get_or_create_slice(cgns, cache, "b0001", "X", 0)
    assert axis_error.value.code == "INVALID_SLICE_AXIS"
    with pytest.raises(PreviewError, match="索引范围") as index_error:
        get_or_create_slice(cgns, cache, "b0001", "J", 3)
    assert index_error.value.code == "SLICE_INDEX_OUT_OF_RANGE"


def test_adf_is_an_explicit_nonfatal_degradation(tmp_path: Path) -> None:
    cgns = tmp_path / "legacy.cgns"
    cgns.write_bytes(b"ADF Database Version 2.0\x00not-hdf5")
    assert detect_cgns_format(cgns) == "ADF"
    manifest = prepare_preview(cgns, tmp_path / "cache")
    assert manifest["available"] is False
    assert manifest["status"] == "UNAVAILABLE"
    assert manifest["format"] == "ADF"
    assert manifest["reason_code"] == "ADF_UNSUPPORTED"
    assert "仍可下载" in manifest["reason"]
    with pytest.raises(PreviewUnavailableError) as error:
        get_or_create_slice(cgns, tmp_path / "cache", "b0001", "I", 0)
    assert error.value.code == "ADF_UNSUPPORTED"


def test_service_cache_and_standalone_block_generation(tmp_path: Path) -> None:
    cgns = _synthetic_cgns(tmp_path / "mesh.cgns")
    service = PreviewService(cgns, tmp_path / "cache")
    manifest = service.prepare()
    manifest_mtime = (tmp_path / "cache" / "manifest.json").stat().st_mtime_ns
    assert service.manifest() == manifest
    assert (tmp_path / "cache" / "manifest.json").stat().st_mtime_ns == manifest_mtime
    assert service.block_asset("b0001", "surface").is_file()
    standalone = generate_block_vtp(cgns, "Rotor Block", "wireframe", tmp_path / "standalone.vtp")
    assert standalone.is_file()
    assert int(_piece(standalone).attrib["NumberOfLines"]) > 0


def test_malformed_hdf5_returns_conversion_reason_without_touching_mesh_status(tmp_path: Path) -> None:
    cgns = tmp_path / "malformed.cgns"
    with h5py.File(cgns, "w") as handle:
        handle.create_group("Base")
    manifest = prepare_preview(cgns, tmp_path / "cache")
    assert manifest["available"] is False
    assert manifest["status"] == "UNAVAILABLE"
    assert manifest["reason_code"] == "NO_STRUCTURED_BLOCKS"


# ---------------------------------------------------------------- CR-07 新增

def _cells(path: Path, section: str) -> list[tuple[int, ...]]:
    """解析 VTP 中 Polys/Lines 的单元索引（connectivity + offsets）。"""

    piece = _piece(path)
    element = piece.find(f"./{section}")
    if element is None:
        # VTP 写入器省略空单元节（如退化网格无多边形）。
        return []
    connectivity = element.find("./DataArray[@Name='connectivity']")
    offsets = element.find("./DataArray[@Name='offsets']")
    assert connectivity is not None and connectivity.text is not None
    assert offsets is not None and offsets.text is not None
    connection = np.fromstring(connectivity.text, sep=" ", dtype=np.int64)
    offset_values = np.fromstring(offsets.text, sep=" ", dtype=np.int64)
    cells: list[tuple[int, ...]] = []
    start = 0
    for offset in offset_values:
        cells.append(tuple(int(value) for value in connection[start:offset]))
        start = int(offset)
    return cells


def _reference_full_points(cgns: Path) -> np.ndarray:
    """旧实现基准：整块读入体坐标并转置为 (I, J, K, 3)。"""

    with h5py.File(cgns, "r") as handle:
        axes = []
        for name in ("CoordinateX", "CoordinateY", "CoordinateZ"):
            values = np.asarray(
                handle[f"Base/Rotor Block/GridCoordinates/{name}/ data"][()],
                dtype=np.float64,
            )
            axes.append(np.transpose(values, (2, 1, 0)))
    return np.stack(axes, axis=-1)


def _reference_surface_geometry(
    points: np.ndarray,
) -> tuple[np.ndarray, list[tuple[int, int, int, int]]]:
    """旧实现基准：完整体坐标上的边界四边形。"""

    ni, nj, nk = (int(value) for value in points.shape[:3])

    def flat(i: int, j: int, k: int) -> int:
        return (i * nj + j) * nk + k

    def boundary(size: int) -> tuple[int, ...]:
        return () if size <= 0 else (0,) if size == 1 else (0, size - 1)

    quads: list[tuple[int, int, int, int]] = []
    for i in boundary(ni):
        for j in range(max(nj - 1, 0)):
            for k in range(max(nk - 1, 0)):
                quads.append((flat(i, j, k), flat(i, j + 1, k), flat(i, j + 1, k + 1), flat(i, j, k + 1)))
    for j in boundary(nj):
        for i in range(max(ni - 1, 0)):
            for k in range(max(nk - 1, 0)):
                quads.append((flat(i, j, k), flat(i + 1, j, k), flat(i + 1, j, k + 1), flat(i, j, k + 1)))
    for k in boundary(nk):
        for i in range(max(ni - 1, 0)):
            for j in range(max(nj - 1, 0)):
                quads.append((flat(i, j, k), flat(i + 1, j, k), flat(i + 1, j + 1, k), flat(i, j + 1, k)))
    used = sorted({point for quad in quads for point in quad})
    mapping = {flat_index: output_index for output_index, flat_index in enumerate(used)}
    flat_points = points.reshape((-1, 3))
    selected = flat_points[np.asarray(used, dtype=np.int64)] if used else np.empty((0, 3))
    polygons = [tuple(mapping[value] for value in quad) for quad in quads]
    return selected, polygons


def _reference_wireframe_geometry(
    points: np.ndarray,
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """旧实现基准：三组完整三重循环中筛选边界棱边。"""

    ni, nj, nk = (int(value) for value in points.shape[:3])

    def flat(i: int, j: int, k: int) -> int:
        return (i * nj + j) * nk + k

    segments: list[tuple[int, int]] = []
    for i in range(max(ni - 1, 0)):
        for j in range(nj):
            for k in range(nk):
                if j in {0, nj - 1} or k in {0, nk - 1}:
                    segments.append((flat(i, j, k), flat(i + 1, j, k)))
    for i in range(ni):
        for j in range(max(nj - 1, 0)):
            for k in range(nk):
                if i in {0, ni - 1} or k in {0, nk - 1}:
                    segments.append((flat(i, j, k), flat(i, j + 1, k)))
    for i in range(ni):
        for j in range(nj):
            for k in range(max(nk - 1, 0)):
                if i in {0, ni - 1} or j in {0, nj - 1}:
                    segments.append((flat(i, j, k), flat(i, j, k + 1)))
    used = sorted({point for segment in segments for point in segment})
    mapping = {flat_index: output_index for output_index, flat_index in enumerate(used)}
    flat_points = points.reshape((-1, 3))
    selected = flat_points[np.asarray(used, dtype=np.int64)] if used else np.empty((0, 3))
    return selected, [(mapping[first], mapping[second]) for first, second in segments]


@pytest.mark.parametrize(
    ("axis", "index", "expected_points", "fixed_coordinate"),
    [
        ("I", 0, 3 * 2, (0, 0.0)),
        ("I", 4, 3 * 2, (0, 4.0)),
        ("J", 0, 5 * 2, (1, 0.0)),
        ("J", 2, 5 * 2, (1, 20.0)),
        ("K", 0, 5 * 3, (2, 0.0)),
        ("K", 1, 5 * 3, (2, 100.0)),
    ],
)
def test_asymmetric_size_first_last_slices(
    tmp_path: Path,
    axis: str,
    index: int,
    expected_points: int,
    fixed_coordinate: tuple[int, float],
) -> None:
    cgns = _synthetic_cgns(tmp_path / "mesh.cgns", (5, 3, 2))
    path = get_or_create_slice(cgns, tmp_path / "cache", "b0001", axis, index)
    points = _points(path)
    assert len(points) == expected_points
    assert np.allclose(points[:, fixed_coordinate[0]], fixed_coordinate[1])


@pytest.mark.parametrize(
    ("size", "axis", "index", "expected_points", "fixed_coordinate"),
    [
        ((1, 3, 4), "I", 0, 12, (0, 0.0)),
        ((1, 3, 4), "J", 0, 4, (1, 0.0)),
        ((1, 3, 4), "J", 2, 4, (1, 20.0)),
        ((1, 3, 4), "K", 0, 3, (2, 0.0)),
        ((1, 3, 4), "K", 3, 3, (2, 300.0)),
        ((1, 1, 1), "I", 0, 1, (0, 0.0)),
        ((1, 1, 1), "J", 0, 1, (1, 0.0)),
        ((1, 1, 1), "K", 0, 1, (2, 0.0)),
        ((4, 1, 1), "I", 0, 1, (0, 0.0)),
        ((4, 1, 1), "I", 3, 1, (0, 3.0)),
    ],
)
def test_degenerate_dimension_slices(
    tmp_path: Path,
    size: tuple[int, int, int],
    axis: str,
    index: int,
    expected_points: int,
    fixed_coordinate: tuple[int, float],
) -> None:
    cgns = _synthetic_cgns(tmp_path / "mesh.cgns", size)
    path = get_or_create_slice(cgns, tmp_path / "cache", "b0001", axis, index)
    piece = _piece(path)
    assert int(piece.attrib["NumberOfPoints"]) == expected_points
    points = _points(path)
    assert np.allclose(points[:, fixed_coordinate[0]], fixed_coordinate[1])


@pytest.mark.parametrize("size", [(4, 3, 2), (3, 5, 2), (1, 3, 4), (2, 2, 2), (1, 1, 1)])
def test_boundary_geometry_matches_reference_algorithm(tmp_path: Path, size: tuple[int, int, int]) -> None:
    """新实现（边界平面读取）与旧算法（整读+三重循环）逐点拓扑一致。"""

    cgns = _synthetic_cgns(tmp_path / "mesh.cgns", size)
    cache = tmp_path / "cache"
    manifest = prepare_preview(cgns, cache)
    block = manifest["blocks"][0]
    reference = _reference_full_points(cgns)
    ref_surface, ref_polygons = _reference_surface_geometry(reference)
    ref_wireframe, ref_lines = _reference_wireframe_geometry(reference)

    surface = cache / block["assets"]["surface"]
    surface_piece = _piece(surface)
    assert int(surface_piece.attrib["NumberOfPoints"]) == len(ref_surface)
    if len(ref_surface):
        assert np.array_equal(_points(surface), ref_surface)
    assert set(_cells(surface, "Polys")) == set(ref_polygons)

    wireframe = cache / block["assets"]["wireframe"]
    wire_piece = _piece(wireframe)
    assert int(wire_piece.attrib["NumberOfPoints"]) == len(ref_wireframe)
    if len(ref_wireframe):
        assert np.array_equal(_points(wireframe), ref_wireframe)
    assert {frozenset(line) for line in _cells(wireframe, "Lines")} == {
        frozenset(line) for line in ref_lines
    }


def test_memory_budget_rejects_new_slice_and_recovers(tmp_path: Path) -> None:
    small = _synthetic_cgns(tmp_path / "small.cgns", (4, 3, 2))
    cache = tmp_path / "cache"
    with pytest.raises(PreviewError) as budget_error:
        get_or_create_slice(small, cache, "b0001", "I", 0, memory_budget_mb=0)
    assert budget_error.value.code == "PREVIEW_MEMORY_BUDGET_EXCEEDED"
    assert not any(cache.rglob("*.vtp"))
    path = get_or_create_slice(small, cache, "b0001", "I", 0, memory_budget_mb=512)
    assert path.is_file()

    big = _synthetic_cgns(tmp_path / "big.cgns", (96, 96, 96))
    big_cache = tmp_path / "big-cache"
    with pytest.raises(PreviewError) as exceeded:
        get_or_create_slice(big, big_cache, "b0001", "K", 0, memory_budget_mb=1)
    assert exceeded.value.code == "PREVIEW_MEMORY_BUDGET_EXCEEDED"
    assert not any(big_cache.rglob("*.vtp"))
    generated = get_or_create_slice(big, big_cache, "b0001", "K", 0, memory_budget_mb=512)
    assert generated.is_file()


def test_cache_limit_rejects_new_generation_existing_cache_readable(tmp_path: Path) -> None:
    cgns = _synthetic_cgns(tmp_path / "mesh.cgns", (4, 3, 2))
    cache = tmp_path / "cache"
    existing = get_or_create_slice(cgns, cache, "b0001", "I", 0)
    filler = cache / "blocks" / "b0001" / "slices" / "filler.vtp"
    filler.write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(PreviewError) as limit_error:
        get_or_create_slice(cgns, cache, "b0001", "J", 0, cache_limit_mb=1)
    assert limit_error.value.code == "PREVIEW_CACHE_LIMIT_EXCEEDED"
    cached = get_or_create_slice(cgns, cache, "b0001", "I", 0, cache_limit_mb=1)
    assert cached == existing
    assert cached.is_file()


def test_conversion_gate_single_flight_shares_generation() -> None:
    calls: list[int] = []

    def convert() -> str:
        time.sleep(0.05)
        calls.append(1)
        return "asset"

    async def scenario() -> list[str]:
        gate = _PreviewConversionGate()
        return await asyncio.gather(
            *(gate.run(("slice", "run1", "b0001", "I", 0), convert) for _ in range(8))
        )

    results = asyncio.run(scenario())
    assert results == ["asset"] * 8
    assert len(calls) == 1


def test_conversion_gate_runs_at_most_one_conversion() -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def convert() -> str:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return "asset"

    async def scenario() -> None:
        gate = _PreviewConversionGate()
        await asyncio.gather(
            *(
                gate.run(("slice", "run1", "b0001", axis, index), convert)
                for axis, index in zip("IJK", range(3))
            )
        )

    asyncio.run(scenario())
    assert peak == 1


def test_api_concurrent_same_slice_requests_generate_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一切片的并发 API 请求共享一次生成（单飞验证）。"""

    settings = Settings.from_env(
        {"MESH_DATA_DIR": str(tmp_path / "data")}, project_root=PROJECT_ROOT
    )
    settings.ensure_directories()
    application = api_module.create_app(settings)
    cgns = _synthetic_cgns(tmp_path / "mesh.cgns", (4, 3, 2))
    service = PreviewService(cgns, tmp_path / "preview-cache")
    monkeypatch.setattr(api_module, "_run_preview_status", lambda database, run_id: "READY")
    monkeypatch.setattr(api_module, "_preview_for_run", lambda *args, **kwargs: service)

    calls: list[tuple[str, str, int]] = []
    real_get_or_create = preview_module.get_or_create_slice

    def counting_get_or_create(cgns_path, cache_dir, block, axis, index, **kwargs):
        time.sleep(0.1)
        calls.append((str(block), str(axis), int(index)))
        return real_get_or_create(cgns_path, cache_dir, block, axis, index, **kwargs)

    monkeypatch.setattr(preview_module, "get_or_create_slice", counting_get_or_create)

    async def scenario():
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.gather(
                *(
                    client.get(
                        "/api/v1/runs/r1/mesh/slice",
                        params={"block": "b0001", "axis": "I", "index": 0},
                    )
                    for _ in range(6)
                )
            )

    responses = asyncio.run(scenario())
    assert [response.status_code for response in responses] == [200] * 6
    assert len(calls) == 1
    assert calls == [("b0001", "I", 0)]
