"""Selective visible radii preserve real mating geometry, not only constants."""

from math import cos, radians, sqrt

import pytest
from build123d import Axis, GeomType, Location, Part, Solid, Vector, export_step, import_step
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.Precision import Precision

from cargo_grid import Interface
from cargo_grid.accessories import (
    Accessory,
    _apply_joins,
    _downward_plug,
    _edge_plan,
    _join_solid,
    _mounted_base,
    _support,
    accessory_datums,
    make_accessory,
)
from cargo_grid.export import _checked_step_roundtrip
from cargo_grid.interfaces import dovetail_face, full_height_part, horizontal_edges, prism


def volume(shape):
    return sum(s.volume for s in shape.solids()) if shape and shape.solids() else 0


def unrounded_edge(spec):
    face, joins = _edge_plan(spec)
    if spec.interface.joint_style == "original":
        part = _apply_joins(prism(face, spec.interface.height), joins, interface=spec.interface)
    else:
        male, female = [], []
        for join in joins:
            profile = dovetail_face(depth=join["depth"]).rotate(Axis.Z, join["angle"])
            profile = profile.moved(Location(join["position"]))
            (male if join["sex"] == "male" else female).append(profile)
        part = full_height_part(face, male, female, spec.interface.height, round_body_corners=False)
    return part.fillet(1, horizontal_edges(part, 0))


@pytest.mark.parametrize("style", ["original", "full-height"])
@pytest.mark.parametrize(
    "spec",
    [
        Accessory("edge-x", nx=2, complete_edge_holes=False),
        Accessory("edge-y", nx=2, complete_edge_holes=False),
        *(Accessory("corner-in", variant=v, complete_edge_holes=False) for v in range(1, 5)),
        *(Accessory("corner-out", variant=v, complete_edge_holes=False) for v in range(1, 7)),
    ],
)
def test_perimeter_rounds_exist_in_step_and_join_tools_are_preserved(spec, style, tmp_path):
    from dataclasses import replace

    spec = replace(spec, interface=Interface(joint_style=style))
    before, after = unrounded_edge(spec), make_accessory(spec)
    assert after.volume < before.volume
    for join in accessory_datums(spec)["joins"]:
        if style == "original":
            tool = _join_solid(join, interface=spec.interface)
            if join["sex"] == "male":
                assert volume(tool.cut(after)) < 1e-7
            else:
                assert volume(tool.intersect(after)) < 1e-7
        else:
            region = Solid.make_box(52, join["depth"] + 5, 13).moved(Location((-26, -4, 0)))
            region = region.rotate(Axis.Z, join["angle"]).moved(Location(join["position"]))
            a, b = before.intersect(region), after.intersect(region)
            assert volume(Part(a.solids()).cut(Part(b.solids()))) < 1e-5
    path = tmp_path / "rounded.step"
    restored, _, _, _, _ = _checked_step_roundtrip(after, path)
    assert restored.is_valid
    volume_budget = max(1e-6, after.area * Precision.Confusion_s())
    assert abs(restored.volume - after.volume) <= volume_budget
    expected_radius = 3 if style == "original" else 2
    assert any(
        face.geom_type == GeomType.CYLINDER
        and BRepAdaptor_Surface(face.wrapped).Cylinder().Radius() == pytest.approx(expected_radius)
        for face in restored.faces()
    )


@pytest.mark.parametrize(
    "spec",
    [
        Accessory("support", nx=2),
        Accessory("support-bit", length=20),
        *(Accessory("support-end", variant=v) for v in range(1, 5)),
    ],
)
def test_support_rounding_preserves_full_height_dovetails(spec, tmp_path):
    before, after = _support(spec, round_top=False), make_accessory(spec)
    assert abs(after.volume - before.volume) > 1
    for join in accessory_datums(spec)["joins"]:
        tool = _join_solid(join)
        if join["sex"] == "male":
            assert volume(tool.cut(after)) < 1e-7
        else:
            assert volume(tool.intersect(after)) < 1e-7
    path = tmp_path / "rail.step"
    restored, _, _, _, _ = _checked_step_roundtrip(
        after,
        path,
    )
    volume_budget = max(1e-6, after.area * Precision.Confusion_s())
    adaptive_volumes = []
    for shape in (after, restored):
        properties = GProp_GProps()
        error = BRepGProp.VolumeProperties_s(shape.wrapped, properties, 1e-12, True, False)
        assert error < 1e-10
        adaptive_volumes.append(properties.Mass())
    assert abs(adaptive_volumes[1] - adaptive_volumes[0]) <= volume_budget
    radii = [
        BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()
        for face in restored.faces()
        if face.geom_type == GeomType.CYLINDER
    ]
    expected_radii = (
        {
            1: {0.75, 1.0, 1.5, 2.5},
            2: {0.25, 1.5, 2.0, 3.0},
            3: {1.0, 2.0, 3.0},
            4: {0.75, 1.0, 2.0, 3.0},
        }[spec.variant]
        if spec.family == "support-end"
        else {3.0}
    )
    assert all(
        any(radius == pytest.approx(expected) for radius in radii) for expected in expected_radii
    )


@pytest.mark.parametrize("variant", range(1, 5))
def test_support_end_window_rims_have_no_sharp_free_edges(variant):
    shape = make_accessory(Accessory("support-end", variant=variant))
    for edge in shape.edges():
        center = edge.center()
        if not (-10.01 <= center.X <= 10.01 and 8 < center.Y < 112):
            continue
        adjacent = [
            face
            for face in shape.faces()
            if any(candidate.is_same(edge) for candidate in face.edges())
        ]
        if len(adjacent) != 2:
            continue
        normals = [face.normal_at(center) for face in adjacent]
        assert normals[0].dot(normals[1]) >= cos(radians(0.001)), (
            variant,
            tuple(center),
            [face.geom_type.name for face in adjacent],
        )


@pytest.mark.parametrize("n,expected", [(1, 123369.26344133946), (2, 474943.3630807313)])
def test_filled_angled_stops_protect_connectors_and_round_free_edges(n, expected, tmp_path):
    spec = Accessory("lock-45", nx=n, ny=n)
    shape = make_accessory(spec)
    # Converge integration separately from the existing CAD construction-error contract.
    integrals = []
    for accuracy in (1e-10, 1e-12):
        properties = GProp_GProps()
        error = BRepGProp.VolumeProperties_s(shape.wrapped, properties, accuracy, True, False)
        assert error < 1e-10
        integrals.append(properties.Mass())
    assert abs(integrals[1] - integrals[0]) < 1e-7
    volume_budget = max(1e-6, shape.area * Precision.Confusion_s())
    assert integrals[1] == pytest.approx(expected, rel=0, abs=volume_budget), {
        "default_volume": shape.volume,
        "adaptive_volumes": integrals,
        "surface_area_mm2": shape.area,
        "existing_export_volume_budget_mm3": volume_budget,
    }
    base = _mounted_base(spec, root_radius=1, round_top=False)
    for x, y, _ in accessory_datums(spec)["mount_centers"]:
        region = Solid.make_box(48, 48, 13.2).moved(Location((x - 24, y - 24, -13)))
        expected_base = Part(base.intersect(region).solids())
        actual = Part(shape.intersect(region).solids())
        assert volume(expected_base.cut(actual)) + volume(actual.cut(expected_base)) < 1e-7
    assert shape.is_inside(Vector(n * 30, n * 30, 20))
    cargo_y = n * 60 - (10 - 4.1)
    assert shape.is_inside(Vector(n * 30, cargo_y - 3, 10))
    assert not shape.is_inside(Vector(n * 30, n * 60 - 5.8, 10))
    # The retained cargo envelope is6mm horizontally, hence6/sqrt(2) normal to its45deg plane.
    assert 6 / sqrt(2) == pytest.approx(4.242640687)
    path = tmp_path / "angled.step"
    assert export_step(shape, path)
    restored = import_step(path)
    assert restored.is_valid and len(restored.solids()) == 1
    assert any(
        face.geom_type == GeomType.CYLINDER
        and BRepAdaptor_Surface(face.wrapped).Cylinder().Radius() == pytest.approx(2)
        and face.bounding_box().max.Z > 49
        for face in restored.faces()
    )


@pytest.mark.parametrize(
    "nx,ny,expected",
    [
        (1, 1, 32894.977551393225),
        (1, 2, 65999.33687214005),
        (2, 2, 132414.07327677213),
    ],
)
def test_attachment_plates_round_the_whole_free_body_and_keep_x_plugs(nx, ny, expected):
    spec = Accessory("plate", nx=nx, ny=ny)
    shape = make_accessory(spec)
    assert shape.volume == pytest.approx(expected, abs=1e-6)
    old_base = _mounted_base(spec, root_radius=2)
    mating_region = Solid.make_box(nx * 60, ny * 60, 13).moved(Location((0, 0, -13)))
    before = Part(old_base.intersect(mating_region).solids())
    after = Part(shape.intersect(mating_region).solids())
    assert volume(before.cut(after)) + volume(after.cut(before)) < 1e-7
    for center in accessory_datums(spec)["mount_centers"]:
        plug = _downward_plug().moved(Location(center))
        assert volume(plug.cut(shape)) < 1e-7
    assert any(
        face.geom_type == GeomType.CYLINDER
        and BRepAdaptor_Surface(face.wrapped).Cylinder().Radius() == pytest.approx(2)
        and face.bounding_box().min.Z >= -1e-5
        for face in shape.faces()
    )
