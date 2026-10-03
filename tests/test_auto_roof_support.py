"""Auto roof support: object-scoped normal Auto support with a PLA interface.

These checks cover the generated settings, per-object flags and prime-tower reservations.
Local Bambu Studio slicing is a separate check; support release, tower stability and print
results remain unverified.
"""

import json
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from build123d import Box

from cargo_grid import BuildVolume, Interface, Tile
from cargo_grid import catalogue as catalogue_module
from cargo_grid import plates as plates_module
from cargo_grid.accessories import Accessory
from cargo_grid.catalogue import accessory_design, h2d_dual_safe_catalogue_job
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, export_job, write_3mf
from cargo_grid.jobs import Design, Job, tile_design, tile_identity
from cargo_grid.packing import PrimeTower, PrintPlacement
from cargo_grid.parameters import Exclusion
from cargo_grid.plates import (
    AUTO_SUPPORT_TOWER_CLEARANCE_MM,
    DEFAULT_PRIME_TOWER,
    H2D_AUTO_SUPPORT_TOWER_ORIGIN,
    needs_auto_support,
)
from cargo_grid.rods import Rod, RodBrace
from cargo_grid.roof_support import (
    OBJECT_AUTO_SUPPORT,
    RoofSupportSettings,
    auto_support_reason,
    effective_object_settings,
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
AUTO = RoofSupportSettings(mode="auto")
BUILD = BuildVolume(350, 320, 325)
H2D_ARGS = [
    "--build-width-mm",
    "350",
    "--build-depth-mm",
    "320",
    "--build-height-mm",
    "325",
    "--bambu",
    "--nozzle-diameter-mm",
    "0.8",
    "--layer-height-mm",
    "0.32",
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


def test_auto_mode_keeps_the_zero_contact_keys_with_global_support_off():
    settings = AUTO.native_settings()
    assert settings == {
        "enable_support": "0",
        "support_filament": "1",
        "support_interface_filament": "2",
        "support_on_build_plate_only": "0",
        "support_interface_top_layers": "2",
        "support_top_z_distance": "0",
        "support_interface_spacing": "0",
        "independent_support_layer_height": "0",
        "support_object_xy_distance": "0.4",
    }
    assert "enable_support" not in AUTO.process_override_keys
    assert {
        "support_top_z_distance",
        "independent_support_layer_height",
        "support_interface_spacing",
        "support_interface_top_layers",
        "support_on_build_plate_only",
    } <= AUTO.process_override_keys


def test_painted_mode_settings_and_key_order_are_unchanged():
    assert list(RoofSupportSettings().native_settings()) == [
        "enable_support",
        "support_type",
        "support_filament",
        "support_interface_filament",
        "support_on_build_plate_only",
        "support_interface_top_layers",
        "support_top_z_distance",
        "support_interface_spacing",
        "independent_support_layer_height",
        "support_object_xy_distance",
    ]
    assert RoofSupportSettings().native_settings()["support_type"] == "normal(manual)"


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"mode": "tree"}, "painted or auto"),
        ({"mode": "auto", "coverage": "full"}, "auto support covers whole roofs"),
    ],
)
def test_invalid_auto_settings_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        RoofSupportSettings(**kwargs)


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
    }
    return female, existing, plain


def test_object_support_follows_female_roofs_and_existing_object_support(designs):
    female, existing, plain = designs
    for design in female.values():
        assert auto_support_reason(design, AUTO) == "retained original female pocket roof"
        assert effective_object_settings(design, AUTO) == OBJECT_AUTO_SUPPORT
        assert needs_auto_support(design)
    for design in existing.values():
        assert design.bambu_object_settings == OBJECT_AUTO_SUPPORT
        assert auto_support_reason(design, AUTO).startswith("object-scoped normal Auto")
        assert needs_auto_support(design)
    for name, design in plain.items():
        assert auto_support_reason(design, AUTO) is None, name
        assert effective_object_settings(design, AUTO) == {}
        assert not needs_auto_support(design)
    # Painted roof jobs leave every accessory's own settings alone.
    painted = RoofSupportSettings()
    assert effective_object_settings(female["tile"], painted) == {}
    assert effective_object_settings(existing["rod"], painted) == OBJECT_AUTO_SUPPORT


def test_one_plate_auto_job_writes_object_flags_tower_and_no_enforcers(designs, tmp_path):
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
    assert flags[female["tile"].name] == ("1", "normal(auto)", ["normal_part"])
    assert flags[existing["vertical-stop"].name] == ("1", "normal(auto)", ["normal_part"])
    assert flags[plain["edge-x"].name] == (None, None, ["normal_part"])
    assert flags[plain["plate"].name] == (None, None, ["normal_part"])
    assert project["enable_support"] == "0" and "support_type" not in project
    assert project["support_interface_filament"] == "2"
    # Bambu's default tower is used: only the per-plate position is written.
    assert project["wipe_tower_x"] == ["290.5"] and project["wipe_tower_y"] == ["8"]
    overrides = set(project["different_settings_to_system"][0].split(";"))
    assert {"wipe_tower_x", "wipe_tower_y"} <= overrides
    for key in TOWER_SETTINGS:
        assert key not in project and key not in overrides
    export = manifest["export"]
    roof = export["roof_support"]
    assert roof["mode"] == "auto" and roof["global_enable_support"] is False
    assert roof["enforcer_count"] == 0
    supported = {entry["design"]: entry for entry in roof["supported_objects"]}
    assert set(supported) == {female["tile"].name, existing["vertical-stop"].name}
    assert supported[female["tile"].name]["support_may_occupy_openings"] == [
        [0.0, 30.0],
        [30.0, 0.0],
    ]
    assert set(roof["unsupported_designs"]) == {plain["edge-x"].name, plain["plate"].name}
    (plate,) = export["prime_tower"]["plates"]
    assert plate["origin_mm"] == [290.5, 8]
    assert plate["reserved_extrusion_bounds_mm"] == [283, 1, 325, 39.5]
    assert "settings" not in export["prime_tower"]
    assert export["prime_tower"]["tower"].startswith("Bambu default")
    assert export["plates"][0]["items"][0]["object_settings"] == OBJECT_AUTO_SUPPORT


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


def test_auto_mode_allows_catalogues_and_extras_but_painted_mode_does_not(designs):
    tile = designs[0]["tile"]
    for kind in ("catalogue", "extras"):
        validate_roof_job(Job([tile], BUILD, kind), AUTO, 0.32)
        with pytest.raises(ValueError, match="catalogues and extras are not supported"):
            validate_roof_job(Job([tile], BUILD, kind), RoofSupportSettings(), 0.32)
    with pytest.raises(ValueError, match="part, layout, catalogue or extras"):
        validate_roof_job(Job([tile], BUILD, "stack-review"), AUTO, 0.32)


@pytest.mark.parametrize(
    "command,message",
    [
        (
            ["catalogue", "--h2d-dual-safe", "--material", H2D_PETG, "PETG", "#637b70"],
            "two official",
        ),
        (
            ["catalogue", "--h2d-dual-safe", *TWO_MATERIALS, "--roof-coverage", "full"],
            "--roof-coverage applies to painted",
        ),
        (["catalogue", "--h2d-dual-safe", *TWO_MATERIALS], "requires one official"),
        (
            ["extras", "zeekr-7x", "--h2d-dual-safe", "--material", H2D_PETG, "PETG", "#637b70"],
            "two official",
        ),
    ],
)
def test_cli_rejects_unsupported_auto_combinations(command, message, tmp_path, capsys):
    mode = ["--roof-support", "--roof-support-mode", "auto"]
    if command[1] == "--h2d-dual-safe" and "requires one official" in message:
        mode = []
    with pytest.raises(SystemExit) as error:
        main([*command, *H2D_ARGS, *mode, "--output", str(tmp_path / "job")])
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "job").exists()


def test_cli_mode_needs_roof_support(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(
            [
                "part",
                *H2D_ARGS,
                *TWO_MATERIALS,
                "--roof-support-mode",
                "auto",
                "--output",
                str(tmp_path / "x"),
            ]
        )
    assert "require --roof-support" in capsys.readouterr().err


@pytest.fixture
def stubbed_h2d(monkeypatch):
    """Stand-in tiles and accessories with real sizes; family decides which need support."""

    def fake_tile(tile):
        size = (tile.nx * 60 + 6, tile.ny * 60 + 6, 13 + tile.interface.solid_bottom_mm)
        name, parameters = tile_identity(tile)
        return Design(name, Box(*size).moved(_corner(size)), parameters)

    monkeypatch.setattr(catalogue_module, "tile_design", fake_tile)
    monkeypatch.setattr(catalogue_module, "make_accessory", lambda spec: Box(20, 30, 10))
    supported = {"tile", "edge-y", "corner-in", "corner-out", "ramp", "vertical-stop", "rod"}

    def fake_needs(design):
        family = design.parameters.get("family", "tile")
        if family == "ramp" and design.parameters.get("ramp_join") == "male":
            return False
        return family in supported

    monkeypatch.setattr(plates_module, "needs_auto_support", fake_needs)
    return fake_needs


def _corner(size):
    from build123d import Location

    return Location(tuple(value / 2 for value in size))


def test_h2d_auto_plan_reserves_a_tower_corner_on_every_support_plate(stubbed_h2d):
    default = h2d_dual_safe_catalogue_job(solid_bottom_mm=T)
    job = h2d_dual_safe_catalogue_job(solid_bottom_mm=T, auto_roof_support=True)
    assert default.prime_tower is None and default.prime_tower_positions == {}
    assert job.prime_tower == DEFAULT_PRIME_TOWER
    plates = {}
    for design, placement in zip(job.designs, job.print_placements):
        plates.setdefault(placement.plate, []).append((design, placement))
    tower_plates = {
        plate
        for plate, members in plates.items()
        if any(stubbed_h2d(design) for design, _ in members)
    }
    assert set(job.prime_tower_positions) == tower_plates
    x0, y0, x1, y1 = DEFAULT_PRIME_TOWER.footprint(*H2D_AUTO_SUPPORT_TOWER_ORIGIN)
    assert (x0, y0, x1, y1) == (283, 1, 325, 39.5)
    assert 25 <= x0 and x1 <= 325
    clear = AUTO_SUPPORT_TOWER_CLEARANCE_MM
    for plate in tower_plates:
        assert job.prime_tower_positions[plate] == H2D_AUTO_SUPPORT_TOWER_ORIGIN
        for design, placement in plates[plate]:
            size = design.bambu_size
            width, depth = size[:2] if placement.rotation == 0 else size[1::-1]
            assert 6 - 1e-6 <= placement.y and placement.y + depth <= 314 + 1e-6
            assert placement.x + width <= 320 + 1e-6
            assert placement.x + width + clear <= x0 + 1e-6 or placement.y >= y1 + clear - 1e-6
        assert job.prime_tower_clearances[plate] == clear
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
            assert job.plate_builds[plate].exclusions[-1] == Exclusion(
                x0 - clear, 0, x1 - x0 + 2 * clear, y1 + clear
            )
    assert {d.name for d in job.designs} == {d.name for d in default.designs}


def test_plain_auto_catalogue_reserves_a_front_left_tower_corner(stubbed_h2d):
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
    x0, y0, x1, y1 = job.prime_tower.footprint(*origin)
    assert (x0, y0) == (0, 0)
    clear = AUTO_SUPPORT_TOWER_CLEARANCE_MM
    for design, placement in zip(job.designs, job.print_placements):
        if placement.plate in job.prime_tower_positions:
            assert placement.x >= x1 + clear - 1e-6 or placement.y >= y1 + clear - 1e-6
    supported_plates = {
        placement.plate
        for design, placement in zip(job.designs, job.print_placements)
        if stubbed_h2d(design)
    }
    assert set(job.prime_tower_positions) == supported_plates
    assert job.part_gap == 2


@pytest.mark.slow
def test_real_h2d_auto_plan_with_solid_bottom(tmp_path):
    job = h2d_dual_safe_catalogue_job(solid_bottom_mm=T, auto_roof_support=True)
    assert max(p.plate for p in job.print_placements) + 1 == 26
    report = write_3mf(job, tmp_path / "auto.3mf", bambu=bambu())
    roof = report["roof_support"]
    assert len(job.designs) == 129
    assert len(roof["supported_objects"]) == 74
    for entry in roof["supported_objects"]:
        assert entry["object_settings"] == OBJECT_AUTO_SUPPORT
    families = {entry["family"] for entry in roof["supported_objects"]}
    assert families == {
        "tile",
        "edge-y",
        "corner-in",
        "corner-out",
        "ramp",
        "vertical-stop",
        "vertical-tile-bracket",
        "rod",
    }
    assert len(report["prime_tower"]["plates"]) == len(job.prime_tower_positions) == 22
