"""HDF5 CGNS 到 vtk.js VTP 预览的几何与缓存测试。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import h5py
import numpy as np
import pytest

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
