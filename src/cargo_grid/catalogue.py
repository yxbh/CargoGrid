"""Finite functional catalogue bounded by the user's usable print envelope."""

import json
from dataclasses import asdict, replace
from functools import partial
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
    make_accessory,
    required_bambu_object_settings,
    tile_matched_perimeter,
)
from cargo_grid.jobs import Design, Job, tile_design
from cargo_grid.packing import h2d_common_build
from cargo_grid.parameters import (
    DEFAULT_HOLE_DIAMETER_MM,
    BuildVolume,
    Interface,
    Tile,
    interface_parameters,
    positive,
)
from cargo_grid.plates import PlateGroup, plan_plates
from cargo_grid.rods import BRACE_SPACINGS_MM, ROD_HEIGHTS_MM, Rod, RodBrace

H2D_DEFAULT_PART_CLEARANCE_MM = 4.0

BRACKET_DISPLAY_NAMES = {
    (1, 2, 2): "Deep tall tile bracket — floor 1x2, wall 1x2",
    (2, 1, 1): "Wide low tile bracket — floor 2x1, wall 2x1",
    (2, 2, 2): "Deep square tile bracket — floor 2x2, wall 2x2",
    (1, 1, 2): "Shallow tall tile bracket — floor 1x1, wall 1x2",
    (2, 1, 2): "Shallow wide tile bracket — floor 2x1, wall 2x2",
}


def accessory_identity_parameters(spec: Accessory | Rod | RodBrace) -> dict:
    parameters = (
        {"family": spec.family, **asdict(spec)}
        if isinstance(spec, (Rod, RodBrace))
        else asdict(spec)
    )
    if isinstance(spec, Accessory):
        parameters["interface"] = interface_parameters(spec.interface)
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
    return parameters


def accessory_identity(spec: Accessory | Rod | RodBrace) -> tuple[str, dict]:
    parameters = accessory_identity_parameters(spec)
    token = sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:10]
    if isinstance(spec, Rod):
        return (
            f"rod_h{spec.above_mat_height_mm:g}_peg{spec.peg_diameter_mm:g}_{token}",
            parameters,
        )
    if isinstance(spec, RodBrace):
        return (
            f"rod-brace_c{spec.center_spacing_mm:g}_bore{spec.bore_diameter_mm:g}_{token}",
            parameters,
        )
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
    return (
        f"{spec.family}_{dimensions}{join_suffix}_v{spec.variant}{edge_suffix}_"
        f"{spec.interface.joint_style}_{token}",
        parameters,
    )


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
                interface.body_height,
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
    # Only tile-edge parts take a solid bottom; the rest keep their standard identities.
    unchanged = replace(interface, solid_bottom_mm=0.0)
    result = []
    perimeter_spec = partial(
        tile_matched_perimeter,
        interface=interface,
        hole_diameter=hole_diameter,
        hole_scope=hole_scope,
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
        result.append(Accessory("support", nx=n, interface=unchanged))
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
    result.extend(Accessory("support-end", variant=v, interface=unchanged) for v in range(1, 5))
    result.extend(
        Accessory("support-bit", length=length, interface=unchanged) for length in (20, 30, 40, 50)
    )
    result.extend(
        Accessory(
            "vertical-tile-bracket",
            nx=x,
            ny=base_y,
            interface=unchanged,
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
        Accessory("vertical-stop", nx=x, ny=y, height=height, interface=unchanged)
        for x, y in VERTICAL_STOP_CELLS
        for height in VERTICAL_STOP_HEIGHTS_MM
    )
    result.extend(
        Accessory("lock-45", nx=x, ny=y, interface=unchanged)
        for x, y in ((1, 1), (2, 2))
        if 50 <= y * interface.pitch and (x == 1 or interface.pitch <= 60)
    )
    result.extend(
        Accessory("plate", nx=x, ny=y, interface=unchanged) for x, y in ((1, 1), (1, 2), (2, 2))
    )
    result.extend(Rod(h, tile_thickness_mm=interface.height) for h in ROD_HEIGHTS_MM)
    result.extend(RodBrace(spacing) for spacing in BRACE_SPACINGS_MM)
    return result


def accessory_design(spec: Accessory | Rod | RodBrace) -> Design:
    name, parameters = accessory_identity(spec)
    if isinstance(spec, (Rod, RodBrace)):
        shape = make_accessory(spec)
        shape.label = name
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
    auto_roof_support: bool = False,
    packing_gap: float = 2,
) -> Job:
    job, sizes = _catalogue_job_with_sizes(
        build,
        interface=interface,
        hole_diameter=hole_diameter,
        hole_scope=hole_scope,
        orient_for_bambu=orient_for_bambu,
    )
    if not auto_roof_support:
        return job
    positive("packing gap", packing_gap, zero=True)
    plan = plan_plates(
        [PlateGroup(None, job.designs, packing_gap)],
        build,
        size=lambda design: sizes[id(design)],
        auto_roof_support=True,
    )
    if not plan.designs:
        raise ValueError("no supported designs fit beside the reserved prime tower")
    omitted = [
        *job.omitted,
        *(
            {
                "name": design.name,
                "parameters": design.parameters,
                "size_mm": sizes[id(design)],
                "reason": "actual bounds do not fit beside the reserved prime tower",
            }
            for design in plan.unfit
        ),
    ]
    return Job(
        plan.designs,
        build,
        "catalogue",
        omitted=omitted,
        part_gap=packing_gap,
        print_placements=plan.placements,
        plate_builds=plan.plate_builds,
        placement_policy={"auto_roof_support": plan.auto_roof_support},
        prime_tower=plan.prime_tower,
        prime_tower_positions=plan.prime_tower_positions,
        prime_tower_reaches=plan.prime_tower_reaches,
        prime_tower_clearances=plan.prime_tower_clearances,
    )


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
    solid_bottom_mm: float = 0.0,
    auto_roof_support: bool = False,
    layer_height_mm: float = 0.32,
) -> Job:
    """Family-grouped H2D plan; ``auto_roof_support`` reserves prime towers for PLA plates.

    ``layer_height_mm`` is the project's layer height, which sets Bambu's tower estimate and so
    where the tower goes.
    """
    positive("H2D packing gap", packing_gap)
    interface = Interface(solid_bottom_mm=solid_bottom_mm)
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
    common_build = h2d_common_build()
    # Tiles that only fit one nozzle's area (the 306 mm 5x5) are left out of the shared plan.
    common_designs = [
        design for design in source.designs if common_build.placement(sizes[id(design)])
    ]
    omitted = [
        {
            "name": design.name,
            "parameters": design.parameters,
            "size_mm": sizes[id(design)],
            "reason": (
                "actual bounds exceed the 300 mm H2D common reach (X 25..325) with its 5 mm "
                "inset; make it with part and choose the nozzle yourself"
            ),
        }
        for design in source.designs
        if not common_build.placement(sizes[id(design)])
    ]
    if any("family" in entry["parameters"] for entry in omitted):
        raise ValueError("H2D dual-safe catalogue unexpectedly omitted an accessory")

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
        PlateGroup("Tiles", family_members({"tile"}), packing_gap),
        PlateGroup("Female ramps", family_members({"ramp"}, "female"), packing_gap),
        PlateGroup("Male ramps", family_members({"ramp"}, "male"), packing_gap),
        PlateGroup("Normal stops", family_members({"vertical-stop"}), packing_gap),
        PlateGroup(
            "Tile brackets - deep and shallow",
            family_members({"vertical-tile-bracket"}),
            packing_gap,
        ),
        PlateGroup("Angled stops", family_members({"lock-45"}), packing_gap),
        PlateGroup("Attachment plates", family_members({"plate"}), packing_gap),
    ]
    perimeter_designs = family_members({"edge-x", "edge-y", "corner-in", "corner-out"})
    for outward in EDGE_OUTWARD_OPTIONS_MM:
        for complete in (False, True):
            mode = "complete holes" if complete else "plain"
            traits = (outward, complete)
            members = [design for design in perimeter_designs if perimeter_traits(design) == traits]
            if members:
                groups.append(
                    PlateGroup(
                        f"{outward:g}mm edges and corners - {mode}",
                        members,
                        packing_gap,
                        projected=True,
                    )
                )
    groups.append(
        PlateGroup(
            "Rails and connectors",
            family_members({"support", "support-bit", "support-end"}),
            packing_gap,
        )
    )
    groups.append(
        PlateGroup("Rods and upper braces", family_members({"rod", "rod-brace"}), packing_gap)
    )
    plan = plan_plates(
        groups,
        common_build,
        size=lambda design: sizes[id(design)],
        auto_roof_support=auto_roof_support,
        layer_height_mm=layer_height_mm,
    )
    if plan.unfit:
        raise ValueError(f"{plan.unfit[0].name} does not fit its H2D plate area")
    designs = plan.designs
    plate_names = plan.plate_names
    if len(designs) != len(common_designs) or len({design.name for design in designs}) != len(
        common_designs
    ):
        raise ValueError("H2D dual-safe family grouping is incomplete or duplicated")
    job = Job(
        designs,
        physical_build,
        "catalogue",
        omitted=omitted,
        print_placements=plan.placements,
        plate_names=plate_names,
        plate_builds=plan.plate_builds,
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
                plate_names[plate]: clearance
                for plate, clearance in plan.projected_clearances.items()
            },
            "projected_footprint_packing": plan.projected_packing,
            "grouped_by_family": True,
            "perimeter_grouping": "outward width and boundary-hole mode",
            "omitted_tiles": [entry["name"] for entry in omitted],
            **(
                {"auto_roof_support": plan.auto_roof_support}
                if plan.auto_roof_support is not None
                else {}
            ),
        },
        projected_footprints=plan.projected_footprints,
        projected_footprint_clearances=plan.projected_clearances,
        prime_tower=plan.prime_tower,
        prime_tower_positions=plan.prime_tower_positions,
        prime_tower_reaches=plan.prime_tower_reaches,
        prime_tower_clearances=plan.prime_tower_clearances,
    )
    job.part_gap = packing_gap
    if plan.plate_count > 36:
        raise ValueError("H2D dual-safe grouped catalogue exceeds the 36-plate limit")
    return job
