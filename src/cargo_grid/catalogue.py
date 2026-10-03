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
from cargo_grid.footprints import pack_projected_footprints, projected_mesh_footprint
from cargo_grid.jobs import Design, Job, tile_design
from cargo_grid.meshes import checked_mesh
from cargo_grid.packing import PrimeTower, PrintPlacement, h2d_common_build, pack_sizes
from cargo_grid.parameters import (
    DEFAULT_HOLE_DIAMETER_MM,
    BuildVolume,
    Exclusion,
    Interface,
    Tile,
    interface_parameters,
    positive,
)
from cargo_grid.rods import BRACE_SPACINGS_MM, ROD_HEIGHTS_MM, Rod, RodBrace
from cargo_grid.roof_support import OBJECT_AUTO_SUPPORT, female_roofs

H2D_DEFAULT_PART_CLEARANCE_MM = 4.0
# Auto roof support prints a PLA interface, so every plate with supported parts needs a prime
# tower that both H2D nozzles reach (X 25..325). Bambu makes tower width and style project-wide,
# so one compact 28 mm rectangular tower with a fixed 3 mm brim serves every plate. It starts
# near the front of a reserved right-hand column (X 284..325, the whole plate depth) and Bambu
# grows it toward the back; models on those plates stay at X <= 276.5, so support feet keep clear
# of the purge line. The column fits beside the 246 mm-wide tiles with a 5-cell side, which
# already span the full common depth.
H2D_AUTO_SUPPORT_TOWER = PrimeTower(width=28, depth=300)
H2D_AUTO_SUPPORT_TOWER_ORIGIN = (293.0, 10.0)
H2D_AUTO_SUPPORT_MODEL_MAX_X = 276.5
# Bambu's automatic support-foot expansion grows the first support layer up to about 5 mm past
# the part outline, so plates with supported parts add this to the model clearance and keep
# parts a further 1 mm from the front and back plate edges.
AUTO_SUPPORT_FOOT_ALLOWANCE_MM = 4.0
AUTO_SUPPORT_PLATE_MARGIN_MM = 6.0
H2D_FOOTPRINT_SEARCH_ALLOWANCE_MM = 0.75

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


def needs_auto_support(design: Design) -> bool:
    """Whether auto roof support switches object support on, so the plate prints PLA."""
    return bool(female_roofs(design)) or design.bambu_object_settings == OBJECT_AUTO_SUPPORT


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
    return _with_left_prime_tower(job, sizes, packing_gap)


def _with_left_prime_tower(
    job: Job, sizes: dict[int, tuple[float, float, float]], packing_gap: float
) -> Job:
    """Pack a mixed catalogue around one prime tower column at the left of the usable area."""
    positive("packing gap", packing_gap, zero=True)
    build = job.build
    usable_depth = build.y - 2 * build.margin - build.reserve_y
    if usable_depth <= 2 * H2D_AUTO_SUPPORT_TOWER.brim + 1:
        raise ValueError("auto roof support needs room for a prime tower at the left of the build")
    tower = PrimeTower(
        width=H2D_AUTO_SUPPORT_TOWER.width,
        depth=usable_depth - 2 * H2D_AUTO_SUPPORT_TOWER.brim - 0.5,
    )
    origin = (build.margin + tower.purge_lead, build.margin + 2 * tower.brim + 0.5)
    x0, y0, x1, y1 = tower.footprint(*origin)
    if any(
        x0 < area.x + area.width and area.x < x1 and y0 < area.y + area.depth and area.y < y1
        for area in build.exclusions
    ):
        raise ValueError("auto roof support needs room for a prime tower at the left of the build")
    support_gap = packing_gap + AUTO_SUPPORT_FOOT_ALLOWANCE_MM
    strip = min(build.x, x1 + support_gap)
    tower_build = BuildVolume(
        build.x,
        build.y,
        build.z,
        margin=build.margin,
        reserve_x=build.reserve_x,
        reserve_y=build.reserve_y,
        reserve_z=build.reserve_z,
        exclusions=(*build.exclusions, Exclusion(0, 0, strip, build.y)),
    )
    designs, omitted = [], list(job.omitted)
    for design in job.designs:
        if tower_build.placement(sizes[id(design)]) is None:
            omitted.append(
                {
                    "name": design.name,
                    "parameters": design.parameters,
                    "size_mm": sizes[id(design)],
                    "reason": "actual bounds do not fit beside the reserved prime tower",
                }
            )
        else:
            designs.append(design)
    if not designs:
        raise ValueError("no supported designs fit beside the reserved prime tower")
    placements = pack_sizes(
        [sizes[id(design)] for design in designs], tower_build, gap=support_gap, pack=True
    )
    positions = {
        placement.plate: origin
        for design, placement in zip(designs, placements)
        if needs_auto_support(design)
    }
    result = Job(
        designs,
        build,
        "catalogue",
        omitted=omitted,
        print_placements=placements,
        placement_policy={
            "auto_roof_support": {
                "prime_tower_plates": [plate + 1 for plate in sorted(positions)],
                "model_min_x_mm": strip,
                "model_gap_mm": support_gap,
                "reason": (
                    "PLA interface plates need a prime tower; it sits at the left of the usable "
                    "area, so check that every nozzle of your printer reaches it"
                ),
            }
        },
        prime_tower=tower,
        prime_tower_positions=positions,
    )
    result.part_gap = packing_gap
    return result


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
) -> Job:
    """Family-grouped H2D plan; ``auto_roof_support`` reserves prime towers for PLA plates."""
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
    tower_build = BuildVolume(
        350,
        320,
        320,
        margin=AUTO_SUPPORT_PLATE_MARGIN_MM,
        exclusions=(
            Exclusion(0, 0, 30, 320),
            Exclusion(H2D_AUTO_SUPPORT_MODEL_MAX_X, 0, 350 - H2D_AUTO_SUPPORT_MODEL_MAX_X, 320),
        ),
    )
    plate_builds = {}
    tower_positions = {}
    tower_groups = []
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
        towered = auto_roof_support and any(needs_auto_support(design) for design in members)
        group_build = tower_build if towered else common_build
        group_gap = packing_gap + AUTO_SUPPORT_FOOT_ALLOWANCE_MM if towered else packing_gap
        if towered:
            tower_groups.append(title)
        rectangular = pack_sizes(
            [sizes[id(design)] for design in members],
            group_build,
            gap=group_gap,
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
                group_gap - H2D_FOOTPRINT_SEARCH_ALLOWANCE_MM,
                group_gap / 2,
            )
            try:
                candidate = pack_projected_footprints(
                    group_footprints,
                    ((30, 6, H2D_AUTO_SUPPORT_MODEL_MAX_X, 314) if towered else (30, 5, 320, 315)),
                    gap=group_gap,
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
                    "minimum_projected_gap_mm": group_gap,
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
            plate_builds[plate_offset + local_plate] = group_build
        if towered:
            for design, placement in zip(members, packed):
                if needs_auto_support(design):
                    tower_positions[plate_offset + placement.plate] = H2D_AUTO_SUPPORT_TOWER_ORIGIN
        designs.extend(members)
        footprints.extend(group_footprints)
        if shape_nested:
            projected_clearances[plate_offset] = group_gap
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
    job = Job(
        designs,
        physical_build,
        "catalogue",
        omitted=omitted,
        print_placements=placements,
        plate_names=plate_names,
        plate_builds=plate_builds,
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
            "omitted_tiles": [entry["name"] for entry in omitted],
            **(
                {
                    "auto_roof_support": {
                        "prime_tower_plates": [plate + 1 for plate in sorted(tower_positions)],
                        "prime_tower_groups": tower_groups,
                        "model_max_x_on_tower_plates_mm": H2D_AUTO_SUPPORT_MODEL_MAX_X,
                        "model_gap_on_tower_plates_mm": packing_gap
                        + AUTO_SUPPORT_FOOT_ALLOWANCE_MM,
                        "reason": (
                            "PLA interface plates need a prime tower both nozzles reach; the "
                            "tower column and its 4 mm clearance are kept free of models"
                        ),
                    }
                }
                if auto_roof_support
                else {}
            ),
        },
        projected_footprints=footprints,
        projected_footprint_clearances=projected_clearances,
        prime_tower=H2D_AUTO_SUPPORT_TOWER if auto_roof_support else None,
        prime_tower_positions=tower_positions,
    )
    job.part_gap = packing_gap
    if plate_offset > 36:
        raise ValueError("H2D dual-safe grouped catalogue exceeds the 36-plate limit")
    return job
