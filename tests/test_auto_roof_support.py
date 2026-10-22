"""Auto roof support: global normal Auto support with a PLA interface.

These checks cover the generated settings, per-object flags and prime-tower reservations.
Local Bambu Studio slicing is a separate check; support release, tower stability and print
results remain unverified.
"""

import json
from dataclasses import replace
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from build123d import Box
from test_h2d_catalogue import _design_group
from tower_checks import assert_towers_stay_where_bambu_keeps_them

from cargo_grid import BuildVolume, Interface, Tile
from cargo_grid import catalogue as catalogue_module
from cargo_grid.accessories import Accessory
from cargo_grid.catalogue import accessory_design, dual_safe_catalogue_job
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, export_job, write_3mf
from cargo_grid.jobs import Design, Job, tile_design
from cargo_grid.packing import PrimeTower, PrintPlacement
from cargo_grid.plates import (
    AUTO_SUPPORT_TOWER_CLEARANCE_MM,
    DEFAULT_PRIME_TOWER,
    BambuTowerEstimate,
    PlateGroup,
    _DualNozzleTowerLayout,
    needs_auto_support,
    plan_plates,
)
from cargo_grid.printers import H2D
from cargo_grid.rods import Rod, RodBrace
from cargo_grid.roof_support import (
    OBJECT_AUTO_SUPPORT,
    RoofSupportSettings,
    effective_object_settings,
    global_auto_support_reason,
    validate_roof_job,
)

T = 1.92
# Bambu's default tower is used, so none of these is written.
TOWER_SETTINGS = (
    "enable_prime_tower",
    "prime_tower_width",
    "prime_tower_brim_width",
    "prime_tower_rib_wall",
    "wipe_tower_rotation_angle",
)
H2D_PETG = "Bambu PETG Basic @BBL H2D 0.8 nozzle"
H2D_PLA = "Bambu PLA Basic @BBL H2D 0.8 nozzle"
MATERIALS = (Material(H2D_PETG, "PETG", "#637b70"), Material(H2D_PLA, "PLA", "#dddddd"))
AUTO = RoofSupportSettings()
BUILD = BuildVolume(350, 320, 325)
BAMBU_ARGS = [
    "--bambu",
    "--nozzle-diameter-mm",
    "0.8",
    "--layer-height-mm",
    "0.32",
]
H2D_ARGS = [
    "--build-width-mm",
    "350",
    "--build-depth-mm",
    "320",
    "--build-height-mm",
    "325",
    *BAMBU_ARGS,
]
TWO_MATERIALS = [
    "--material",
    H2D_PETG,
    "PETG",
    "#637b70",
    "--material",
    H2D_PLA,
    "PLA",
    "#dddddd",
]


def bambu():
    return BambuSettings(MATERIALS, 0.8, 0.32, AUTO, machine_nozzle_count=2)


def test_auto_mode_keeps_the_zero_contact_keys_with_global_support_on():
    settings = AUTO.native_settings()
    assert settings == {
        "enable_support": "1",
        "support_type": "normal(auto)",
        "support_interface_filament": "2",
        "support_top_z_distance": "0",
        "support_interface_spacing": "0",
        "independent_support_layer_height": "0",
        "support_object_xy_distance": "0.4",
    }
    assert {"enable_support", "support_type"} <= AUTO.process_override_keys
    assert {
        "support_top_z_distance",
        "independent_support_layer_height",
        "support_interface_spacing",
    } <= AUTO.process_override_keys


@pytest.fixture(scope="module")
def designs():
    interface = Interface(solid_bottom_mm=T)
    female = {
        "tile": tile_design(Tile(1, 1, interface)),
        "edge-y": accessory_design(Accessory("edge-y", interface=interface)),
        "corner-in v1": accessory_design(Accessory("corner-in", interface=interface)),
        "female ramp": accessory_design(Accessory("ramp", interface=interface)),
    }
    existing = {
        "vertical-stop": accessory_design(Accessory("vertical-stop", nx=1, ny=1, height=60)),
        "shallow bracket": accessory_design(
            Accessory("vertical-tile-bracket", nx=1, ny=1, panel_height_cells=2)
        ),
        "rod": accessory_design(Rod(120)),
    }
    plain = {
        "edge-x": accessory_design(Accessory("edge-x", interface=interface)),
        "male ramp": accessory_design(Accessory("ramp", ramp_join="male", interface=interface)),
        "plate": accessory_design(Accessory("plate")),
        "lock-45": accessory_design(Accessory("lock-45")),
        "brace": accessory_design(RodBrace(60)),
        "support rail": accessory_design(Accessory("support", nx=1)),
        "rail end": accessory_design(Accessory("support-end", variant=1)),
    }
    return female, existing, plain


def test_objects_inherit_global_support_without_redundant_overrides(designs):
    female, existing, plain = designs
    for design in female.values():
        assert global_auto_support_reason(design) == "retained original female pocket roof"
        assert effective_object_settings(design, AUTO) == {}
        assert needs_auto_support(design)
    for design in existing.values():
        assert design.bambu_object_settings == OBJECT_AUTO_SUPPORT
        assert global_auto_support_reason(design).startswith("normal Auto")
        assert effective_object_settings(design, AUTO) == {}
        assert needs_auto_support(design)
    for name, design in plain.items():
        assert global_auto_support_reason(design).startswith("global normal Auto"), name
        assert effective_object_settings(design, AUTO) == {}
        assert needs_auto_support(design)
    assert effective_object_settings(female["tile"], None) == {}
    assert effective_object_settings(existing["rod"], None) == OBJECT_AUTO_SUPPORT


def test_object_support_off_is_not_an_accepted_design_setting():
    with pytest.raises(ValueError, match="unsupported Bambu per-object settings"):
        Design("protected", Box(10, 10, 10), {}, bambu_object_settings={"enable_support": "0"})


def test_auto_job_cannot_bypass_required_accessory_settings(designs, tmp_path):
    from dataclasses import replace

    rod = replace(designs[1]["rod"], bambu_object_settings={})
    job = Job([designs[0]["tile"], rod], BUILD, "part")
    with pytest.raises(ValueError, match="validated object settings"):
        write_3mf(job, tmp_path / "invalid.3mf", bambu=bambu())


def test_one_plate_auto_job_writes_global_support_and_tower(designs, tmp_path):
    female, existing, plain = designs
    chosen = [female["tile"], plain["edge-x"], plain["plate"], existing["vertical-stop"]]
    tower = PrimeTower()
    job = Job(
        chosen,
        BUILD,
        "part",
        print_placements=[
            PrintPlacement(0, 40, 20, 0),
            PrintPlacement(0, 140, 20, 0),
            PrintPlacement(0, 40, 140, 0),
            PrintPlacement(0, 140, 140, 0),
        ],
        prime_tower=tower,
        prime_tower_positions={0: (290.5, 8)},
    )
    manifest = json.loads(export_job(job, tmp_path / "job", stl=False, bambu=bambu()).read_text())
    with ZipFile(tmp_path / "job" / "job.3mf") as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        project = json.loads(archive.read("Metadata/project_settings.config"))
    flags = {}
    for obj in config.findall("object"):
        metadata = {m.get("key"): m.get("value") for m in obj.findall("metadata")}
        flags[metadata["name"].rsplit("_batch_", 1)[0]] = (
            metadata.get("enable_support"),
            metadata.get("support_type"),
            [part.get("subtype") for part in obj.findall("part")],
        )
    assert flags[female["tile"].name] == (None, None, ["normal_part"])
    assert flags[existing["vertical-stop"].name] == (None, None, ["normal_part"])
    assert flags[plain["edge-x"].name] == (None, None, ["normal_part"])
    assert flags[plain["plate"].name] == (None, None, ["normal_part"])
    assert project["enable_support"] == "1" and project["support_type"] == "normal(auto)"
    assert project["support_interface_filament"] == "2"
    # Bambu's default tower is used: only the per-plate position is written.
    assert project["wipe_tower_x"] == ["290.5"] and project["wipe_tower_y"] == ["8"]
    overrides = set(project["different_settings_to_system"][0].split(";"))
    assert {"wipe_tower_x", "wipe_tower_y"} <= overrides
    for key in TOWER_SETTINGS:
        assert key not in project and key not in overrides
    export = manifest["export"]
    roof = export["roof_support"]
    assert roof["mode"] == "auto" and roof["global_enable_support"] is True
    supported = {entry["design"]: entry for entry in roof["supported_objects"]}
    assert set(supported) == {design.name for design in chosen}
    assert supported[female["tile"].name]["support_may_occupy_openings"] == [
        [0.0, 30.0],
        [30.0, 0.0],
    ]
    assert roof["unsupported_designs"] == []
    (plate,) = export["prime_tower"]["plates"]
    assert plate["origin_mm"] == [290.5, 8]
    assert plate["reserved_extrusion_bounds_mm"] == [283, 1, 325, 39.5]
    assert "settings" not in export["prime_tower"]
    assert export["prime_tower"]["tower"].startswith("Bambu default")
    assert "object_settings" not in export["plates"][0]["items"][0]


@pytest.mark.parametrize(
    "position,message",
    [((150, 30), "within 2 mm of a placed model"), ((330, 10), "leaves the build plate")],
)
def test_prime_tower_must_stay_on_the_plate_and_clear_of_models(
    designs, position, message, tmp_path
):
    job = Job(
        [designs[0]["tile"]],
        BUILD,
        "part",
        print_placements=[PrintPlacement(0, 100, 20, 0)],
        prime_tower=PrimeTower(),
        prime_tower_positions={0: position},
    )
    with pytest.raises(ValueError, match=message):
        write_3mf(job, tmp_path / "tower.3mf", bambu=bambu())
    assert not (tmp_path / "tower.3mf").exists()


@pytest.mark.parametrize("offset,fits", [(0.0, True), (-0.01, False)])
def test_planned_tower_clearance_allows_touching_and_names_each_side(
    designs, tmp_path, offset, fits
):
    reservation = _DualNozzleTowerLayout(H2D, BambuTowerEstimate(0.2)).reserve(14.92)
    area = reservation.build.exclusions[-1]
    job = Job(
        [designs[0]["tile"]],
        BUILD,
        "part",
        # The model's front edge sits on the recomputed back edge of the model-free area.
        print_placements=[PrintPlacement(0, 230, area.y + area.depth + offset, 0)],
        prime_tower=reservation.reach,
        prime_tower_positions={0: reservation.origin},
        prime_tower_reaches={0: reservation.reach},
        prime_tower_clearances={0: reservation.clearance},
    )
    if fits:
        write_3mf(job, tmp_path / "tower.3mf", bambu=bambu())
    else:
        with pytest.raises(ValueError, match=r"reserved clearance .*\(1.5/6.5/6.5/6.5 mm\)"):
            write_3mf(job, tmp_path / "tower.3mf", bambu=bambu())


def test_tower_positions_need_a_tower_and_valid_plates():
    with pytest.raises(ValueError, match="require a prime tower"):
        Job([Design("box", Box(1, 1, 1), {})], BUILD, "part", prime_tower_positions={0: (1, 1)})
    with pytest.raises(ValueError, match="finite"):
        Job(
            [Design("box", Box(1, 1, 1), {})],
            BUILD,
            "part",
            prime_tower=PrimeTower(),
            prime_tower_positions={0: (float("nan"), 1)},
        )


def test_auto_support_allows_catalogues_and_extras(designs):
    tile = designs[0]["tile"]
    for kind in ("catalogue", "extras"):
        validate_roof_job(Job([tile], BUILD, kind), AUTO, 0.32)
    with pytest.raises(ValueError, match="part, layout, catalogue or extras"):
        validate_roof_job(Job([tile], BUILD, "stack-review"), AUTO, 0.32)


def test_auto_part_job_turns_rail_ends_over_without_object_overrides(designs):
    tile, rail_end = designs[0]["tile"], designs[2]["rail end"]
    assert rail_end.apply_orientation_to_bambu and rail_end.recommended_print_rotation_x == 180
    validate_roof_job(Job([tile, rail_end], BUILD, "part"), AUTO, 0.32)
    assert effective_object_settings(tile, AUTO) == {}
    assert effective_object_settings(rail_end, AUTO) == {}
    with pytest.raises(ValueError, match="no retained original female pocket roofs"):
        validate_roof_job(Job([rail_end], BUILD, "part"), AUTO, 0.32)


@pytest.mark.parametrize(
    "command,message",
    [
        (
            ["catalogue", "--printer", "h2d", "--material", H2D_PETG, "PETG", "#637b70"],
            "two official",
        ),
        (["catalogue", "--printer", "h2d", *TWO_MATERIALS], "requires one official"),
        (
            ["extras", "zeekr-7x", "--printer", "h2d", "--material", H2D_PETG, "PETG", "#637b70"],
            "two official",
        ),
    ],
)
def test_cli_rejects_unsupported_auto_combinations(command, message, tmp_path, capsys):
    mode = [
        "--auto-roof-support",
    ]
    if "requires one official" in message:
        mode = []
    with pytest.raises(SystemExit) as error:
        main([*command, *BAMBU_ARGS, *mode, "--output", str(tmp_path / "job")])
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "job").exists()


@pytest.mark.parametrize("layer_height", [0.32, 0.24])
def test_h2d_auto_plan_reserves_a_tower_corner_on_every_support_plate(
    catalogue_stand_ins, layer_height
):
    default = dual_safe_catalogue_job(H2D, solid_bottom_mm=T)
    job = dual_safe_catalogue_job(
        H2D, solid_bottom_mm=T, auto_roof_support=True, layer_height_mm=layer_height
    )
    assert default.prime_tower is None and default.prime_tower_positions == {}
    plates = {}
    for design, placement in zip(job.designs, job.print_placements):
        plates.setdefault(placement.plate, []).append((design, placement))
    tower_plates = set(plates)
    assert set(job.prime_tower_positions) == tower_plates
    assert_towers_stay_where_bambu_keeps_them(job, layer_height)
    for plate in tower_plates:
        x0, _, x1, _ = job.tower_bounds(plate)
        assert 25 <= x0 and x1 <= 325
        for design, placement in plates[plate]:
            size = design.bambu_size
            width, depth = size[:2] if placement.rotation % 180 == 0 else size[1::-1]
            assert 6 - 1e-6 <= placement.y and placement.y + depth <= 314 + 1e-6
            assert placement.x + width <= 320 + 1e-6
    # The 5x5 only fits one nozzle's area, so neither H2D plan includes it or maps a nozzle.
    for plan in (default, job):
        assert not any(
            "family" not in d.parameters and (d.parameters["nx"], d.parameters["ny"]) == (5, 5)
            for d in plan.designs
        )
        assert [entry["parameters"]["nx"] for entry in plan.omitted] == [5]
        assert plan.plate_settings == {}
    policy = job.placement_policy
    assert "exception" not in policy
    assert policy["auto_roof_support"]["prime_tower_plates"] == [
        p + 1 for p in sorted(tower_plates)
    ]
    # Plates without supported parts keep the full common area.
    for plate, members in plates.items():
        if plate not in tower_plates:
            assert job.plate_builds[plate].exclusions[-1].x == 320
        else:
            k0, l0, k1, l1 = job.tower_clearance(plate).grow(job.tower_bounds(plate))
            area = job.plate_builds[plate].exclusions[-1]
            assert (area.x, area.y, area.x + area.width, area.y + area.depth) == pytest.approx(
                (k0, max(0, l0), k1, l1)
            )
    assert {d.name for d in job.designs} == {d.name for d in default.designs}


def test_plain_auto_catalogue_reserves_a_front_left_tower_corner(catalogue_stand_ins):
    build = BuildVolume(350, 320, 325)
    job = catalogue_module.catalogue_job(
        build,
        interface=Interface(solid_bottom_mm=T),
        orient_for_bambu=True,
        auto_roof_support=True,
        packing_gap=2,
    )
    assert job.prime_tower == DEFAULT_PRIME_TOWER
    (origin,) = set(job.prime_tower_positions.values())
    assert origin == (15, 15)
    x0, y0, x1, y1 = job.prime_tower.footprint(*origin)
    clear = AUTO_SUPPORT_TOWER_CLEARANCE_MM
    for design, placement in zip(job.designs, job.print_placements):
        if placement.plate in job.prime_tower_positions:
            assert placement.x >= x1 + clear - 1e-6 or placement.y >= y1 + clear - 1e-6
    supported_plates = {placement.plate for placement in job.print_placements}
    assert set(job.prime_tower_positions) == supported_plates
    assert job.part_gap == 2


AUTO_GROUP_PLATES = {
    "Tiles": 12,
    "Female ramps": 1,
    "Male ramps": 1,
    "Normal stops": 2,
    "Tile brackets - deep and shallow": 1,
    "Angled stops": 1,
    "Attachment plates": 1,
    "10mm edges and corners - complete holes": 1,
    "20mm edges and corners - complete holes": 2,
    "30mm edges and corners - complete holes": 2,
    "Rails and connectors": 2,
    "Rods and upper braces": 1,
}


def _real_group_export_job(job, group):
    indices = [i for i, design in enumerate(job.designs) if _design_group(design)[0] == group]
    designs = [job.designs[i] for i in indices]
    if group != "Tiles":
        witness = next(
            i
            for i, design in enumerate(job.designs)
            if "family" not in design.parameters
            and (design.parameters["nx"], design.parameters["ny"]) == (1, 1)
        )
        indices.append(witness)
        # A mixed Auto job needs a real female roof even when this group has none.
        designs.append(tile_design(Tile(1, 1, interface=Interface(solid_bottom_mm=T))))
    placements = [job.print_placements[i] for i in indices]
    plates = {plate: index for index, plate in enumerate(sorted({p.plate for p in placements}))}
    per_plate = (
        "plate_names",
        "plate_settings",
        "plate_builds",
        "projected_footprint_clearances",
        "prime_tower_positions",
        "prime_tower_reaches",
        "prime_tower_clearances",
    )
    return replace(
        job,
        designs=designs,
        print_placements=[replace(p, plate=plates[p.plate]) for p in placements],
        projected_footprints=[job.projected_footprints[i] for i in indices],
        placement_policy={},
        omitted=[],
        **{
            name: {plates[p]: value for p, value in getattr(job, name).items() if p in plates}
            for name in per_plate
        },
    )


def test_family_plate_counts_compose_without_mixing_groups():
    shape = Box(70, 70, 5)
    groups = [
        PlateGroup(title, [Design(f"{title}-{i}", shape, {}) for i in range(count)], 4)
        for title, count in AUTO_GROUP_PLATES.items()
    ]
    plan = plan_plates(groups, BuildVolume(100, 100, 10), size=lambda design: (70, 70, 5))
    assert plan.plate_count == sum(AUTO_GROUP_PLATES.values()) == 27
    assert list(plan.plate_names.values()) == [
        title if count == 1 else f"{title} {i + 1}"
        for title, count in AUTO_GROUP_PLATES.items()
        for i in range(count)
    ]
    assert len({p.plate for p in plan.placements}) == len(plan.designs) == 27


@pytest.mark.slow
@pytest.mark.parametrize("group", AUTO_GROUP_PLATES)
def test_real_h2d_auto_group_with_solid_bottom(tmp_path, real_catalogue_group, group):
    job = dual_safe_catalogue_job(H2D, solid_bottom_mm=T, auto_roof_support=True)
    assert real_catalogue_group
    assert len(real_catalogue_group) == len(set(real_catalogue_group))
    group_plates = {
        placement.plate
        for design, placement in zip(job.designs, job.print_placements, strict=True)
        if _design_group(design)[0] == group
    }
    assert len(group_plates) == AUTO_GROUP_PLATES[group]
    assert len(job.designs) == 131
    families = {design.parameters.get("family", "tile") for design in job.designs}
    assert families == {
        "tile",
        "edge-y",
        "corner-in",
        "corner-out",
        "ramp",
        "vertical-stop",
        "vertical-tile-bracket",
        "rod",
        "edge-x",
        "plate",
        "lock-45",
        "rod-brace",
        "support",
        "support-end",
        "support-bit",
    }
    exported = _real_group_export_job(job, group)
    report = write_3mf(exported, tmp_path / "auto.3mf", bambu=bambu())
    roof = report["roof_support"]
    assert len(roof["supported_objects"]) == len(exported.designs)
    assert {entry["family"] for entry in roof["supported_objects"]} == {
        design.parameters.get("family", "tile") for design in exported.designs
    }
    for entry in roof["supported_objects"]:
        assert entry["object_settings"] == {}
    assert len(report["prime_tower"]["plates"]) == len(exported.prime_tower_positions)
