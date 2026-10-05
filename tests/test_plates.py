"""Shared plate planning: named groups, prime-tower reservations and fit reporting."""

import pytest
from build123d import Box
from tower_checks import assert_towers_stay_where_bambu_keeps_them

from cargo_grid import plates as plates_module
from cargo_grid.jobs import Design, Job
from cargo_grid.packing import TowerClearance, h2d_common_build
from cargo_grid.parameters import BuildVolume, Exclusion
from cargo_grid.plates import (
    AUTO_SUPPORT_FOOT_ALLOWANCE_MM,
    AUTO_SUPPORT_TOWER_CLEARANCE_MM,
    DEFAULT_PRIME_TOWER,
    H2D_TOWER_LEFT_CLEARANCE_MM,
    BambuTowerEstimate,
    PlateGroup,
    _H2DTowerLayout,
    _projected_bounds,
    plan_plates,
)


def _design(name, size, supported=False):
    return Design(name, Box(*size), {"family": "test", "supported": supported})


@pytest.fixture
def stub_support(monkeypatch):
    monkeypatch.setattr(
        plates_module, "needs_auto_support", lambda design: design.parameters["supported"]
    )


def _size(design):
    box = design.shape.bounding_box()
    return (box.size.X, box.size.Y, box.size.Z)


def test_groups_get_consecutive_named_plates_without_towers_by_default():
    groups = [
        PlateGroup("A", [_design(f"a{i}", (200, 200, 10)) for i in range(2)], 4),
        PlateGroup("B", [_design("b", (50, 50, 10))], 10),
    ]
    plan = plan_plates(groups, h2d_common_build(), size=_size)
    assert plan.plate_names == {0: "A 1", 1: "A 2", 2: "B"}
    assert plan.plate_count == 3
    assert plan.group_gaps == {"A": 4, "B": 10}
    assert plan.prime_tower is None and plan.prime_tower_positions == {}
    assert plan.auto_roof_support is None
    assert set(plan.plate_builds.values()) == {h2d_common_build()}


def test_h2d_auto_support_towers_only_plates_with_supported_designs(stub_support):
    groups = [
        PlateGroup("Plain", [_design("p", (50, 50, 10))], 4),
        PlateGroup(
            "Supported",
            [_design("s", (50, 50, 10), True), _design("t", (50, 50, 10))],
            4,
        ),
    ]
    plan = plan_plates(
        groups, h2d_common_build(), size=_size, auto_roof_support=True, layer_height_mm=0.32
    )
    reservation = _H2DTowerLayout(BambuTowerEstimate(0.32)).reserve(10)
    assert plan.prime_tower_positions == {1: (284.49, 15.0)} == {1: reservation.origin}
    assert plan.prime_tower_reaches == {1: reservation.reach}
    clear = TowerClearance(
        H2D_TOWER_LEFT_CLEARANCE_MM,
        AUTO_SUPPORT_TOWER_CLEARANCE_MM,
        AUTO_SUPPORT_TOWER_CLEARANCE_MM,
        AUTO_SUPPORT_TOWER_CLEARANCE_MM,
    )
    assert plan.prime_tower_clearances == {1: clear}
    assert plan.group_gaps == {"Plain": 4, "Supported": 4 + AUTO_SUPPORT_FOOT_ALLOWANCE_MM}
    assert plan.plate_builds[0] == h2d_common_build()
    assert plan.plate_builds[1] == reservation.build
    k0, _, _, l1 = clear.grow(reservation.bounds)
    for design, placement in zip(plan.designs, plan.placements):
        if placement.plate == 1:
            assert placement.x + _size(design)[0] <= k0 or placement.y >= l1
    record = plan.auto_roof_support
    assert record["prime_tower_plates"] == [2]
    assert record["prime_tower_groups"] == ["Supported"]
    assert record["prime_tower_origins_mm"] == {2: (284.49, 15.0)}
    assert record["model_clearance_to_tower_bounds_mm"]["left"] == H2D_TOWER_LEFT_CLEARANCE_MM


def test_h2d_auto_support_needs_the_layer_height(stub_support):
    groups = [PlateGroup("S", [_design("s", (50, 50, 10), True)], 4)]
    with pytest.raises(ValueError, match="layer height"):
        plan_plates(groups, h2d_common_build(), size=_size, auto_roof_support=True)


@pytest.mark.parametrize(
    "layer_height,height,side,origin",
    [
        # Positions Bambu Studio 02.08.02.61 wrote when it opened CargoGrid's catalogues.
        (0.32, 14.92, 25.506, (284.49, 15.0)),
        (0.2, 14.92, 30.765, (279.23, 15.0)),
        # The 0.4 nozzle profile's 0.24 mm layers.
        (0.24, 14.92, 28.577, (281.42, 15.0)),
        # The 120 mm bracket plate is deeper than the purge needs, so height sets the size.
        (0.32, 120.0, 28.324, (281.67, 15.0)),
    ],
)
def test_bambu_tower_estimate_matches_bambu_positions(layer_height, height, side, origin):
    estimate = BambuTowerEstimate(layer_height)
    assert estimate.side_mm(height) == pytest.approx(side, abs=1e-3)
    reservation = _H2DTowerLayout(estimate).reserve(height)
    assert reservation.origin == origin
    x, y = origin
    brim = estimate.brim_mm(height)
    assert reservation.bounds == pytest.approx(
        (x - brim, y - brim, 325, y + estimate.side_mm(height) + brim + 1)
    )
    assert BambuTowerEstimate.brim_mm(50) == 4 and BambuTowerEstimate.brim_mm(150) == 8


def test_h2d_towers_keep_tiles_with_a_five_cell_side_beside_them(stub_support):
    """A 246 mm-wide tile at the inset edge still fits beside the 0.24 mm-layer tower."""
    wide = _design("wide", (246, 306, 14.92), True)
    plan = plan_plates(
        [PlateGroup("Tiles", [wide], 8)],
        h2d_common_build(),
        size=_size,
        auto_roof_support=True,
        layer_height_mm=0.24,
    )
    assert plan.unfit == [] and plan.placements[0].x == 30
    job = Job(
        plan.designs,
        BuildVolume(350, 320, 325),
        "catalogue",
        print_placements=plan.placements,
        plate_builds=plan.plate_builds,
        prime_tower=plan.prime_tower,
        prime_tower_positions=plan.prime_tower_positions,
        prime_tower_reaches=plan.prime_tower_reaches,
        prime_tower_clearances=plan.prime_tower_clearances,
    )
    assert_towers_stay_where_bambu_keeps_them(job, 0.24, size=_size)


def test_generic_build_uses_a_front_left_corner_and_reports_unfit_designs(stub_support):
    build = BuildVolume(200, 200, 50)
    wide = _design("wide", (190, 190, 10))
    groups = [PlateGroup("G", [_design("s", (40, 40, 10), True), wide], 2)]
    plan = plan_plates(groups, build, size=_size, auto_roof_support=True)
    assert plan.unfit == [wide]
    (origin,) = set(plan.prime_tower_positions.values())
    # Bambu Studio moves towers closer than 15 mm to the bed edges, so they start there.
    assert origin == (15, 15)
    x0, y0, x1, y1 = plan.prime_tower.footprint(*origin)
    assert (x0, y0) == (15 - DEFAULT_PRIME_TOWER.left, 15 - DEFAULT_PRIME_TOWER.front)
    clear = AUTO_SUPPORT_TOWER_CLEARANCE_MM
    assert all(
        placement.x >= x1 + clear or placement.y >= y1 + clear for placement in plan.placements
    )
    assert plan.auto_roof_support["reserved_tower_bounds_mm"] == {1: (x0, y0, x1, y1)}
    assert plan.auto_roof_support["prime_tower_groups"] == ["G"]


def test_projected_bounds_keep_side_exclusions_and_return_corner_obstacles():
    assert _projected_bounds(h2d_common_build()) == ((30, 5, 320, 315), ())
    reservation = _H2DTowerLayout(BambuTowerEstimate(0.32)).reserve(14.92)
    bounds, (area,) = _projected_bounds(reservation.build)
    assert bounds == (30, 6, 320, 314)
    assert area == pytest.approx(reservation.clearance.grow(reservation.bounds))
    inner = BuildVolume(200, 200, 50, exclusions=(Exclusion(50, 50, 10, 10),))
    assert _projected_bounds(inner) == ((0, 0, 200, 200), ((50, 50, 60, 60),))
