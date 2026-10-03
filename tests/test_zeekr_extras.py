"""The Zeekr 7X expansion set: 40 mm straight edges plus the rear-panel contour pieces."""

import json
from collections import defaultdict
from hashlib import sha256
from math import hypot
from types import SimpleNamespace
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from build123d import Box

from cargo_grid import cli
from cargo_grid.accessories import EDGE_OUTWARD_OPTIONS_MM, Accessory, tile_matched_perimeter
from cargo_grid.catalogue import (
    H2D_DEFAULT_PART_CLEARANCE_MM,
    accessory_design,
    accessory_variants,
)
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, export_job
from cargo_grid.jobs import Design
from cargo_grid.packing import h2d_common_build
from cargo_grid.parameters import BuildVolume, Exclusion, Interface, interface_parameters
from cargo_grid.rods import Rod, RodBrace
from cargo_grid.vehicles import zeekr_7x, zeekr_7x_rear_review
from cargo_grid.vehicles.zeekr_7x import CONTOUR_GAP_MM, extras_job, variants

COLLECTION = "zeekr-7x"
H2D = BuildVolume(350, 320, 325)
H2D_OPTIONS = [
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
]
H2D_BAMBU = BambuSettings(
    (Material("Bambu PETG Basic @BBL H2D 0.8 nozzle", "PETG", "#637b70"),),
    0.8,
    0.32,
    printer_settings_id="Bambu Lab H2D 0.8 nozzle",
    print_settings_id="0.32mm Balanced Strength @BBL H2D 0.8 nozzle",
    bed_type="Textured PEI Plate",
    machine_nozzle_count=2,
    printer_model="Bambu Lab H2D",
)
# Actual generated bounds of the measured contour pieces, rounded to 1 um. The slow real-set
# test checks that stand-ins with these bounds produce the same plates as the real pieces.
SIDE_CAP_SIZES = {
    ("west", "north"): (68.5, 147.673, 13),
    ("west", "south"): (68.565, 171.152, 13),
    ("east", "north"): (62.5, 147.673, 13),
    ("east", "south"): (62.565, 171.152, 13),
}
SOUTH_RAMP_SIZES = (
    (240, 91.397, 13),
    (240, 100.638, 13),
    (120, 101.0, 13),
    (240, 100.638, 13),
    (240, 91.397, 13),
)
PLATES = {
    0: ("Zeekr 7X - Male 40mm edges", {"edge-x"}, 5),
    1: ("Zeekr 7X - Female 40mm edges", {"edge-y"}, 5),
    2: ("Zeekr 7X - Rear panel contour side caps", {"zeekr-rear-contour-side"}, 4),
    3: ("Zeekr 7X - Rear panel south contour ramps 1", {"zeekr-rear-contour-ramp"}, 2),
    4: ("Zeekr 7X - Rear panel south contour ramps 2", {"zeekr-rear-contour-ramp"}, 3),
}
GROUP_GAPS = {
    "Zeekr 7X - Male 40mm edges": H2D_DEFAULT_PART_CLEARANCE_MM,
    "Zeekr 7X - Female 40mm edges": H2D_DEFAULT_PART_CLEARANCE_MM,
    "Zeekr 7X - Rear panel contour side caps": CONTOUR_GAP_MM,
    "Zeekr 7X - Rear panel south contour ramps": CONTOUR_GAP_MM,
}
STANDARD_PARTS = [
    {"family": "tile", "width_cells": 4, "depth_cells": 4, "quantity": 4},
    {"family": "tile", "width_cells": 2, "depth_cells": 4, "quantity": 1},
    {"family": "edge-y", "length_cells": 4, "edge_outward_mm": 30.0, "quantity": 4},
    {"family": "edge-y", "length_cells": 2, "edge_outward_mm": 30.0, "quantity": 1},
]


def _stand_in_contours(side_sizes=SIDE_CAP_SIZES, ramp_sizes=SOUTH_RAMP_SIZES):
    """Contour pieces with the real names and bounds, without their slow construction."""

    def designs(parameters):
        interface = interface_parameters(parameters.interface)
        caps = [
            Design(
                f"zeekr_rear_{side}_{segment}_cap",
                Box(*side_sizes[side, segment]),
                {
                    "family": "zeekr-rear-contour-side",
                    "side": side,
                    "segment": segment,
                    "interface": interface,
                },
                display_name=(
                    f"{side.title()} {'male' if side == 'west' else 'female'} "
                    f"contour cap - {segment} segment"
                ),
            )
            for side in ("west", "east")
            for segment in ("north", "south")
        ]
        ramps = [
            Design(
                f"zeekr_rear_south_ramp_{index + 1}_{cells}cell",
                Box(*ramp_sizes[index]),
                {"family": "zeekr-rear-contour-ramp", "width_cells": cells, "interface": interface},
                display_name=f"South male contour ramp {index + 1} - {cells} cells",
            )
            for index, cells in enumerate(zeekr_7x_rear_review.TEST_TILE_MODULES)
        ]
        return caps, ramps

    return designs


@pytest.fixture
def contour_stand_ins(monkeypatch):
    monkeypatch.setattr(zeekr_7x_rear_review, "rear_panel_designs", _stand_in_contours())


@pytest.fixture(scope="module")
def expansion_plan():
    """Real 40 mm edges with contour stand-ins, planned once for read-only checks."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(zeekr_7x_rear_review, "rear_panel_designs", _stand_in_contours())
        job = extras_job(
            H2D,
            placement_build=h2d_common_build(),
            edge_gap=H2D_DEFAULT_PART_CLEARANCE_MM,
        )
    return job


def _plate_rectangles(job):
    rectangles = defaultdict(list)
    for design, placement in zip(job.designs, job.print_placements, strict=True):
        width, depth, height = design.size
        if placement.rotation == 90:
            width, depth = depth, width
        assert placement.rotation in (0, 90)
        assert 30 <= placement.x and placement.x + width <= 320 + 1e-6
        assert 5 <= placement.y and placement.y + depth <= 315 + 1e-6
        assert height == pytest.approx(13)
        rectangles[placement.plate].append(
            (placement.x, placement.x + width, placement.y, placement.y + depth)
        )
    return rectangles


def _assert_group_clearances(job):
    for plate, bounds in _plate_rectangles(job).items():
        name = job.plate_names[plate]
        gap = next(value for group, value in GROUP_GAPS.items() if name.startswith(group))
        for index, first in enumerate(bounds):
            for second in bounds[index + 1 :]:
                dx = max(0, first[0] - second[1], second[0] - first[1])
                dy = max(0, first[2] - second[3], second[2] - first[3])
                assert hypot(dx, dy) >= gap - 1e-5


def _plate_members(job):
    members = defaultdict(list)
    for design, placement in zip(job.designs, job.print_placements, strict=True):
        members[placement.plate].append(design.name)
    return dict(members)


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


@pytest.mark.parametrize(
    "settings,message",
    [
        ({"interface": Interface(joint_style="full-height")}, "original roofed"),
        ({"interface": Interface(pitch=50)}, "standard 60 mm unit"),
        ({"interface": Interface(height=15)}, "standard 60 mm unit"),
        ({"interface": Interface(fit_offset=0.1)}, "standard 60 mm unit"),
        ({"hole_diameter": None}, "full-scope 10 mm hole pattern"),
        ({"hole_diameter": 8}, "full-scope 10 mm hole pattern"),
        ({"hole_scope": "interior"}, "full-scope 10 mm hole pattern"),
        ({"hole_scope": "unknown"}, "full-scope 10 mm hole pattern"),
        ({"edge_gap": -1}, "edge packing gap"),
        ({"contour_gap": 0}, "contour packing gap"),
    ],
)
def test_expansion_set_rejects_nonstandard_settings_before_building(
    monkeypatch,
    settings,
    message,
):
    def unexpected(*args, **kwargs):
        raise AssertionError("geometry was built before the settings were checked")

    monkeypatch.setattr(zeekr_7x, "accessory_design", unexpected)
    monkeypatch.setattr(zeekr_7x_rear_review, "rear_panel_designs", unexpected)
    with pytest.raises(ValueError, match=message):
        extras_job(H2D, **settings)


def test_expansion_set_omits_only_straight_edges_that_do_not_fit(contour_stand_ins):
    build = BuildVolume(305, 290, 13, exclusions=(Exclusion(0, 0, 10, 290),))
    job = extras_job(build)
    assert [(d["parameters"]["family"], d["parameters"]["nx"]) for d in job.omitted] == [
        ("edge-x", 5),
        ("edge-y", 5),
    ]
    edges = [d for d in job.designs if d.parameters["family"] in {"edge-x", "edge-y"}]
    assert sorted((d.parameters["family"], d.parameters["nx"]) for d in edges) == [
        (family, n) for family in ("edge-x", "edge-y") for n in range(1, 5)
    ]
    assert len(job.designs) == 8 + 9
    assert job.manifest_metadata["inventory"]["straight_40mm_male_edges"] == 4
    assert job.manifest_metadata["straight_edges"]["lengths_cells"] == [1, 2, 3, 4]


def test_expansion_set_refuses_an_envelope_too_small_for_a_contour_piece(contour_stand_ins):
    with pytest.raises(ValueError, match="zeekr_rear_south_ramp_1_4cell: actual bounds"):
        extras_job(BuildVolume(230, 120, 13))
    with pytest.raises(ValueError, match="no supported designs fit"):
        extras_job(BuildVolume(59, 39, 13))


def test_grouping_never_adds_a_plate_when_the_combined_edges_fit_one(monkeypatch):
    small = {key: (50, 50, 13) for key in SIDE_CAP_SIZES}
    monkeypatch.setattr(
        zeekr_7x_rear_review,
        "rear_panel_designs",
        _stand_in_contours(small, [(50, 50, 13)] * 5),
    )
    job = extras_job(BuildVolume(110, 110, 13))
    assert job.placement_policy["grouped_by_connector_sex"] is False
    assert job.plate_names[0] == "Zeekr 7X - 40mm edges"
    assert {d.parameters["family"] for d in job.designs[:2]} == {"edge-x", "edge-y"}
    assert {p.plate for p in job.print_placements[:2]} == {0}
    assert all(p.plate > 0 for p in job.print_placements[2:])


def test_h2d_expansion_set_plans_five_family_plates_in_common_reach(expansion_plan):
    job = expansion_plan
    assert job.kind == "extras" and job.omitted == []
    assert len(job.designs) == 19
    assert {plate: name for plate, (name, _, _) in PLATES.items()} == job.plate_names
    members = _plate_members(job)
    for plate, (_, families, count) in PLATES.items():
        assert len(members[plate]) == count
        assert {
            design.parameters["family"]
            for design, placement in zip(job.designs, job.print_placements)
            if placement.plate == plate
        } == families
    assert members[3] == ["zeekr_rear_south_ramp_2_4cell", "zeekr_rear_south_ramp_4_4cell"]
    assert [d.parameters["nx"] for d in job.designs[:10]] == [1, 2, 3, 4, 5] * 2
    assert all(d.quantity == 1 and not d.apply_orientation_to_bambu for d in job.designs)
    assert all(d.bambu_object_settings == {} for d in job.designs)
    assert job.plate_settings == {}
    assert job.projected_footprints is None
    assert job.projected_footprint_clearances == {}
    assert job.part_gap == H2D_DEFAULT_PART_CLEARANCE_MM == 4
    assert CONTOUR_GAP_MM == 10
    _assert_group_clearances(job)
    policy = job.placement_policy
    assert policy["collection"] == COLLECTION
    assert policy["grouped_by_connector_sex"] is True
    assert policy["minimum_model_gap_mm"] == 4
    assert policy["plate_group_minimum_model_gap_mm"] == GROUP_GAPS
    assert policy["outline_parameters_mm"] == {
        "pen_offset": 5,
        "east_edge_x": 602.5,
        "centre_depth": 365,
        "traced_corner_radius": 11.4,
        "true_corner_radius": pytest.approx(6.4),
    }
    metadata = job.manifest_metadata
    assert metadata["inventory"] == {
        "straight_40mm_male_edges": 5,
        "straight_40mm_female_edges": 5,
        "rear_panel_side_caps": 4,
        "rear_panel_south_contour_ramps": 5,
        "standard_tiles_and_north_edges_included": False,
    }
    assert metadata["assembly"]["standard_parts_printed_separately"] == STANDARD_PARTS
    assert metadata["assembly"]["tile_modules_west_to_east_cells"] == (4, 4, 2, 4, 4)
    assert metadata["assembly"]["outline_footprint_mm"] == (1205, 365)
    serialized = json.dumps(metadata)
    for private in ("/Users/", "session-state", "npy", "photogrammetry"):
        assert private not in serialized.lower()


def test_h2d_expansion_set_exports_one_project_with_named_plates(expansion_plan, tmp_path):
    manifest_path = export_job(expansion_plan, tmp_path / "set", stl=False, bambu=H2D_BAMBU)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["kind"] == "extras"
    assert len(manifest["designs"]) == 19
    assert manifest["placement_policy"] == json.loads(json.dumps(expansion_plan.placement_policy))
    assert manifest["job_metadata"]["inventory"]["rear_panel_side_caps"] == 4
    assert manifest["export"]["sliced"] is False
    for design in manifest["designs"]:
        if design["parameters"]["family"] in {"edge-x", "edge-y"}:
            assert design["step_roundtrip"] == "passed"
            assert design["step_volume_delta_mm3"] <= design["step_volume_budget_mm3"]
            assert design["step_bounds_delta_mm"] <= 1e-5
            assert design["mesh"]["closed_oriented_manifold"]
            assert design["compatibility"]["edge_outward_mm"] == 40
            assert design["compatibility"]["edge_hole_diameter_mm"] == 10
    with ZipFile(manifest_path.parent / "job.3mf") as archive:
        assert archive.testzip() is None
        assert not any("slice" in name.lower() for name in archive.namelist())
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        assert len(config.findall("object")) == 19
        plates = config.findall("plate")
        assert len(plates) == 5
        for plate in plates:
            meta = {e.get("key"): e.get("value") for e in plate.findall("metadata")}
            name, _, count = PLATES[int(meta["plater_id"]) - 1]
            assert meta["plater_name"] == name
            assert meta["filament_map_mode"] == "Auto For Match"
            assert "filament_maps" not in meta and "filament_volume_maps" not in meta
            assert len(plate.findall("model_instance")) == count
    with pytest.raises(ValueError, match="not empty"):
        export_job(expansion_plan, manifest_path.parent)


@pytest.mark.parametrize(
    "options,edge_gap,contour_gap",
    [
        ([], 4, 10),
        (["--packing-gap-mm", "6"], 6, 6),
        (["--packing-gap-mm", "12"], 12, 12),
    ],
)
def test_cli_h2d_extras_routes_shared_printer_settings(
    tmp_path,
    monkeypatch,
    accessory_metadata_shape,
    contour_stand_ins,
    options,
    edge_gap,
    contour_gap,
):
    captured = []

    def capture(job, output, **settings):
        captured.append((job, settings))
        return output / "manifest.json"

    monkeypatch.setattr(cli, "export_job", capture)
    command = ["extras", COLLECTION, *H2D_OPTIONS, "--output", str(tmp_path / "set"), *options]
    assert main(command) == 0
    job, settings = captured[0]
    assert len(job.designs) == 19
    assert job.build == H2D
    assert job.part_gap == min(edge_gap, contour_gap)
    policy = job.placement_policy
    assert policy["minimum_actual_part_xy_clearance_mm"] == job.part_gap
    assert policy["minimum_model_gap_mm"] == job.part_gap
    gaps = policy["plate_group_minimum_model_gap_mm"]
    # The small metadata-only edge boxes fit one plate, so the edges share one group here.
    assert set(gaps) == {
        "Zeekr 7X - 40mm edges",
        "Zeekr 7X - Rear panel contour side caps",
        "Zeekr 7X - Rear panel south contour ramps",
    }
    assert gaps == {name: edge_gap if "40mm" in name else contour_gap for name in gaps}
    assert policy["common_reach_mm"] == {
        "min_x": 25,
        "max_x": 325,
        "min_y": 0,
        "max_y": 320,
        "max_z": 320,
    }
    assert job.projected_footprints is None
    assert settings["stack"] is None
    assert settings["bambu"].machine_nozzle_count == 2
    assert settings["bambu"].roof_support is None
    assert settings["bambu"].printer_settings_id == "Bambu Lab H2D 0.8 nozzle"


def test_cli_extras_accept_a_solid_bottom_for_every_piece(
    tmp_path,
    monkeypatch,
    accessory_metadata_shape,
    contour_stand_ins,
):
    captured = []
    monkeypatch.setattr(cli, "export_job", lambda job, output, **_: captured.append(job) or output)
    command = [
        "extras",
        COLLECTION,
        *H2D_OPTIONS,
        "--solid-bottom-thickness-mm",
        "1.92",
        "--output",
        str(tmp_path / "set"),
    ]
    assert main(command) == 0
    (job,) = captured
    assert len(job.designs) == 19
    assert {design.parameters["interface"]["solid_bottom_mm"] for design in job.designs} == {1.92}
    solid_bottom = job.manifest_metadata["solid_bottom"]
    assert solid_bottom["body_thickness_mm"] == pytest.approx(14.92)
    assert "recheck clearance under the lift-out panel" in solid_bottom["note"]
    standard_parts = job.manifest_metadata["assembly"]["standard_parts_printed_separately"]
    assert {part["solid_bottom_thickness_mm"] for part in standard_parts} == {1.92}


def test_extras_without_a_solid_bottom_record_no_solid_bottom(expansion_plan):
    assert "solid_bottom" not in expansion_plan.manifest_metadata
    assert expansion_plan.manifest_metadata["assembly"]["standard_parts_printed_separately"] == (
        STANDARD_PARTS
    )
    assert all("solid_bottom_mm" not in d.parameters["interface"] for d in expansion_plan.designs)


def test_cli_extras_outside_h2d_keeps_the_ordinary_and_contour_default_gaps(
    tmp_path,
    monkeypatch,
    accessory_metadata_shape,
    contour_stand_ins,
):
    captured = []
    monkeypatch.setattr(cli, "export_job", lambda job, output, **_: captured.append(job) or output)
    build = ["--build-width-mm", "400", "--build-depth-mm", "400", "--build-height-mm", "50"]
    assert main(["extras", COLLECTION, *build, "--output", str(tmp_path / "set")]) == 0
    (job,) = captured
    assert job.build == BuildVolume(400, 400, 50)
    assert job.part_gap == 2
    assert set(job.placement_policy["plate_group_minimum_model_gap_mm"].values()) == {2, 10}
    assert "common_reach_mm" not in job.placement_policy
    assert sorted(d.parameters["nx"] for d in job.designs if "nx" in d.parameters) == sorted(
        [*range(1, 7)] * 2
    )


@pytest.mark.parametrize(
    "options,message",
    [
        (["--no-holes"], "full-scope 10 mm hole pattern"),
        (["--hole-scope", "interior"], "full-scope 10 mm hole pattern"),
        (["--hole-diameter-mm", "8"], "full-scope 10 mm hole pattern"),
        (["--unit-size-mm", "50"], "standard 60 mm unit"),
        (["--tile-thickness-mm", "15"], "standard 60 mm unit"),
        (["--joint-style", "full-height"], "original roofed"),
    ],
)
def test_cli_extras_rejects_patterns_the_contour_pieces_cannot_match(
    options,
    message,
    tmp_path,
    capsys,
):
    build = ["--build-width-mm", "350", "--build-depth-mm", "320", "--build-height-mm", "325"]
    with pytest.raises(SystemExit) as caught:
        main(["extras", COLLECTION, *build, "--output", str(tmp_path / "set"), *options])
    assert caught.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "set").exists()


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


@pytest.mark.parametrize(
    "command",
    [
        ["extras", "unknown"],
        ["extras", "zeekr-7x-rear-panel"],
        ["catalogue", "--collection", "zeekr-7x-extras"],
    ],
)
def test_cli_only_accepts_the_single_expansion_set_route(command, tmp_path, capsys):
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
    if command[1] == "zeekr-7x-rear-panel":
        assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize(
    "options,message",
    [
        (["--roof-support"], "catalogues and extras are not supported"),
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
        (["--packing-gap-mm", "0"], "H2D packing gap"),
        (["--packing-gap-mm", "-1"], "H2D packing gap"),
    ],
)
def test_extras_reject_unsupported_shared_workflows(options, message, tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        main(["extras", COLLECTION, *H2D_OPTIONS, "--output", str(tmp_path / "set"), *options])
    assert caught.value.code == 2
    assert message in capsys.readouterr().err


@pytest.fixture(scope="module")
def expansion_set_job():
    return SimpleNamespace(
        job=extras_job(
            H2D,
            placement_build=h2d_common_build(),
            edge_gap=H2D_DEFAULT_PART_CLEARANCE_MM,
        )
    )


@pytest.mark.slow
def test_real_expansion_set_matches_the_planned_plates(expansion_set_job, expansion_plan):
    job = expansion_set_job.job
    assert [d.name for d in job.designs] == [d.name for d in expansion_plan.designs]
    assert job.plate_names == expansion_plan.plate_names
    assert _plate_members(job) == _plate_members(expansion_plan)
    for design, planned in zip(job.designs, expansion_plan.designs):
        assert design.size == pytest.approx(planned.size, abs=1e-3)
        assert design.shape.is_valid and len(design.shape.solids()) == 1
        assert design.shape.volume > 0
    assert job.placement_policy == expansion_plan.placement_policy
    assert job.manifest_metadata == expansion_plan.manifest_metadata
    _assert_group_clearances(job)
    contour = job.designs[10:]
    assert [d.parameters["family"] for d in contour] == [
        *(["zeekr-rear-contour-side"] * 4),
        *(["zeekr-rear-contour-ramp"] * 5),
    ]
    assert {d.parameters["connector_sex"] for d in contour[:2]} == {"male"}
    assert {d.parameters["connector_sex"] for d in contour[2:4]} == {"female"}
    assert {d.parameters["connector_sex"] for d in contour[4:]} == {"male"}


@pytest.mark.slow
def test_real_expansion_set_exports_every_piece_in_one_project(expansion_set_job, tmp_path):
    manifest_path = export_job(expansion_set_job.job, tmp_path / "set", stl=False, bambu=H2D_BAMBU)
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest["designs"]) == 19
    assert len(list((tmp_path / "set").glob("*.step"))) == 19
    for design in manifest["designs"]:
        assert design["step_roundtrip"] == "passed"
        assert design["step_volume_delta_mm3"] <= design["step_volume_budget_mm3"]
        assert design["step_bounds_delta_mm"] <= 1e-5
        assert design["mesh"]["closed_oriented_manifold"]
        assert design["compatibility"]["tile_edge_interface_present"]
    plates = manifest["export"]["plates"]
    assert [len(plate["items"]) for plate in plates] == [count for _, _, count in PLATES.values()]
    serialized = json.dumps(manifest)
    for private in ("/Users/", "session-state", "photogrammetry"):
        assert private not in serialized
    with ZipFile(manifest_path.parent / "job.3mf") as archive:
        assert archive.testzip() is None
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        names = [
            {e.get("key"): e.get("value") for e in plate.findall("metadata")}["plater_name"]
            for plate in config.findall("plate")
        ]
        assert sorted(names) == sorted(name for name, _, _ in PLATES.values())
