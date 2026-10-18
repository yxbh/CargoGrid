"""Zeekr 7X expansion set built from shared CargoGrid geometry."""

from collections import Counter
from dataclasses import replace
from typing import Literal

from cargo_grid.accessories import Accessory, tile_matched_perimeter
from cargo_grid.catalogue import accessory_design
from cargo_grid.jobs import Job
from cargo_grid.packing import TowerClearance
from cargo_grid.parameters import (
    DEFAULT_HOLE_DIAMETER_MM,
    BuildVolume,
    Interface,
    positive,
)
from cargo_grid.plates import (
    AUTO_SUPPORT_TOWER_CLEARANCE_MM,
    PlateGroup,
    PlatePlan,
    plan_plates,
)
from cargo_grid.vehicles import zeekr_7x_rear_review as rear_panel

OUTWARD_MM = 40.0
NORTH_EDGE_OUTWARD_MM = 30.0
PLATE_PREFIX = "Zeekr 7X - "
STRAIGHT_LENGTHS_CELLS = (2, 3, 4, 2, 3)
RUN_PAIR_NOTE = (
    "Two butted strips cover the 300 mm five-cell run. Each strip is held by the tile joint "
    "along its length; the seam matches a tile-to-tile seam. The two end quarters and the "
    "tile complete the 10 mm hole at the seam. The butted pair has not been test-fitted."
)


def variants(
    interface: Interface = Interface(),
    *,
    hole_diameter: float | None = DEFAULT_HOLE_DIAMETER_MM,
    hole_scope: Literal["interior", "full"] = "full",
) -> list[Accessory]:
    """Required 2/3/4-cell edges, with the five-cell run supplied by a second 2+3 pair."""
    if interface.joint_style != "original":
        raise ValueError("Zeekr 7X extras require original roofed tile-edge joints")
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
        for cells in STRAIGHT_LENGTHS_CELLS
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
    gap: float = 2,
    auto_roof_support: bool = False,
    layer_height_mm: float = 0.32,
) -> Job:
    """Build the whole expansion set: 40 mm straight edges plus rear-panel contour pieces.

    Every piece is required on every build; a fit failure refuses the set.
    ``auto_roof_support`` reserves a prime tower on every plate.
    """
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
    positive("Zeekr 7X packing gap", gap, zero=True)
    envelope = placement_build or build

    edges, sizes, sources = [], {}, {}
    run_pairs = {"male": [], "female": []}
    for spec in variants(interface, hole_diameter=hole_diameter, hole_scope=hole_scope):
        key = (spec.family, spec.nx)
        if key in sources:
            source = sources[key]
            gender = "male" if spec.family == "edge-x" else "female"
            design = replace(
                source,
                name=f"{source.name}_5cell-run",
                display_name=f"5-cell run pair - {spec.nx}-cell {gender} 40mm edge",
            )
            run_pairs[gender].append(
                {"design": design.name, "source_design": source.name, "length_cells": spec.nx}
            )
        else:
            design = accessory_design(spec)
            sources[key] = design
        size = design.size
        if envelope.placement(size) is None or build.placement(size) is None:
            raise ValueError(
                f"{design.name}: actual bounds {size} exceed the configured build envelope"
            )
        edges.append(design)
        sizes[id(design)] = size

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
        result = plan_plates(
            groups,
            envelope,
            size=lambda design: sizes[id(design)],
            auto_roof_support=auto_roof_support,
            layer_height_mm=layer_height_mm,
        )
        if result.unfit:
            raise ValueError(f"{result.unfit[0].name} does not fit its plate area")
        return result

    edge_tower_clearance = TowerClearance.uniform(AUTO_SUPPORT_TOWER_CLEARANCE_MM)
    edge_groups = [
        PlateGroup(f"{PLATE_PREFIX}{title}", members, gap, tower_clearance=edge_tower_clearance)
        for title, members in (("Male 40mm edges", male), ("Female 40mm edges", female))
        if members
    ]
    combined_edges = [
        PlateGroup(f"{PLATE_PREFIX}40mm edges", edges, gap, tower_clearance=edge_tower_clearance)
    ]
    grouped = plan(combined_edges).plate_count >= plan(edge_groups).plate_count
    groups = [
        *(edge_groups if grouped else combined_edges),
        PlateGroup(f"{PLATE_PREFIX}Rear panel contour side caps", side_caps, gap, projected=True),
        PlateGroup(
            f"{PLATE_PREFIX}Rear panel south contour ramps", south_ramps, gap, projected=True
        ),
    ]
    plates = plan(groups)
    return Job(
        plates.designs,
        build,
        "extras",
        part_gap=gap,
        print_placements=plates.placements,
        plate_names=plates.plate_names,
        plate_builds=plates.plate_builds,
        prime_tower=plates.prime_tower,
        prime_tower_positions=plates.prime_tower_positions,
        prime_tower_reaches=plates.prime_tower_reaches,
        prime_tower_clearances=plates.prime_tower_clearances,
        projected_footprints=plates.projected_footprints,
        projected_footprint_clearances=plates.projected_clearances,
        placement_policy={
            "collection": "zeekr-7x",
            "minimum_model_gap_mm": gap,
            "plate_group_minimum_model_gap_mm": plates.group_gaps,
            "projected_footprint_packing": plates.projected_packing,
            "projected_footprint_gap_overrides_mm": {
                plates.plate_names[plate]: clearance
                for plate, clearance in plates.projected_clearances.items()
            },
            "grouped_by_family": True,
            "grouped_by_connector_sex": grouped,
            **(
                {"auto_roof_support": plates.auto_roof_support}
                if plates.auto_roof_support is not None
                else {}
            ),
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
                "five_cell_run_pairs": run_pairs,
                "five_cell_run_note": RUN_PAIR_NOTE,
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
