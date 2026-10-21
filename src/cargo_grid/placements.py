"""Check measured placements independently of CAD construction and file writing."""

from math import isfinite

from cargo_grid.footprints import ProjectedFootprint, placed_footprint
from cargo_grid.packing import PACKING_ROTATIONS, PrintPlacement
from cargo_grid.parameters import BuildVolume, positive


def _explicit_placements(
    placements: list[PrintPlacement],
    sizes: list[tuple[float, float, float]],
    build,
    gap: float,
    projected_footprints: list[ProjectedFootprint | None] | None = None,
    projected_clearances: dict[int, float] | None = None,
    plate_builds: dict[int, BuildVolume] | None = None,
) -> list[PrintPlacement]:
    if len(placements) != len(sizes):
        raise ValueError("explicit print placements must match packed batches")
    projected_clearances = projected_clearances or {}
    plate_builds = plate_builds or {}
    placement_plates = {
        placement.plate for placement in placements if isinstance(placement, PrintPlacement)
    }
    if set(plate_builds) - placement_plates:
        raise ValueError("plate_builds entry has no placed batch")
    occupied = {}
    projected = {}
    for index, (placement, size) in enumerate(zip(placements, sizes)):
        if not isinstance(placement, PrintPlacement):
            raise ValueError("explicit placements must be PrintPlacement instances")
        if (
            isinstance(placement.plate, bool)
            or not isinstance(placement.plate, int)
            or placement.plate < 0
            or isinstance(placement.rotation, bool)
            or not isinstance(placement.rotation, int)
            or placement.rotation not in PACKING_ROTATIONS
        ):
            raise ValueError(
                "explicit placements require nonnegative plates and 0/90/180/270 rotations"
            )
        if not isfinite(placement.x) or not isfinite(placement.y):
            raise ValueError("explicit placement coordinates must be finite")
        for dimension in size:
            positive("part dimension", dimension)
        width, depth = size[:2] if placement.rotation % 180 == 0 else size[1::-1]
        if not build.contains_box(placement.x, placement.y, width, depth, size[2]):
            raise ValueError(f"explicit placement {index} exceeds the build envelope")
        plate_build = plate_builds.get(placement.plate)
        if plate_build is not None and not plate_build.contains_box(
            placement.x, placement.y, width, depth, size[2]
        ):
            raise ValueError(f"explicit placement {index} exceeds its plate build envelope")
        rectangles = occupied.setdefault(placement.plate, [])
        if placement.plate in projected_clearances:
            if projected_footprints is None or projected_footprints[index] is None:
                raise ValueError(f"explicit placement {index} lacks its projected footprint")
            geometry = placed_footprint(projected_footprints[index], placement)
            minimum_x, minimum_y, maximum_x, maximum_y = geometry.bounds
            footprint_width = maximum_x - minimum_x
            footprint_depth = maximum_y - minimum_y
            if not build.contains_box(
                placement.x,
                placement.y,
                footprint_width,
                footprint_depth,
                size[2],
            ):
                raise ValueError(
                    f"explicit placement {index} projected footprint exceeds the build envelope"
                )
            if plate_build is not None and not plate_build.contains_box(
                placement.x,
                placement.y,
                footprint_width,
                footprint_depth,
                size[2],
            ):
                raise ValueError(
                    f"explicit placement {index} projected footprint exceeds its plate build envelope"
                )
            clearance = projected_clearances[placement.plate]
            plate_footprints = projected.setdefault(placement.plate, [])
            if any(geometry.distance(other) < clearance - 1e-6 for other in plate_footprints):
                raise ValueError(
                    f"explicit placement {index} violates projected-footprint clearance"
                )
            plate_footprints.append(geometry)
        elif any(
            placement.x < x + w + gap - 1e-6
            and placement.x + width + gap > x + 1e-6
            and placement.y < y + d + gap - 1e-6
            and placement.y + depth + gap > y + 1e-6
            for x, y, w, d in rectangles
        ):
            raise ValueError(f"explicit placement {index} overlaps another packed part")
        rectangles.append((placement.x, placement.y, width, depth))
    return placements
