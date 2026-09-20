"""The named collection adds straight edges, never standard catalogue members."""

import json
from collections import defaultdict
from hashlib import sha256
from math import hypot
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from cargo_grid import cli
from cargo_grid.accessories import EDGE_OUTWARD_OPTIONS_MM, Accessory, tile_matched_perimeter
from cargo_grid.catalogue import (
    H2D_DEFAULT_PART_CLEARANCE_MM,
    accessory_design,
    accessory_variants,
)
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, export_job
from cargo_grid.packing import h2d_common_build, pack_sizes
from cargo_grid.parameters import BuildVolume, Exclusion, Interface
from cargo_grid.rods import Rod, RodBrace
from cargo_grid.vehicles.zeekr_7x import extras_job, variants

COLLECTION = "zeekr-7x"
H2D = BuildVolume(350, 320, 325)


def test_standard_catalogue_inventory_and_all_accessory_ids_stay_unchanged(
    accessory_metadata_shape,
):
    specs = accessory_variants(H2D)
    assert EDGE_OUTWARD_OPTIONS_MM == (10, 20, 30)
    assert len(specs) == 105
    accessories = [spec for spec in specs if isinstance(spec, Accessory)]
    assert len(accessories) == 101
    assert all(spec.edge_outward != 40 for spec in accessories)
    assert {spec for spec in specs if isinstance(spec, Rod)} == {Rod(120), Rod(240)}
    assert {spec for spec in specs if isinstance(spec, RodBrace)} == {RodBrace(60), RodBrace(120)}
    names = [accessory_design(spec).name for spec in accessories]
    assert sha256(json.dumps(names).encode()).hexdigest() == (
        "b6ca62c04cd1b530a56a8abf9a1dd5195967841d42f5fb17de9a9f9c521ae89b"
    )


@pytest.mark.parametrize(
    "diameter,scope,complete",
    [
        (10, "full", True),
        (None, "full", False),
        (10, "interior", False),
        (8, "full", True),
        (25, "full", False),
    ],
)
def test_extras_select_one_matching_hole_mode_and_only_ten_straight_parts(
    diameter,
    scope,
    complete,
):
    specs = variants(
        H2D,
        hole_diameter=diameter,
        hole_scope=scope,
    )
    assert len(specs) == 10
    assert {(spec.family, spec.nx) for spec in specs} == {
        (family, n) for family in ("edge-x", "edge-y") for n in range(1, 6)
    }
    assert all(spec.edge_outward == 40 for spec in specs)
    assert all(spec.complete_edge_holes is complete for spec in specs)
    assert all(spec.edge_hole_diameter == (diameter if complete else None) for spec in specs)


@pytest.mark.parametrize(
    "diameter,scope,complete",
    [(10, "full", True), (8, "full", True), (None, "full", False), (10, "interior", False)],
)
def test_shared_factory_keeps_ten_mm_completion_and_extras_policy_aligned(
    diameter,
    scope,
    complete,
):
    for family in ("edge-x", "edge-y", "corner-in", "corner-out"):
        for width in (10, 20, 30):
            spec = tile_matched_perimeter(
                family,
                outward=width,
                hole_diameter=diameter,
                hole_scope=scope,
            )
            assert spec.complete_edge_holes is complete
            assert spec.edge_hole_diameter == (diameter if complete else None)


@pytest.mark.parametrize("family", ["corner-in", "corner-out"])
def test_forty_mm_corners_are_not_supported(family):
    with pytest.raises(ValueError, match="40 mm is supported only for straight"):
        Accessory(family, edge_outward=40)


def test_extras_reject_invalid_hole_policy_and_full_height():
    with pytest.raises(ValueError, match="original roofed"):
        extras_job(H2D, interface=Interface(joint_style="full-height"))
    with pytest.raises(ValueError, match="original roofed"):
        Accessory("edge-y", edge_outward=40, interface=Interface(joint_style="full-height"))
    with pytest.raises(ValueError, match="hole_scope"):
        extras_job(H2D, hole_scope="unknown")
    with pytest.raises(ValueError, match="hole_diameter"):
        extras_job(H2D, hole_diameter=-1)


def test_extras_use_actual_bounds_for_fitting_lengths_and_omissions():
    job = extras_job(BuildVolume(245, 43, 13))
    assert [(d.parameters["family"], d.parameters["nx"]) for d in job.designs] == [
        ("edge-y", n) for n in range(1, 5)
    ]
    assert len(job.omitted) == 4
    assert all(d["parameters"]["family"] == "edge-x" for d in job.omitted)
    assert all(d["size_mm"][1] == pytest.approx(46) for d in job.omitted)
    assert job.placement_policy["collection"] == COLLECTION
    with pytest.raises(ValueError, match="no supported designs fit"):
        extras_job(BuildVolume(59, 39, 13))


def test_h2d_extras_two_plates_have_all_lengths_and_common_reach(tmp_path):
    assert H2D_DEFAULT_PART_CLEARANCE_MM == 4
    gap = H2D_DEFAULT_PART_CLEARANCE_MM
    job = extras_job(H2D, placement_build=h2d_common_build(), part_gap=gap)
    assert job.plate_names == {
        0: "Zeekr 7X - Male 40mm edges",
        1: "Zeekr 7X - Female 40mm edges",
    }
    assert job.kind == "extras" and job.omitted == []
    assert job.plate_settings == {}
    assert job.projected_footprints is None
    assert job.projected_footprint_clearances == {}
    assert job.part_gap == gap
    assert job.placement_policy["collection"] == COLLECTION
    assert "exception" not in job.placement_policy
    rectangles = defaultdict(list)
    sizes = []
    for design, placement in zip(job.designs, job.print_placements):
        assert design.quantity == 1
        assert design.bambu_object_settings == {}
        assert not design.apply_orientation_to_bambu
        width, depth, height = design.shape.bounding_box().size
        sizes.append((width, depth, height))
        if placement.rotation == 90:
            width, depth = depth, width
        assert 30 <= placement.x and placement.x + width <= 320 + 1e-6
        assert 5 <= placement.y and placement.y + depth <= 315 + 1e-6
        assert height == pytest.approx(13)
        rectangles[placement.plate].append(
            (placement.x, placement.x + width, placement.y, placement.y + depth)
        )
        assert design.parameters["family"] == ("edge-x" if placement.plate == 0 else "edge-y")
    assert [d.parameters["nx"] for d in job.designs] == [1, 2, 3, 4, 5] * 2
    for bounds in rectangles.values():
        for i, first in enumerate(bounds):
            for second in bounds[i + 1 :]:
                dx = max(0, first[0] - second[1], second[0] - first[1])
                dy = max(0, first[2] - second[3], second[2] - first[3])
                assert hypot(dx, dy) >= gap - 1e-5
    common = BuildVolume(
        350,
        320,
        320,
        margin=5,
        exclusions=(Exclusion(0, 0, 30, 320), Exclusion(320, 0, 30, 320)),
    )
    assert max(p.plate for p in pack_sizes(sizes, common, gap=gap)) == 1
    manifest_path = export_job(
        job,
        tmp_path / "extras",
        stl=False,
        bambu=BambuSettings(
            (Material("Bambu PETG Basic @BBL H2D 0.8 nozzle", "PETG", "#637b70"),),
            0.8,
            0.32,
            machine_nozzle_count=2,
            printer_settings_id="Bambu Lab H2D 0.8 nozzle",
            print_settings_id="0.32mm Balanced Strength @BBL H2D 0.8 nozzle",
            printer_model="Bambu Lab H2D",
        ),
    )
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest["designs"]) == 10
    assert manifest["placement_policy"] == job.placement_policy
    for design in manifest["designs"]:
        assert design["step_roundtrip"] == "passed"
        assert design["step_volume_delta_mm3"] <= design["step_volume_budget_mm3"]
        assert design["step_bounds_delta_mm"] <= 1e-5
        assert design["mesh"]["closed_oriented_manifold"]
        assert design["compatibility"]["edge_outward_mm"] == 40
        assert design["compatibility"]["edge_hole_diameter_mm"] == 10
    with ZipFile(manifest_path.parent / "job.3mf") as archive:
        assert archive.testzip() is None
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        assert len(config.findall("object")) == 10
        plates = config.findall("plate")
        assert len(plates) == 2
        for plate in plates:
            meta = {e.get("key"): e.get("value") for e in plate.findall("metadata")}
            assert meta["filament_map_mode"] == "Auto For Match"
            assert "filament_maps" not in meta and "filament_volume_maps" not in meta
            assert len(plate.findall("model_instance")) == 5
    with pytest.raises(ValueError, match="not empty"):
        export_job(job, manifest_path.parent)


@pytest.mark.parametrize(
    "options,complete,diameter",
    [
        ([], True, 10),
        (["--no-holes"], False, None),
        (["--hole-scope", "interior"], False, None),
        (["--hole-diameter-mm", "8"], True, 8),
    ],
)
def test_cli_extras_selector_exports_only_requested_collection(
    tmp_path,
    options,
    complete,
    diameter,
):
    output = tmp_path / "extras"
    assert (
        main(
            [
                "extras",
                COLLECTION,
                "--build-width-mm",
                "65",
                "--build-depth-mm",
                "65",
                "--build-height-mm",
                "13",
                "--output",
                str(output),
                "--no-stl",
                *options,
            ]
        )
        == 0
    )
    manifest = json.loads((output / "manifest.json").read_text())
    assert len(manifest["designs"]) == 2
    assert manifest["placement_policy"]["collection"] == COLLECTION
    for design in manifest["designs"]:
        assert design["parameters"]["family"] in {"edge-x", "edge-y"}
        assert design["compatibility"]["complete_edge_holes"] is complete
        assert design["compatibility"]["edge_hole_diameter_mm"] == diameter


def test_cli_standalone_forty_mm_straight_and_corner_rejection(tmp_path, capsys):
    args = [
        "part",
        "--edge-outward-mm",
        "40",
        "--build-width-mm",
        "250",
        "--build-depth-mm",
        "50",
        "--build-height-mm",
        "13",
        "--no-stl",
    ]
    assert (
        main(
            [
                *args,
                "--family",
                "edge-x",
                "--length-cells",
                "4",
                "--output",
                str(tmp_path / "straight"),
            ]
        )
        == 0
    )
    design = json.loads((tmp_path / "straight" / "manifest.json").read_text())["designs"][0]
    assert design["size_mm"] == pytest.approx((240, 46, 13))
    with pytest.raises(SystemExit) as caught:
        main([*args, "--family", "corner-out", "--output", str(tmp_path / "corner")])
    assert caught.value.code == 2
    assert "40 mm is supported only for straight" in capsys.readouterr().err


def test_grouping_never_adds_a_plate_when_the_combined_recipe_fits_one():
    job = extras_job(BuildVolume(110, 110, 13))
    assert len(job.designs) == 2
    assert {p.plate for p in job.print_placements} == {0}
    assert job.placement_policy["grouped_by_connector_sex"] is False


@pytest.mark.parametrize("requested_gap,expected_gap", [(None, 4), (6, 6), (10, 10)])
def test_cli_h2d_extras_routes_shared_printer_settings(
    tmp_path,
    monkeypatch,
    accessory_metadata_shape,
    requested_gap,
    expected_gap,
):
    captured = []

    def capture(job, output, **settings):
        captured.append((job, settings))
        return output / "manifest.json"

    monkeypatch.setattr(cli, "export_job", capture)
    assert (
        main(
            [
                "extras",
                "zeekr-7x",
                "--h2d-dual-safe",
                "--build-width-mm",
                "350",
                "--build-depth-mm",
                "320",
                "--build-height-mm",
                "325",
                "--bambu",
                "--material",
                "Bambu PETG Basic @BBL H2D 0.8 nozzle",
                "PETG",
                "#637b70",
                "--nozzle-diameter-mm",
                "0.8",
                "--layer-height-mm",
                "0.32",
                "--output",
                str(tmp_path / "extras"),
                *([] if requested_gap is None else ["--packing-gap-mm", str(requested_gap)]),
            ]
        )
        == 0
    )
    job, settings = captured[0]
    assert len(job.designs) == 10
    assert job.build == H2D
    assert job.part_gap == expected_gap
    assert job.placement_policy["minimum_actual_part_xy_clearance_mm"] == expected_gap
    assert job.placement_policy["minimum_model_gap_mm"] == expected_gap
    assert job.projected_footprints is None
    assert job.projected_footprint_clearances == {}
    assert job.placement_policy["common_reach_mm"] == {
        "min_x": 25,
        "max_x": 325,
        "min_y": 0,
        "max_y": 320,
        "max_z": 320,
    }
    assert settings["stack"] is None
    assert settings["bambu"].machine_nozzle_count == 2
    assert settings["bambu"].roof_support is None
    assert settings["bambu"].printer_settings_id == "Bambu Lab H2D 0.8 nozzle"


@pytest.mark.parametrize(
    "command",
    [
        ["extras", "unknown"],
        ["catalogue", "--collection", "zeekr-7x-extras"],
    ],
)
def test_cli_only_accepts_the_named_extras_route(command, tmp_path):
    with pytest.raises(SystemExit) as caught:
        main(
            [
                *command,
                "--build-width-mm",
                "65",
                "--build-depth-mm",
                "65",
                "--build-height-mm",
                "13",
                "--output",
                str(tmp_path / "extras"),
            ]
        )
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "options,message",
    [
        (["--roof-support"], "roof supports require a tile-only"),
        (
            [
                "--stack-count",
                "2",
                "--stack-gap-mm",
                "0.4",
                "--stack-interface-thickness-mm",
                "0.4",
                "--stack-material-slots",
                "1",
                "1",
                "1",
            ],
            "not mixed catalogue samples",
        ),
        (["--h2d-dual-safe", "--packing-gap-mm", "0"], "H2D packing gap"),
        (["--h2d-dual-safe", "--packing-gap-mm", "-1"], "H2D packing gap"),
    ],
)
def test_extras_reject_unsupported_shared_workflows(options, message, tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "extras",
                "zeekr-7x",
                "--build-width-mm",
                "350",
                "--build-depth-mm",
                "320",
                "--build-height-mm",
                "325",
                "--bambu",
                "--material",
                "Bambu PETG Basic @BBL H2D 0.8 nozzle",
                "PETG",
                "#637b70",
                "--nozzle-diameter-mm",
                "0.8",
                "--layer-height-mm",
                "0.32",
                "--output",
                str(tmp_path / "extras"),
                *options,
            ]
        )
    assert caught.value.code == 2
    assert message in capsys.readouterr().err
