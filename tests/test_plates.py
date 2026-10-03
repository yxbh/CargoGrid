"""Shared plate planning: named groups, prime-tower reservations and fit reporting."""

import pytest
from build123d import Box

from cargo_grid import plates as plates_module
from cargo_grid.jobs import Design
from cargo_grid.packing import h2d_common_build
from cargo_grid.parameters import BuildVolume, Exclusion
from cargo_grid.plates import (
    AUTO_SUPPORT_FOOT_ALLOWANCE_MM,
    H2D_AUTO_SUPPORT_MODEL_MAX_X,
    H2D_AUTO_SUPPORT_TOWER_ORIGIN,
    PlateGroup,
    _projected_bounds,
    h2d_auto_support_build,
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
    plan = plan_plates(groups, h2d_common_build(), size=_size, auto_roof_support=True)
    assert plan.prime_tower_positions == {1: H2D_AUTO_SUPPORT_TOWER_ORIGIN}
    assert plan.group_gaps == {"Plain": 4, "Supported": 4 + AUTO_SUPPORT_FOOT_ALLOWANCE_MM}
    assert plan.plate_builds[0] == h2d_common_build()
    assert plan.plate_builds[1] == h2d_auto_support_build()
    for design, placement in zip(plan.designs, plan.placements):
        if placement.plate == 1:
            assert placement.x + _size(design)[0] <= H2D_AUTO_SUPPORT_MODEL_MAX_X
    assert plan.auto_roof_support["prime_tower_plates"] == [2]
    assert plan.auto_roof_support["prime_tower_groups"] == ["Supported"]


def test_generic_build_uses_a_left_column_and_reports_unfit_designs(stub_support):
    build = BuildVolume(200, 200, 50)
    wide = _design("wide", (190, 190, 10))
    groups = [PlateGroup("G", [_design("s", (40, 40, 10), True), wide], 2)]
    plan = plan_plates(groups, build, size=_size, auto_roof_support=True)
    assert plan.unfit == [wide]
    (origin,) = set(plan.prime_tower_positions.values())
    x1 = plan.prime_tower.footprint(*origin)[2]
    assert all(
        placement.x >= x1 + 2 + AUTO_SUPPORT_FOOT_ALLOWANCE_MM for placement in plan.placements
    )
    assert plan.auto_roof_support["model_min_x_mm"] == x1 + 6
    assert plan.auto_roof_support["prime_tower_groups"] == ["G"]


def test_projected_bounds_follow_side_exclusions_only():
    assert _projected_bounds(h2d_common_build()) == (30, 5, 320, 315)
    assert _projected_bounds(h2d_auto_support_build()) == (30, 6, 276.5, 314)
    with pytest.raises(ValueError, match="full-depth side exclusions"):
        _projected_bounds(BuildVolume(200, 200, 50, exclusions=(Exclusion(50, 50, 10, 10),)))
