"""Finite functional catalogue bounded by the user's usable print envelope."""

import json
from dataclasses import asdict
from hashlib import sha256
from math import floor
from typing import Literal

from cargo_grid.accessories import (
    EDGE_OUTWARD_OPTIONS_MM,
    VERTICAL_BRACKET_CONFIGS,
    VERTICAL_STOP_CELLS,
    VERTICAL_STOP_HEIGHTS_MM,
    Accessory,
    accessory_datums,
    bambu_print_rotation,
    bambu_print_rotation_y,
    edge_hole_completion_supported,
    make_accessory,
    required_bambu_object_settings,
)
from cargo_grid.footprints import pack_projected_footprints, projected_mesh_footprint
from cargo_grid.jobs import Design, Job, tile_design
from cargo_grid.meshes import checked_mesh
from cargo_grid.packing import PrintPlacement, pack_sizes
from cargo_grid.parameters import (
    DEFAULT_HOLE_DIAMETER_MM,
    BuildVolume,
    Exclusion,
    Interface,
    Tile,
    positive,
)
from cargo_grid.rods import BRACE_SPACINGS_MM, ROD_HEIGHTS_MM, Rod, RodBrace

H2D_DEFAULT_PART_CLEARANCE_MM = 4.0
H2D_FOOTPRINT_SEARCH_ALLOWANCE_MM = 0.75

BRACKET_DISPLAY_NAMES = {
    (1, 2, 2): "Deep tall tile bracket — floor 1x2, wall 1x2",
    (2, 1, 1): "Wide low tile bracket — floor 2x1, wall 2x1",
    (2, 2, 2): "Deep square tile bracket — floor 2x2, wall 2x2",
    (1, 1, 2): "Shallow tall tile bracket — floor 1x1, wall 1x2",
    (2, 1, 2): "Shallow wide tile bracket — floor 2x1, wall 2x2",
}


def tile_sizes(build: BuildVolume, interface: Interface = Interface()) -> list[tuple[int, int]]:
    longest = max(build.usable[:2])
    maximum = max(0, floor((longest - 6 + 1e-6) / interface.pitch))
    return [
        (x, y)
        for x in range(1, maximum + 1)
        for y in range(1, maximum + 1)
        if build.placement(
            (
                x * interface.pitch + interface.male_join_depth,
                y * interface.pitch + interface.male_join_depth,
                interface.height,
            )
        )
        is not None
    ]


def accessory_variants(
    build: BuildVolume,
    interface: Interface = Interface(),
    *,
    hole_diameter: float | None = DEFAULT_HOLE_DIAMETER_MM,
    hole_scope: Literal["interior", "full"] = "full",
) -> list[Accessory | Rod | RodBrace]:
    nmax = max(1, floor(max(build.usable[:2]) / interface.pitch))
    result = []

    def perimeter_spec(
        family: str,
        *,
        nx: int = 1,
        variant: int = 1,
        outward: float,
    ) -> Accessory:
        complete = (
            hole_diameter is not None
            and hole_scope == "full"
            and edge_hole_completion_supported(
                family,
                nx,
                variant,
                interface,
                hole_diameter,
            )
        )
        return Accessory(
            family,
            nx=nx,
            variant=variant,
            interface=interface,
            edge_outward=outward,
            complete_edge_holes=complete,
            edge_hole_diameter=hole_diameter if complete else None,
        )

    for n in range(1, nmax + 1):
        result.extend(
            perimeter_spec(
                family,
                nx=n,
                outward=outward,
            )
            for family in ("edge-x", "edge-y")
            for outward in EDGE_OUTWARD_OPTIONS_MM
        )
        result.append(Accessory("support", nx=n, interface=interface))
    result.extend(
        perimeter_spec(
            family,
            variant=variant,
            outward=outward,
        )
        for family, variants in (("corner-in", range(1, 5)), ("corner-out", range(1, 7)))
        for variant in variants
        for outward in EDGE_OUTWARD_OPTIONS_MM
    )
    result.extend(Accessory("support-end", variant=v, interface=interface) for v in range(1, 5))
    result.extend(
        Accessory("support-bit", length=length, interface=interface) for length in (20, 30, 40, 50)
    )
    result.extend(
        Accessory(
            "vertical-tile-bracket",
            nx=x,
            ny=base_y,
            interface=interface,
            panel_height_cells=panel_z if panel_z != base_y else None,
        )
        for x, base_y, panel_z in VERTICAL_BRACKET_CONFIGS
    )
    if interface.joint_style == "original":
        result.extend(
            Accessory("ramp", nx=n, interface=interface, ramp_join=join)
            for join in ("female", "male")
            for n in range(1, nmax + 1)
        )
    result.extend(
        Accessory("vertical-stop", nx=x, ny=y, height=height, interface=interface)
        for x, y in VERTICAL_STOP_CELLS
        for height in VERTICAL_STOP_HEIGHTS_MM
    )
    result.extend(
        Accessory("lock-45", nx=x, ny=y, interface=interface)
        for x, y in ((1, 1), (2, 2))
        if 50 <= y * interface.pitch and (x == 1 or interface.pitch <= 60)
    )
    result.extend(
        Accessory("plate", nx=x, ny=y, interface=interface) for x, y in ((1, 1), (1, 2), (2, 2))
    )
    result.extend(Rod(h, tile_thickness_mm=interface.height) for h in ROD_HEIGHTS_MM)
    result.extend(RodBrace(spacing) for spacing in BRACE_SPACINGS_MM)
    return result


def accessory_design(spec: Accessory | Rod | RodBrace) -> Design:
    if isinstance(spec, (Rod, RodBrace)):
        parameters = {"family": spec.family, **asdict(spec)}
        token = sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:10]
        shape = make_accessory(spec)
        shape.label = f"{shape.label}_{token}"
        display = (
            f"Rod - {spec.above_mat_height_mm:g} mm above mat - {spec.peg_diameter_mm:g} mm peg"
            if isinstance(spec, Rod)
            else f"Upper rod brace - {spec.center_spacing_mm:g} mm centres - {spec.bore_diameter_mm:.1f} mm bores"
        )
        return Design(
            shape.label,
            shape,
            parameters,
            display_name=display,
            recommended_print_rotation_y=bambu_print_rotation_y(spec),
            apply_orientation_to_bambu=isinstance(spec, Rod),
            bambu_object_settings=dict(required_bambu_object_settings(parameters)),
        )
    parameters = asdict(spec)
    if spec.family != "ramp" or spec.ramp_join == "female":
        del parameters["ramp_join"]
    if parameters["panel_height_cells"] is None:
        del parameters["panel_height_cells"]
    if parameters["edge_outward"] == 10.0:
        del parameters["edge_outward"]
    if not parameters["complete_edge_holes"]:
        del parameters["complete_edge_holes"]
    if parameters["edge_hole_diameter"] in (None, DEFAULT_HOLE_DIAMETER_MM):
        del parameters["edge_hole_diameter"]
    token = sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:10]
    dimensions = (
        f"{spec.nx}x{spec.ny}_h{spec.height:g}"
        if spec.family == "vertical-stop"
        else f"base{spec.nx}x{spec.ny}_wall{spec.nx}x{spec.panel_height_cells}"
        if spec.family == "vertical-tile-bracket" and spec.panel_height_cells is not None
        else f"{spec.nx}x{spec.ny}"
    )
    edge_suffix = f"_out{spec.edge_outward:g}mm" if spec.edge_outward != 10 else ""
    if spec.complete_edge_holes:
        edge_suffix += (
            "_complete-holes"
            if spec.edge_hole_diameter == DEFAULT_HOLE_DIAMETER_MM
            else f"_complete-{spec.edge_hole_diameter:g}mm-holes"
        )
    join_suffix = "_male" if spec.family == "ramp" and spec.ramp_join == "male" else ""
    name = (
        f"{spec.family}_{dimensions}{join_suffix}_v{spec.variant}{edge_suffix}_"
        f"{spec.interface.joint_style}_{token}"
    )
    shape = make_accessory(spec)
    shape.label = name
    rotation = bambu_print_rotation(spec)
    rotation_y = bambu_print_rotation_y(spec)
    datums = accessory_datums(spec)
    return Design(
        name,
        shape,
        parameters,
        display_name=BRACKET_DISPLAY_NAMES.get(
            (spec.nx, spec.ny, spec.panel_height_cells or spec.ny)
        )
        if spec.family == "vertical-tile-bracket"
        else None,
        recommended_print_rotation_x=rotation,
        recommended_print_rotation_y=rotation_y,
        apply_orientation_to_bambu=rotation is not None or rotation_y is not None,
        bambu_object_settings=dict(required_bambu_object_settings(parameters)),
        holes=[
            {
                "x": x,
                "y": y,
                "accepted": True,
                "reason": "matching accepted full-pattern tile boundary site",
            }
            for x, y in datums.get("edge_hole_centers", [])
        ],
    )


def catalogue_job(
    build: BuildVolume,
    *,
    interface: Interface = Interface(),
    hole_diameter: float | None = DEFAULT_HOLE_DIAMETER_MM,
    hole_scope: Literal["interior", "full"] = "full",
    orient_for_bambu: bool = False,
) -> Job:
    job, _ = _catalogue_job_with_sizes(
        build,
        interface=interface,
        hole_diameter=hole_diameter,
        hole_scope=hole_scope,
        orient_for_bambu=orient_for_bambu,
    )
    return job


def _catalogue_job_with_sizes(
    build: BuildVolume,
    *,
    interface: Interface,
    hole_diameter: float | None,
    hole_scope: Literal["interior", "full"],
    orient_for_bambu: bool,
) -> tuple[Job, dict[int, tuple[float, float, float]]]:
    designs = [
        tile_design(Tile(x, y, interface, hole_diameter, hole_scope=hole_scope))
        for x, y in tile_sizes(build, interface)
    ]
    # Retained designs keep these identities alive until this request finishes packing.
    sizes = {
        id(design): design.bambu_size if orient_for_bambu else design.size for design in designs
    }
    omitted = []
    for spec in accessory_variants(
        build,
        interface,
        hole_diameter=hole_diameter,
        hole_scope=hole_scope,
    ):
        design = accessory_design(spec)
        size = design.bambu_size if orient_for_bambu else design.size
        if build.placement(size) is None:
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
            sizes[id(design)] = size
    for design in designs:
        if build.placement(sizes[id(design)]) is None:
            raise ValueError(f"unexpected actual-bounds fit failure: {design.name}")
    if not designs:
        raise ValueError("no supported designs fit the configured build envelope")
    return Job(designs, build, "catalogue", omitted=omitted), sizes


def h2d_dual_safe_catalogue_job(
    *,
    hole_diameter: float | None = DEFAULT_HOLE_DIAMETER_MM,
    hole_scope: Literal["interior", "full"] = "full",
    packing_gap: float = H2D_DEFAULT_PART_CLEARANCE_MM,
) -> Job:
    positive("H2D packing gap", packing_gap)
    interface = Interface()
    physical_build = BuildVolume(350, 320, 325)
    source, sizes = _catalogue_job_with_sizes(
        physical_build,
        interface=interface,
        hole_diameter=hole_diameter,
        hole_scope=hole_scope,
        orient_for_bambu=True,
    )
    if source.omitted:
        raise ValueError("H2D dual-safe catalogue unexpectedly omitted standard designs")
    exception = next(
        design
        for design in source.designs
        if "family" not in design.parameters
        and design.parameters["nx"] == 5
        and design.parameters["ny"] == 5
    )
    common_designs = [design for design in source.designs if design is not exception]

    def family_members(families: set[str], ramp_join: str | None = None) -> list[Design]:
        return [
            design
            for design in common_designs
            if design.parameters.get("family", "tile") in families
            and (ramp_join is None or design.parameters.get("ramp_join", "female") == ramp_join)
        ]

    def perimeter_traits(design: Design) -> tuple[float, bool]:
        parameters = design.parameters
        outward = parameters.get("edge_outward", 10.0)
        complete = parameters.get("complete_edge_holes", False)
        return outward, complete

    groups = [
        ("Tiles", family_members({"tile"})),
        ("Female ramps", family_members({"ramp"}, "female")),
        ("Male ramps", family_members({"ramp"}, "male")),
        ("Normal stops", family_members({"vertical-stop"})),
        (
            "Tile brackets - deep and shallow",
            family_members({"vertical-tile-bracket"}),
        ),
        ("Angled stops", family_members({"lock-45"})),
        ("Attachment plates", family_members({"plate"})),
    ]
    perimeter_designs = family_members({"edge-x", "edge-y", "corner-in", "corner-out"})
    for outward in EDGE_OUTWARD_OPTIONS_MM:
        for complete in (False, True):
            mode = "complete holes" if complete else "plain"
            traits = (outward, complete)
            members = [design for design in perimeter_designs if perimeter_traits(design) == traits]
            if members:
                groups.append((f"{outward:g}mm edges and corners - {mode}", members))
    groups.append(
        (
            "Rails and connectors",
            family_members({"support", "support-bit", "support-end"}),
        )
    )
    groups.append(("Rods and upper braces", family_members({"rod", "rod-brace"})))
    common_build = BuildVolume(
        350,
        320,
        320,
        margin=5,
        exclusions=(
            Exclusion(0, 0, 30, 320),
            Exclusion(320, 0, 30, 320),
        ),
    )
    designs = []
    placements = []
    footprints = []
    projected_clearances = {}
    projected_packing = {}
    plate_names = {}
    plate_offset = 0
    for title, members in groups:
        perimeter_group = bool(members) and all(
            design.parameters.get("family") in {"edge-x", "edge-y", "corner-in", "corner-out"}
            for design in members
        )
        rectangular = pack_sizes(
            [sizes[id(design)] for design in members],
            common_build,
            gap=packing_gap,
            pack=True,
        )
        shape_nested = False
        rectangle_plate_count = max(placement.plate for placement in rectangular) + 1
        if perimeter_group and rectangle_plate_count > 1:
            group_footprints = []
            for design in members:
                vertices, faces, _ = checked_mesh(design.bambu_shape)
                group_footprints.append(projected_mesh_footprint(vertices, faces))
            search_gap = max(
                packing_gap - H2D_FOOTPRINT_SEARCH_ALLOWANCE_MM,
                packing_gap / 2,
            )
            try:
                candidate = pack_projected_footprints(
                    group_footprints,
                    (30, 5, 320, 315),
                    gap=packing_gap,
                    search_gap=search_gap,
                )
            except ValueError as error:
                packed = rectangular
                group_footprints = [None] * len(members)
                projected_packing[title] = {
                    "status": "rectangle fallback",
                    "reason": str(error),
                }
            else:
                packed = candidate
                shape_nested = True
                projected_packing[title] = {
                    "status": "applied",
                    "minimum_projected_gap_mm": packing_gap,
                    "search_gap_mm": search_gap,
                    "grid_mm": 1,
                    "maximum_candidate_positions": 4_000_000,
                    "maximum_order_attempts": 8,
                }
        else:
            group_footprints = [None] * len(members)
            packed = rectangular
            if perimeter_group:
                projected_packing[title] = {
                    "status": "rectangle retained",
                    "reason": "the group already fits one plate at the requested bounds gap",
                }
        group_plate_count = max(placement.plate for placement in packed) + 1
        for local_plate in range(group_plate_count):
            plate_names[plate_offset + local_plate] = (
                title if group_plate_count == 1 else f"{title} {local_plate + 1}"
            )
        designs.extend(members)
        footprints.extend(group_footprints)
        if shape_nested:
            projected_clearances[plate_offset] = packing_gap
        placements.extend(
            PrintPlacement(
                placement.plate + plate_offset,
                placement.x,
                placement.y,
                placement.rotation,
            )
            for placement in packed
        )
        plate_offset += group_plate_count
    if len(designs) != len(common_designs) or len({design.name for design in designs}) != len(
        common_designs
    ):
        raise ValueError("H2D dual-safe family grouping is incomplete or duplicated")
    exception_placement = pack_sizes(
        [sizes[id(exception)]],
        BuildVolume(325, 320, 320, margin=5),
        gap=packing_gap,
        pack=True,
    )[0]
    designs.append(exception)
    footprints.append(None)
    placements.append(
        PrintPlacement(
            plate_offset,
            exception_placement.x,
            exception_placement.y,
            exception_placement.rotation,
        )
    )
    plate_names[plate_offset] = "5x5 TILE - SINGLE NOZZLE ONLY - LEFT"
    plate_settings = {
        plate_offset: {
            "filament_map_mode": "Manual",
            "filament_maps": "1",
            "filament_volume_maps": "0",
        }
    }
    job = Job(
        designs,
        physical_build,
        "catalogue",
        print_placements=placements,
        plate_names=plate_names,
        plate_settings=plate_settings,
        placement_policy={
            "name": "H2D dual-nozzle safe",
            "common_reach_mm": {"min_x": 25, "max_x": 325, "min_y": 0, "max_y": 320, "max_z": 320},
            "common_model_inset_mm": 5,
            "minimum_model_gap_mm": packing_gap,
            "minimum_actual_part_xy_clearance_mm": packing_gap,
            "clearance_measurement": (
                "model bounds on rectangle-packed plates; all-height projected model "
                "footprints on marked shape-packed plates"
            ),
            "projected_footprint_gap_overrides_mm": {
                plate_names[plate]: clearance for plate, clearance in projected_clearances.items()
            },
            "projected_footprint_packing": projected_packing,
            "grouped_by_family": True,
            "perimeter_grouping": "outward width and boundary-hole mode",
            "exception": {
                "design": exception.name,
                "plate": plate_offset + 1,
                "reach": "left nozzle only: X 0..325, Y 0..320, Z <= 320",
                "filament_slot": 1,
            },
        },
        projected_footprints=footprints,
        projected_footprint_clearances=projected_clearances,
    )
    job.part_gap = packing_gap
    if plate_offset + 1 > 36:
        raise ValueError("H2D dual-safe grouped catalogue exceeds the 36-plate limit")
    return job
