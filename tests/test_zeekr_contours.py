"""Selected contour geometry and assembled mating checks."""

import pytest
from build123d import Axis, Edge, GeomType, Location, Solid, Wire
from local_geometry import bounded_distance
from OCP.BRepAdaptor import BRepAdaptor_Surface
from zeekr_assembly import assemble_rear_panel

from cargo_grid.interfaces import make_plug
from cargo_grid.meshes import checked_mesh
from cargo_grid.parameters import Tile
from cargo_grid.tiles import make_tile
from cargo_grid.vehicles.zeekr_7x_contours import (
    CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU,
    DEFAULT_EAST_EDGE_X_MM,
    DEFAULT_PEN_OFFSET_MM,
    TILE_MODULES,
    TRACED_NORTH_CORNER_RADIUS_MM,
    ContourOutline,
    ContourParameters,
    _corrected_north_edge_station_arrays,
    _crown_coefficients,
    _merged_taper_stations,
    _north_corner_geometry,
    _side_design,
    _south_design,
    _tail_slope_dy_dx,
    _taper_interpolators,
    rear_panel_designs,
    right_outline_x_mm,
    right_wall_x_mm,
    tail_y_mm,
)


def _placed(design):
    assert len(design.assembly_frames) == 1
    return design.shape.moved(Location(design.assembly_frames[0]))


def _intersection_volume(first, second):
    intersection = first.intersect(second)
    return 0 if intersection is None else sum(solid.volume for solid in intersection.solids())


def _witness_intersection_volume(placed, bore):
    witness = bore.bounding_box()
    candidates = []
    for shape, bounds in placed:
        if (
            bounds.min.X <= witness.max.X
            and bounds.max.X >= witness.min.X
            and bounds.min.Y <= witness.max.Y
            and bounds.max.Y >= witness.min.Y
            and bounds.min.Z <= witness.max.Z
            and bounds.max.Z >= witness.min.Z
        ):
            candidates.append(shape)
    return sum(
        solid.volume
        for shape in candidates
        for intersection in [shape.intersect(bore)]
        if intersection is not None
        for solid in intersection.solids()
    )


@pytest.fixture(scope="module")
def assembled_parts():
    return assemble_rear_panel()


@pytest.fixture(scope="module")
def assembled_witnesses(assembled_parts):
    placed = [_placed(design) for design in assembled_parts]
    return [(shape, shape.bounding_box()) for shape in placed]


def test_outline_curve_is_owned_and_uses_vehicle_coordinates():
    curve = Wire.make_polygon(
        [(-605, 620, 0), (615, 620, 0), (615, 990, 0), (-605, 990, 0)], close=True
    )
    outline = ContourOutline(curve)
    same = ContourOutline(curve)
    assert outline.digest == same.digest
    assert len(outline.digest) == 64
    curve.move(Location((100, 200, 0)))
    assert outline.north_y(0) == pytest.approx(620, abs=1e-7)
    assert outline.south_y(0) == pytest.approx(990, abs=1e-7)
    assert outline.side_x("west", 800) == pytest.approx(-605, abs=1e-7)
    assert outline.side_x("east", 800) == pytest.approx(615, abs=1e-7)
    assert outline.south_slope(0) == pytest.approx(0, abs=1e-12)
    parameters = ContourParameters(outline=outline)
    assert tail_y_mm(0, parameters) == pytest.approx(990, abs=1e-7)
    assert _tail_slope_dy_dx(0, parameters) == pytest.approx(0, abs=1e-12)
    with pytest.raises(ValueError, match="construction ray"):
        outline.south_y(700)
    with pytest.raises(ValueError, match="preset outline adjustments"):
        ContourParameters(outline=outline, outline_inset_mm=1)


def test_outline_stations_interpolate_one_closed_curve():
    from math import cos, pi, sin

    stations = [(620 * cos(index * pi / 6), 800 + 190 * sin(index * pi / 6)) for index in range(12)]
    outline = ContourOutline.from_stations(stations)
    assert outline.north_y(0) == pytest.approx(610, abs=1e-7)
    assert outline.south_y(0) == pytest.approx(990, abs=1e-7)
    assert outline.side_x("west", 800) == pytest.approx(-620, abs=1e-7)
    assert outline.side_x("east", 800) == pytest.approx(620, abs=1e-7)
    measured_slope = (outline.south_y(0.01) - outline.south_y(-0.01)) / 0.02
    assert outline.south_slope(0) == pytest.approx(measured_slope, abs=1e-8, rel=0)
    closed = ContourOutline.from_stations([*stations, stations[0]])
    assert closed.digest == outline.digest
    for invalid in (stations[:3], [*stations[:3], (float("nan"), 0)]):
        with pytest.raises(ValueError, match="distinct finite"):
            ContourOutline.from_stations(invalid)
    with pytest.raises(ValueError, match="closed wire"):
        ContourOutline(Edge.make_line((0, 0, 0), (1, 1, 0)))
    with pytest.raises(ValueError, match="Z=0"):
        ContourOutline(Wire.make_polygon([(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], close=True))


@pytest.mark.slow
def test_supplied_outline_drives_all_nine_real_contour_parts_and_identity():
    outline = ContourOutline(
        Wire.make_polygon(
            [(-605, 620, 0), (615, 620, 0), (615, 990, 0), (-605, 990, 0)], close=True
        )
    )
    parameters = ContourParameters(outline=outline)
    caps, ramps = rear_panel_designs(parameters)
    assert len(caps) == 4
    assert len(ramps) == 5
    for design in caps + ramps:
        _assert_valid_single_solid(design)
        assert design.size[2] == pytest.approx(13, abs=1e-6)
        assert design.name.endswith(f"_outline-{outline.digest[:16]}")
        assert design.parameters["outline"]["sha256"] == outline.digest
    west = next(design for design in caps if design.parameters["side"] == "west")
    east = next(design for design in caps if design.parameters["side"] == "east")
    assert west.shape.bounding_box().min.X == pytest.approx(-65, abs=1e-6)
    assert east.shape.bounding_box().max.X == pytest.approx(75, abs=1e-6)
    for ramp in ramps:
        assert ramp.parameters["finished_run_range_mm"] == pytest.approx((100, 100), abs=1e-7)
        assert "south_contour_source" not in ramp.parameters
        for _, y, slope in ramp.mating_datums["global_contour_samples_x_y_slope"]:
            assert y == pytest.approx(990, abs=1e-7)
            assert slope == pytest.approx(0, abs=1e-12)
    _, _, mesh = checked_mesh(ramps[0].shape)
    assert mesh["closed_oriented_manifold"]


@pytest.mark.slow
def test_assembled_panel_has_selected_frames_and_complementary_perimeter_joins(assembled_parts):
    assert len(assembled_parts) == 19
    assert [design.assembly_frames[0] for design in assembled_parts[:5]] == [
        (-540, 60, 0),
        (-300, 60, 0),
        (-60, 60, 0),
        (60, 60, 0),
        (300, 60, 0),
    ]
    assert [design.parameters["nx"] for design in assembled_parts[:5]] == list(TILE_MODULES)
    north, south = assembled_parts[5:10], assembled_parts[10:15]
    assert {design.assembly_frames[0][1] for design in north} == {300}
    assert {design.assembly_frames[0][1] for design in south} == {60}
    assert {join["sex"] for design in north for join in design.mating_datums["joins"]} == {"female"}
    assert {join["sex"] for design in south for join in design.mating_datums["joins"]} == {"male"}


def test_measured_outline_uses_exact_mirrored_pchip_and_named_inset():
    parameters = ContourParameters()
    stations = _merged_taper_stations(
        parameters.pen_offset_mm,
        parameters.se_along_edge_shift_mm,
    )
    for u, s in stations:
        y = 620 + s
        x = parameters.east_edge_x_mm - u
        assert right_wall_x_mm(y) == pytest.approx(x)
        assert -right_wall_x_mm(y) == pytest.approx(-x)
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
        list(zip(*_corrected_north_edge_station_arrays(parameters.pen_offset_mm), strict=True))[
            ::-1
        ],
        expected_north_stations,
        strict=True,
    ):
        assert actual == pytest.approx(expected)
    corner = _north_corner_geometry(parameters)
    assert corner["center"] == pytest.approx((596.1, 633.277017506))
    assert corner["north_tangent"] == pytest.approx((596.578655647, 626.894941921))
    assert right_outline_x_mm(
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


def _assert_valid_single_solid(design):
    assert design.shape.is_valid
    assert len(design.shape.solids()) == 1
    assert design.shape.volume > 0


def _assert_bores_clear(design, centers):
    for center in centers:
        bore = Solid.make_cylinder(4.99, 13).moved(Location((*center, 0)))
        assert _intersection_volume(design.shape, bore) == pytest.approx(0, abs=1e-8)


def _assert_contact_without_overlap(first, second):
    assert bounded_distance(first, second) < 1e-6
    assert _intersection_volume(first, second) < 1e-7


@pytest.fixture(scope="module")
def representative_tile():
    return make_tile(Tile(4, 4))


def test_representative_real_contour_west_side_caps_holes_sockets_and_contacts(
    representative_tile,
):
    parameters = ContourParameters()
    west_north = _side_design("west", "north", parameters)
    west_south = _side_design("west", "south", parameters)
    for design in (west_north, west_south):
        _assert_valid_single_solid(design)
    _, _, report = checked_mesh(west_south.shape)
    assert report["closed_oriented_manifold"]
    assert {join["sex"] for join in west_north.mating_datums["joins"]} == {"male"}
    assert {
        center[1]
        for center in west_north.mating_datums["accessory_socket_centers"]
        + west_south.mating_datums["accessory_socket_centers"]
    } == {150, 210, 270}
    _assert_bores_clear(west_south, ((-30, 60), (-30, 180)))
    _assert_bores_clear(west_north, ((-30, 180), (-30, 300)))
    placed_tile_west = representative_tile.moved(Location((-540, 60, 0)))
    for first, second in (
        (placed_tile_west, _placed(west_north)),
        (placed_tile_west, _placed(west_south)),
        (_placed(west_north), _placed(west_south)),
    ):
        _assert_contact_without_overlap(first, second)


def test_representative_real_contour_east_side_cap_is_female_and_meets_its_tile(
    representative_tile,
):
    east_north = _side_design("east", "north", ContourParameters())
    _assert_valid_single_solid(east_north)
    assert {join["sex"] for join in east_north.mating_datums["joins"]} == {"female"}
    _assert_contact_without_overlap(
        representative_tile.moved(Location((300, 60, 0))), _placed(east_north)
    )


def test_representative_real_contour_west_ramp_holes_joins_mesh_and_contact(representative_tile):
    west_ramp = _south_design(4, 0, -540, ContourParameters())
    _assert_valid_single_solid(west_ramp)
    _, _, report = checked_mesh(west_ramp.shape)
    assert report["closed_oriented_manifold"]
    assert {join["sex"] for join in west_ramp.mating_datums["joins"]} == {"male"}
    _assert_bores_clear(west_ramp, ((0, 0), (30, 0), (60, 0), (240, 0)))
    _assert_contact_without_overlap(
        representative_tile.moved(Location((-540, 60, 0))), _placed(west_ramp)
    )


@pytest.mark.slow
def test_side_caps_have_grid_sockets_and_completed_boundary_holes(
    assembled_parts, assembled_witnesses
):
    job = assembled_parts
    caps = [
        design for design in job if design.parameters.get("family") == "zeekr-rear-contour-side"
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

    for side_x in (-540, 540):
        for y in range(60, 301, 30):
            bore = Solid.make_cylinder(4.99, 13).moved(Location((side_x, y, 0)))
            assert _witness_intersection_volume(assembled_witnesses, bore) == pytest.approx(
                0, abs=1e-8
            )
    for side_x in (-570, 570):
        for y in range(60, 301, 60):
            bore = Solid.make_cylinder(4.99, 13).moved(Location((side_x, y, 0)))
            assert _witness_intersection_volume(assembled_witnesses, bore) == pytest.approx(
                0, abs=1e-8
            )


@pytest.mark.slow
def test_side_caps_have_exact_mirrored_pen_corrected_r6_4_north_corners(
    assembled_parts,
):
    caps = [
        design
        for design in assembled_parts
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


@pytest.mark.slow
def test_south_ramps_complete_every_tile_boundary_hole_through_tabs_and_shelf(
    assembled_parts, assembled_witnesses
):
    job = assembled_parts
    ramps = [
        design for design in job if design.parameters.get("family") == "zeekr-rear-contour-ramp"
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

    for x, y in expected_global:
        bore = Solid.make_cylinder(4.99, 13).moved(Location((x, y, 0)))
        assert _witness_intersection_volume(assembled_witnesses, bore) == pytest.approx(0, abs=1e-8)

    north_centers = {
        (
            design.assembly_frames[0][0] + hole["x"],
            design.assembly_frames[0][1] + hole["y"],
        )
        for design in job[5:10]
        for hole in design.holes
        if hole["accepted"]
    }
    assert north_centers == {(float(x), 300.0) for x in range(-540, 541, 30)}
    for x, y in north_centers:
        bore = Solid.make_cylinder(4.99, 13).moved(Location((x, y, 0)))
        assert _witness_intersection_volume(assembled_witnesses, bore) == pytest.approx(0, abs=1e-8)


@pytest.mark.slow
def test_south_modules_share_endpoints_and_plan_tangents(assembled_parts):
    job = assembled_parts
    parameters = ContourParameters()
    south = job[10:15]
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


@pytest.mark.slow
def test_custom_solids_mesh_and_assembly_contacts_have_no_nominal_gap_or_overlap(
    assembled_parts,
):
    job = assembled_parts
    designs = {design.name: design for design in job}
    for design in job[10:]:
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
        assert bounded_distance(first, second) < 1e-6
        assert _intersection_volume(first, second) < 1e-7
