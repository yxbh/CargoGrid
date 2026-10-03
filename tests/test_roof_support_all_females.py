"""Roof enforcers under every retained female pocket, including mixed part jobs.

These are non-printing slicer requests. Support release and cleanup after printing remain
unverified; checks here cover where the requests go.
"""

import json
from dataclasses import replace
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from cargo_grid import BuildVolume, Interface, Tile
from cargo_grid.accessories import Accessory
from cargo_grid.catalogue import accessory_design
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, export_job, write_3mf
from cargo_grid.jobs import Job, tile_design
from cargo_grid.packing import PrintPlacement
from cargo_grid.rods import RodBrace
from cargo_grid.roof_support import (
    RoofSupportSettings,
    female_roofs,
    roof_enforcers,
    validate_roof_job,
)

T = 1.92  # six 0.32 mm layers
LAYER = 0.32
BUILD = BuildVolume(350, 320, 325)
MATERIALS = (Material("Model PETG", "PETG", "#778877"), Material("Interface PLA", "PLA", "#dddddd"))
CLI_BAMBU = [
    "--bambu",
    "--material",
    "Model PETG",
    "PETG",
    "#778877",
    "--material",
    "Interface PLA",
    "PLA",
    "#dddddd",
    "--nozzle-diameter-mm",
    "0.8",
    "--layer-height-mm",
    "0.32",
    "--roof-support",
]


def bambu(coverage="critical"):
    return BambuSettings(MATERIALS, 0.8, LAYER, RoofSupportSettings(coverage=coverage))


def female_design(kind, thickness):
    interface = Interface(solid_bottom_mm=thickness)
    if kind == "tile":
        return tile_design(Tile(2, 1, interface))
    spec = {
        "edge-y": Accessory("edge-y", nx=2, interface=interface),
        "corner-in v4": Accessory("corner-in", variant=4, interface=interface),
        "corner-out v3": Accessory("corner-out", variant=3, interface=interface),
        "corner-out v2 half": Accessory("corner-out", variant=2, interface=interface),
        "ramp": Accessory("ramp", nx=2, interface=interface),
    }[kind]
    return accessory_design(spec)


# (female roofs, openings under them) per design; every roof gets two critical pads.
EXPECTED = {
    "tile": ({("west", 1), ("south", 1), ("south", 2)}, {(0, 30), (30, 0), (90, 0)}),
    "edge-y": ({("south", 1), ("south", 2)}, {(30, 0), (90, 0)}),
    "corner-in v4": ({("west", 1), ("south", 1)}, {(0, 30), (30, 0)}),
    "corner-out v3": ({("west", 1), ("south", 1)}, {(60, 30), (30, 60)}),
    "corner-out v2 half": ({("south", 1)}, {(30, 0)}),
    "ramp": ({("south", 1), ("south", 2)}, set()),
}


@pytest.mark.parametrize("thickness", [0.0, T])
@pytest.mark.parametrize("kind", list(EXPECTED))
def test_every_female_roof_gets_valid_enforcers_that_bracket_its_ceiling(kind, thickness):
    design = female_design(kind, thickness)
    interface = Interface(solid_bottom_mm=thickness)
    ceiling = interface.female_opening_height
    roofs = female_roofs(design)
    sides, openings = EXPECTED[kind]
    assert {(roof.side, roof.index) for roof in roofs} == sides
    assert {point for roof in roofs for point in roof.occupied_openings} == openings
    shape_bounds = design.shape.bounding_box()
    for coverage in ("critical", "full"):
        volumes = roof_enforcers(design, LAYER, coverage)
        if coverage == "critical":
            assert len(volumes) == 2 * len(roofs)
        assert {(v.roof_side, v.roof_index) for v in volumes} == sides
        for volume in volumes:
            bounds = volume.shape.bounding_box()
            assert volume.subtype == "support_enforcer" and volume.slot == 1
            assert volume.shape.is_valid and len(volume.shape.solids()) == 1
            assert volume.shape.volume > 0
            assert bounds.min.Z == pytest.approx(ceiling - 1)
            assert bounds.max.Z == pytest.approx(ceiling + 1)
            # Enforcers sit inside the part's plan outline, under a pocket roof.
            assert bounds.min.X >= shape_bounds.min.X - 1e-6
            assert bounds.max.X <= shape_bounds.max.X + 1e-6
            assert bounds.min.Y >= shape_bounds.min.Y - 1e-6
            assert bounds.max.Y <= shape_bounds.max.Y + 1e-6
            assert volume.name.startswith(design.name + "_")


@pytest.mark.parametrize(
    "spec",
    [
        Accessory("edge-x", nx=2),
        Accessory("edge-x", nx=1, interface=Interface(solid_bottom_mm=T)),
        Accessory("corner-out", variant=6),
        Accessory("ramp", ramp_join="male"),
        Accessory("support", nx=1),
        Accessory("plate"),
        RodBrace(60),
    ],
    ids=lambda spec: f"{spec.family}",
)
def test_designs_without_female_roofs_get_no_enforcers(spec):
    design = accessory_design(spec)
    assert female_roofs(design) == []
    assert roof_enforcers(design, LAYER) == []


def test_tile_only_enforcers_are_unchanged_at_standard_thickness():
    design = tile_design(Tile(2, 1))
    critical = roof_enforcers(design, 0.2, "critical")
    suffix = [volume.name[len(design.name) + 1 :] for volume in critical]
    assert suffix == [
        "west_roof_1_critical_lower_enforcer",
        "west_roof_1_critical_upper_enforcer",
        "south_roof_1_critical_lower_enforcer",
        "south_roof_1_critical_upper_enforcer",
        "south_roof_2_critical_lower_enforcer",
        "south_roof_2_critical_upper_enforcer",
    ]
    bounds = [
        value
        for volume in critical
        for value in (*volume.shape.bounding_box().min, *volume.shape.bounding_box().max)
    ]
    assert bounds == pytest.approx(
        [
            *(1, 21, 9.2, 5.1, 24, 11.2),
            *(1, 36, 9.2, 5.1, 39, 11.2),
            *(21, 1, 9.2, 24, 5, 11.2),
            *(36, 1, 9.2, 39, 5, 11.2),
            *(81, 1, 9.2, 84, 5, 11.2),
            *(96, 1, 9.2, 99, 5, 11.2),
        ],
        abs=1e-6,
    )
    assert [v.shape.volume for v in critical] == pytest.approx([24.6, 24.6, 24, 24, 24, 24])
    full = roof_enforcers(design, 0.2, "full")
    assert [v.shape.volume for v in full] == pytest.approx(
        [156.752936248, 75.349310836, 75.349310836, 75.349310836, 75.349310836]
    )


def test_tile_only_manifest_keeps_its_target_description(tmp_path):
    job = Job([tile_design(Tile(1, 1))], BuildVolume(150, 150, 50), "part")
    report = write_3mf(job, tmp_path / "tile.3mf", bambu=bambu())
    roof = report["roof_support"]
    assert roof["target"] == (
        "retained west (negative-X) and south (negative-Y) original female pocket roofs"
    )
    assert all(
        set(target) == {"design", "batch", "side", "roof_indices", "roof_count", "enforcer_count"}
        for target in roof["targets"]
    )


def one_plate_job(designs, positions):
    return Job(
        designs,
        BUILD,
        "part",
        print_placements=[PrintPlacement(0, x, y, 0) for x, y in positions],
    )


def plate_parts(path):
    with ZipFile(path) as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
    result = {}
    for obj in config.findall("./object"):
        name = next(m.get("value") for m in obj.findall("metadata") if m.get("key") == "name")
        # Bambu objects are named after their design plus the batch they print in.
        result[name.rsplit("_batch_", 1)[0]] = [part.get("subtype") for part in obj.findall("part")]
    return result, config


@pytest.mark.parametrize("thickness", [0.0, T])
def test_mixed_one_plate_job_puts_enforcers_only_under_female_roofs(thickness, tmp_path):
    interface = Interface(solid_bottom_mm=thickness)
    tile = tile_design(Tile(1, 1, interface))
    second = replace(tile, name=tile.name + "_copy2", display_name="Tile copy 2")
    edge = accessory_design(Accessory("edge-x", interface=interface))
    job = one_plate_job([tile, second, edge], [(20, 20), (110, 20), (20, 110)])
    path = tmp_path / "mixed.3mf"
    report = write_3mf(job, path, bambu=bambu())
    assert [plate["number"] for plate in report["plates"]] == [1]
    assert report["plates"][0]["quantity"] == 3
    roof = report["roof_support"]
    assert {target["design"] for target in roof["targets"]} == {tile.name, second.name}
    assert roof["untargeted_designs"] == [edge.name]
    assert roof["enforcer_count"] == 8
    parts, config = plate_parts(path)
    assert parts[edge.name] == ["normal_part"]
    for design in (tile, second):
        subtypes = parts[design.display_name or design.name]
        assert subtypes.count("normal_part") == 1 and subtypes.count("support_enforcer") == 4
    assert len(config.findall("./plate")) == 1


def test_mixed_part_job_records_non_tile_targets_and_occupied_openings(tmp_path):
    interface = Interface(solid_bottom_mm=T)
    tile = tile_design(Tile(1, 1, interface))
    edge = accessory_design(Accessory("edge-y", interface=interface))
    ramp = accessory_design(Accessory("ramp", interface=interface))
    job = one_plate_job([tile, edge, ramp], [(20, 20), (110, 20), (20, 110)])
    manifest = json.loads(
        (export_job(job, tmp_path / "job", stl=False, bambu=bambu("full"))).read_text()
    )
    roof = manifest["export"]["roof_support"]
    assert roof["target"].startswith("every retained original female pocket roof")
    targets = {(t["design"], t["side"]): t for t in roof["targets"]}
    assert set(targets) == {
        (tile.name, "west"),
        (tile.name, "south"),
        (edge.name, "south"),
        (ramp.name, "south"),
    }
    assert "family" not in targets[tile.name, "west"]
    assert targets[edge.name, "south"]["family"] == "edge-y"
    assert targets[edge.name, "south"]["support_may_occupy_openings"] == [[30.0, 0.0]]
    assert targets[ramp.name, "south"]["support_may_occupy_openings"] == []
    assert roof["untargeted_designs"] == []


def test_job_without_female_roofs_is_rejected():
    job = Job([accessory_design(Accessory("edge-x"))], BUILD, "part")
    with pytest.raises(ValueError, match="no retained original female pocket roofs"):
        validate_roof_job(job, RoofSupportSettings(), LAYER)


@pytest.mark.parametrize(
    "spec", [Accessory("plate"), Accessory("support-end", variant=1)], ids=["plate", "rail-end"]
)
def test_oriented_models_stay_out_of_roof_jobs(spec, tmp_path):
    oriented = accessory_design(spec)
    job = one_plate_job([tile_design(Tile(1, 1)), oriented], [(20, 20), (110, 20)])
    with pytest.raises(
        ValueError, match="Bambu-oriented models cannot use stacking or roof"
    ) as error:
        write_3mf(job, tmp_path / "plate.3mf", bambu=bambu())
    assert oriented.name in str(error.value)
    assert "--roof-support-mode auto" in str(error.value)
    assert not (tmp_path / "plate.3mf").exists()


def test_full_height_designs_stay_out_of_roof_jobs():
    full = Interface(joint_style="full-height")
    job = Job(
        [tile_design(Tile(1, 1)), accessory_design(Accessory("edge-x", interface=full))],
        BUILD,
        "part",
    )
    with pytest.raises(ValueError, match="original roofed joints"):
        validate_roof_job(job, RoofSupportSettings(), LAYER)


@pytest.mark.parametrize(
    "extra,message",
    [
        (["--family", "edge-x"], "no retained original female pocket roofs"),
        (["--family", "plate"], "Bambu-oriented models"),
        (["--family", "support-end", "--variant-number", "3"], "Bambu-oriented models"),
    ],
)
def test_cli_rejects_parts_without_supportable_roofs(extra, message, tmp_path, capsys):
    build = ["--build-width-mm", "350", "--build-depth-mm", "320", "--build-height-mm", "325"]
    with pytest.raises(SystemExit) as error:
        main(["part", *build, *CLI_BAMBU, *extra, "--output", str(tmp_path / "job")])
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "job").exists()


def test_cli_female_edge_part_gets_roof_enforcers(tmp_path):
    output = tmp_path / "edge"
    command = [
        "part",
        "--build-width-mm",
        "150",
        "--build-depth-mm",
        "150",
        "--build-height-mm",
        "50",
        "--family",
        "edge-y",
        "--length-cells",
        "1",
        "--solid-bottom-thickness-mm",
        "1.92",
        *CLI_BAMBU,
        "--no-stl",
        "--output",
        str(output),
    ]
    assert main(command) == 0
    roof = json.loads((output / "manifest.json").read_text())["export"]["roof_support"]
    (target,) = roof["targets"]
    assert target["side"] == "south" and target["family"] == "edge-y"
    assert target["enforcer_count"] == 2
    assert target["support_may_occupy_openings"] == [[30.0, 0.0]]
