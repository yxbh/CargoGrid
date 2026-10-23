"""Cross-post extras: cross, tee, corner and X posts at both heights, with pegs and an X plug."""

import json
from dataclasses import replace
from hashlib import sha256

from cargo_grid.accessories import required_bambu_object_settings, required_bambu_print_rotation
from cargo_grid.cross_posts import (
    ARM_LAYOUTS,
    ARM_THICKNESS_MM,
    CROSS_POST_FAMILY,
    CROSS_POST_HEIGHTS_MM,
    LEAD_IN_HEIGHT_MM,
    LEAD_IN_INSET_MM,
    MOUNTS,
    CrossPost,
    cross_post_datums,
    make_cross_post,
    normalized_interface,
    peg_sites_accepted,
)
from cargo_grid.jobs import Design, Job
from cargo_grid.parameters import (
    DEFAULT_HOLE_DIAMETER_MM,
    BuildVolume,
    Interface,
    interface_parameters,
    positive,
)
from cargo_grid.plates import PlateGroup, plan_plates

PLATE_PREFIX = "Cross posts - "
SHAPE_TITLES = {"cross": "Cross post", "tee": "Tee post", "corner": "Corner post", "x": "X post"}
MOUNT_TITLES = {"pegs": "pegs", "x-plug": "X plug"}


def _identity(
    arms: str, mount: str, height: float, peg_diameter: float | None, interface: Interface
) -> tuple[str, dict]:
    parameters = {
        "family": CROSS_POST_FAMILY,
        "arms": arms,
        "mount": mount,
        "height_mm": height,
        "interface": interface_parameters(interface),
    }
    if peg_diameter is not None:
        parameters["peg_diameter_mm"] = peg_diameter
    token = sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:10]
    peg = f"_peg{peg_diameter:g}" if peg_diameter is not None else ""
    return f"{CROSS_POST_FAMILY}_{arms}_{mount}_h{height:g}{peg}_{token}", parameters


def cross_post_identity(spec: CrossPost) -> tuple[str, dict]:
    return _identity(spec.arms, spec.mount, spec.height_mm, spec.peg_diameter_mm, spec.interface)


def cross_post_design(spec: CrossPost) -> Design:
    name, parameters = cross_post_identity(spec)
    shape = make_cross_post(spec)
    shape.label = name
    rotation = required_bambu_print_rotation(parameters)
    peg = (
        f" {spec.peg_diameter_mm:g} mm"
        if spec.peg_diameter_mm not in (None, DEFAULT_HOLE_DIAMETER_MM)
        else ""
    )
    return Design(
        name,
        shape,
        parameters,
        display_name=(
            f"{SHAPE_TITLES[spec.arms]} - {MOUNT_TITLES[spec.mount]}{peg} - {spec.height_mm:g} mm"
        ),
        recommended_print_rotation_x=rotation,
        apply_orientation_to_bambu=rotation is not None,
        bambu_object_settings=dict(required_bambu_object_settings(parameters)),
        mating_datums=cross_post_datums(spec),
    )


def extras_job(
    build: BuildVolume,
    *,
    interface: Interface = Interface(),
    placement_build: BuildVolume | None = None,
    gap: float = 2,
    orient_for_bambu: bool = False,
    auto_roof_support: bool = False,
    layer_height_mm: float = 0.32,
) -> Job:
    """Every arm layout at both heights, one plate per mount.

    Peg posts need full-pattern round holes at every half-unit point; at unit sizes whose tiles
    don't keep them, the peg posts are recorded as omitted. ``orient_for_bambu`` packs the
    Bambu print poses; otherwise the source orientation. ``auto_roof_support`` reserves a
    prime tower on each plate, and the X-plug posts' object support then gets the PLA interface;
    ``layer_height_mm`` sets where Bambu keeps the tower.
    """
    positive("cross post packing gap", gap, zero=True)
    envelope = placement_build or build
    groups, sizes, omitted = [], {}, []
    for mount in MOUNTS:
        # Pegs don't use the X socket fit offset, so peg posts keep their standard identity.
        mounted = normalized_interface(
            replace(interface, fit_offset=0.0) if mount == "pegs" else interface
        )
        designs = []
        for arms in ARM_LAYOUTS:
            for height in CROSS_POST_HEIGHTS_MM:
                if mount == "pegs" and not peg_sites_accepted(mounted):
                    name, parameters = _identity(
                        arms, mount, height, DEFAULT_HOLE_DIAMETER_MM, mounted
                    )
                    omitted.append(
                        {
                            "name": name,
                            "parameters": parameters,
                            "size_mm": None,
                            "reason": (
                                f"tiles with {mounted.pitch:g} mm units don't keep a round hole "
                                "at every half-unit point"
                            ),
                        }
                    )
                    continue
                design = cross_post_design(CrossPost(arms, mount, height, interface=mounted))
                size = design.bambu_size if orient_for_bambu else design.size
                if envelope.placement(size) is None or build.placement(size) is None:
                    raise ValueError(
                        f"{design.name}: actual bounds {size} exceed the configured build envelope"
                    )
                designs.append(design)
                sizes[id(design)] = size
        if designs:
            groups.append(PlateGroup(f"{PLATE_PREFIX}{MOUNT_TITLES[mount]}", designs, gap))
    plates = plan_plates(
        groups,
        envelope,
        size=lambda design: sizes[id(design)],
        auto_roof_support=auto_roof_support,
        layer_height_mm=layer_height_mm,
    )
    if plates.unfit:
        raise ValueError(f"{plates.unfit[0].name} does not fit its plate area")
    return Job(
        plates.designs,
        build,
        "extras",
        omitted=omitted,
        part_gap=gap,
        print_placements=plates.placements,
        plate_names=plates.plate_names,
        plate_builds=plates.plate_builds,
        prime_tower=plates.prime_tower,
        prime_tower_positions=plates.prime_tower_positions,
        prime_tower_reaches=plates.prime_tower_reaches,
        prime_tower_clearances=plates.prime_tower_clearances,
        placement_policy={
            "collection": CROSS_POST_FAMILY,
            "minimum_model_gap_mm": gap,
            "plate_group_minimum_model_gap_mm": plates.group_gaps,
            "one_plate_per_mount": True,
            "print_poses_packed": orient_for_bambu,
            **(
                {"auto_roof_support": plates.auto_roof_support}
                if plates.auto_roof_support is not None
                else {}
            ),
        },
        manifest_metadata={
            "scope": (
                "Cross-post extras: box-corner posts with cross, tee, corner and diagonal X arm "
                "layouts, kept out of the standard catalogue"
            ),
            "inventory": {
                f"{arms}_{mount.replace('-', '_')}_h{height:g}": sum(
                    (d.parameters["arms"], d.parameters["mount"], d.parameters["height_mm"])
                    == (arms, mount, height)
                    for d in plates.designs
                )
                for mount in MOUNTS
                for arms in ARM_LAYOUTS
                for height in CROSS_POST_HEIGHTS_MM
            },
            "arm_thickness_mm": ARM_THICKNESS_MM,
            "box_face_offset_from_arm_centre_line_mm": ARM_THICKNESS_MM / 2,
            "lead_in_mm": {"inset": LEAD_IN_INSET_MM, "height": LEAD_IN_HEIGHT_MM},
            "positions": {
                "pegs": (
                    "every half-unit point: cell corners, edge midpoints and over X sockets; "
                    "X posts only over X sockets and at edge midpoints; needs full-pattern round "
                    "holes"
                ),
                "x_plug": "over X sockets at cell centres; works without round holes",
            },
            "print_poses": {
                "pegs": "top down (X=180) on the flat top, so the pegs grow up; no support",
                "x_plug": (
                    "as modelled, standing on the plug, with normal Auto support under the arm "
                    "undersides beside it"
                ),
                "support": (
                    "X-plug posts: an object override in PETG-only projects, or inherited global "
                    "support with the PLA interface and a prime tower on each plate with auto "
                    "roof support"
                ),
            },
            "physical_evidence": (
                "None. Peg and plug fit, how a post with several pegs seats across a tile seam, "
                "support removal under the X-plug posts' arms, and strength have not been tested."
            ),
            "limitations": (
                "No load, restraint or crash rating. Posts are held only by the fit of their pegs "
                "or plug."
            ),
        },
    )
