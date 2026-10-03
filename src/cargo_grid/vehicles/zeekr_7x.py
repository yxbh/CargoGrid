"""Zeekr 7X expansion set built from shared CargoGrid geometry."""

from collections import Counter
from dataclasses import replace
from math import floor
from typing import Literal

from cargo_grid.accessories import Accessory, tile_matched_perimeter
from cargo_grid.catalogue import accessory_design
from cargo_grid.jobs import Job
from cargo_grid.parameters import (
    DEFAULT_HOLE_DIAMETER_MM,
    BuildVolume,
    Interface,
    positive,
)
from cargo_grid.plates import PlateGroup, PlatePlan, plan_plates
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


def standard_rear_panel_parts(solid_bottom_mm: float = 0.0) -> list[dict]:
    """Ordinary catalogue parts that complete the rear panel; not part of this set."""
    rows = round(rear_panel.TEST_FIELD_DEPTH_MM / Interface().pitch)
    lengths = sorted(Counter(rear_panel.TEST_TILE_MODULES).items(), reverse=True)
    parts = [
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
    if solid_bottom_mm:
        for part in parts:
            part["solid_bottom_thickness_mm"] = solid_bottom_mm
    return parts


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
    if replace(interface, solid_bottom_mm=0.0) != Interface():
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

    parameters = rear_panel.RearReviewParameters(interface=interface)
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

    def plan(groups: list[PlateGroup]) -> PlatePlan:
        result = plan_plates(groups, envelope, size=lambda design: sizes[id(design)])
        if result.unfit:
            raise ValueError(f"{result.unfit[0].name} does not fit its plate area")
        return result

    edge_groups = [
        PlateGroup(f"{PLATE_PREFIX}{title}", members, edge_gap)
        for title, members in (("Male 40mm edges", male), ("Female 40mm edges", female))
        if members
    ]
    combined_edges = [PlateGroup(f"{PLATE_PREFIX}40mm edges", edges, edge_gap)]
    grouped = plan(combined_edges).plate_count >= plan(edge_groups).plate_count
    groups = [
        *(edge_groups if grouped else combined_edges),
        PlateGroup(f"{PLATE_PREFIX}Rear panel contour side caps", side_caps, contour_gap),
        PlateGroup(f"{PLATE_PREFIX}Rear panel south contour ramps", south_ramps, contour_gap),
    ]
    plates = plan(groups)
    job_gap = min(edge_gap, contour_gap)
    return Job(
        plates.designs,
        build,
        "extras",
        omitted=omitted,
        part_gap=job_gap,
        print_placements=plates.placements,
        plate_names=plates.plate_names,
        plate_builds=plates.plate_builds,
        placement_policy={
            "collection": "zeekr-7x",
            "minimum_model_gap_mm": job_gap,
            "plate_group_minimum_model_gap_mm": plates.group_gaps,
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
                "standard_parts_printed_separately": standard_rear_panel_parts(
                    interface.solid_bottom_mm
                ),
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
            **(
                {
                    "solid_bottom": {
                        "solid_bottom_mm": interface.solid_bottom_mm,
                        "body_thickness_mm": interface.body_height,
                        "note": (
                            f"Every piece has a {interface.solid_bottom_mm:g} mm closed floor "
                            "and sits that much higher than the test-fitted version; recheck "
                            "clearance under the lift-out panel. Print the matching tiles and "
                            "north edges with the same solid bottom."
                        ),
                    }
                }
                if interface.solid_bottom_mm
                else {}
            ),
        },
    )
