"""Zeekr 7X expansion set built from shared CargoGrid geometry."""

from collections import Counter
from math import floor
from typing import Literal

from cargo_grid.accessories import Accessory, tile_matched_perimeter
from cargo_grid.catalogue import accessory_design
from cargo_grid.jobs import Design, Job
from cargo_grid.packing import PrintPlacement, pack_sizes
from cargo_grid.parameters import (
    DEFAULT_HOLE_DIAMETER_MM,
    BuildVolume,
    Interface,
    positive,
)
from cargo_grid.vehicles import zeekr_7x_rear_review as rear_panel

OUTWARD_MM = 40.0
CONTOUR_GAP_MM = rear_panel.H2D_REVIEW_GAP_MM
NORTH_EDGE_OUTWARD_MM = 30.0
PLATE_PREFIX = "Zeekr 7X - "


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


def standard_rear_panel_parts() -> list[dict]:
    """Ordinary catalogue parts that complete the rear panel; not part of this set."""
    rows = round(rear_panel.TEST_FIELD_DEPTH_MM / Interface().pitch)
    lengths = sorted(Counter(rear_panel.TEST_TILE_MODULES).items(), reverse=True)
    return [
        *(
            {"family": "tile", "width_cells": cells, "depth_cells": rows, "quantity": quantity}
            for cells, quantity in lengths
        ),
        *(
            {
                "family": "edge-y",
                "length_cells": cells,
                "edge_outward_mm": NORTH_EDGE_OUTWARD_MM,
                "quantity": quantity,
            }
            for cells, quantity in lengths
        ),
    ]


def _plates(
    groups: list[tuple[str, list[Design], float]],
    sizes: dict[int, tuple[float, float, float]],
    envelope: BuildVolume,
) -> tuple[list[Design], list[PrintPlacement], dict[int, str]]:
    designs, placements, names = [], [], {}
    for title, members, gap in groups:
        packed = pack_sizes([sizes[id(design)] for design in members], envelope, gap=gap)
        count = max(placement.plate for placement in packed) + 1
        offset = len(names)
        for plate in range(count):
            label = f"{PLATE_PREFIX}{title}"
            names[offset + plate] = label if count == 1 else f"{label} {plate + 1}"
        designs.extend(members)
        placements.extend(
            PrintPlacement(placement.plate + offset, placement.x, placement.y, placement.rotation)
            for placement in packed
        )
    return designs, placements, names


def extras_job(
    build: BuildVolume,
    *,
    interface: Interface = Interface(),
    hole_diameter: float | None = DEFAULT_HOLE_DIAMETER_MM,
    hole_scope: Literal["interior", "full"] = "full",
    placement_build: BuildVolume | None = None,
    edge_gap: float = 2,
    contour_gap: float = CONTOUR_GAP_MM,
) -> Job:
    """Build the whole expansion set: 40 mm straight edges plus rear-panel contour pieces."""
    if interface.joint_style != "original":
        raise ValueError("Zeekr 7X extras require original roofed tile-edge joints")
    if interface != Interface():
        raise ValueError(
            "the Zeekr 7X expansion set requires the standard 60 mm unit, "
            "13 mm tile thickness and zero fit offset"
        )
    if hole_diameter != DEFAULT_HOLE_DIAMETER_MM or hole_scope != "full":
        raise ValueError(
            "the Zeekr 7X expansion set requires the standard full-scope 10 mm hole pattern; "
            "make other 40 mm straight-edge hole modes as single edge-x/edge-y parts"
        )
    positive("Zeekr 7X edge packing gap", edge_gap, zero=True)
    positive("Zeekr 7X contour packing gap", contour_gap)
    envelope = placement_build or build

    edges, sizes, omitted = [], {}, []
    for spec in variants(envelope, interface, hole_diameter=hole_diameter, hole_scope=hole_scope):
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
            edges.append(design)
            sizes[id(design)] = size
    if not edges:
        raise ValueError("no supported designs fit the configured build envelope")

    parameters = rear_panel.RearReviewParameters()
    side_caps, south_ramps = rear_panel.rear_panel_designs(parameters)
    for design in side_caps + south_ramps:
        size = design.size
        if envelope.placement(size) is None or build.placement(size) is None:
            raise ValueError(
                f"{design.name}: actual bounds {size} exceed the configured build envelope"
            )
        sizes[id(design)] = size

    male = [design for design in edges if design.parameters["family"] == "edge-x"]
    female = [design for design in edges if design.parameters["family"] == "edge-y"]
    edge_groups = [
        (title, members, edge_gap)
        for title, members in (("Male 40mm edges", male), ("Female 40mm edges", female))
        if members
    ]
    _, grouped_placements, _ = _plates(edge_groups, sizes, envelope)
    _, combined_placements, _ = _plates([("40mm edges", edges, edge_gap)], sizes, envelope)
    grouped = max(p.plate for p in combined_placements) >= max(p.plate for p in grouped_placements)
    groups = [
        *(edge_groups if grouped else [("40mm edges", edges, edge_gap)]),
        ("Rear panel contour side caps", side_caps, contour_gap),
        ("Rear panel south contour ramps", south_ramps, contour_gap),
    ]
    designs, placements, plate_names = _plates(groups, sizes, envelope)
    job_gap = min(edge_gap, contour_gap)
    return Job(
        designs,
        build,
        "extras",
        omitted=omitted,
        part_gap=job_gap,
        print_placements=placements,
        plate_names=plate_names,
        placement_policy={
            "collection": "zeekr-7x",
            "minimum_model_gap_mm": job_gap,
            "plate_group_minimum_model_gap_mm": {
                f"{PLATE_PREFIX}{title}": gap for title, _, gap in groups
            },
            "grouped_by_family": True,
            "grouped_by_connector_sex": grouped,
            "outward_body_width_mm": OUTWARD_MM,
            "outline_parameters_mm": {
                "pen_offset": parameters.pen_offset_mm,
                "east_edge_x": parameters.east_edge_x_mm,
                "centre_depth": parameters.centre_depth_mm,
                "traced_corner_radius": parameters.traced_north_corner_radius_mm,
                "true_corner_radius": parameters.north_corner_radius_mm,
            },
        },
        manifest_metadata={
            "scope": (
                "Zeekr 7X expansion set: 40 mm straight edges and measured rear "
                "lift-out-panel contour pieces"
            ),
            "inventory": {
                "straight_40mm_male_edges": len(male),
                "straight_40mm_female_edges": len(female),
                "rear_panel_side_caps": len(side_caps),
                "rear_panel_south_contour_ramps": len(south_ramps),
                "standard_tiles_and_north_edges_included": False,
            },
            "straight_edges": {
                "outward_body_width_mm": OUTWARD_MM,
                "lengths_cells": sorted({design.parameters["nx"] for design in edges}),
                "male": "edge-x; joins a tile's female south or west edge",
                "female": "edge-y; joins a tile's male north or east edge",
                "note": (
                    "Generic straight strips, not a measured vehicle outline. There are no "
                    "40 mm corners, and an open strip end still needs its neighbouring "
                    "corner quarter."
                ),
            },
            "assembly": {
                "tile_modules_west_to_east_cells": rear_panel.TEST_TILE_MODULES,
                "tile_field_cells": (18, 4),
                "tile_male_directions": "NORTH and EAST",
                "west_cap": "male",
                "east_cap": "female",
                "south_ramps": "male",
                "north_edges": "standard 30 mm female edge-y strips",
                "standard_parts_printed_separately": standard_rear_panel_parts(),
                "outline_footprint_mm": (
                    2 * parameters.east_edge_x_mm,
                    parameters.centre_depth_mm,
                ),
            },
            "outline": {
                "source": (
                    "tape measurements and pen traces corrected by the measured "
                    "5 mm pen-barrel offset"
                ),
                "pen_offset_mm": parameters.pen_offset_mm,
                "east_edge_x_mm": parameters.east_edge_x_mm,
                "centre_depth_mm": parameters.centre_depth_mm,
                "traced_north_corner_radius_mm": parameters.traced_north_corner_radius_mm,
                "true_north_corner_radius_mm": parameters.north_corner_radius_mm,
                "physical_test": (
                    "one user test fit of the corrected outline was judged good enough for now"
                ),
            },
            "limitations": (
                "No general vehicle-fit, strength, flatness or service guarantee. "
                "Print the documented standard tiles and 30 mm north edges separately."
            ),
        },
    )
