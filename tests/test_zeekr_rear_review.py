"""Measured rear-panel review recipe and local artifact checks."""

import json
import os
from collections import defaultdict
from math import hypot
from pathlib import Path

import pytest
from build123d import Axis, GeomType, Location, Solid
from OCP.BRepAdaptor import BRepAdaptor_Surface

from cargo_grid import cli
from cargo_grid.cli import main
from cargo_grid.interfaces import make_plug
from cargo_grid.meshes import checked_mesh
from cargo_grid.parameters import BuildVolume, Exclusion, Tile
from cargo_grid.tiles import make_tile
from cargo_grid.vehicles.zeekr_7x_rear_review import (
    CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU,
    DEFAULT_EAST_EDGE_X_MM,
    DEFAULT_PEN_OFFSET_MM,
    H2D_REVIEW_GAP_MM,
    ORIGINAL_RIGHT_WALL_STATIONS_MM,
    SCAN_ALIGNMENT,
    SOUTH_CONTOUR_ANALYSIS,
    TEST_TILE_MODULES,
    TRACED_NORTH_CORNER_RADIUS_MM,
    RearReviewParameters,
    _corrected_north_edge_stations,
    _crown_coefficients,
    _merged_taper_stations,
    _north_corner_geometry,
    _tail_slope_dy_dx,
    _taper_interpolators,
    inspect_scan_obj,
    rear_panel_job,
    rear_review_job,
    right_review_outline_x_mm,
    right_wall_x_mm,
    tail_y_mm,
)


def _placed(design):
    assert len(design.assembly_frames) == 1
    return design.shape.moved(Location(design.assembly_frames[0]))


def _intersection_volume(first, second):
    intersection = first.intersect(second)
    return 0 if intersection is None else sum(solid.volume for solid in intersection.solids())


@pytest.fixture(scope="module")
def review_job():
    return rear_review_job()


@pytest.fixture(scope="module")
def product_job():
    return rear_panel_job(
        BuildVolume(350, 320, 325),
        placement_build=BuildVolume(
            350,
            320,
            320,
            margin=5,
            exclusions=(
                Exclusion(0, 0, 30, 320),
                Exclusion(320, 0, 30, 320),
            ),
        ),
    )


def test_measured_outline_uses_exact_mirrored_pchip_and_named_inset():
    parameters = RearReviewParameters()
    stations = _merged_taper_stations(
        parameters.pen_offset_mm,
        parameters.se_along_edge_shift_mm,
    )
    for u, s in stations:
        y = 620 + s
        x = parameters.east_edge_x_mm - u
        assert right_wall_x_mm(y) == pytest.approx(x)
        assert -right_wall_x_mm(y) == pytest.approx(-x)
    assert ORIGINAL_RIGHT_WALL_STATIONS_MM[-1] != pytest.approx(
        (620 + stations[-1][1], parameters.east_edge_x_mm - stations[-1][0])
    )
    assert stations[0] == pytest.approx((0, 219))
    assert stations[-1] == pytest.approx((155.571, 342.132711096))
    expected_north_stations = (
        (95.3491456385, 0),
        (75.3615510440, 1.3869109519),
        (55.4111033364, 2.8830707447),
        (35.4111033364, 4.6830707447),
        (15.3739497412, 6.1859965494),
    )
    for actual, expected in zip(
        _corrected_north_edge_stations(parameters.pen_offset_mm),
        expected_north_stations,
        strict=True,
    ):
        assert actual == pytest.approx(expected)
    corner = _north_corner_geometry(parameters)
    assert corner["center"] == pytest.approx((596.1, 633.277017506))
    assert corner["north_tangent"] == pytest.approx((596.578655647, 626.894941921))
    assert right_review_outline_x_mm(
        corner["east_tangent"][1],
        traced_north_corner_radius_mm=TRACED_NORTH_CORNER_RADIUS_MM,
        pen_offset_mm=DEFAULT_PEN_OFFSET_MM,
        east_edge_x_mm=DEFAULT_EAST_EDGE_X_MM,
    ) == pytest.approx(602.5)
    taper_samples = [right_wall_x_mm(y) for y in range(839, 963)]
    assert all(first >= second for first, second in zip(taper_samples, taper_samples[1:]))
    _, inverse = _taper_interpolators(
        parameters.pen_offset_mm,
        parameters.se_along_edge_shift_mm,
    )
    assert inverse(219, 1) == pytest.approx(0, abs=1e-12)
    assert all(right_wall_x_mm(y) <= parameters.east_edge_x_mm for y in range(839, 963))

    assert parameters.traced_north_corner_radius_mm == 11.4
    assert parameters.pen_offset_mm == 5
    assert parameters.north_corner_radius_mm == pytest.approx(6.4)
    assert parameters.east_edge_x_mm == 602.5
    assert parameters.se_along_edge_shift_mm == 0
    assert parameters.centre_depth_mm == 365
    assert tail_y_mm(-446.929, parameters) == pytest.approx(962.132711096)
    assert tail_y_mm(0, parameters) == pytest.approx(985)
    assert tail_y_mm(446.929, parameters) == pytest.approx(962.132711096)
    samples = [(x, tail_y_mm(x, parameters)) for x in range(-446, 447)]
    for (left_x, left_y), (right_x, right_y) in zip(samples, reversed(samples)):
        assert left_x == -right_x
        assert left_y == pytest.approx(right_y, abs=1e-12)
    assert all(first[1] < second[1] for first, second in zip(samples[:446], samples[1:447]))
    assert all(first[1] > second[1] for first, second in zip(samples[446:], samples[447:]))
    maximum = max(samples, key=lambda sample: sample[1])
    assert maximum == pytest.approx((0, 985))
    assert sum(y == maximum[1] for _, y in samples) == 1

    a, b, junction_s, slope = _crown_coefficients(
        parameters.centre_depth_mm,
        parameters.se_along_edge_shift_mm,
    )
    assert a == pytest.approx(1.0030842844733856e-4)
    assert b == pytest.approx(7.095866780978381e-11)
    assert junction_s == pytest.approx(342.132711096)
    assert slope == CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU
    assert _tail_slope_dy_dx(446.929, parameters) == pytest.approx(
        -CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU,
        abs=2e-8,
    )
    assert _tail_slope_dy_dx(-446.929, parameters) == pytest.approx(
        CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU,
        abs=2e-8,
    )
    assert "concave quartic" in SOUTH_CONTOUR_ANALYSIS["curve_family"]
    assert SCAN_ALIGNMENT["lateral_residuals_mm"] == {
        "median_absolute": 1.881,
        "rms": 4.120,
        "p95_absolute": 9.537,
        "maximum_absolute": 12.483,
        "signed_mean": 0.066,
        "signed_median": -0.388,
    }


def test_review_inventory_frames_connector_sexes_and_actual_h2d_packing(review_job):
    job = review_job
    assert len(job.designs) == 19
    assert job.kind == "zeekr-7x-rear-review"
    assert job.part_gap == H2D_REVIEW_GAP_MM
    assert job.footprint == pytest.approx((1205, 365))
    assert job.manifest_metadata["outline"]["nominal_contour_gap_mm"] == 0
    assert job.manifest_metadata["outline"]["prior_full_boot_inset_convention_mm"] == 5
    assert job.manifest_metadata["selected_test_placement"]["tile_modules_west_to_east_cells"] == (
        4,
        4,
        2,
        4,
        4,
    )

    expected_frames = [
        (-540, 60, 0),
        (-300, 60, 0),
        (-60, 60, 0),
        (60, 60, 0),
        (300, 60, 0),
    ]
    assert [design.assembly_frames[0] for design in job.designs[:5]] == expected_frames
    assert [design.parameters["nx"] for design in job.designs[:5]] == list(TEST_TILE_MODULES)
    tile_sides = {
        side: {
            join["sex"] for join in job.designs[0].mating_datums["joins"] if join["side"] == side
        }
        for side in ("north", "east", "south", "west")
    }
    assert tile_sides == {
        "north": {"male"},
        "east": {"male"},
        "south": {"female"},
        "west": {"female"},
    }

    north = job.designs[5:10]
    south = job.designs[10:15]
    west = [design for design in job.designs if "_west_" in design.name]
    east = [design for design in job.designs if "_east_" in design.name]
    assert {join["sex"] for design in north for join in design.mating_datums["joins"]} == {"female"}
    assert {join["sex"] for design in south for join in design.mating_datums["joins"]} == {"male"}
    assert {join["sex"] for design in west for join in design.mating_datums["joins"]} == {"male"}
    assert {join["sex"] for design in east for join in design.mating_datums["joins"]} == {"female"}
    assert {design.assembly_frames[0][1] for design in north} == {300}
    assert {design.assembly_frames[0][1] for design in south} == {60}
    assert {design.parameters["connector_sex"] for design in south} == {"male"}
    assert all(
        design.parameters["south_contour_bow_mm"] == pytest.approx(22.867288904) for design in south
    )
    west_south = next(design for design in west if design.parameters["segment"] == "south")
    east_south = next(design for design in east if design.parameters["segment"] == "south")
    assert west_south.shape.bounding_box().min.X < -62.5
    assert west_south.shape.bounding_box().max.X == pytest.approx(6)
    assert east_south.shape.bounding_box().min.X == pytest.approx(0, abs=1e-6)
    assert east_south.shape.bounding_box().max.X > 62.5
    assert west_south.assembly_frames == [(-540, 0, 0)]
    assert east_south.assembly_frames == [(540, 0, 0)]
    west_samples = west_south.mating_datums["corner_ramp_samples_local_x_run_mm"]
    east_samples = east_south.mating_datums["corner_ramp_samples_local_x_run_mm"]
    assert len(west_samples) == len(east_samples)
    for (west_x, west_run), (east_x, east_run) in zip(
        reversed(west_samples),
        east_samples,
    ):
        assert -west_x == pytest.approx(east_x)
        assert west_run == pytest.approx(east_run)

    rectangles = {}
    for design, placement in zip(job.designs, job.print_placements):
        width, depth, height = design.size
        if placement.rotation == 90:
            width, depth = depth, width
        assert placement.rotation in (0, 90)
        assert 30 <= placement.x
        assert placement.x + width <= 320 + 1e-6
        assert 5 <= placement.y
        assert placement.y + depth <= 315 + 1e-6
        assert height == pytest.approx(13)
        rectangles.setdefault(placement.plate, []).append(
            (placement.x, placement.x + width, placement.y, placement.y + depth)
        )
    for bounds in rectangles.values():
        for index, first in enumerate(bounds):
            for second in bounds[index + 1 :]:
                dx = max(0, first[0] - second[1], second[0] - first[1])
                dy = max(0, first[2] - second[3], second[2] - first[3])
                assert hypot(dx, dy) >= H2D_REVIEW_GAP_MM - 1e-5


def test_product_recipe_contains_only_nine_contour_pieces_with_clean_metadata(
    product_job,
):
    job = product_job
    assert job.kind == "zeekr-7x-rear-panel"
    assert len(job.designs) == 9
    assert [design.parameters["family"] for design in job.designs] == [
        *(["zeekr-rear-contour-side"] * 4),
        *(["zeekr-rear-contour-ramp"] * 5),
    ]
    assert job.part_gap == 10
    assert job.placement_policy["collection"] == "zeekr-7x-rear-panel"
    assert job.manifest_metadata["inventory"] == {
        "side_caps": 4,
        "south_contour_ramps": 5,
        "standard_tiles_and_north_edges_included": False,
    }
    serialized = json.dumps(job.manifest_metadata)
    assert "/Users/" not in serialized
    assert "session-state" not in serialized
    assert "npy" not in serialized.lower()
    assert "photogrammetry" not in serialized.lower()
    rectangles = defaultdict(list)
    for design, placement in zip(job.designs, job.print_placements):
        width, depth, height = design.size
        if placement.rotation == 90:
            width, depth = depth, width
        assert 30 <= placement.x
        assert placement.x + width <= 320 + 1e-6
        assert 5 <= placement.y
        assert placement.y + depth <= 315 + 1e-6
        assert height == pytest.approx(13)
        rectangles[placement.plate].append(
            (placement.x, placement.x + width, placement.y, placement.y + depth)
        )
    assert "side caps" in job.plate_names[0].lower()
    assert all(
        "south contour ramps" in job.plate_names[plate].lower()
        for plate in range(1, max(rectangles) + 1)
    )
    for bounds in rectangles.values():
        for index, first in enumerate(bounds):
            for second in bounds[index + 1 :]:
                dx = max(0, first[0] - second[1], second[0] - first[1])
                dy = max(0, first[2] - second[3], second[2] - first[3])
                assert hypot(dx, dy) >= 10 - 1e-5


def test_cli_routes_rear_panel_recipe_with_ten_mm_default_gap(
    tmp_path,
    monkeypatch,
    product_job,
):
    captured = []

    def capture(job, output, **settings):
        captured.append((job, settings))
        return output / "manifest.json"

    monkeypatch.setattr(cli, "export_job", capture)
    monkeypatch.setattr(
        cli.zeekr_7x_rear_review,
        "rear_panel_job",
        lambda *args, **kwargs: product_job,
    )
    assert (
        main(
            [
                "extras",
                "zeekr-7x-rear-panel",
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
                str(tmp_path / "rear-panel"),
            ]
        )
        == 0
    )
    job, settings = captured[0]
    assert job.kind == "zeekr-7x-rear-panel"
    assert len(job.designs) == 9
    assert job.part_gap == 10
    assert job.placement_policy["minimum_actual_part_xy_clearance_mm"] == 10
    assert settings["bambu"].machine_nozzle_count == 2
    assert settings["bambu"].printer_settings_id == "Bambu Lab H2D 0.8 nozzle"

    with pytest.raises(SystemExit) as caught:
        main(
            [
                "extras",
                "zeekr-7x-rear-panel",
                "--build-width-mm",
                "350",
                "--build-depth-mm",
                "320",
                "--build-height-mm",
                "325",
                "--no-holes",
                "--output",
                str(tmp_path / "invalid"),
            ]
        )
    assert caught.value.code == 2


def test_side_caps_have_grid_sockets_and_completed_boundary_holes(review_job):
    job = review_job
    caps = [
        design
        for design in job.designs
        if design.parameters.get("family") == "zeekr-rear-contour-side"
    ]
    expected_sockets = {
        "west": {(-570, y, 13) for y in (150, 210, 270)},
        "east": {(570, y, 13) for y in (150, 210, 270)},
    }
    expected_holes = {
        "west": {(-540, y) for y in range(60, 301, 30)},
        "east": {(540, y) for y in range(60, 301, 30)},
    }
    expected_interior_holes = {
        "west": {(-570, y) for y in range(60, 301, 60)},
        "east": {(570, y) for y in range(60, 301, 60)},
    }
    baseline_tile = make_tile(Tile())
    baseline_plug = make_plug().rotate(Axis.X, 180).moved(Location((30, 30, 13)))
    baseline_intersection = sum(
        solid.volume for solid in baseline_plug.intersect(baseline_tile).solids()
    )
    for side in ("west", "east"):
        members = [design for design in caps if design.parameters["side"] == side]
        sockets = {
            (
                x + design.assembly_frames[0][0],
                y + design.assembly_frames[0][1],
                z + design.assembly_frames[0][2],
            )
            for design in members
            for x, y, z in design.mating_datums["accessory_socket_centers"]
        }
        holes = {
            (
                x + design.assembly_frames[0][0],
                y + design.assembly_frames[0][1],
            )
            for design in members
            for x, y in design.mating_datums["completed_10mm_boundary_hole_centers"]
        }
        interior_holes = {
            (
                x + design.assembly_frames[0][0],
                y + design.assembly_frames[0][1],
            )
            for design in members
            for x, y in design.mating_datums["completed_10mm_interior_hole_centers"]
        }
        assert sockets == expected_sockets[side]
        assert holes == expected_holes[side]
        assert interior_holes == expected_interior_holes[side]
        assert sum(design.parameters["accessory_socket_count"] for design in members) == 3
        for design in members:
            radii = [
                BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()
                for face in design.shape.faces()
                if face.geom_type == GeomType.CYLINDER
            ]
            expected_entry_faces = 8 * design.parameters["accessory_socket_count"]
            assert (
                sum(radius == pytest.approx(3, abs=1e-7) for radius in radii)
                == expected_entry_faces
            )
            assert all(
                report["minimum_reported_material_mm"] >= 1.5
                for report in design.mating_datums["socket_feasibility"]
            )
            for report in design.mating_datums["socket_feasibility"]:
                assert report["minimum_outboard_contour_mm"] >= 7.2
                assert report["minimum_10mm_boundary_hole_mm"] >= 5.44
                assert report["minimum_cap_split_seam_mm"] >= 4.72
                assert report["minimum_south_corner_transition_mm"] >= 3.0
                assert report["minimum_north_corner_transition_mm"] >= 34.72
                if side == "east":
                    assert report["minimum_tile_edge_join_mm"] == pytest.approx(1.621825406947976)
            for x, y, z in design.mating_datums["accessory_socket_centers"]:
                plug = make_plug().rotate(Axis.X, 180).moved(Location((x, y, z)))
                interference = sum(solid.volume for solid in plug.intersect(design.shape).solids())
                assert interference == pytest.approx(baseline_intersection, abs=1e-7)
        rejected = [
            report
            for design in members
            for report in design.mating_datums["rejected_socket_candidates"]
        ]
        assert len(rejected) == 1
        assert rejected[0]["center"] == ((-30.0, 90.0, 0) if side == "west" else (30.0, 90.0, 0))
        assert rejected[0]["minimum_south_corner_transition_mm"] == pytest.approx(-15.3989607866)
        assert rejected[0]["minimum_reported_material_mm"] < 1.5
        accepted_hole_reports = [
            report
            for design in members
            for report in design.mating_datums["interior_hole_feasibility"]
        ]
        assert {tuple(report["assembly_center"][:2]) for report in accepted_hole_reports} == (
            expected_interior_holes[side]
        )
        assert all(
            report["minimum_reported_material_mm"] == pytest.approx(3.946810683739102)
            for report in accepted_hole_reports
        )
        assert all(
            report["minimum_adjacent_boundary_hole_web_mm"] == pytest.approx(20)
            for report in accepted_hole_reports
        )
        assert all(
            report["minimum_contour_web_mm"] >= 11.805485495 for report in accepted_hole_reports
        )
        assert all(
            report["minimum_join_web_mm"] >= 21.658633371 for report in accepted_hole_reports
        )
        rejected_holes = [
            report
            for design in members
            for report in design.mating_datums["rejected_interior_hole_candidates"]
        ]
        assert {abs(report["assembly_center"][0]) for report in rejected_holes} == {600}
        assert all(
            report["rejection_reason"] == "outer column breaks through corrected contour"
            for report in rejected_holes
        )
        assert min(report["minimum_contour_web_mm"] for report in rejected_holes) < -18
        assert not any(
            tuple(report["assembly_center"][:2]) == ((-570, 90) if side == "west" else (570, 90))
            for report in accepted_hole_reports + rejected_holes
        )
        segment_holes = {
            design.parameters["segment"]: {
                tuple(report["assembly_center"][:2])
                for report in design.mating_datums["interior_hole_feasibility"]
            }
            for design in members
        }
        signed_x = -570 if side == "west" else 570
        assert (signed_x, 60) in segment_holes["south"]
        assert (signed_x, 60) not in segment_holes["north"]
        assert (signed_x, 180) in segment_holes["south"]
        assert (signed_x, 180) in segment_holes["north"]
        assert (signed_x, 300) in segment_holes["north"]
        assert (signed_x, 300) not in segment_holes["south"]

    placed = [design.shape.moved(Location(design.assembly_frames[0])) for design in job.designs]
    for side_x in (-540, 540):
        for y in range(60, 301, 30):
            bore = Solid.make_cylinder(4.99, 13).moved(Location((side_x, y, 0)))
            candidates = [
                shape
                for shape in placed
                if shape.bounding_box().min.X <= side_x + 5
                and shape.bounding_box().max.X >= side_x - 5
                and shape.bounding_box().min.Y <= y + 5
                and shape.bounding_box().max.Y >= y - 5
            ]
            intersections = [shape.intersect(bore) for shape in candidates]
            assert sum(
                solid.volume
                for intersection in intersections
                if intersection is not None
                for solid in intersection.solids()
            ) == pytest.approx(0, abs=1e-8)
    for side_x in (-570, 570):
        for y in range(60, 301, 60):
            bore = Solid.make_cylinder(4.99, 13).moved(Location((side_x, y, 0)))
            assert sum(
                solid.volume
                for shape in placed
                for intersection in [shape.intersect(bore)]
                if intersection is not None
                for solid in intersection.solids()
            ) == pytest.approx(0, abs=1e-8)


def test_side_caps_have_exact_mirrored_pen_corrected_r6_4_north_corners(
    review_job,
):
    caps = [
        design
        for design in review_job.designs
        if design.parameters.get("family") == "zeekr-rear-contour-side"
        and design.parameters["segment"] == "north"
    ]
    assert TRACED_NORTH_CORNER_RADIUS_MM == 11.4
    true_radius = TRACED_NORTH_CORNER_RADIUS_MM - DEFAULT_PEN_OFFSET_MM
    assert true_radius == pytest.approx(6.4)
    for design in caps:
        cylinders = [
            BRepAdaptor_Surface(face.wrapped).Cylinder()
            for face in design.shape.faces()
            if face.geom_type == GeomType.CYLINDER
            and BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()
            == pytest.approx(true_radius, abs=1e-7)
        ]
        assert len(cylinders) == 1
        cylinder = cylinders[0]
        expected_x = -56.1 if design.parameters["side"] == "west" else 56.1
        assert cylinder.Location().X() == pytest.approx(expected_x)
        assert cylinder.Location().Y() == pytest.approx(316.722982494)
        assert abs(cylinder.Axis().Direction().Z()) == pytest.approx(1)


def test_south_ramps_complete_every_tile_boundary_hole_through_tabs_and_shelf(
    review_job,
):
    job = review_job
    ramps = [
        design
        for design in job.designs
        if design.parameters.get("family") == "zeekr-rear-contour-ramp"
    ]
    expected_global = {(float(x), 60.0) for x in range(-540, 541, 30)}
    actual_global = {
        (
            design.assembly_frames[0][0] + center[0],
            design.assembly_frames[0][1] + center[1],
        )
        for design in ramps
        for center in design.mating_datums["completed_10mm_south_boundary_hole_centers"]
    }
    assert actual_global == expected_global
    reports = [
        report
        for design in ramps
        for report in design.mating_datums["south_boundary_hole_feasibility"]
    ]
    assert min(report["minimum_high_shelf_material_behind_cut_mm"] for report in reports) == (
        pytest.approx(5)
    )
    assert min(report["minimum_adjacent_hole_web_mm"] for report in reports) == pytest.approx(20)
    tab_reports = [report for report in reports if report["cuts_male_tab"]]
    assert len(tab_reports) == 18
    assert all(
        report["minimum_male_tab_root_side_web_mm"] == pytest.approx(6.085786437626908)
        for report in tab_reports
    )
    assert {
        report["assembly_center"][0]
        for report in reports
        if report["ownership"] == "module-seam quarter"
    } == {-300, -60, 60, 300}
    assert {
        report["assembly_center"][0]
        for report in reports
        if report["ownership"] == "outer side-corner quarter"
    } == {-540, 540}

    placed = [design.shape.moved(Location(design.assembly_frames[0])) for design in job.designs]
    for x, y in expected_global:
        bore = Solid.make_cylinder(4.99, 13).moved(Location((x, y, 0)))
        assert sum(
            solid.volume
            for shape in placed
            for intersection in [shape.intersect(bore)]
            if intersection is not None
            for solid in intersection.solids()
        ) == pytest.approx(0, abs=1e-8)

    north_centers = {
        (
            design.assembly_frames[0][0] + hole["x"],
            design.assembly_frames[0][1] + hole["y"],
        )
        for design in job.designs[5:10]
        for hole in design.holes
        if hole["accepted"]
    }
    assert north_centers == {(float(x), 300.0) for x in range(-540, 541, 30)}
    for x, y in north_centers:
        bore = Solid.make_cylinder(4.99, 13).moved(Location((x, y, 0)))
        assert sum(
            solid.volume
            for shape in placed
            for intersection in [shape.intersect(bore)]
            if intersection is not None
            for solid in intersection.solids()
        ) == pytest.approx(0, abs=1e-8)


def test_south_modules_share_reviewed_endpoints_and_plan_tangents(review_job):
    job = review_job
    parameters = RearReviewParameters()
    south = job.designs[10:15]
    seams = (-300, -60, 60, 300)
    for first, second, seam in zip(south, south[1:], seams):
        first_samples = first.mating_datums["nose_samples_local_x_run_mm"]
        second_samples = second.mating_datums["nose_samples_local_x_run_mm"]
        assert first_samples[-1][1] == pytest.approx(second_samples[0][1], abs=1e-9)
        first_slope = (first_samples[-1][1] - first_samples[-2][1]) / (
            first_samples[-1][0] - first_samples[-2][0]
        )
        second_slope = (second_samples[1][1] - second_samples[0][1]) / (
            second_samples[1][0] - second_samples[0][0]
        )
        expected_slope = _tail_slope_dy_dx(seam, parameters)
        assert first_slope == pytest.approx(expected_slope, abs=1e-9)
        assert second_slope == pytest.approx(expected_slope, abs=1e-9)


def test_custom_solids_mesh_and_assembly_contacts_have_no_nominal_gap_or_overlap(
    review_job,
):
    job = review_job
    designs = {design.name: design for design in job.designs}
    for design in job.designs[10:]:
        assert design.shape.is_valid
        assert len(design.shape.solids()) == 1
        assert design.shape.volume > 0
        _, _, report = checked_mesh(design.shape)
        assert report["closed_oriented_manifold"]
        assert report["mesh_volume_mm3"] > 0

    tile_names = [
        "zeekr_rear_test_tile_1_4x4",
        "zeekr_rear_test_tile_2_4x4",
        "zeekr_rear_test_tile_3_2x4",
        "zeekr_rear_test_tile_4_4x4",
        "zeekr_rear_test_tile_5_4x4",
    ]
    north_names = [
        "zeekr_rear_north_1_4cell",
        "zeekr_rear_north_2_4cell",
        "zeekr_rear_north_3_2cell",
        "zeekr_rear_north_4_4cell",
        "zeekr_rear_north_5_4cell",
    ]
    south_names = [
        "zeekr_rear_south_ramp_1_4cell",
        "zeekr_rear_south_ramp_2_4cell",
        "zeekr_rear_south_ramp_3_2cell",
        "zeekr_rear_south_ramp_4_4cell",
        "zeekr_rear_south_ramp_5_4cell",
    ]
    contacts = [
        *zip(tile_names, north_names),
        *zip(tile_names, south_names),
        ("zeekr_rear_test_tile_1_4x4", "zeekr_rear_west_north_cap"),
        ("zeekr_rear_test_tile_1_4x4", "zeekr_rear_west_south_cap"),
        ("zeekr_rear_test_tile_5_4x4", "zeekr_rear_east_north_cap"),
        ("zeekr_rear_test_tile_5_4x4", "zeekr_rear_east_south_cap"),
        ("zeekr_rear_west_north_cap", "zeekr_rear_west_south_cap"),
        ("zeekr_rear_east_north_cap", "zeekr_rear_east_south_cap"),
        ("zeekr_rear_south_ramp_1_4cell", "zeekr_rear_west_south_cap"),
        ("zeekr_rear_south_ramp_5_4cell", "zeekr_rear_east_south_cap"),
        ("zeekr_rear_north_1_4cell", "zeekr_rear_west_north_cap"),
        ("zeekr_rear_north_5_4cell", "zeekr_rear_east_north_cap"),
        *zip(north_names, north_names[1:]),
        *zip(south_names, south_names[1:]),
    ]
    for first_name, second_name in contacts:
        first = _placed(designs[first_name])
        second = _placed(designs[second_name])
        assert first.distance_to(second) < 1e-6
        assert _intersection_volume(first, second) < 1e-7


@pytest.mark.reference
def test_supplied_scan_matches_frozen_review_registration_facts():
    source = os.environ.get("CARGO_GRID_ZEEKR_SCAN")
    if not source:
        pytest.skip("Set CARGO_GRID_ZEEKR_SCAN to verify the supplied local OBJ")
    report = inspect_scan_obj(Path(source))
    assert report["matches_review_source"]
    assert report["literal_counts"] == {
        "vertices": 5164,
        "texture_coordinates": 6206,
        "normals": 5161,
        "faces": 10096,
    }
    assert report["referenced_vertices"] == 5160
    assert report["pca_extents_obj"] == pytest.approx((1.332152, 0.398826, 0.046206), abs=5e-6)
