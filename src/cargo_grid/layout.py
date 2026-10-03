"""Exact rectangular layouts: full-pitch interiors and integral perimeter fill."""

from dataclasses import dataclass
from functools import lru_cache
from math import floor
from typing import Literal

from cargo_grid.parameters import (
    DEFAULT_HOLE_DIAMETER_MM,
    BuildVolume,
    Interface,
    Tile,
    positive,
)


@dataclass(frozen=True)
class PlacedTile:
    tile: Tile
    x: float
    y: float


@dataclass(frozen=True)
class Layout:
    width: float
    depth: float
    pieces: tuple[PlacedTile, ...]
    distribution: str


def _partition(
    cells: int, pitch: float, before: float, after: float, limit: float
) -> tuple[int, ...]:
    @lru_cache
    def solve(start: int) -> tuple[int, ...] | None:
        if start == cells:
            return ()
        best = None
        for length in range(1, cells - start + 1):
            end = start + length
            size = length * pitch + (before if start == 0 else 0)
            size += after if end == cells else 6
            if size > limit + 1e-6:
                break
            rest = solve(end)
            if rest is not None and (best is None or len(rest) + 1 <= len(best)):
                best = (length, *rest)
        return best

    result = solve(0)
    if result is None:
        raise ValueError(
            f"cannot fit {cells} full cells with {before:g}/{after:g} mm integrated "
            f"fill and 6 mm joining tabs into {limit:g} mm; increase usable size"
        )
    return result


def exact_layout(
    width: float,
    depth: float,
    build: BuildVolume,
    *,
    interface: Interface = Interface(),
    distribution: Literal["balanced", "positive", "negative"] = "balanced",
    hole_diameter: float | None = DEFAULT_HOLE_DIAMETER_MM,
    hole_scope: Literal["interior", "full"] = "full",
) -> Layout:
    positive("width", width)
    positive("depth", depth)
    if distribution not in ("balanced", "positive", "negative"):
        raise ValueError("filler distribution must be balanced, positive, or negative")
    p = interface.pitch
    nx, ny = floor((width + 1e-8) / p), floor((depth + 1e-8) / p)
    if min(nx, ny) < 1:
        raise ValueError(f"exact layout needs at least one {p:g} mm cell in each axis")
    if interface.body_height > build.usable[2] + 1e-6:
        raise ValueError("tile height exceeds usable Z")

    def split(residual: float) -> tuple[float, float]:
        residual = max(0.0, residual)
        if distribution == "balanced":
            return residual / 2, residual / 2
        return (0, residual) if distribution == "positive" else (residual, 0)

    west, east = split(width - nx * p)
    south, north = split(depth - ny * p)
    candidates = []
    failures = []
    for available_x, available_y in (build.usable[:2], build.usable[1::-1]):
        try:
            columns = _partition(nx, p, west, east, available_x)
            rows = _partition(ny, p, south, north, available_y)
        except ValueError as error:
            failures.append(str(error))
            continue
        pieces = []
        ix = 0
        for cx in columns:
            iy = 0
            for cy in rows:
                tile = Tile(
                    cx,
                    cy,
                    interface,
                    hole_diameter,
                    west=ix > 0,
                    east=ix + cx < nx,
                    south=iy > 0,
                    north=iy + cy < ny,
                    filler_west=west if ix == 0 else 0,
                    filler_east=east if ix + cx == nx else 0,
                    filler_south=south if iy == 0 else 0,
                    filler_north=north if iy + cy == ny else 0,
                    hole_scope=hole_scope,
                )
                size = (
                    cx * p
                    + tile.filler_west
                    + tile.filler_east
                    + (interface.male_join_depth if tile.east else 0),
                    cy * p
                    + tile.filler_south
                    + tile.filler_north
                    + (interface.male_join_depth if tile.north else 0),
                    interface.body_height,
                )
                if build.placement(size) is None:
                    break
                pieces.append(PlacedTile(tile, west + ix * p, south + iy * p))
                iy += cy
            ix += cx
        if len(pieces) == len(columns) * len(rows):
            candidates.append(pieces)
    if not candidates:
        details = "; ".join(dict.fromkeys(failures)) or "excluded regions prevent placement"
        raise ValueError(f"no supported full-cell rectangular partition: {details}")
    return Layout(width, depth, tuple(min(candidates, key=len)), distribution)
