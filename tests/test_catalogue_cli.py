import hashlib
import json
from dataclasses import asdict
from zipfile import ZipFile

import pytest
from build123d import Box

from cargo_grid import cli
from cargo_grid.accessories import (
    Accessory,
    BambuPrintPolicy,
    bambu_print_policy,
    bambu_print_rotation,
    bambu_print_rotation_y,
    required_bambu_object_settings,
)
from cargo_grid.catalogue import (
    accessory_design,
    accessory_identity,
    accessory_variants,
    tile_sizes,
)
from cargo_grid.cli import _resolved_hole_diameter, main, parser
from cargo_grid.jobs import Design, tile_identity
from cargo_grid.parameters import BuildVolume, Tile
from cargo_grid.tiles import hole_placements


def test_complete_ordered_tile_family():
    assert set(tile_sizes(BuildVolume(246, 246, 13))) == {
        (x, y) for x in range(1, 5) for y in range(1, 5)
    }
    assert (5, 1) in tile_sizes(BuildVolume(306, 126, 13))
    assert (1, 5) in tile_sizes(BuildVolume(306, 126, 13))
    assert (5, 5) not in tile_sizes(BuildVolume(306, 126, 13))


def test_standard_catalogue_identity_payload_is_frozen_without_building_geometry():
    build = BuildVolume(350, 320, 325)
    records = []
    for x, y in tile_sizes(build):
        name, parameters = tile_identity(Tile(x, y))
        records.append({"name": name, "parameters": parameters})
    records.extend(
        {"name": name, "parameters": parameters}
        for name, parameters in (accessory_identity(spec) for spec in accessory_variants(build))
    )
    assert len(records) == 130
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"))
    assert (
        hashlib.sha256(payload.encode()).hexdigest()
        == "ce06ed147f704b78915c7cee4ac1b18ec83987df64402ad2e3001e5fe4bb588c"
    )


def test_typed_and_serialized_accessory_print_policies_agree_for_full_inventory():
    for spec in accessory_variants(BuildVolume(350, 320, 325)):
        _, parameters = accessory_identity(spec)
        assert bambu_print_policy(parameters) == BambuPrintPolicy(
            bambu_print_rotation(spec),
            bambu_print_rotation_y(spec),
            required_bambu_object_settings(parameters),
        )


def test_ramp_design_identity_preserves_legacy_female_and_non_ramp_names(
    accessory_metadata_shape,
):
    female = accessory_design(Accessory("ramp", nx=3))
    explicit_female = accessory_design(Accessory("ramp", nx=3, ramp_join="female"))
    male = accessory_design(Accessory("ramp", nx=3, ramp_join="male"))
    plate = accessory_design(Accessory("plate"))
    assert female.name == explicit_female.name == "ramp_3x1_v1_original_d01b948fe4"
    assert female.parameters == explicit_female.parameters
    assert "ramp_join" not in female.parameters
    assert "ramp_join" not in plate.parameters
    assert plate.name == "plate_1x1_v1_original_d7790348d5"
    assert male.name.startswith("ramp_3x1_male_v1_original_")
    assert male.parameters == {**female.parameters, "ramp_join": "male"}


def test_bracket_design_names_and_pose_metadata(accessory_metadata_shape):
    a = accessory_design(Accessory("vertical-tile-bracket", nx=1, ny=2))
    b = accessory_design(Accessory("vertical-tile-bracket", nx=2, ny=1))
    assert a.name != b.name
    assert a.display_name == "Deep tall tile bracket — floor 1x2, wall 1x2"
    assert b.display_name == "Wide low tile bracket — floor 2x1, wall 2x1"
    assert a.apply_orientation_to_bambu
    assert a.recommended_print_rotation_x == pytest.approx(134.22827708427317)
    assert a.shape.label == a.name
    assert b.shape.label == b.name


@pytest.mark.parametrize(
    "extra",
    [
        ["--stack-count", "2"],
        ["--bambu"],
        ["--material", "Unknown", "PETG", "#ffffff"],
    ],
)
def test_cli_rejects_incomplete_settings(tmp_path, extra):
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "part",
                "--build-width-mm",
                "150",
                "--build-depth-mm",
                "150",
                "--build-height-mm",
                "50",
                "--output",
                str(tmp_path / "job"),
                *extra,
            ]
        )
    assert caught.value.code == 2


PHYSICAL_HOLE_OPTIONS = [
    pytest.param([], 10, "full", 8, id="default"),
    pytest.param(["--no-holes"], None, "full", 0, id="solid"),
    pytest.param(
        ["--hole-diameter-mm", "8", "--hole-scope", "interior"],
        8,
        "interior",
        0,
        id="interior",
    ),
]
HOLE_OPTIONS = [
    *PHYSICAL_HOLE_OPTIONS,
    pytest.param(["--holes"], 10, "full", 8, id="alias"),
]


def tile_command(output):
    return [
        "part",
        "--build-width-mm",
        "150",
        "--build-depth-mm",
        "150",
        "--build-height-mm",
        "50",
        "--no-stl",
        "--output",
        str(output),
    ]


@pytest.mark.parametrize("extra,diameter,scope,holes", HOLE_OPTIONS)
def test_cli_hole_options_reach_tile_construction(
    extra, diameter, scope, holes, tmp_path, monkeypatch
):
    captured = []
    exported = []
    output = tmp_path / "captured"
    command = [*tile_command(output), *extra]
    args = parser().parse_args(command)
    assert _resolved_hole_diameter(args) == diameter
    assert vars(args).get("holes") == (
        True if "--holes" in extra else False if "--no-holes" in extra else None
    )
    expected = Tile(1, 1, hole_diameter=diameter, hole_scope=scope)

    def capture_tile(spec):
        captured.append(spec)
        return Design(
            "captured_tile",
            Box(2, 3, 4),
            asdict(spec),
            holes=[asdict(hole) for hole in hole_placements(spec)],
        )

    def capture_export(job, path, **kwargs):
        exported.append((job, path, kwargs))
        return path / "manifest.json"

    monkeypatch.setattr(cli, "tile_design", capture_tile)
    monkeypatch.setattr(cli, "export_job", capture_export)
    assert main(command) == 0
    assert captured == [expected]
    assert len(exported) == 1
    job, path, settings = exported[0]
    assert path == output
    assert settings == {"stl": False, "bambu": None, "stack": None}
    assert job.kind == "part" and len(job.designs) == 1
    assert job.designs[0].parameters == asdict(expected)
    assert sum(hole["accepted"] for hole in job.designs[0].holes) == holes
    assert not output.exists()


@pytest.mark.parametrize("extra,diameter,scope,holes", PHYSICAL_HOLE_OPTIONS)
def test_cli_unique_hole_configurations_export_manifest(extra, diameter, scope, holes, tmp_path):
    output = tmp_path / "job"
    command = [*tile_command(output), *extra]
    assert main(command) == 0
    manifest = json.loads((output / "manifest.json").read_text())
    design = manifest["designs"][0]
    assert design["parameters"] == asdict(Tile(1, 1, hole_diameter=diameter, hole_scope=scope))
    assert sum(hole["accepted"] for hole in design["hole_placements"]) == holes
    assert (output / f"{design['name']}.step").is_file()
    assert not list(output.glob("*.stl"))
    with ZipFile(output / "job.3mf") as archive:
        assert archive.testzip() is None
        assert "3D/3dmodel.model" in archive.namelist()


def test_accessory_part_ignores_default_tile_holes_but_rejects_explicit_holes(tmp_path):
    common = [
        "part",
        "--family",
        "plate",
        "--build-width-mm",
        "150",
        "--build-depth-mm",
        "150",
        "--build-height-mm",
        "50",
        "--no-stl",
    ]
    assert main([*common, "--output", str(tmp_path / "plain")]) == 0
    with pytest.raises(SystemExit) as caught:
        main([*common, "--holes", "--output", str(tmp_path / "invalid")])
    assert caught.value.code == 2


def test_perimeter_cli_resolves_matching_diameter_and_plain_override(tmp_path):
    common = [
        "part",
        "--family",
        "edge-y",
        "--edge-outward-mm",
        "20",
        "--build-width-mm",
        "150",
        "--build-depth-mm",
        "150",
        "--build-height-mm",
        "50",
        "--output",
        str(tmp_path / "unused"),
    ]
    custom = parser().parse_args([*common, "--hole-diameter-mm", "8"])
    assert _resolved_hole_diameter(custom) == 8
    assert custom.complete_edge_holes is None
    plain = parser().parse_args([*common, "--plain-edge"])
    assert _resolved_hole_diameter(plain) is None
    assert plain.complete_edge_holes is False
    incompatible = parser().parse_args([*common, "--plain-edge", "--hole-diameter-mm", "8"])
    with pytest.raises(ValueError, match="cannot be combined with --plain-edge"):
        _resolved_hole_diameter(incompatible)
