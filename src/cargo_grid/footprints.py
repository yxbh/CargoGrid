"""Bounded deterministic packing for all-height projected mesh footprints."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice, permutations, product
from math import ceil, floor, sqrt

import numpy as np
from scipy.signal import fftconvolve
from shapely import affinity, contains_xy, polygons, set_precision, union_all
from shapely.geometry.base import BaseGeometry

from cargo_grid.packing import PrintPlacement
from cargo_grid.parameters import count, positive

_PROJECTION_UNION_CHUNK = 2048
_PROJECTION_UNION_FAN_IN = 32


@dataclass(frozen=True)
class ProjectedFootprint:
    geometry: BaseGeometry

    def __post_init__(self) -> None:
        if (
            not isinstance(self.geometry, BaseGeometry)
            or self.geometry.is_empty
            or not self.geometry.is_valid
            or self.geometry.area <= 0
        ):
            raise ValueError("projected footprint must be a valid positive-area geometry")
        minimum_x, minimum_y, _, _ = self.geometry.bounds
        translate_x = 0 if abs(minimum_x) <= 1e-12 else -minimum_x
        translate_y = 0 if abs(minimum_y) <= 1e-12 else -minimum_y
        normalized = affinity.translate(self.geometry, translate_x, translate_y)
        if not normalized.is_valid:
            normalized = set_precision(normalized, 1e-12)
        if normalized.is_empty or not normalized.is_valid or normalized.area <= 0:
            raise ValueError("projected footprint normalization produced an invalid geometry")
        object.__setattr__(
            self,
            "geometry",
            normalized,
        )

    @property
    def size(self) -> tuple[float, float]:
        minimum_x, minimum_y, maximum_x, maximum_y = self.geometry.bounds
        return maximum_x - minimum_x, maximum_y - minimum_y


@dataclass(frozen=True)
class _Raster:
    mask: np.ndarray
    minimum_x: int
    minimum_y: int
    width: float
    depth: float
    rotation: int


def projected_mesh_footprint(
    vertices: np.ndarray,
    faces: np.ndarray,
) -> ProjectedFootprint:
    return projected_meshes_footprint([(vertices, faces)])


def projected_meshes_footprint(
    meshes: list[tuple[np.ndarray, np.ndarray]],
) -> ProjectedFootprint:
    projected = []
    for vertices, faces in meshes:
        vertices = np.asarray(vertices, dtype=float)
        faces = np.asarray(faces, dtype=np.int64)
        if vertices.ndim != 2 or vertices.shape[1] < 2:
            raise ValueError("projected footprint vertices must be an Nx2 or Nx3 array")
        if faces.ndim != 2 or faces.shape[1] != 3:
            raise ValueError("projected footprint faces must be an Mx3 array")
        triangles = vertices[faces, :2]
        first = triangles[:, 1] - triangles[:, 0]
        second = triangles[:, 2] - triangles[:, 0]
        area = first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]
        selected = triangles[np.abs(area) > 1e-12]
        for start in range(0, len(selected), _PROJECTION_UNION_CHUNK):
            projected.append(union_all(polygons(selected[start : start + _PROJECTION_UNION_CHUNK])))
            if len(projected) == _PROJECTION_UNION_FAN_IN:
                projected = [union_all(projected)]
    if not projected:
        raise ValueError("projected mesh has no nondegenerate XY triangles")
    geometry = union_all(projected)
    if geometry.is_empty or not geometry.is_valid or geometry.area <= 0:
        raise ValueError("projected mesh did not form a valid positive-area footprint")
    return ProjectedFootprint(geometry)


def oriented_footprint(
    footprint: ProjectedFootprint,
    rotation: int,
) -> ProjectedFootprint:
    if rotation not in (0, 90):
        raise ValueError("projected footprint rotation must be 0 or 90 degrees")
    geometry = (
        footprint.geometry
        if rotation == 0
        else affinity.rotate(footprint.geometry, rotation, origin=(0, 0))
    )
    return ProjectedFootprint(geometry)


def placed_footprint(
    footprint: ProjectedFootprint,
    placement: PrintPlacement,
) -> BaseGeometry:
    oriented = oriented_footprint(footprint, placement.rotation)
    return affinity.translate(oriented.geometry, placement.x, placement.y)


def minimum_projected_clearance(
    footprints: list[ProjectedFootprint],
    placements: list[PrintPlacement],
    *,
    plate: int,
) -> float:
    if len(footprints) != len(placements):
        raise ValueError("projected footprints must match placement count")
    selected = [
        placed_footprint(footprint, placement)
        for footprint, placement in zip(footprints, placements)
        if placement.plate == plate
    ]
    if len(selected) < 2:
        raise ValueError("projected clearance requires at least two parts on the plate")
    return min(
        first.distance(second)
        for index, first in enumerate(selected)
        for second in selected[index + 1 :]
    )


def _rasterize(
    footprint: ProjectedFootprint,
    *,
    search_gap: float,
    grid: float,
    mesh_error: float,
    rotation: int,
) -> _Raster:
    oriented = oriented_footprint(footprint, rotation)
    width, depth = oriented.size
    cell_error = grid / sqrt(2)
    protected = oriented.geometry.buffer(
        search_gap / 2 + mesh_error + cell_error,
        quad_segs=16,
    )
    minimum_x, minimum_y, maximum_x, maximum_y = protected.bounds
    ix0 = floor(minimum_x / grid)
    iy0 = floor(minimum_y / grid)
    ix1 = ceil(maximum_x / grid)
    iy1 = ceil(maximum_y / grid)
    xs = (np.arange(ix0, ix1) + 0.5) * grid
    ys = (np.arange(iy0, iy1) + 0.5) * grid
    xx, yy = np.meshgrid(xs, ys)
    mask = contains_xy(protected, xx, yy)
    if not mask.any():
        raise ValueError("projected footprint raster is empty")
    return _Raster(mask, ix0, iy0, width, depth, rotation)


def _placement_geometries(
    footprints: list[ProjectedFootprint],
    placements: dict[int, tuple[float, float, int]],
) -> list[BaseGeometry]:
    return [
        affinity.translate(
            oriented_footprint(footprint, placements[index][2]).geometry,
            placements[index][0],
            placements[index][1],
        )
        for index, footprint in enumerate(footprints)
    ]


def _clearance_key(
    geometries: list[BaseGeometry],
    target: float,
) -> tuple[tuple[float, ...], tuple[float, int, int]]:
    distances = sorted(
        (first.distance(second), index, other_index)
        for index, first in enumerate(geometries)
        for other_index, second in enumerate(geometries[index + 1 :], index + 1)
    )
    return tuple(min(distance, target) for distance, _, _ in distances), distances[0]


def _refine_clearance(
    footprints: list[ProjectedFootprint],
    placements: dict[int, tuple[float, float, int]],
    *,
    width: float,
    depth: float,
    target: float,
    grid: float,
    tolerance: float,
    max_refinements: int,
) -> dict[int, tuple[float, float, int]]:
    for _ in range(max_refinements + 1):
        geometries = _placement_geometries(footprints, placements)
        baseline, minimum = _clearance_key(geometries, target)
        if minimum[0] >= target - tolerance:
            return placements
        options = []
        for index in minimum[1:]:
            x, y, rotation = placements[index]
            for dx, dy in ((0, grid), (grid, 0), (0, -grid), (-grid, 0)):
                moved = dict(placements)
                moved[index] = (x + dx, y + dy, rotation)
                geometry = affinity.translate(
                    oriented_footprint(footprints[index], rotation).geometry,
                    x + dx,
                    y + dy,
                )
                minimum_x, minimum_y, maximum_x, maximum_y = geometry.bounds
                if (
                    minimum_x < -tolerance
                    or minimum_y < -tolerance
                    or maximum_x > width + tolerance
                    or maximum_y > depth + tolerance
                ):
                    continue
                key, moved_minimum = _clearance_key(
                    _placement_geometries(footprints, moved),
                    target,
                )
                if key > baseline:
                    options.append((key, moved_minimum[0], moved))
        if not options:
            break
        placements = max(options, key=lambda option: (option[0], option[1]))[2]
    raise ValueError(f"could not refine projected-footprint placement to {target:g} mm clearance")


def _packing_orders(
    footprints: list[ProjectedFootprint],
    rasters: dict[tuple[int, int], _Raster],
    max_order_attempts: int,
):
    groups = {}
    for index, footprint in enumerate(footprints):
        raster = rasters[index, 0]
        key = (
            -round(max(raster.width, raster.depth) + min(raster.width, raster.depth), 6),
            -round(raster.width * raster.depth, 6),
        )
        groups.setdefault(key, []).append(index)
    input_order = [index for key in sorted(groups) for index in groups[key]]
    yield input_order
    seen = {tuple(input_order)}
    variants = []
    for key in sorted(groups):
        stable = sorted(
            groups[key],
            key=lambda index: (
                round(footprints[index].geometry.area, 9),
                footprints[index].geometry.normalize().wkt,
                index,
            ),
        )
        variants.append(list(islice(permutations(stable), max_order_attempts)))
    for selection in product(*variants):
        order = tuple(index for group in selection for index in group)
        if order in seen:
            continue
        yield list(order)
        seen.add(order)
        if len(seen) >= max_order_attempts:
            break


def _pack_order(
    rasters: dict[tuple[int, int], _Raster],
    order: list[int],
    *,
    width: float,
    depth: float,
    grid: float,
    search_gap: float,
    mesh_error: float,
    tolerance: float,
    max_candidate_positions: int,
) -> dict[int, tuple[float, float, int]] | None:
    protection_cells = ceil((search_gap / 2 + mesh_error + grid) / grid)
    global_minimum = -protection_cells
    occupied = np.zeros(
        (
            ceil(depth / grid) + 2 * protection_cells,
            ceil(width / grid) + 2 * protection_cells,
        ),
        dtype=np.uint8,
    )
    placements: dict[int, tuple[float, float, int]] = {}
    candidate_positions = 0
    for index in order:
        options = []
        for rotation in (0, 90):
            raster = rasters[index, rotation]
            if raster.width > width + tolerance or raster.depth > depth + tolerance:
                continue
            collision = fftconvolve(
                occupied,
                raster.mask[::-1, ::-1].astype(np.uint8),
                mode="valid",
            )
            max_x = floor((width - raster.width) / grid)
            max_y = floor((depth - raster.depth) / grid)
            candidate_positions += (max_x + 1) * (max_y + 1)
            if candidate_positions > max_candidate_positions:
                raise ValueError("projected-footprint search exceeded its candidate budget")
            for y in range(max_y + 1):
                top = y + raster.minimum_y - global_minimum
                if not 0 <= top < collision.shape[0]:
                    continue
                for x in range(max_x + 1):
                    left = x + raster.minimum_x - global_minimum
                    if 0 <= left < collision.shape[1] and collision[top, left] < 0.5:
                        options.append(
                            (
                                x * grid + raster.width,
                                y * grid + raster.depth,
                                x,
                                y,
                                rotation,
                                raster,
                            )
                        )
        if not options:
            return None
        _, _, x, y, rotation, raster = min(options)
        top = y + raster.minimum_y - global_minimum
        left = x + raster.minimum_x - global_minimum
        raster_height, raster_width = raster.mask.shape
        occupied[top : top + raster_height, left : left + raster_width] |= raster.mask
        placements[index] = (x * grid, y * grid, rotation)
    return placements


def pack_projected_footprints(
    footprints: list[ProjectedFootprint],
    usable_bounds: tuple[float, float, float, float],
    *,
    gap: float,
    search_gap: float,
    grid: float = 1.0,
    mesh_error: float = 0.021,
    tolerance: float = 1e-6,
    max_candidate_positions: int = 4_000_000,
    max_refinements: int = 20,
    max_order_attempts: int = 8,
) -> list[PrintPlacement]:
    """Pack one plate by conservative raster search, then verify exact footprint distance."""

    if not footprints:
        return []
    positive("projected footprint gap", gap)
    positive("projected footprint search gap", search_gap)
    positive("projected footprint grid", grid)
    positive("projected footprint mesh error", mesh_error, zero=True)
    positive("projected footprint tolerance", tolerance)
    count("projected footprint candidate budget", max_candidate_positions)
    count("projected footprint refinement limit", max_refinements)
    count("projected footprint order attempts", max_order_attempts)
    if search_gap > gap:
        raise ValueError("projected footprint search gap cannot exceed the required gap")
    minimum_x, minimum_y, maximum_x, maximum_y = usable_bounds
    width, depth = maximum_x - minimum_x, maximum_y - minimum_y
    positive("projected footprint region width", width)
    positive("projected footprint region depth", depth)
    rasters = {
        (index, rotation): _rasterize(
            footprint,
            search_gap=search_gap,
            grid=grid,
            mesh_error=mesh_error,
            rotation=rotation,
        )
        for index, footprint in enumerate(footprints)
        for rotation in (0, 90)
    }
    placements = None
    for order in _packing_orders(footprints, rasters, max_order_attempts):
        candidate = _pack_order(
            rasters,
            order,
            width=width,
            depth=depth,
            grid=grid,
            search_gap=search_gap,
            mesh_error=mesh_error,
            tolerance=tolerance,
            max_candidate_positions=max_candidate_positions,
        )
        if candidate is None:
            continue
        try:
            placements = _refine_clearance(
                footprints,
                candidate,
                width=width,
                depth=depth,
                target=gap,
                grid=grid,
                tolerance=tolerance,
                max_refinements=max_refinements,
            )
        except ValueError:
            continue
        break
    if placements is None:
        raise ValueError("could not fit projected footprints on one plate")
    result = [
        PrintPlacement(
            0,
            minimum_x + placements[index][0],
            minimum_y + placements[index][1],
            placements[index][2],
        )
        for index in range(len(footprints))
    ]
    if minimum_projected_clearance(footprints, result, plate=0) < gap - tolerance:
        raise ValueError("projected-footprint placement failed final clearance validation")
    return result
