"""把 HDF5 CGNS 结构化网格转换为 vtk.js 可读取的 VTP 预览。

本模块有意不依赖 VTK/PyVista。HDF5 与数组依赖延迟导入，因此 API 即使在
缺少预览依赖或遇到 ADF CGNS 时仍能启动，并返回明确的降级原因。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
MANIFEST_SCHEMA_VERSION = 1


class PreviewError(RuntimeError):
    """带稳定错误码的预览错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PreviewUnavailableError(PreviewError):
    """表示原始网格可用，但 Viewer 无法生成。"""


@dataclass(frozen=True)
class _BlockDescriptor:
    block_id: str
    index: int
    base: str
    name: str
    zone_path: str
    coordinate_paths: tuple[str, str, str]
    storage_shape: tuple[int, int, int]

    @property
    def size(self) -> tuple[int, int, int]:
        # CGNS/HDF5 以 K,J,I 顺序存储；网页接口统一为 I,J,K。
        return tuple(reversed(self.storage_shape))


class PreviewService:
    """面向 API/Worker 的轻量预览服务。"""

    def __init__(self, cgns_path: str | os.PathLike[str], cache_dir: str | os.PathLike[str]):
        self.cgns_path = Path(cgns_path).resolve()
        self.cache_dir = Path(cache_dir).resolve()

    def prepare(self, *, force: bool = False) -> dict[str, Any]:
        return prepare_preview(self.cgns_path, self.cache_dir, force=force)

    def manifest(self) -> dict[str, Any]:
        path = self.cache_dir / "manifest.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if _manifest_matches_source(data, self.cgns_path):
                    return data
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        return self.prepare()

    def block_asset(self, block: str | int, mode: str) -> Path:
        normalized_mode = mode.lower()
        if normalized_mode not in {"surface", "wireframe"}:
            raise PreviewError("INVALID_PREVIEW_MODE", "预览模式仅支持 surface 或 wireframe")
        manifest = self.manifest()
        block_data = _manifest_block(manifest, block)
        relative = (block_data.get("assets") or {}).get(normalized_mode)
        if not relative:
            raise PreviewUnavailableError(
                "PREVIEW_ASSET_UNAVAILABLE", f"block {block!r} 没有 {normalized_mode} 预览"
            )
        path = (self.cache_dir / relative).resolve()
        _ensure_within(path, self.cache_dir)
        if not path.is_file():
            # 缓存被运维清理后可透明重建预生成资产。
            self.prepare(force=True)
        if not path.is_file():
            raise PreviewUnavailableError("PREVIEW_ASSET_MISSING", f"预览缓存不存在：{relative}")
        return path

    def slice_asset(
        self,
        block: str | int,
        axis: str,
        index: int,
        *,
        memory_budget_mb: int | None = None,
        cache_limit_mb: int | None = None,
    ) -> Path:
        return get_or_create_slice(
            self.cgns_path,
            self.cache_dir,
            block,
            axis,
            index,
            memory_budget_mb=memory_budget_mb,
            cache_limit_mb=cache_limit_mb,
        )


def detect_cgns_format(path: str | os.PathLike[str]) -> str:
    """区分 HDF5 与旧 ADF CGNS，不对 ADF 做误导性的解析尝试。"""

    cgns_path = Path(path)
    try:
        with cgns_path.open("rb") as stream:
            signature = stream.read(len(HDF5_SIGNATURE))
    except OSError as exc:
        raise PreviewError("CGNS_READ_FAILED", f"无法读取 CGNS：{exc}") from exc
    return "HDF5" if signature == HDF5_SIGNATURE else "ADF"


def build_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读取 block、I/J/K 维度与物理范围；ADF 返回显式降级 manifest。"""

    cgns_path = Path(path).resolve()
    if detect_cgns_format(cgns_path) != "HDF5":
        return _unavailable_manifest(
            cgns_path,
            format_name="ADF",
            reason_code="ADF_UNSUPPORTED",
            reason="当前 Viewer 仅支持 HDF5 CGNS；ADF 网格仍可下载并用于质量查看。",
        )
    try:
        h5py, np = _preview_dependencies()
    except PreviewUnavailableError as exc:
        return _unavailable_manifest(
            cgns_path,
            format_name="HDF5",
            reason_code=exc.code,
            reason=str(exc),
        )

    try:
        with h5py.File(cgns_path, "r") as handle:
            descriptors = _discover_blocks(handle)
            blocks: list[dict[str, Any]] = []
            for descriptor in descriptors:
                bounds = _coordinate_bounds(handle, descriptor)
                size = list(descriptor.size)
                blocks.append(
                    {
                        "id": descriptor.block_id,
                        "index": descriptor.index,
                        "base": descriptor.base,
                        "name": descriptor.name,
                        "size": size,
                        "dimensions": {"I": size[0], "J": size[1], "K": size[2]},
                        "index_ranges": {
                            "I": [0, size[0] - 1],
                            "J": [0, size[1] - 1],
                            "K": [0, size[2] - 1],
                        },
                        "bounds": {
                            "x": bounds[0],
                            "y": bounds[1],
                            "z": bounds[2],
                        },
                        "point_count": math.prod(size),
                        "cell_count": math.prod(max(value - 1, 0) for value in size),
                        "modes": ["surface", "wireframe", "slice"],
                    }
                )
    except PreviewError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise PreviewError("CGNS_PARSE_FAILED", f"HDF5 CGNS 解析失败：{exc}") from exc

    source = _source_metadata(cgns_path)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": "READY",
        "available": True,
        "format": "HDF5",
        "reason_code": None,
        "reason": None,
        "source": source,
        "blocks": blocks,
        "capabilities": {
            "surface": True,
            "wireframe": True,
            "slice": True,
            "slice_index_base": 0,
        },
    }


def prepare_preview(
    cgns_path: str | os.PathLike[str],
    cache_dir: str | os.PathLike[str],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """预生成各 block 表面/线框并原子写入 manifest。"""

    source = Path(cgns_path).resolve()
    cache = Path(cache_dir).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    manifest_path = cache / "manifest.json"
    if not force and manifest_path.is_file():
        try:
            cached = json.loads(manifest_path.read_text(encoding="utf-8"))
            if _manifest_matches_source(cached, source) and _manifest_assets_exist(cached, cache):
                return cached
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

    try:
        manifest = build_manifest(source)
        if not manifest["available"]:
            _atomic_write_json(manifest_path, manifest)
            return manifest
        h5py, _ = _preview_dependencies()
        with h5py.File(source, "r") as handle:
            descriptors = _discover_blocks(handle)
            by_id = {descriptor.block_id: descriptor for descriptor in descriptors}
            for block in manifest["blocks"]:
                descriptor = by_id[block["id"]]
                relative_dir = Path("blocks") / descriptor.block_id
                surface_relative = relative_dir / "surface.vtp"
                wireframe_relative = relative_dir / "wireframe.vtp"
                _write_surface_vtp_atomic(cache / surface_relative, handle, descriptor)
                _write_wireframe_vtp_atomic(cache / wireframe_relative, handle, descriptor)
                block["assets"] = {
                    "surface": surface_relative.as_posix(),
                    "wireframe": wireframe_relative.as_posix(),
                }
        _atomic_write_json(manifest_path, manifest)
        return manifest
    except PreviewError as exc:
        manifest = _unavailable_manifest(
            source,
            format_name="HDF5" if detect_cgns_format(source) == "HDF5" else "ADF",
            reason_code=exc.code,
            reason=str(exc),
        )
    except Exception as exc:  # 预览失败不能反转网格运行的成功状态。
        manifest = _unavailable_manifest(
            source,
            format_name="HDF5" if detect_cgns_format(source) == "HDF5" else "ADF",
            reason_code="PREVIEW_CONVERSION_FAILED",
            reason=f"预览转换失败：{type(exc).__name__}: {exc}",
        )
    _atomic_write_json(manifest_path, manifest)
    return manifest


def generate_block_vtp(
    cgns_path: str | os.PathLike[str],
    block: str | int,
    mode: str,
    output_path: str | os.PathLike[str],
) -> Path:
    """生成单个 block 的 surface 或 wireframe VTP。"""

    source = Path(cgns_path).resolve()
    if detect_cgns_format(source) != "HDF5":
        raise PreviewUnavailableError("ADF_UNSUPPORTED", "ADF CGNS 不支持三维预览")
    h5py, _ = _preview_dependencies()
    destination = Path(output_path).resolve()
    normalized_mode = mode.lower()
    with h5py.File(source, "r") as handle:
        descriptor = _select_descriptor(_discover_blocks(handle), block)
        if normalized_mode == "surface":
            _write_surface_vtp_atomic(destination, handle, descriptor)
        elif normalized_mode == "wireframe":
            _write_wireframe_vtp_atomic(destination, handle, descriptor)
        else:
            raise PreviewError("INVALID_PREVIEW_MODE", "预览模式仅支持 surface 或 wireframe")
    return destination


def get_or_create_slice(
    cgns_path: str | os.PathLike[str],
    cache_dir: str | os.PathLike[str],
    block: str | int,
    axis: str,
    index: int,
    *,
    memory_budget_mb: int | None = None,
    cache_limit_mb: int | None = None,
) -> Path:
    """按 0 基 I/J/K 索引生成切片；写入完成前缓存路径不可见。

    只通过 HDF5 hyperslab 读取请求平面，不载入全部体坐标。``memory_budget_mb``
    为切片工作内存预算（估计的可计算数组大小），``cache_limit_mb`` 为本运行
    切片磁盘缓存累计上限；超限拒绝新生成，已存在缓存继续可读。
    """

    source = Path(cgns_path).resolve()
    cache = Path(cache_dir).resolve()
    normalized_axis = axis.upper()
    if normalized_axis not in {"I", "J", "K"}:
        raise PreviewError("INVALID_SLICE_AXIS", "切片轴仅支持 I、J 或 K")
    if isinstance(index, bool) or not isinstance(index, int):
        raise PreviewError("INVALID_SLICE_INDEX", "切片索引必须是 0 基整数")
    if detect_cgns_format(source) != "HDF5":
        raise PreviewUnavailableError("ADF_UNSUPPORTED", "ADF CGNS 不支持按需切片")
    h5py, _ = _preview_dependencies()
    with h5py.File(source, "r") as handle:
        descriptor = _select_descriptor(_discover_blocks(handle), block)
        axis_number = {"I": 0, "J": 1, "K": 2}[normalized_axis]
        axis_size = descriptor.size[axis_number]
        if index < 0 or index >= axis_size:
            raise PreviewError(
                "SLICE_INDEX_OUT_OF_RANGE",
                f"{descriptor.name} 的 {normalized_axis} 索引范围为 0..{axis_size - 1}",
            )
        destination = (
            cache / "blocks" / descriptor.block_id / "slices" / normalized_axis / f"{index}.vtp"
        ).resolve()
        _ensure_within(destination, cache)
        if destination.is_file() and destination.stat().st_size > 0:
            return destination
        point_count, quad_count = _slice_plane_sizes(descriptor.size, axis_number)
        _check_slice_budget(point_count, quad_count, memory_budget_mb=memory_budget_mb)
        _check_slice_cache_limit(cache, point_count, quad_count, cache_limit_mb=cache_limit_mb)
        plane = _read_slice_plane(handle, descriptor, axis_number, index)
    slice_points, polygons = _slice_geometry(plane)
    _write_polydata_atomic(destination, slice_points, polygons=polygons)
    return destination


def _preview_dependencies() -> tuple[Any, Any]:
    try:
        import h5py
        import numpy as np
    except ImportError as exc:
        raise PreviewUnavailableError(
            "PREVIEW_DEPENDENCY_MISSING",
            "缺少 h5py 或 numpy，无法生成 HDF5 CGNS 预览。",
        ) from exc
    return h5py, np


def _discover_blocks(handle: Any) -> list[_BlockDescriptor]:
    h5py, _ = _preview_dependencies()
    candidates_by_zone: dict[
        str,
        tuple[int, str, str, str, tuple[str, str, str], tuple[int, int, int]],
    ] = {}

    def visitor(name: str, obj: Any) -> None:
        if not isinstance(obj, h5py.Group):
            return
        label = _attribute_text(obj.attrs.get("label"))
        if obj.name.rsplit("/", 1)[-1] != "GridCoordinates" and label != "GridCoordinates_t":
            return
        paths: list[str] = []
        shapes: list[tuple[int, ...]] = []
        for coordinate_name in ("CoordinateX", "CoordinateY", "CoordinateZ"):
            dataset = _coordinate_dataset(obj, coordinate_name)
            if dataset is None:
                return
            paths.append(dataset.name)
            shapes.append(tuple(int(value) for value in dataset.shape))
        if len(set(shapes)) != 1 or len(shapes[0]) != 3:
            raise PreviewError(
                "UNSUPPORTED_BLOCK_DIMENSIONS",
                f"{obj.parent.name} 的 X/Y/Z 坐标必须是相同的三维数组：{shapes}",
            )
        storage_shape = shapes[0]
        if any(value <= 0 for value in storage_shape):
            raise PreviewError("EMPTY_COORDINATES", f"{obj.parent.name} 包含空坐标维度")
        zone = obj.parent
        base = zone.parent
        candidate = (
            0 if obj.name.rsplit("/", 1)[-1] == "GridCoordinates" else 1,
            base.name.rsplit("/", 1)[-1],
            zone.name.rsplit("/", 1)[-1],
            zone.name,
            tuple(paths),
            storage_shape,
        )
        previous = candidates_by_zone.get(zone.name)
        if previous is None or candidate[0] < previous[0]:
            # 一个 Zone 可包含历史坐标集；Viewer 以标准 GridCoordinates 为
            # 当前坐标，避免把同一结构 block 重复暴露。
            candidates_by_zone[zone.name] = candidate

    handle.visititems(visitor)
    candidates = [value[1:] for value in candidates_by_zone.values()]
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    if not candidates:
        raise PreviewError(
            "NO_STRUCTURED_BLOCKS", "CGNS 中未找到 GridCoordinates/CoordinateX,Y,Z 三维结构化 block"
        )
    return [
        _BlockDescriptor(
            block_id=f"b{index:04d}",
            index=index,
            base=base,
            name=name,
            zone_path=zone_path,
            coordinate_paths=paths,
            storage_shape=storage_shape,
        )
        for index, (base, name, zone_path, paths, storage_shape) in enumerate(candidates, start=1)
    ]


def _coordinate_dataset(group: Any, coordinate_name: str) -> Any | None:
    h5py, _ = _preview_dependencies()
    direct = group.get(coordinate_name)
    if isinstance(direct, h5py.Dataset):
        return direct
    if isinstance(direct, h5py.Group):
        data = direct.get(" data")
        if isinstance(data, h5py.Dataset):
            return data
        datasets = [value for value in direct.values() if isinstance(value, h5py.Dataset)]
        if len(datasets) == 1:
            return datasets[0]
    # 某些写入器不保留标准节点名，但会保留 DataArray_t 名称属性。
    for child in group.values():
        if not isinstance(child, h5py.Group):
            continue
        child_name = child.name.rsplit("/", 1)[-1]
        if child_name.lower() != coordinate_name.lower():
            continue
        data = child.get(" data")
        if isinstance(data, h5py.Dataset):
            return data
    return None


_BOUNDS_SCAN_ELEMENTS = 4 * 1024 * 1024  # 包围盒分块归约的单块元素数


def _coordinate_bounds(handle: Any, descriptor: _BlockDescriptor) -> list[list[float]]:
    """分块归约各坐标数据集的范围，不一次性载入整个数组。"""

    _, np = _preview_dependencies()
    bounds: list[list[float]] = []
    for dataset_path in descriptor.coordinate_paths:
        dataset = handle[dataset_path]
        shape = tuple(int(value) for value in dataset.shape)
        if not shape:
            raise PreviewError("EMPTY_COORDINATES", f"{descriptor.name} 包含空坐标数组")
        row_elements = math.prod(shape[1:])
        rows_per_chunk = max(1, _BOUNDS_SCAN_ELEMENTS // max(row_elements, 1))
        minimum = math.inf
        maximum = -math.inf
        finite_found = False
        for start in range(0, shape[0], rows_per_chunk):
            values = np.asarray(dataset[start : start + rows_per_chunk], dtype=np.float64)
            finite = values[np.isfinite(values)]
            if finite.size == 0:
                continue
            finite_found = True
            minimum = min(minimum, float(np.min(finite)))
            maximum = max(maximum, float(np.max(finite)))
        if not finite_found:
            raise PreviewError(
                "INVALID_COORDINATES", f"{descriptor.name} 坐标全部为非有限值"
            )
        bounds.append([minimum, maximum])
    return bounds


def _slice_plane_sizes(size: tuple[int, int, int], axis_number: int) -> tuple[int, int]:
    """返回 (平面点数, 平面四边形数)。"""

    others = [value for position, value in enumerate(size) if position != axis_number]
    first, second = others
    return first * second, max(first - 1, 0) * max(second - 1, 0)


def _slice_memory_estimate(point_count: int, quad_count: int) -> int:
    """估计切片转换的工作内存（可计算数组大小）：三个坐标平面、
    组装后的 (N,3) 输出及 Python 多边形索引对象。"""

    return point_count * 8 * 6 + quad_count * 4 * 28


def _slice_file_estimate(point_count: int, quad_count: int) -> int:
    """估计切片 VTP 的 ASCII 文本大小：坐标约 26 字节/分量，拓扑约 60 字节/四边形。"""

    return point_count * 3 * 26 + quad_count * 60


def _check_slice_budget(
    point_count: int, quad_count: int, *, memory_budget_mb: int | None
) -> None:
    if memory_budget_mb is None:
        return
    estimated = _slice_memory_estimate(point_count, quad_count)
    limit_bytes = memory_budget_mb * 1024 * 1024
    if estimated > limit_bytes:
        raise PreviewError(
            "PREVIEW_MEMORY_BUDGET_EXCEEDED",
            f"切片估计工作内存 {estimated} 字节超过预算 {limit_bytes} 字节，拒绝生成",
        )


def _check_slice_cache_limit(
    cache: Path, point_count: int, quad_count: int, *, cache_limit_mb: int | None
) -> None:
    if cache_limit_mb is None:
        return
    existing = sum(
        path.stat().st_size
        for path in cache.glob("blocks/*/slices/**/*.vtp")
        if path.is_file()
    )
    projected = existing + _slice_file_estimate(point_count, quad_count)
    limit_bytes = cache_limit_mb * 1024 * 1024
    if projected > limit_bytes:
        raise PreviewError(
            "PREVIEW_CACHE_LIMIT_EXCEEDED",
            f"本运行切片磁盘缓存 {existing} 字节加本次生成将超过上限 {limit_bytes} 字节，拒绝生成新切片",
        )


def _attribute_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    if hasattr(value, "tobytes"):
        try:
            return value.tobytes().decode("utf-8", errors="replace").rstrip("\x00")
        except (AttributeError, UnicodeError):
            pass
    return str(value)


def _read_points_at(
    handle: Any, descriptor: _BlockDescriptor, triples: Sequence[tuple[int, int, int]]
) -> Any:
    """只读取指定 (i, j, k) 索引处的坐标，不载入其余体坐标。

    每个点归属其所在的一个边界平面，用 hyperslab 读取该平面后在平面内
    按索引取点（h5py 的逐点索引要求升序，平面内 numpy 索引无此限制）。
    """

    _, np = _preview_dependencies()
    if not triples:
        return np.empty((0, 3), dtype=np.float64)
    plane_specs: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for axis_number, size in enumerate(descriptor.size):
        for index in _boundary_indices(size):
            key = (axis_number, index)
            if key not in seen:
                seen.add(key)
                plane_specs.append(key)
    by_plane: dict[int, list[tuple[int, tuple[int, int, int]]]] = {}
    for position, triple in enumerate(triples):
        for plane_number, (axis_number, index) in enumerate(plane_specs):
            if triple[axis_number] == index:
                by_plane.setdefault(plane_number, []).append((position, triple))
                break
    output = np.empty((len(triples), 3), dtype=np.float64)
    for plane_number, entries in by_plane.items():
        axis_number, index = plane_specs[plane_number]
        other_axes = [value for value in (0, 1, 2) if value != axis_number]
        first_axis, second_axis = other_axes
        positions = [position for position, _ in entries]
        first_values = np.asarray(
            [triple[first_axis] for _, triple in entries], dtype=np.int64
        )
        second_values = np.asarray(
            [triple[second_axis] for _, triple in entries], dtype=np.int64
        )
        axes = []
        for path in descriptor.coordinate_paths:
            dataset = handle[path]
            if axis_number == 0:  # I 边界：存储 [:, :, i] -> (K, J)
                plane = dataset[:, :, index]
            elif axis_number == 1:  # J 边界：存储 [:, j, :] -> (K, I)
                plane = dataset[:, index, :]
            else:  # K 边界：存储 [k, :, :] -> (J, I)
                plane = dataset[index, :, :]
            plane = np.asarray(np.transpose(plane, (1, 0)), dtype=np.float64)
            axes.append(plane[first_values, second_values])
        points = np.stack(axes, axis=-1)
        if not bool(np.all(np.isfinite(points))):
            raise PreviewError("INVALID_COORDINATES", f"{descriptor.name} 含 NaN 或无穷坐标")
        output[np.asarray(positions, dtype=np.int64)] = points
    return output


def _read_slice_plane(
    handle: Any, descriptor: _BlockDescriptor, axis_number: int, index: int
) -> Any:
    """用 HDF5 hyperslab 直接读取一个 I/J/K 平面，转换为 (first, second, 3)。

    I 对应存储的 ``[:, :, i]``、J 对应 ``[:, j, :]``、K 对应 ``[k, :, :]``；
    平面再转置为 I,J,K 顺序坐标（J,K / I,K / I,J 主轴优先）。
    """

    _, np = _preview_dependencies()
    axes = []
    for path in descriptor.coordinate_paths:
        dataset = handle[path]
        if axis_number == 0:  # I 平面：存储 [:, :, i] -> (K, J)
            plane = dataset[:, :, index]
        elif axis_number == 1:  # J 平面：存储 [:, j, :] -> (K, I)
            plane = dataset[:, index, :]
        else:  # K 平面：存储 [k, :, :] -> (J, I)
            plane = dataset[index, :, :]
        axes.append(np.asarray(np.transpose(plane, (1, 0)), dtype=np.float64))
    plane_points = np.stack(axes, axis=-1)
    if not bool(np.all(np.isfinite(plane_points))):
        raise PreviewError("INVALID_COORDINATES", f"{descriptor.name} 含 NaN 或无穷坐标")
    return plane_points


def _write_surface_vtp_atomic(path: Path, handle: Any, descriptor: _BlockDescriptor) -> None:
    used, polygons = _surface_geometry(descriptor.size)
    selected_points = _read_points_at(handle, descriptor, used)
    _write_polydata_atomic(path, selected_points, polygons=polygons)


def _write_wireframe_vtp_atomic(path: Path, handle: Any, descriptor: _BlockDescriptor) -> None:
    used, lines = _wireframe_geometry(descriptor.size)
    selected_points = _read_points_at(handle, descriptor, used)
    _write_polydata_atomic(path, selected_points, lines=lines)


def _surface_geometry(
    size: tuple[int, int, int],
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int, int]]]:
    """返回 (使用的边界点 (i,j,k), 四边形索引)；只枚举边界平面。"""

    quads = list(_boundary_quads(size))
    used = sorted({triple for quad in quads for triple in quad})
    mapping = {triple: output_index for output_index, triple in enumerate(used)}
    polygons = [tuple(mapping[value] for value in quad) for quad in quads]
    return used, polygons


def _wireframe_geometry(
    size: tuple[int, int, int],
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int]]]:
    """返回 (使用的边界点 (i,j,k), 线索引)；只枚举边界棱边并去重。"""

    ni, nj, nk = size
    segments: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = []
    if ni > 1:
        for i in range(ni - 1):
            for j, k in _boundary_pairs(nj, nk):
                segments.append(((i, j, k), (i + 1, j, k)))
    if nj > 1:
        for j in range(nj - 1):
            for i, k in _boundary_pairs(ni, nk):
                segments.append(((i, j, k), (i, j + 1, k)))
    if nk > 1:
        for k in range(nk - 1):
            for i, j in _boundary_pairs(ni, nj):
                segments.append(((i, j, k), (i, j, k + 1)))
    used = sorted({triple for segment in segments for triple in segment})
    mapping = {triple: output_index for output_index, triple in enumerate(used)}
    return used, [(mapping[first], mapping[second]) for first, second in segments]


def _slice_geometry(plane: Any) -> tuple[Any, list[tuple[int, int, int, int]]]:
    """由 (first, second, 3) 平面点阵生成切片四边形拓扑。"""

    _, np = _preview_dependencies()
    first_size, second_size = (int(value) for value in plane.shape[:2])
    selected = np.asarray(plane, dtype=np.float64).reshape((-1, 3))
    polygons: list[tuple[int, int, int, int]] = []
    for first in range(max(first_size - 1, 0)):
        for second in range(max(second_size - 1, 0)):
            origin = first * second_size + second
            polygons.append(
                (origin, origin + second_size, origin + second_size + 1, origin + 1)
            )
    return selected, polygons


def _boundary_quads(
    size: tuple[int, int, int],
) -> Iterator[tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]]:
    """枚举边界平面上的四边形（顶点为 (i,j,k) 三元组），只遍历边界。"""

    ni, nj, nk = size
    for i in _boundary_indices(ni):
        for j in range(max(nj - 1, 0)):
            for k in range(max(nk - 1, 0)):
                yield ((i, j, k), (i, j + 1, k), (i, j + 1, k + 1), (i, j, k + 1))
    for j in _boundary_indices(nj):
        for i in range(max(ni - 1, 0)):
            for k in range(max(nk - 1, 0)):
                yield ((i, j, k), (i + 1, j, k), (i + 1, j, k + 1), (i, j, k + 1))
    for k in _boundary_indices(nk):
        for i in range(max(ni - 1, 0)):
            for j in range(max(nj - 1, 0)):
                yield ((i, j, k), (i + 1, j, k), (i + 1, j + 1, k), (i, j + 1, k))


def _boundary_indices(size: int) -> tuple[int, ...]:
    if size <= 0:
        return ()
    return (0,) if size == 1 else (0, size - 1)


def _boundary_pairs(first_size: int, second_size: int) -> Iterator[tuple[int, int]]:
    """枚举 (first, second) 平面中位于边界上的索引对，去重且不遍历内部。"""

    for first in range(first_size):
        for second in _boundary_indices(second_size):
            yield (first, second)
    for first in _boundary_indices(first_size):
        for second in range(1, second_size - 1):
            yield (first, second)


def _write_polydata_atomic(
    path: Path,
    points: Any,
    *,
    polygons: Sequence[Sequence[int]] = (),
    lines: Sequence[Sequence[int]] = (),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write('<?xml version="1.0"?>\n')
            stream.write('<VTKFile type="PolyData" version="1.0" byte_order="LittleEndian">\n')
            stream.write("  <PolyData>\n")
            stream.write(
                "    <Piece NumberOfPoints=\"{}\" NumberOfVerts=\"0\" NumberOfLines=\"{}\" "
                "NumberOfStrips=\"0\" NumberOfPolys=\"{}\">\n".format(len(points), len(lines), len(polygons))
            )
            stream.write("      <Points>\n")
            stream.write('        <DataArray type="Float64" NumberOfComponents="3" format="ascii">\n          ')
            _write_numbers(stream, (float(value) for point in points for value in point), floating=True)
            stream.write("\n        </DataArray>\n      </Points>\n")
            if lines:
                _write_cells(stream, "Lines", lines)
            if polygons:
                _write_cells(stream, "Polys", polygons)
            stream.write("    </Piece>\n  </PolyData>\n</VTKFile>\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _write_cells(stream: Any, section: str, cells: Sequence[Sequence[int]]) -> None:
    stream.write(f"      <{section}>\n")
    # vtk.js 对 Int32 有原生 TypedArray 支持；预览资产不可能接近 2^31 点，
    # 因而无需使用其兼容性较弱的 Int64 降级路径。
    stream.write('        <DataArray type="Int32" Name="connectivity" format="ascii">\n          ')
    _write_numbers(stream, (value for cell in cells for value in cell), floating=False)
    stream.write("\n        </DataArray>\n")
    stream.write('        <DataArray type="Int32" Name="offsets" format="ascii">\n          ')
    running = 0
    offsets = []
    for cell in cells:
        running += len(cell)
        offsets.append(running)
    _write_numbers(stream, offsets, floating=False)
    stream.write(f"\n        </DataArray>\n      </{section}>\n")


def _write_numbers(stream: Any, values: Iterable[float | int], *, floating: bool) -> None:
    chunk: list[str] = []
    for value in values:
        chunk.append(format(float(value), ".17g") if floating else str(int(value)))
        if len(chunk) >= 4096:
            stream.write(" ".join(chunk))
            stream.write(" ")
            chunk.clear()
    if chunk:
        stream.write(" ".join(chunk))


def _select_descriptor(descriptors: Sequence[_BlockDescriptor], block: str | int) -> _BlockDescriptor:
    if isinstance(block, int) and not isinstance(block, bool):
        if 0 <= block < len(descriptors):
            return descriptors[block]
        raise PreviewError("BLOCK_NOT_FOUND", f"找不到 0 基 block 索引：{block}")
    text = str(block)
    matches = [
        item
        for item in descriptors
        if text in {item.block_id, item.name, str(item.index)}
    ]
    if not matches:
        raise PreviewError("BLOCK_NOT_FOUND", f"找不到 block：{block}")
    if len(matches) > 1 and not any(item.block_id == text for item in matches):
        raise PreviewError("BLOCK_NAME_AMBIGUOUS", f"block 名称不唯一，请使用 manifest 中的 id：{block}")
    return matches[0]


def _manifest_block(manifest: dict[str, Any], block: str | int) -> dict[str, Any]:
    if not manifest.get("available"):
        raise PreviewUnavailableError(
            str(manifest.get("reason_code") or "PREVIEW_UNAVAILABLE"),
            str(manifest.get("reason") or "预览不可用"),
        )
    blocks = manifest.get("blocks", [])
    if isinstance(block, int) and not isinstance(block, bool):
        if 0 <= block < len(blocks):
            return blocks[block]
        raise PreviewError("BLOCK_NOT_FOUND", f"找不到 0 基 block 索引：{block}")
    text = str(block)
    matches = [
        item
        for item in blocks
        if text in {str(item.get("id")), str(item.get("name")), str(item.get("index"))}
    ]
    if not matches:
        raise PreviewError("BLOCK_NOT_FOUND", f"找不到 block：{block}")
    if len(matches) > 1 and not any(str(item.get("id")) == text for item in matches):
        raise PreviewError("BLOCK_NAME_AMBIGUOUS", f"block 名称不唯一，请使用 manifest 中的 id：{block}")
    return matches[0]


def _source_metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        # manifest 只读取文件头和元数据时避免再次扫描大型 CGNS；大小和纳秒时间
        # 对不可变 run 足以检测误复用，运行产物另有 SHA-256 清单。
        "cache_key": hashlib.sha256(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii")).hexdigest(),
    }


def _unavailable_manifest(
    path: Path,
    *,
    format_name: str,
    reason_code: str,
    reason: str,
) -> dict[str, Any]:
    try:
        source = _source_metadata(path)
    except OSError:
        source = None
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": "UNAVAILABLE",
        "available": False,
        "format": format_name,
        "reason_code": reason_code,
        "reason": reason,
        "source": source,
        "blocks": [],
        "capabilities": {
            "surface": False,
            "wireframe": False,
            "slice": False,
            "slice_index_base": 0,
        },
    }


def _manifest_matches_source(manifest: dict[str, Any], source: Path) -> bool:
    try:
        return manifest.get("source") == _source_metadata(source)
    except OSError:
        return False


def _manifest_assets_exist(manifest: dict[str, Any], cache: Path) -> bool:
    if not manifest.get("available"):
        return True
    for block in manifest.get("blocks", []):
        assets = block.get("assets") or {}
        for mode in ("surface", "wireframe"):
            relative = assets.get(mode)
            if not relative:
                return False
            candidate = (cache / relative).resolve()
            try:
                _ensure_within(candidate, cache)
            except PreviewError:
                return False
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                return False
    return True


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise PreviewError("UNSAFE_PREVIEW_PATH", "预览缓存路径越出数据目录") from exc


# 兼容清晰的动词命名，便于 API 和离线工具复用。
generate_preview_assets = prepare_preview
inspect_cgns = build_manifest


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "PreviewError",
    "PreviewService",
    "PreviewUnavailableError",
    "build_manifest",
    "detect_cgns_format",
    "generate_block_vtp",
    "generate_preview_assets",
    "get_or_create_slice",
    "inspect_cgns",
    "prepare_preview",
]
