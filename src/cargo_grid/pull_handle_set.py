"""Pull-handle extras: the two-unit handle with and without its front strap bar."""

import json
from hashlib import sha256

from cargo_grid.accessories import required_bambu_object_settings
from cargo_grid.jobs import Design, Job
from cargo_grid.parameters import BuildVolume, Interface, interface_parameters, positive
from cargo_grid.plates import PlateGroup, plan_plates
from cargo_grid.pull_handles import (
    HAND_CLEARANCE_MM,
    PULL_HANDLE_FAMILY,
    STRAP_SLOT_HEIGHT_MM,
    STRAP_SLOT_WIDTH_MM,
    PullHandle,
    depth_cells,
    front_rest_margin_mm,
    make_pull_handle,
    print_rotation_x,
    pull_handle_datums,
    width_cells,
)

PLATE_PREFIX = "Pull handle - "
VARIANT_TITLES = {True: "strap bar", False: "no strap bar"}


def pull_handle_identity(spec: PullHandle) -> tuple[str, dict]:
    parameters = {
        "family": PULL_HANDLE_FAMILY,
        "strap_bar": spec.strap_bar,
        "interface": interface_parameters(spec.interface),
    }
    token = sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:10]
    variant = "strap-bar" if spec.strap_bar else "no-strap-bar"
    cells = f"{width_cells(spec.interface)}x{depth_cells(spec.interface)}"
    return f"{PULL_HANDLE_FAMILY}_{cells}_{variant}_{token}", parameters


def pull_handle_design(spec: PullHandle) -> Design:
    name, parameters = pull_handle_identity(spec)
    shape = make_pull_handle(spec)
    shape.label = name
    cells = f"{width_cells(spec.interface)}x{depth_cells(spec.interface)}"
    return Design(
        name,
        shape,
        parameters,
        display_name=f"Pull handle - {cells} - {VARIANT_TITLES[spec.strap_bar]}",
        recommended_print_rotation_x=print_rotation_x(spec.strap_bar, spec.interface),
        apply_orientation_to_bambu=True,
        bambu_object_settings=dict(required_bambu_object_settings(parameters)),
        mating_datums=pull_handle_datums(spec),
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
    """One handle of each version, each on its own named plate.

    ``orient_for_bambu`` packs the Bambu print poses; otherwise the source orientation.
    ``auto_roof_support`` reserves a prime tower on the strap-bar handle's plate, whose object
    support then gets the PLA interface; ``layer_height_mm`` sets where Bambu keeps the tower.
    """
    positive("pull handle packing gap", gap, zero=True)
    specs = [PullHandle(strap_bar=strap_bar, interface=interface) for strap_bar in (True, False)]
    datums = pull_handle_datums(specs[0])
    envelope = placement_build or build
    designs, sizes, rest_margins = [], {}, {}
    for spec in specs:
        design = pull_handle_design(spec)
        rest_margins[spec.strap_bar] = front_rest_margin_mm(
            design.shape, spec.interface, spec.strap_bar
        )
        size = design.bambu_size if orient_for_bambu else design.size
        if envelope.placement(size) is None or build.placement(size) is None:
            raise ValueError(
                f"{design.name}: actual bounds {size} exceed the configured build envelope"
            )
        designs.append(design)
        sizes[id(design)] = size
    plates = plan_plates(
        [
            PlateGroup(f"{PLATE_PREFIX}{VARIANT_TITLES[spec.strap_bar]}", [design], gap)
            for spec, design in zip(specs, designs)
        ],
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
        part_gap=gap,
        print_placements=plates.placements,
        plate_names=plates.plate_names,
        plate_builds=plates.plate_builds,
        prime_tower=plates.prime_tower,
        prime_tower_positions=plates.prime_tower_positions,
        prime_tower_reaches=plates.prime_tower_reaches,
        prime_tower_clearances=plates.prime_tower_clearances,
        placement_policy={
            "collection": PULL_HANDLE_FAMILY,
            "minimum_model_gap_mm": gap,
            "plate_group_minimum_model_gap_mm": plates.group_gaps,
            "one_plate_per_version": True,
            "print_poses_packed": orient_for_bambu,
            **(
                {"auto_roof_support": plates.auto_roof_support}
                if plates.auto_roof_support is not None
                else {}
            ),
        },
        manifest_metadata={
            "scope": (
                "Pull-handle extras: a hand-sized X-plug pull handle with and without a "
                "front strap bar, kept out of the standard catalogue"
            ),
            "cells": datums["cells"],
            "footprint_mm": datums["footprint_mm"],
            "inventory": {
                "pull_handle_with_strap_bar": 1,
                "pull_handle_without_strap_bar": 1,
            },
            "hand_opening_mm": {
                "width": datums["hand_opening_mm"]["width"],
                "height": HAND_CLEARANCE_MM,
            },
            "strap_slot_mm": {"width": STRAP_SLOT_WIDTH_MM, "height": STRAP_SLOT_HEIGHT_MM},
            "print_poses": {
                "strap_bar": (
                    "lies on its front on the grip's top-front edge and the strap-bar or "
                    "seat edge below it"
                ),
                "no_strap_bar": "lies on its front on the grip's top-front and seat edges",
                "support": (
                    "object-scoped normal Auto on both; with auto roof support it gets the PLA "
                    "interface and each plate a prime tower"
                ),
                "strap_bar_centre_of_mass_inside_rest_edges_mm": round(rest_margins[True], 2),
                "no_strap_bar_centre_of_mass_inside_rest_edges_mm": round(rest_margins[False], 2),
            },
            "physical_evidence": (
                "An earlier 2x1 strap-bar prototype at the standard 60/13 settings with a 34 mm "
                "hand opening was printed in the same front-down pose in PETG with a 0.8 mm "
                "nozzle and 0.32 mm layers; the user reported that the X plugs fitted a tile and "
                "the support came off cleanly, and that the opening was too tight. That support "
                "was PETG only. This 40 mm version, the PLA support interface, the no-strap-bar "
                "version in its front-down pose, other unit sizes and thicknesses, insertion "
                "force, retention and pull strength have not been tested."
            ),
            "limitations": (
                "No load, pull or restraint rating. The X plugs are held only by their fit."
            ),
        },
    )
