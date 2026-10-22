"""Bounded deterministic packing for all-height projected mesh footprints."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from itertools import islice, permutations, product
from math import ceil, floor, sqrt
from multiprocessing import get_context
from os import environ
from random import Random

import numpy as np
from scipy.signal import fftconvolve
from shapely import affinity, contains_xy, polygons, set_precision, union_all
from shapely.geometry.base import BaseGeometry

from cargo_grid.packing import PACKING_ROTATIONS, PrintPlacement, packing_score
from cargo_grid.parameters import count, positive

_PROJECTION_UNION_CHUNK = 2048
_PROJECTION_UNION_FAN_IN = 32
_PROJECTION_PRECISION_MM = 1e-12
PACKING_SEARCH_ATTEMPTS = 32
PACKING_SEARCH_SEED = 0
PACKING_SEARCH_BATCH_SIZE = 8
PACKING_WORKERS_ENV = "CARGO_GRID_PACKING_WORKERS"


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
        translate_x = 0 if abs(minimum_x) <= _PROJECTION_PRECISION_MM else -minimum_x
        translate_y = 0 if abs(minimum_y) <= _PROJECTION_PRECISION_MM else -minimum_y
        normalized = affinity.translate(self.geometry, translate_x, translate_y)
        if not normalized.is_valid:
            normalized = set_precision(normalized, _PROJECTION_PRECISION_MM)
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


_Attempt = tuple[list[int], tuple[int, ...], int | None, dict[int, tuple[int, ...]] | None]


@dataclass(frozen=True)
class _PackingContext:
    footprints: list[ProjectedFootprint]
    rasters: dict[tuple[int, int], _Raster]
    bounding_sizes: list[tuple[float, float]]
    minimum_x: float
    minimum_y: float
    width: float
    depth: float
    gap: float
    search_gap: float
    grid: float
    mesh_error: float
    tolerance: float
    max_candidate_positions: int
    max_refinements: int
    obstacles: tuple[tuple[float, float, float, float], ...]


_worker_context: _PackingContext | None = None


class _CandidateBudgetExceeded(ValueError):
    pass


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
            # Node coincident projection slivers consistently without changing the 3D mesh.
            projected.append(
                union_all(
                    set_precision(
                        polygons(selected[start : start + _PROJECTION_UNION_CHUNK]),
                        _PROJECTION_PRECISION_MM,
                    ),
                    grid_size=_PROJECTION_PRECISION_MM,
                )
            )
            if len(projected) == _PROJECTION_UNION_FAN_IN:
                projected = [union_all(projected, grid_size=_PROJECTION_PRECISION_MM)]
    if not projected:
        raise ValueError("projected mesh has no nondegenerate XY triangles")
    geometry = union_all(projected, grid_size=_PROJECTION_PRECISION_MM)
    if geometry.is_empty or not geometry.is_valid or geometry.area <= 0:
        raise ValueError("projected mesh did not form a valid positive-area footprint")
    return ProjectedFootprint(geometry)


def oriented_footprint(
    footprint: ProjectedFootprint,
    rotation: int,
) -> ProjectedFootprint:
    if rotation not in PACKING_ROTATIONS:
        raise ValueError("projected footprint rotation must be 0, 90, 180 or 270 degrees")
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
    bounding_size: tuple[float, float] | None = None,
) -> _Raster:
    oriented = oriented_footprint(footprint, rotation)
    width, depth = oriented.size
    if bounding_size is not None:
        width, depth = bounding_size if rotation % 180 == 0 else bounding_size[::-1]
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


def _blocked(
    obstacles: tuple[tuple[float, float, float, float], ...],
    x: float,
    y: float,
    width: float,
    depth: float,
) -> bool:
    """Whether a placed bounding box overlaps any obstacle rectangle (local coordinates)."""
    return any(
        x < x1 and x0 < x + width and y < y1 and y0 < y + depth for x0, y0, x1, y1 in obstacles
    )


def _clearance_key(
    pairs: dict[tuple[int, int], float],
    target: float,
) -> tuple[tuple[float, ...], tuple[float, int, int]]:
    distances = sorted((distance, first, second) for (first, second), distance in pairs.items())
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
    obstacles: tuple[tuple[float, float, float, float], ...] = (),
    bounding_sizes: list[tuple[float, float]] | None = None,
) -> dict[int, tuple[float, float, int]]:
    if len(placements) < 2:
        return placements
    oriented = [
        oriented_footprint(footprint, placements[index][2]).geometry
        for index, footprint in enumerate(footprints)
    ]
    geometries = [
        affinity.translate(geometry, placements[index][0], placements[index][1])
        for index, geometry in enumerate(oriented)
    ]
    pairs = {
        (first, second): geometry.distance(geometries[second])
        for first, geometry in enumerate(geometries)
        for second in range(first + 1, len(geometries))
    }
    for _ in range(max_refinements + 1):
        baseline, minimum = _clearance_key(pairs, target)
        if minimum[0] >= target - tolerance:
            return placements
        options = []
        for index in minimum[1:]:
            x, y, rotation = placements[index]
            for dx, dy in ((0, grid), (grid, 0), (0, -grid), (-grid, 0)):
                moved = dict(placements)
                moved[index] = (x + dx, y + dy, rotation)
                dimensions = (
                    bounding_sizes[index] if bounding_sizes is not None else footprints[index].size
                )
                w, d = dimensions if rotation % 180 == 0 else dimensions[::-1]
                minimum_x, minimum_y = x + dx, y + dy
                maximum_x, maximum_y = minimum_x + w, minimum_y + d
                if (
                    minimum_x < -tolerance
                    or minimum_y < -tolerance
                    or maximum_x > width + tolerance
                    or maximum_y > depth + tolerance
                    or _blocked(
                        obstacles,
                        minimum_x,
                        minimum_y,
                        maximum_x - minimum_x,
                        maximum_y - minimum_y,
                    )
                ):
                    continue
                geometry = affinity.translate(oriented[index], x + dx, y + dy)
                moved_pairs = dict(pairs)
                for first, second in pairs:
                    if first == index:
                        moved_pairs[first, second] = geometry.distance(geometries[second])
                    elif second == index:
                        moved_pairs[first, second] = geometries[first].distance(geometry)
                key, moved_minimum = _clearance_key(moved_pairs, target)
                if key > baseline:
                    options.append((key, moved_minimum[0], moved, index, geometry, moved_pairs))
        if not options:
            break
        best = max(options, key=lambda option: (option[0], option[1]))
        placements = best[2]
        geometries[best[3]] = best[4]
        pairs = best[5]
    raise ValueError(f"could not refine projected-footprint placement to {target:g} mm clearance")


def _packing_orders(
    footprints: list[ProjectedFootprint],
    rasters: dict[tuple[int, int], _Raster],
    max_order_attempts: int,
) -> Iterator[list[int]]:
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


def _candidate_anchor(
    occupied: np.ndarray,
    raster: _Raster,
    *,
    max_x: int,
    max_y: int,
    global_minimum: int,
    grid: float,
    obstacles: tuple[tuple[float, float, float, float], ...],
) -> tuple[int, int] | None:
    collision = fftconvolve(occupied, raster.mask[::-1, ::-1].astype(np.uint8), mode="valid")
    left = raster.minimum_x - global_minimum
    top = raster.minimum_y - global_minimum
    x0, y0 = max(0, -left), max(0, -top)
    x1 = min(max_x + 1, collision.shape[1] - left)
    y1 = min(max_y + 1, collision.shape[0] - top)
    if x1 <= x0 or y1 <= y0:
        return None
    free = collision[y0 + top : y1 + top, x0 + left : x1 + left] < 0.5
    xs = np.arange(x0, x1) * grid
    ys = np.arange(y0, y1) * grid
    for ox0, oy0, ox1, oy1 in obstacles:
        blocked_x = (xs < ox1) & (ox0 < xs + raster.width)
        blocked_y = (ys < oy1) & (oy0 < ys + raster.depth)
        free &= ~(blocked_y[:, None] & blocked_x[None, :])
    columns = np.flatnonzero(free.any(axis=0))
    if not len(columns):
        return None
    column = int(columns[0])
    return x0 + column, y0 + int(np.flatnonzero(free[:, column])[0])


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
    rotations: tuple[int, ...],
    first_rotation: int | None = None,
    rotation_orders: dict[int, tuple[int, ...]] | None = None,
    obstacles: tuple[tuple[float, float, float, float], ...] = (),
) -> dict[int, tuple[float, float, int]]:
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
        for rotation in rotation_orders[index] if rotation_orders else rotations:
            if not placements and first_rotation is not None and rotation != first_rotation:
                continue
            raster = rasters[index, rotation]
            if raster.width > width + tolerance or raster.depth > depth + tolerance:
                continue
            max_x = floor((width - raster.width + tolerance) / grid)
            max_y = floor((depth - raster.depth + tolerance) / grid)
            candidate_positions += (max_x + 1) * (max_y + 1)
            if candidate_positions > max_candidate_positions:
                raise _CandidateBudgetExceeded(
                    "projected-footprint search exceeded its candidate budget"
                )
            anchor = _candidate_anchor(
                occupied,
                raster,
                max_x=max_x,
                max_y=max_y,
                global_minimum=global_minimum,
                grid=grid,
                obstacles=obstacles,
            )
            if anchor is not None:
                x, y = anchor
                options.append(
                    (x * grid + raster.width, y * grid + raster.depth, x, y, rotation, raster)
                )
                if rotation_orders:
                    break
        if not options:
            continue
        _, _, x, y, rotation, raster = min(options)
        top = y + raster.minimum_y - global_minimum
        left = x + raster.minimum_x - global_minimum
        raster_height, raster_width = raster.mask.shape
        occupied[top : top + raster_height, left : left + raster_width] |= raster.mask
        placements[index] = (x * grid, y * grid, rotation)
    return placements


def _packing_attempts(
    footprints: list[ProjectedFootprint],
    rasters: dict[tuple[int, int], _Raster],
    limit: int,
) -> Iterator[_Attempt]:
    orders = list(_packing_orders(footprints, rasters, 8))
    initial = [(order, (0, 90), None, None) for order in orders] + [
        (order, PACKING_ROTATIONS, rotation, None)
        for order in orders[:4]
        for rotation in PACKING_ROTATIONS
    ]
    yield from initial[:limit]
    random = Random(PACKING_SEARCH_SEED)
    for iteration in range(len(initial), limit):
        order = list(orders[iteration % len(orders)])
        random.shuffle(order)
        rotation_orders = {
            index: tuple(random.sample(PACKING_ROTATIONS, len(PACKING_ROTATIONS)))
            for index in order
        }
        yield order, PACKING_ROTATIONS, None, rotation_orders


def _pack_attempt(
    context: _PackingContext, attempt: _Attempt
) -> tuple[list[PrintPlacement] | None, bool]:
    order, rotations, first_rotation, rotation_orders = attempt
    remaining = list(order)
    result: dict[int, PrintPlacement] = {}
    plate = 0
    while remaining:
        try:
            candidate = _pack_order(
                context.rasters,
                remaining,
                width=context.width,
                depth=context.depth,
                grid=context.grid,
                search_gap=context.search_gap,
                mesh_error=context.mesh_error,
                tolerance=context.tolerance,
                max_candidate_positions=context.max_candidate_positions,
                rotations=rotations,
                first_rotation=first_rotation,
                rotation_orders=rotation_orders,
                obstacles=context.obstacles,
            )
        except _CandidateBudgetExceeded:
            return None, True
        if not candidate:
            return None, False
        indices = sorted(candidate)
        try:
            refined = _refine_clearance(
                [context.footprints[index] for index in indices],
                {local: candidate[index] for local, index in enumerate(indices)},
                width=context.width,
                depth=context.depth,
                target=context.gap,
                grid=context.grid,
                tolerance=context.tolerance,
                max_refinements=context.max_refinements,
                obstacles=context.obstacles,
                bounding_sizes=[context.bounding_sizes[index] for index in indices],
            )
        except ValueError:
            return None, False
        for local, index in enumerate(indices):
            x, y, rotation = refined[local]
            result[index] = PrintPlacement(
                plate, context.minimum_x + x, context.minimum_y + y, rotation
            )
        remaining = [index for index in remaining if index not in candidate]
        plate += 1
    return [result[index] for index in range(len(context.footprints))], False


def _restart_attempts(
    order: list[int], layout: list[PrintPlacement], limit: int, random: Random
) -> list[_Attempt]:
    attempts = []
    for iteration in range(limit):
        changed = list(order)
        if iteration % 8 == 0:
            random.shuffle(changed)
        elif len(changed) > 1:
            first, second = random.sample(range(len(changed)), 2)
            if iteration % 2:
                changed[first], changed[second] = changed[second], changed[first]
            else:
                changed.insert(second, changed.pop(first))
        preferences = {
            index: (placement.rotation,)
            + tuple(r for r in PACKING_ROTATIONS if r != placement.rotation)
            for index, placement in enumerate(layout)
        }
        if iteration % 3 == 0:
            index = random.randrange(len(layout))
            rotation = random.choice(PACKING_ROTATIONS)
            preferences[index] = (rotation,) + tuple(r for r in PACKING_ROTATIONS if r != rotation)
        attempts.append((changed, PACKING_ROTATIONS, None, preferences))
    return attempts


def _initialize_worker(context: _PackingContext) -> None:
    global _worker_context
    _worker_context = context


def _worker_attempt(attempt: _Attempt) -> tuple[list[PrintPlacement] | None, bool]:
    if _worker_context is None:
        raise RuntimeError("projected-footprint worker was not initialized")
    return _pack_attempt(_worker_context, attempt)


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
    max_layout_attempts: int = PACKING_SEARCH_ATTEMPTS,
    obstacles: tuple[tuple[float, float, float, float], ...] = (),
    workers: int | None = None,
    bounding_sizes: list[tuple[float, float]] | None = None,
) -> list[PrintPlacement]:
    """Fill successive plates by conservative raster search and exact footprint distance.

    ``obstacles`` are (x0, y0, x1, y1) plate rectangles that no footprint bounding box may
    overlap, matching how rectangle packing treats build exclusions. Each plate attempts
    every remaining footprint before carrying unplaced pieces to the next plate.
    """

    if not footprints:
        return []
    positive("projected footprint gap", gap)
    positive("projected footprint search gap", search_gap)
    positive("projected footprint grid", grid)
    positive("projected footprint mesh error", mesh_error, zero=True)
    positive("projected footprint tolerance", tolerance)
    count("projected footprint candidate budget", max_candidate_positions)
    count("projected footprint refinement limit", max_refinements)
    count("projected footprint layout attempts", max_layout_attempts)
    if workers is None:
        workers = int(environ.get(PACKING_WORKERS_ENV, "1"))
    count("projected footprint workers", workers)
    if search_gap > gap:
        raise ValueError("projected footprint search gap cannot exceed the required gap")
    minimum_x, minimum_y, maximum_x, maximum_y = usable_bounds
    width, depth = maximum_x - minimum_x, maximum_y - minimum_y
    positive("projected footprint region width", width)
    positive("projected footprint region depth", depth)
    if bounding_sizes is None:
        bounding_sizes = [footprint.size for footprint in footprints]
    if len(bounding_sizes) != len(footprints):
        raise ValueError("projected footprint bounds must match the footprints")
    for footprint, (w, d) in zip(footprints, bounding_sizes):
        positive("projected footprint bounding width", w)
        positive("projected footprint bounding depth", d)
        if w < footprint.size[0] - tolerance or d < footprint.size[1] - tolerance:
            raise ValueError("projected footprint bounds must contain the mesh")
        if not (
            (w <= width + tolerance and d <= depth + tolerance)
            or (d <= width + tolerance and w <= depth + tolerance)
        ):
            raise ValueError("could not fit projected footprint inside the plate bounds")
    local_obstacles = tuple(
        (x0 - minimum_x, y0 - minimum_y, x1 - minimum_x, y1 - minimum_y)
        for x0, y0, x1, y1 in obstacles
    )
    rasters = {
        (index, rotation): _rasterize(
            footprint,
            search_gap=search_gap,
            grid=grid,
            mesh_error=mesh_error,
            rotation=rotation,
            bounding_size=bounding_sizes[index],
        )
        for index, footprint in enumerate(footprints)
        for rotation in PACKING_ROTATIONS
    }

    context = _PackingContext(
        footprints,
        rasters,
        bounding_sizes,
        minimum_x,
        minimum_y,
        width,
        depth,
        gap,
        search_gap,
        grid,
        mesh_error,
        tolerance,
        max_candidate_positions,
        max_refinements,
        local_obstacles,
    )
    sizes = [(*size, 0.0) for size in bounding_sizes]
    protected_gap = max(0.0, gap - tolerance)
    lower_bound = max(
        1,
        ceil(
            sum(f.geometry.buffer(protected_gap / 2).area for f in footprints)
            / ((width + protected_gap) * (depth + protected_gap))
        ),
    )
    attempts: list[_Attempt] = []
    initial_attempts = _packing_attempts(footprints, rasters, max_layout_attempts)
    results: list[tuple[list[PrintPlacement] | None, bool]] = []
    candidates: list[tuple[tuple[int, float], int, list[PrintPlacement]]] = []
    current: tuple[tuple[int, float], int, list[PrintPlacement]] | None = None
    random = Random(PACKING_SEARCH_SEED)
    reached_lower_bound = False
    executor = (
        ProcessPoolExecutor(
            min(workers, max_layout_attempts, PACKING_SEARCH_BATCH_SIZE),
            mp_context=get_context("spawn"),
            initializer=_initialize_worker,
            initargs=(context,),
        )
        if workers > 1
        else nullcontext()
    )
    with executor as pool:
        for offset in range(0, max_layout_attempts, PACKING_SEARCH_BATCH_SIZE):
            limit = min(PACKING_SEARCH_BATCH_SIZE, max_layout_attempts - offset)
            if offset < PACKING_SEARCH_ATTEMPTS or not candidates:
                batch = list(islice(initial_attempts, limit))
            else:
                parent = (
                    min(candidates, key=lambda candidate: candidate[:2])
                    if offset % PACKING_SEARCH_ATTEMPTS == 0
                    else current
                )
                _, index, layout = parent
                batch = _restart_attempts(attempts[index][0], layout, limit, random)
            attempts.extend(batch)
            batch_results = (
                list(pool.map(_worker_attempt, batch))
                if pool is not None
                else [_pack_attempt(context, attempt) for attempt in batch]
            )
            results.extend(batch_results)
            for index, (layout, _) in enumerate(batch_results):
                if layout is None:
                    continue
                candidate = (packing_score(layout, sizes), offset + index, layout)
                candidates.append(candidate)
                if current is None or candidate[0] <= current[0] or random.random() < 0.03:
                    current = candidate
            if candidates and min(candidate[0][0] for candidate in candidates) == lower_bound:
                if reached_lower_bound:
                    break
                reached_lower_bound = True
    if not candidates:
        if any(over_budget for _, over_budget in results):
            raise _CandidateBudgetExceeded(
                "projected-footprint search exceeded its candidate budget"
            )
        raise ValueError("could not fit projected footprints with the required clearance")
    _, _, result = min(candidates, key=lambda candidate: candidate[:2])
    for plate in range(max(p.plate for p in result) + 1):
        if sum(p.plate == plate for p in result) > 1 and (
            minimum_projected_clearance(footprints, result, plate=plate) < gap - tolerance
        ):
            raise ValueError("projected-footprint placement failed final clearance validation")
    for index, placement in enumerate(result):
        dimensions = bounding_sizes[index]
        w, d = dimensions if placement.rotation % 180 == 0 else dimensions[::-1]
        if (
            placement.x < minimum_x - tolerance
            or placement.y < minimum_y - tolerance
            or placement.x + w > maximum_x + tolerance
            or placement.y + d > maximum_y + tolerance
        ):
            raise ValueError("projected-footprint placement exceeds its plate bounds")
        if _blocked(
            obstacles,
            placement.x,
            placement.y,
            w,
            d,
        ):
            raise ValueError("projected-footprint placement overlaps a reserved area")
    return result
