"""Zeekr 7X extras recipes built from shared CargoGrid geometry."""

from math import floor
from typing import Literal

from cargo_grid.accessories import Accessory, tile_matched_perimeter
from cargo_grid.catalogue import accessory_design
from cargo_grid.jobs import Job
from cargo_grid.packing import PrintPlacement, pack_sizes
from cargo_grid.parameters import DEFAULT_HOLE_DIAMETER_MM, BuildVolume, Interface

OUTWARD_MM = 40.0


def variants(
    build: BuildVolume,
    interface: Interface = Interface(),
    *,
    hole_diameter: float | None = DEFAULT_HOLE_DIAMETER_MM,
    hole_scope: Literal["interior", "full"] = "full",
) -> list[Accessory]:
    """Candidate whole-cell straight edges; the job checks their actual bounds."""
    if interface.joint_style != "original":
        raise ValueError("Zeekr 7X extras require original roofed tile-edge joints")
    maximum = max(1, floor(max(build.usable[:2]) / interface.pitch))
    return [
        tile_matched_perimeter(
            family,
            nx=cells,
            outward=OUTWARD_MM,
            interface=interface,
            hole_diameter=hole_diameter,
            hole_scope=hole_scope,
        )
        for family in ("edge-x", "edge-y")
        for cells in range(1, maximum + 1)
    ]


def extras_job(
    build: BuildVolume,
    *,
    interface: Interface = Interface(),
    hole_diameter: float | None = DEFAULT_HOLE_DIAMETER_MM,
    hole_scope: Literal["interior", "full"] = "full",
    placement_build: BuildVolume | None = None,
    part_gap: float = 2,
) -> Job:
    """Build only this recipe, using a caller-supplied printer/packing envelope."""
    envelope = placement_build or build
    designs, sizes, omitted = [], [], []
    for spec in variants(
        envelope,
        interface,
        hole_diameter=hole_diameter,
        hole_scope=hole_scope,
    ):
        design = accessory_design(spec)
        size = design.size
        if envelope.placement(size) is None or build.placement(size) is None:
            omitted.append(
                {
                    "name": design.name,
                    "parameters": design.parameters,
                    "size_mm": size,
                    "reason": "actual bounds exceed usable envelope",
                }
            )
        else:
            designs.append(design)
            sizes.append(size)
    if not designs:
        raise ValueError("no supported designs fit the configured build envelope")

    placements, plate_names = [], {}
    offset = 0
    for family, sex in (("edge-x", "Male"), ("edge-y", "Female")):
        members = [i for i, design in enumerate(designs) if design.parameters["family"] == family]
        if not members:
            continue
        packed = pack_sizes([sizes[i] for i in members], envelope, gap=part_gap)
        count = max(p.plate for p in packed) + 1
        for plate in range(count):
            title = f"Zeekr 7X - {sex} 40mm edges"
            plate_names[offset + plate] = title if count == 1 else f"{title} {plate + 1}"
        placements.extend(PrintPlacement(p.plate + offset, p.x, p.y, p.rotation) for p in packed)
        offset += count
    combined = pack_sizes(sizes, envelope, gap=part_gap)
    combined_count = max(p.plate for p in combined) + 1
    grouped = combined_count >= offset
    if not grouped:
        placements = combined
        plate_names = {
            plate: f"Zeekr 7X - 40mm edges {plate + 1}" for plate in range(combined_count)
        }
    return Job(
        designs,
        build,
        "extras",
        omitted=omitted,
        part_gap=part_gap,
        print_placements=placements,
        plate_names=plate_names,
        placement_policy={
            "collection": "zeekr-7x",
            "minimum_model_gap_mm": part_gap,
            "grouped_by_connector_sex": grouped,
            "outward_body_width_mm": OUTWARD_MM,
        },
    )
