"""Nominal geometry checks, not physical fit or strength certification."""

from dataclasses import FrozenInstanceError, replace
from math import cos, radians, sin

import pytest
from build123d import Location, Vector

from cargo_grid.accessories import Accessory, accessory_datums, make_accessory
from cargo_grid.interfaces import make_plug
from cargo_grid.parameters import Interface

STANDARD = [
    *(Accessory(family, nx=n) for family in ("edge-x", "edge-y", "support") for n in range(1, 5)),
    *(Accessory("corner-in", variant=v) for v in range(1, 5)),
    *(Accessory("corner-out", variant=v) for v in range(1, 7)),
    *(Accessory("vertical-tile-bracket", nx=x, ny=y) for x, y in ((1, 2), (2, 1), (2, 2))),
    *(
        Accessory("vertical-stop", nx=x, ny=y, height=height)
        for x, y in ((1, 2), (2, 1), (2, 2))
        for height in (60, 120)
    ),
    *(Accessory("lock-45", nx=n, ny=n) for n in (1, 2)),
    *(Accessory("plate", nx=x, ny=y) for x, y in ((1, 1), (1, 2), (2, 2))),
    *(Accessory("support-bit", length=n) for n in (20, 30, 40, 50)),
    *(Accessory("support-end", variant=v) for v in range(1, 5)),
]


@pytest.mark.parametrize(
    "spec", STANDARD, ids=lambda s: f"{s.family}-{s.nx}-{s.ny}-{s.variant}-{s.length}-{s.height}"
)
@pytest.mark.parametrize("joint_style", ["full-height", "original"])
def test_standard_families_are_connected_and_labeled(spec, joint_style):
    spec = replace(spec, interface=replace(spec.interface, joint_style=joint_style))
    part = make_accessory(spec)
    assert part.is_valid
    assert len(part.solids()) == 1
    assert part.volume > 0
    assert part.label.startswith(spec.family)
    box = part.bounding_box()
    if spec.family in ("vertical-tile-bracket", "vertical-stop", "lock-45", "plate"):
        assert box.min.Z == pytest.approx(-12.8)
        top = (
            spec.ny * 60 + 7.578174593052
            if spec.family == "vertical-tile-bracket"
            else spec.height
            if spec.family == "vertical-stop"
            else 4.1
            if spec.family == "plate"
            else spec.height
        )
        assert box.max.Z == pytest.approx(top)
        assert len(accessory_datums(spec)["mount_centers"]) == spec.nx * spec.ny
    elif spec.family.startswith("support"):
        assert box.size.X == pytest.approx(45)
        assert box.min.Z == pytest.approx(-25)
        assert box.max.Z == pytest.approx(0, abs=1e-6)
        expected = (
            spec.length + 5
            if spec.family == "support-bit"
            else 120 + (5 if spec.variant in (3, 4) else 0)
            if spec.family == "support-end"
            else spec.nx * 60 + 5
        )
        assert box.size.Y == pytest.approx(expected)
        assert accessory_datums(spec)["mount_centers"] == []
    else:
        assert box.min.Z == pytest.approx(0, abs=1e-6)
        assert box.max.Z == pytest.approx(13)


@pytest.mark.parametrize("family,depth", [("edge-x", 16), ("edge-y", 10)])
@pytest.mark.parametrize("joint_style", ["full-height", "original"])
def test_straight_edge_envelopes_and_join_pitch(family, depth, joint_style):
    spec = Accessory(family, nx=5, interface=Interface(joint_style=joint_style))
    part = make_accessory(spec)
    assert tuple(part.bounding_box().size) == pytest.approx((300, depth, 13))
    joins = accessory_datums(spec)["joins"]
    assert [j["position"][0] for j in joins] == [30, 90, 150, 210, 270]
    assert {j["sex"] for j in joins} == {"male" if family == "edge-x" else "female"}
    assert all(
        (j["height"] == 13) if joint_style == "full-height" else (j["height"] < 13) for j in joins
    )


@pytest.mark.parametrize(
    "variant,size,sexes",
    [
        (1, (60, 66), ("female", "male")),
        (2, (66, 66), ("male", "male")),
        (3, (66, 60), ("male", "female")),
        (4, (60, 60), ("female", "female")),
    ],
)
def test_inner_corner_directional_joins(variant, size, sexes):
    spec = Accessory("corner-in", variant=variant)
    box = make_accessory(spec).bounding_box()
    assert (box.size.X, box.size.Y) == pytest.approx(size)
    assert tuple(j["sex"] for j in accessory_datums(spec)["joins"]) == sexes


@pytest.mark.parametrize(
    "variant,size",
    [
        (1, (16, 69.12132034355965)),
        (2, (69.12132034355965, 10)),
        (3, (70, 70)),
        (4, (10, 69.12132034355965)),
        (5, (69.12132034355965, 16)),
        (6, (70, 70)),
    ],
)
def test_outer_corner_miter_and_envelopes(variant, size):
    box = make_accessory(Accessory("corner-out", variant=variant)).bounding_box()
    assert (box.size.X, box.size.Y) == pytest.approx(size)


@pytest.mark.parametrize(
    "spec",
    [
        Accessory("edge-x", complete_edge_holes=False),
        Accessory("edge-y", complete_edge_holes=False),
        *(Accessory("corner-in", variant=v, complete_edge_holes=False) for v in range(1, 5)),
        *(Accessory("corner-out", variant=v, complete_edge_holes=False) for v in range(1, 7)),
        Accessory("edge-x", interface=Interface(height=14), complete_edge_holes=False),
        Accessory("edge-y", interface=Interface(height=14), complete_edge_holes=False),
    ],
    ids=lambda s: f"{s.family}-v{s.variant}-h{s.interface.height}",
)
def test_original_tile_facing_joins_preserve_rounded_shoulders(spec):
    spec = replace(spec, interface=replace(spec.interface, joint_style="original"))
    part = make_accessory(spec)
    assert part.bounding_box().max.Z == pytest.approx(spec.interface.height)
    for join in accessory_datums(spec)["joins"]:
        male = join["sex"] == "male"
        angle = radians(join["angle"])
        x, y, z = join["position"]
        # A 1 mm concave shoulder reaches 1-sqrt(3)/2 mm beyond the wall here.
        for depth, in_tool in ((0.1, True), (0.2, False)):
            point = Vector(
                x - depth * sin(angle),
                y + depth * cos(angle),
                z + join["height"] + 0.5,
            )
            assert part.is_inside(point) == (in_tool if male else not in_tool)


def test_plug_shoulder_registration_preserves_shared_plug():
    from build123d import Axis

    spec = Accessory("plate", ny=2)
    part = make_accessory(spec)
    centers = accessory_datums(spec)["mount_centers"]
    assert centers == [(30, 30, 0), (30, 90, 0)]
    for center in centers:
        plug = make_plug().rotate(Axis.X, 180).moved(Location(center))
        assert plug.cut(part).volume == pytest.approx(0, abs=1e-5)
        assert part.is_inside(Vector(center[0], center[1], 0.01))
    assert accessory_datums(spec)["plug_tip_z"] == -12.8


def test_filled_angled_stop_retains_inclined_cargo_face():
    part = make_accessory(Accessory("lock-45"))
    for height in (10, 30, 45):
        lean = height - 4.1
        y = 60 - lean
        assert part.is_inside(Vector(30, y - 3, height))
        assert not part.is_inside(Vector(30, y + 0.1, height))
        assert part.is_inside(Vector(30, y - 6.1, height))


def test_support_connections_are_full_height_and_not_tile_joins():
    spec = Accessory("support-bit", length=20)
    datums = accessory_datums(spec)
    male, female = datums["joins"]
    assert (male["depth"], female["depth"]) == (5, 5.085)
    assert all(
        j["height"] == 25 and j["position"][2] == -25 and j["interface"] == "support-dovetail"
        for j in datums["joins"]
    )
    rail = make_accessory(spec)
    following = rail.moved(Location((0, 20, 0)))
    assert rail.volume - rail.cut(following).volume == pytest.approx(0, abs=1e-4)
    assert rail.distance_to(following) == pytest.approx(0, abs=1e-5)


@pytest.mark.parametrize(
    "variant,name,ramp,sex",
    [
        (1, "X", 75, "female"),
        (2, "Xs", 55, "female"),
        (3, "Y", 75, "male"),
        (4, "Ys", 55, "male"),
    ],
)
def test_support_end_bearing_ramp_datums(variant, name, ramp, sex):
    spec = Accessory("support-end", variant=variant)
    datums = accessory_datums(spec)
    assert datums["end"] == name
    assert datums["ramp_length"] == ramp
    assert datums["ramp_rise"] == 13
    assert len(datums["joins"]) == 1
    assert datums["joins"][0]["sex"] == sex
    rail = make_accessory(spec)
    end = 1 if variant in (1, 2) else 119
    assert rail.is_inside(Vector(20, end, -10))
    assert not rail.is_inside(Vector(20, end, -20))


def test_custom_pitch_and_extended_rails():
    spec = Accessory("support", nx=6, interface=Interface(pitch=65))
    assert make_accessory(spec).bounding_box().size.Y == pytest.approx(395)
    assert accessory_datums(spec)["joins"][1]["position"][1] == 390


@pytest.mark.parametrize(
    "kwargs",
    [
        {"family": "unknown"},
        {"family": "plate", "nx": 0},
        {"family": "plate", "nx": True},
        {"family": "edge-x", "nx": 1.5},
        {"family": "plate", "nx": 2, "ny": 1},
        {"family": "lock-90", "nx": 4},
        {"family": "vertical-tile-bracket"},
        {"family": "vertical-tile-bracket", "nx": 3},
        {"family": "vertical-tile-bracket", "nx": 2, "height": 100},
        {"family": "vertical-stop"},
        {"family": "vertical-stop", "nx": 2, "ny": 1, "height": 100},
        {"family": "lock-45", "ny": 2},
        {"family": "corner-in", "variant": 5},
        {"family": "corner-out", "variant": 7},
        {"family": "support-end", "variant": 5},
        {"family": "support-end", "nx": 2},
        {"family": "edge-y", "ny": 2},
        {"family": "edge-x", "variant": 2},
        {"family": "edge-x", "edge_outward": 15},
        {"family": "edge-x", "complete_edge_holes": 1},
        {"family": "plate", "edge_outward": 20},
        {"family": "support", "complete_edge_holes": True},
        {"family": "support-bit", "length": 19},
        {"family": "support-bit", "length": float("nan")},
        {"family": "support-bit", "length": float("inf")},
        {"family": "lock-90", "height": 11},
        {"family": "lock-45", "height": 61},
        {"family": "plate", "interface": None},
    ],
)
def test_invalid_accessory_specifications(kwargs):
    with pytest.raises(ValueError):
        Accessory(**kwargs)


def test_accessory_specification_is_frozen():
    spec = Accessory("plate")
    with pytest.raises(FrozenInstanceError):
        spec.nx = 2
    with pytest.raises(ValueError):
        make_accessory(None)
