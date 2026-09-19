"""Finite functional catalogue bounded by the user's usable print envelope."""

import json
from dataclasses import asdict
from hashlib import sha256
from math import floor
from typing import Literal

from cargo_grid.accessories import (
    VERTICAL_BRACKET_CONFIGS,
    VERTICAL_STOP_CELLS,
    VERTICAL_STOP_HEIGHTS_MM,
    Accessory,
    bambu_print_rotation,
    bambu_print_rotation_y,
    make_accessory,
    required_bambu_object_settings,
)
from cargo_grid.jobs import Design, Job, tile_design
from cargo_grid.packing import PrintPlacement, pack_sizes
from cargo_grid.parameters import DEFAULT_HOLE_DIAMETER_MM, BuildVolume, Exclusion, Interface, Tile
from cargo_grid.rods import BRACE_SPACINGS_MM, ROD_HEIGHTS_MM, Rod, RodBrace

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
    build: BuildVolume, interface: Interface = Interface()
) -> list[Accessory | Rod | RodBrace]:
    nmax = max(1, floor(max(build.usable[:2]) / interface.pitch))
    result = []
    for n in range(1, nmax + 1):
        result.extend(
            Accessory(family, nx=n, interface=interface)
            for family in ("edge-x", "edge-y", "support")
        )
    result.extend(Accessory("corner-in", variant=v, interface=interface) for v in range(1, 5))
    result.extend(Accessory("corner-out", variant=v, interface=interface) for v in range(1, 7))
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
    token = sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:10]
    dimensions = (
        f"{spec.nx}x{spec.ny}_h{spec.height:g}"
        if spec.family == "vertical-stop"
        else f"base{spec.nx}x{spec.ny}_wall{spec.nx}x{spec.panel_height_cells}"
        if spec.family == "vertical-tile-bracket" and spec.panel_height_cells is not None
        else f"{spec.nx}x{spec.ny}"
    )
    join_suffix = "_male" if spec.family == "ramp" and spec.ramp_join == "male" else ""
    name = f"{spec.family}_{dimensions}{join_suffix}_v{spec.variant}_{spec.interface.joint_style}_{token}"
    shape = make_accessory(spec)
    shape.label = name
    rotation = bambu_print_rotation(spec)
    rotation_y = bambu_print_rotation_y(spec)
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
    for spec in accessory_variants(build, interface):
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
) -> Job:
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
    groups = (
        ("Tiles", {"tile"}, None),
        ("Female ramps", {"ramp"}, "female"),
        ("Male ramps", {"ramp"}, "male"),
        ("Normal stops", {"vertical-stop"}, None),
        (
            "Tile brackets - deep and shallow",
            {"vertical-tile-bracket"},
            None,
        ),
        ("Angled stops", {"lock-45"}, None),
        ("Attachment plates", {"plate"}, None),
        ("Edges and corners", {"edge-x", "edge-y", "corner-in", "corner-out"}, None),
        ("Rails and connectors", {"support", "support-bit", "support-end"}, None),
        ("Rods and upper braces", {"rod", "rod-brace"}, None),
    )
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
    plate_names = {}
    plate_offset = 0
    for title, families, ramp_join in groups:
        members = [
            design
            for design in common_designs
            if design.parameters.get("family", "tile") in families
            and (ramp_join is None or design.parameters.get("ramp_join", "female") == ramp_join)
        ]
        packed = pack_sizes(
            [sizes[id(design)] for design in members],
            common_build,
            gap=10,
            pack=True,
        )
        group_plate_count = max(placement.plate for placement in packed) + 1
        for local_plate in range(group_plate_count):
            plate_names[plate_offset + local_plate] = (
                title if group_plate_count == 1 else f"{title} {local_plate + 1}"
            )
        designs.extend(members)
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
        gap=10,
        pack=True,
    )[0]
    designs.append(exception)
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
            "minimum_model_gap_mm": 10,
            "grouped_by_family": True,
            "exception": {
                "design": exception.name,
                "plate": plate_offset + 1,
                "reach": "left nozzle only: X 0..325, Y 0..320, Z <= 320",
                "filament_slot": 1,
            },
        },
    )
    job.part_gap = 10
    if plate_offset + 1 > 36:
        raise ValueError("H2D dual-safe grouped catalogue exceeds the 36-plate limit")
    return job
