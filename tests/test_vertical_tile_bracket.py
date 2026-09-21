"""Approved reference-space geometry, not a physical load or fit certification."""

import json
from dataclasses import replace

import pytest
from build123d import (
    Axis,
    Compound,
    Edge,
    GeomType,
    Location,
    Part,
    Solid,
    Vector,
    export_step,
    import_step,
)
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.Precision import Precision

from cargo_grid import BuildVolume, Interface, Tile, make_tile
from cargo_grid.accessories import (
    VERTICAL_BRACKET_CELLS,
    VERTICAL_BRACKET_CONFIGS,
    Accessory,
    _bidirectional_panel_post,
    _mounted_base,
    _panel_connector,
    _vertical_bracket,
    accessory_datums,
    make_accessory,
    make_bidirectional_panel_connector,
)
from cargo_grid.catalogue import accessory_design, accessory_variants
from cargo_grid.cli import main
from cargo_grid.interfaces import make_plug, x_profile
from cargo_grid.tiles import hole_placements


def volume(shape):
    return sum(s.volume for s in shape.solids()) if shape and shape.solids() else 0


def difference(a, b):
    return volume(a.cut(b)) + volume(b.cut(a))


def clipped(shape, x, y, z, width, depth, height):
    box = Solid.make_box(width, depth, height).moved(Location((x, y, z)))
    return Part(shape.intersect(box).solids())


def contact_faces(shape, z, sign):
    return [
        f
        for f in shape.faces()
        if f.geom_type == GeomType.PLANE
        and sign * f.normal_at().Z > 0.99
        and abs(f.bounding_box().min.Z - z) < 1e-5
        and abs(f.bounding_box().max.Z - z) < 1e-5
    ]


@pytest.mark.parametrize("nx,ny", VERTICAL_BRACKET_CELLS)
def test_bracket_solid_datums_and_actual_step_roundtrip(nx, ny, tmp_path):
    spec = Accessory("vertical-tile-bracket", nx=nx, ny=ny)
    part = make_accessory(spec)
    datums = accessory_datums(spec)
    assert part.is_valid and len(part.solids()) == 1
    assert tuple(part.bounding_box().min) == pytest.approx((0, 0, -12.8), abs=1e-5)
    assert tuple(part.bounding_box().max) == pytest.approx(
        (60 * nx, 60 * ny, 60 * ny + 7.578174593052), abs=1e-5
    )
    assert len(datums["mount_centers"]) == nx * ny
    assert len(datums["panel_plug_centers"]) == nx * ny
    assert datums["panel_seat_y"] == 60 * ny - 13
    assert datums["panel_plug_tip_y"] == pytest.approx(60 * ny - 0.2)
    assert datums["panel_bottom_z"] == datums["bearing_z"] == 6.1
    assert datums["nominal_bearing_gap"] == 0
    assert datums["outward_tile_face"] == "underside"
    assert datums["supported_outward_tile_faces"] == ("underside", "top")
    assert datums["top_outward_tile_rotation_degrees"] == {"z": 180, "x": -90}
    assert datums["panel_stem_profile_inset"] == 0.08
    assert datums["panel_tip_transition"] == pytest.approx((10.7, 10.8))
    assert not datums["bidirectional_physical_fit_verified"]
    path = tmp_path / f"bracket_{nx}x{ny}.step"
    assert export_step(part, path)
    restored = import_step(path)
    assert restored.is_valid and len(restored.solids()) == 1
    volume_budget = max(1e-6, part.area * Precision.Confusion_s())
    assert abs(restored.volume - part.volume) <= volume_budget
    assert tuple(restored.bounding_box().size) == pytest.approx(tuple(part.bounding_box().size))


@pytest.mark.parametrize(
    "nx,ny,expected",
    [
        (1, 2, 561919.7811806262),
        (2, 1, 338854.6656768019),
        (2, 2, 1124551.6313881178),
    ],
)
def test_bracket_volume_fixture_and_mixed_free_edge_radii(nx, ny, expected):
    spec = Accessory("vertical-tile-bracket", nx=nx, ny=ny)
    core = _vertical_bracket(spec, round_lip=False)
    rounded = make_accessory(spec)
    assert rounded.volume == pytest.approx(expected, abs=1e-5)
    assert volume(rounded.cut(core)) > 0
    assert volume(core.cut(rounded)) > 0
    assert any(
        f.geom_type == GeomType.CYLINDER
        and BRepAdaptor_Surface(f.wrapped).Cylinder().Radius() == pytest.approx(1)
        and f.bounding_box().min.Y >= 60 * ny - 1 - 1e-5
        for f in rounded.faces()
    )
    assert any(
        f.geom_type == GeomType.CYLINDER
        and BRepAdaptor_Surface(f.wrapped).Cylinder().Radius() == pytest.approx(2)
        and f.bounding_box().max.Z > 10
        for f in rounded.faces()
    )
    assert any(
        f.geom_type == GeomType.CYLINDER
        and BRepAdaptor_Surface(f.wrapped).Cylinder().Radius() == pytest.approx(3)
        and f.bounding_box().max.Y < 1
        and f.bounding_box().min.Z > 19
        for f in rounded.faces()
    )


@pytest.mark.parametrize("nx,base_y,panel_z", VERTICAL_BRACKET_CONFIGS)
def test_all_five_brackets_have_one_g1_r3_front_to_slope_transition(
    nx,
    base_y,
    panel_z,
):
    shape = make_accessory(
        Accessory(
            "vertical-tile-bracket",
            nx=nx,
            ny=base_y,
            panel_height_cells=panel_z if panel_z != base_y else None,
        )
    )
    transitions = [
        face
        for face in shape.faces()
        if face.geom_type == GeomType.CYLINDER
        and BRepAdaptor_Surface(face.wrapped).Cylinder().Radius() == pytest.approx(3)
        and face.bounding_box().max.Y < 1
        and face.bounding_box().min.Z > 19
    ]
    assert len(transitions) == 1
    assert min(edge.length for edge in transitions[0].edges()) > 1
    for edge in transitions[0].edges():
        adjacent = [
            face
            for face in shape.faces()
            if any(candidate.is_same(edge) for candidate in face.edges())
        ]
        assert len(adjacent) == 2
        normals = [face.normal_at(edge.center()) for face in adjacent]
        assert normals[0].dot(normals[1]) == pytest.approx(1, abs=1e-9)


@pytest.mark.parametrize("nx,ny", [(2, 1), (2, 2)])
def test_approved_unrounded_core_metrics(nx, ny):
    part = _vertical_bracket(Accessory("vertical-tile-bracket", nx=nx, ny=ny), round_lip=False)
    expected = {1: 331424.04093559954, 2: 1107294.0342363608}
    assert part.volume == pytest.approx(expected[ny], abs=1e-5)


@pytest.mark.parametrize("nx,ny", VERTICAL_BRACKET_CELLS)
def test_original_lower_base_and_both_plug_sets_are_retained(nx, ny):
    spec = Accessory("vertical-tile-bracket", nx=nx, ny=ny)
    part = make_accessory(spec)
    base = _mounted_base(spec, root_radius=1)
    assert (
        difference(
            clipped(part, 0, 0, -13, nx * 60, ny * 60, 13),
            clipped(base, 0, 0, -13, nx * 60, ny * 60, 13),
        )
        < 1e-5
    )
    # The filled upper-rim band remains solid away from the rounded exterior.
    band = Solid.make_box(nx * 60 - 6, ny * 60 - 6, 1).moved(Location((3, 3, 3.1)))
    assert volume(band.cut(part)) < 1e-5
    for x, y, z in accessory_datums(spec)["mount_centers"]:
        plug = make_plug().rotate(Axis.X, 180).moved(Location((x, y, z)))
        assert volume(plug.cut(part)) < 1e-5
    for point in accessory_datums(spec)["panel_plug_centers"]:
        plug = _bidirectional_panel_post().rotate(Axis.X, -90).moved(Location(point))
        assert volume(plug.cut(part)) < 1e-5


def test_accepted_bidirectional_panel_post_preserves_tip_and_nominal_interference():
    post = _bidirectional_panel_post()
    exact = make_plug()
    tip_region = Solid.make_box(100, 100, 2.1).moved(Location((-50, -50, 10.8)))
    assert (
        difference(
            Part(post.intersect(tip_region).solids()),
            Part(exact.intersect(tip_region).solids()),
        )
        < 1e-7
    )
    assert all(
        face.bounding_box().min.Z >= 10.8 - 1e-5
        for face in post.faces()
        if face.geom_type == GeomType.TORUS
    )
    socket = x_profile()
    chosen = x_profile(plug=True, offset=-0.08)
    prior = x_profile(plug=True, offset=-0.075)
    assert sum(face.area for face in chosen.cut(socket).faces()) < 1e-9
    assert sum(face.area for face in prior.cut(socket).faces()) == pytest.approx(
        0.12236687691122836,
        abs=1e-8,
    )
    assert difference(make_bidirectional_panel_connector(), _panel_connector()) < 1e-7

    spec = Accessory("vertical-tile-bracket", nx=1, ny=1, panel_height_cells=2)
    bracket = make_accessory(spec)
    underside_outward = make_tile(Tile(1, 2)).rotate(Axis.X, 90).moved(Location((0, 60, 6.1)))
    top_outward = (
        make_tile(Tile(1, 2)).rotate(Axis.Z, 180).rotate(Axis.X, -90).moved(Location((60, 47, 6.1)))
    )
    assert volume(bracket.intersect(underside_outward)) == pytest.approx(
        5.439166120503271,
        abs=1e-6,
    )
    assert volume(bracket.intersect(top_outward)) < 1e-7
    assert volume(bracket.intersect(top_outward.moved(Location((0, 4, 0))))) == pytest.approx(
        5.4391660871705465,
        abs=1e-6,
    )


# Portable runs cover every bracket and both hole modes, with the default holes on the largest.
PORTABLE_INSERTION_CASES = {(True, 2, 2), (False, 1, 2), (True, 2, 1)}


@pytest.mark.parametrize(
    "holes,nx,ny",
    [
        (holes, nx, ny)
        if (holes, nx, ny) in PORTABLE_INSERTION_CASES
        else pytest.param(holes, nx, ny, marks=pytest.mark.slow)
        for nx, ny in VERTICAL_BRACKET_CELLS
        for holes in (False, True)
    ],
)
def test_separate_tile_insertion_bearing_and_solid_backing(holes, nx, ny):
    spec = Accessory("vertical-tile-bracket", nx=nx, ny=ny)
    part = make_accessory(spec)
    core = _vertical_bracket(spec, round_lip=False)
    tile_spec = Tile(
        nx, ny, hole_diameter=10 if holes else None, hole_scope="full" if holes else "interior"
    )
    tile = make_tile(tile_spec).rotate(Axis.X, 90).moved(Location((0, 60 * ny, 6.1)))
    assert tile.bounding_box().max.Y == pytest.approx(ny * 60, abs=1e-5)
    for offset in (0, 1, 4, 8, 12.8, 15):
        placed = tile.moved(Location((0, offset, 0)))
        assert abs(volume(part.intersect(placed)) - volume(core.intersect(placed))) < 1e-4
    bearing = sum(
        sum(f.area for f in q.faces()) if q else 0
        for top in contact_faces(part, 6.1, 1)
        for bottom in contact_faces(tile, 6.1, -1)
        for q in [top.intersect(bottom)]
    )
    expected_bearing = (
        260.891859550497 * nx
        if holes
        else {
            (1, 2): 356.89187154230996,
            (2, 1): 735.7837431264309,
            (2, 2): 735.7837429654181,
        }[(nx, ny)]
    )
    assert bearing == pytest.approx(expected_bearing, abs=1e-5)
    core_bearing = sum(
        sum(f.area for f in q.faces()) if q else 0
        for top in contact_faces(core, 6.1, 1)
        for bottom in contact_faces(tile, 6.1, -1)
        for q in [top.intersect(bottom)]
    )
    assert bearing >= core_bearing - 1e-9
    assert bearing > 0
    assert part.is_inside(Vector(nx * 30, ny * 60 - 13 - 0.01, 20))
    # Exact unmodified neighbor tiles can extend sideways/upward with the same wall origin.
    for neighbor, x, z in (
        (Tile(1, ny), -60, 6.1),
        (Tile(1, ny), nx * 60, 6.1),
        (Tile(nx, 1), 0, 6.1 + ny * 60),
    ):
        shape = make_tile(neighbor).rotate(Axis.X, 90).moved(Location((x, ny * 60, z)))
        assert volume(part.intersect(shape)) < 1e-5
    lower = make_tile(Tile(nx, 1)).rotate(Axis.X, 90).moved(Location((0, ny * 60, 6.1 - 60)))
    assert volume(part.intersect(lower)) > 1
    if holes:
        interior = [
            h for h in hole_placements(tile_spec) if 0 < h.x < nx * 60 and 0 < h.y < ny * 60
        ]
        assert len(interior) == (5 if (nx, ny) == (2, 2) else 1)
        for hole in interior:
            ray = Edge.make_line((hole.x, 0, 6.1 + hole.y), (hole.x, ny * 60 + 1, 6.1 + hole.y))
            material = part.intersect(ray)
            assert material and material.edges()
            end = Compound(material.edges()).bounding_box().max.Y
            assert ny * 60 - end == pytest.approx(13, abs=1e-5)


def test_bracket_variants_and_scaled_interface_policy():
    variants = accessory_variants(BuildVolume(350, 320, 325))
    assert {
        (a.nx, a.ny, a.panel_height_cells or a.ny)
        for a in variants
        if a.family == "vertical-tile-bracket"
    } == set(VERTICAL_BRACKET_CONFIGS)
    assert not any(a.family == "lock-90" for a in variants)
    custom = Interface(pitch=65)
    assert any(
        a.family == "vertical-tile-bracket"
        for a in accessory_variants(BuildVolume(350, 320, 325), custom)
    )
    custom_bracket = make_accessory(
        Accessory(
            "vertical-tile-bracket",
            nx=1,
            ny=1,
            panel_height_cells=2,
            interface=custom,
        )
    )
    assert custom_bracket.is_valid
    with pytest.raises(ValueError, match="was replaced"):
        Accessory("lock-90")
    assert make_accessory(
        replace(
            Accessory("vertical-tile-bracket", nx=2), interface=Interface(joint_style="full-height")
        )
    ).is_valid


@pytest.mark.parametrize(
    "nx,expected_volume",
    [(1, 281317.69598481583), (2, 563188.0488542926)],
)
def test_shallow_brackets_round_free_body_and_preserve_mating_regions(
    nx, expected_volume, tmp_path
):
    spec = Accessory(
        "vertical-tile-bracket",
        nx=nx,
        ny=1,
        panel_height_cells=2,
    )
    shape = make_accessory(spec)
    datums = accessory_datums(spec)
    assert shape.is_valid and len(shape.solids()) == 1
    assert shape.volume == pytest.approx(expected_volume, abs=1e-5)
    assert tuple(shape.bounding_box().min) == pytest.approx((0, 0, -12.8), abs=1e-5)
    assert tuple(shape.bounding_box().max) == pytest.approx(
        (60 * nx, 60, 127.578174593052), abs=1e-5
    )
    assert datums["base_cells_x_y"] == (nx, 1)
    assert datums["panel_cells_x_z"] == (nx, 2)
    assert len(datums["mount_centers"]) == nx
    assert len(datums["panel_plug_centers"]) == nx * 2
    assert datums["panel_seat_y"] == 47
    assert datums["panel_plug_tip_y"] == pytest.approx(59.8)
    assert datums["supported_outward_tile_faces"] == ("underside", "top")

    base = _mounted_base(spec, root_radius=1, round_top=False)
    floor_region = Solid.make_box(nx * 60, 60, 13).moved(Location((0, 0, -13)))
    assert (
        difference(
            Part(shape.intersect(floor_region).solids()),
            Part(base.intersect(floor_region).solids()),
        )
        < 1e-7
    )

    node = _panel_connector()
    connectors = [
        node.rotate(Axis.X, 90).moved(Location((column * 60, 47, 6.1 + row * 60)))
        for row in range(2)
        for column in range(nx)
    ]
    connector_union = connectors[0].fuse(*connectors[1:])
    panel_region = Solid.make_box(nx * 60, 12.9999, 130).moved(Location((0, 47.0001, 6.1)))
    assert (
        difference(
            Part(shape.intersect(panel_region).solids()),
            Part(connector_union.intersect(panel_region).solids()),
        )
        < 1e-7
    )
    assert volume(connector_union.cut(shape)) < 1e-7
    assert not any(
        face.geom_type == GeomType.PLANE
        and face.bounding_box().size.Y < 1e-6
        and abs(face.bounding_box().min.Y - 42.9) < 1e-5
        and face.bounding_box().max.Z > 100
        for face in shape.faces()
    )

    path = tmp_path / f"shallow-{nx}.step"
    assert export_step(shape, path)
    restored = import_step(path)
    assert restored.is_valid and len(restored.solids()) == 1
    volume_budget = max(1e-6, shape.area * Precision.Confusion_s())
    assert restored.volume == pytest.approx(shape.volume, abs=volume_budget)
    transitions = [
        face
        for face in restored.faces()
        if face.geom_type == GeomType.CYLINDER
        and BRepAdaptor_Surface(face.wrapped).Cylinder().Radius() == pytest.approx(3)
        and face.bounding_box().max.Y < 1
        and face.bounding_box().min.Z > 19
    ]
    assert len(transitions) == 1
    transition = transitions[0]
    assert transition.bounding_box().min.X == pytest.approx(2)
    assert transition.bounding_box().max.X == pytest.approx(60 * nx - 2)
    assert min(edge.length for edge in transition.edges()) > 1
    for edge in transition.edges():
        adjacent = [
            face
            for face in restored.faces()
            if any(candidate.is_same(edge) for candidate in face.edges())
        ]
        assert len(adjacent) == 2
        normals = [face.normal_at(edge.center()) for face in adjacent]
        assert normals[0].dot(normals[1]) == pytest.approx(1, abs=1e-9)


@pytest.mark.parametrize("nx", [1, 2])
def test_shallow_bracket_side_down_pose_and_scoped_support(nx):
    spec = Accessory(
        "vertical-tile-bracket",
        nx=nx,
        ny=1,
        panel_height_cells=2,
    )
    design = accessory_design(spec)
    assert design.recommended_print_rotation_x is None
    assert design.recommended_print_rotation_y == -90
    assert design.apply_orientation_to_bambu
    assert design.bambu_object_settings == {
        "enable_support": "1",
        "support_type": "normal(auto)",
    }
    assert design.bambu_size == pytest.approx((140.378174593052, 60, 60 * nx))


def test_explicit_matching_panel_height_normalizes_to_stable_existing_identity(
    accessory_metadata_shape,
):
    implicit = Accessory("vertical-tile-bracket", nx=2, ny=1)
    explicit = Accessory(
        "vertical-tile-bracket",
        nx=2,
        ny=1,
        panel_height_cells=1,
    )
    assert explicit == implicit
    assert accessory_design(explicit).name == accessory_design(implicit).name


def test_cli_names_floor_depth_and_independent_panel_height(tmp_path):
    output = tmp_path / "shallow"
    assert (
        main(
            [
                "part",
                "--family",
                "vertical-tile-bracket",
                "--width-cells",
                "1",
                "--depth-cells",
                "1",
                "--panel-height-cells",
                "2",
                "--build-width-mm",
                "150",
                "--build-depth-mm",
                "150",
                "--build-height-mm",
                "150",
                "--no-stl",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    parameters = json.loads((output / "manifest.json").read_text())["designs"][0]["parameters"]
    assert parameters["nx"] == parameters["ny"] == 1
    assert parameters["panel_height_cells"] == 2
    with pytest.raises(SystemExit):
        main(
            [
                "part",
                "--family",
                "plate",
                "--width-cells",
                "1",
                "--depth-cells",
                "1",
                "--panel-height-cells",
                "2",
                "--build-width-mm",
                "150",
                "--build-depth-mm",
                "150",
                "--build-height-mm",
                "150",
                "--output",
                str(tmp_path / "invalid"),
            ]
        )
