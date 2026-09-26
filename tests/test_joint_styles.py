"""Actual joint volumes, openings, sockets and exported style identity."""

import json
from dataclasses import replace
from math import cos, radians, sin

import pytest
from build123d import Compound, GeomType, Location, Vector, export_step, import_step

from cargo_grid import BuildVolume, Interface, Tile, make_tile
from cargo_grid.accessories import Accessory, accessory_datums, make_accessory
from cargo_grid.catalogue import accessory_design, tile_sizes
from cargo_grid.cli import main, parser
from cargo_grid.export import export_job
from cargo_grid.interfaces import socket_entry_tool
from cargo_grid.jobs import Job, layout_job, tile_design
from cargo_grid.layout import exact_layout
from cargo_grid.validation import crossings, segments, shape_mesh


def test_default_contract_and_invalid_styles():
    assert Interface().joint_style == "original"
    assert Tile().interface.joint_style == "original"
    assert Interface().reference_defaults
    assert Interface().reference_socket_dimensions
    assert Interface().compatibility()["geometry_warning"] is None
    assert not Interface().compatibility()["experimental"]
    experimental = Interface(joint_style="full-height")
    assert "0.146 mm" in experimental.compatibility()["geometry_warning"]
    assert experimental.compatibility()["experimental"]
    assert not experimental.reference_defaults
    for value in ("full", "reference", "", None, 1):
        with pytest.raises(ValueError, match="joint_style"):
            Interface(joint_style=value)
    for command in ("part", "layout", "catalogue"):
        args = [
            command,
            "--build-width-mm",
            "150",
            "--build-depth-mm",
            "150",
            "--build-height-mm",
            "50",
            "--output",
            "outputs/example",
        ]
        if command == "layout":
            args += ["--layout-width-mm", "120", "--layout-depth-mm", "120"]
        assert parser().parse_args(args).joint_style == "original"
        assert (
            parser().parse_args(args + ["--joint-style", "full-height"]).joint_style
            == "full-height"
        )


@pytest.mark.parametrize("style", ["full-height", "original"])
def test_actual_sections_open_through_and_male_top_planes(style, tmp_path):
    shape = make_tile(Tile(2, 3, Interface(joint_style=style), hole_diameter=None))
    full = style == "full-height"
    for z in (10.3, 11, 12, 12.9):
        for xyz in ((123, 30, z), (30, 183, z)):
            assert shape.is_inside(Vector(*xyz)) == full
        for xyz in ((3, 30, z), (30, 3, z)):
            assert shape.is_inside(Vector(*xyz)) != full
    mesh = shape_mesh(shape)
    for axis, base in ((0, 120), (1, 180)):
        cuts = crossings(segments(*mesh, 12), axis, base + 3)
        assert (len(cuts) > 0) == full
    assert shape.is_valid and len(shape.solids()) == 1
    assert style in shape.label
    file = tmp_path / (style + ".step")
    assert export_step(shape, file)
    restored = import_step(file)
    assert restored.is_valid
    assert restored.is_inside(Vector(123, 30, 12)) == full
    assert restored.is_inside(Vector(3, 30, 12)) != full


@pytest.mark.parametrize("axis", [0, 1])
def test_full_height_mating_has_no_intersection_in_both_directions(axis):
    tile = make_tile(Tile(interface=Interface(joint_style="full-height"), hole_diameter=None))
    move = [0, 0, 0]
    move[axis] = 60
    adjacent = tile.moved(Location(move))
    common = tile.intersect(adjacent)
    volume = 0 if common is None else sum(s.volume for s in common.solids())
    assert volume < 1e-4
    assert tile.distance_to(adjacent) < 1e-6
    old = make_tile(Tile(interface=Interface(joint_style="original"), hole_diameter=None)).moved(
        Location(move)
    )
    assert sum(s.volume for s in tile.intersect(old).solids()) > 100


@pytest.mark.parametrize(
    "family,variants",
    [
        ("edge-x", (1,)),
        ("edge-y", (1,)),
        ("corner-in", (1, 2, 3, 4)),
        ("corner-out", (1, 2, 3, 4, 5, 6)),
    ],
)
def test_every_tile_facing_family_opens_and_reaches_top(family, variants):
    for v in variants:
        for style in ("full-height", "original"):
            spec = Accessory(
                family,
                variant=v,
                interface=Interface(joint_style=style),
                complete_edge_holes=False,
            )
            shape = make_accessory(spec)
            for join in accessory_datums(spec)["joins"]:
                x, y, z = join["position"]
                angle = radians(join["angle"])
                point = Vector(x - 3 * sin(angle), y + 3 * cos(angle), 12)
                expected = (join["sex"] == "male") == (style == "full-height")
                assert shape.is_inside(point) == expected, (spec, join)
                assert join["joint_style"] == style


def test_socket_throat_and_unaffected_entry_envelope_preserved():
    original = make_tile(Tile(interface=Interface(joint_style="original"), hole_diameter=None))
    current = make_tile(Tile(interface=Interface(joint_style="full-height"), hole_diameter=None))
    for z in (1.5, 5, 9.5, 10.5, 12):
        for angle in range(0, 360, 5):
            for radius in (10, 14, 19, 24, 28):
                p = Vector(30 + radius * cos(radians(angle)), 30 + radius * sin(radians(angle)), z)
                # Lower material/socket envelope is unchanged. At the upper
                # entry only edge-pocket cuts may remove material.
                if 10 < p.X < 50 and 10 < p.Y < 50:
                    assert original.is_inside(p) == current.is_inside(p), (z, angle, radius)
    cutter = socket_entry_tool(Interface())
    assert cutter.is_valid and cutter.volume > 0
    assert original.cut(current).volume > 0  # roof removal is an intentional surface change
    assert Interface().compatibility()["original_x_attachment_dimensions"]


def test_open_pockets_reduce_plate_bearing_land_without_changing_seating_datum():
    plate = make_accessory(Accessory("plate")).moved(Location((0, 0, 13)))
    shoulder = [
        f
        for f in plate.faces()
        if f.geom_type == GeomType.PLANE
        and abs(f.bounding_box().min.Z - 13) < 1e-5
        and abs(f.bounding_box().max.Z - 13) < 1e-5
    ]
    areas = {}
    for style in ("original", "full-height"):
        tile = make_tile(Tile(interface=Interface(joint_style=style), hole_diameter=None))
        top = [
            f
            for f in tile.faces()
            if f.geom_type == GeomType.PLANE
            and abs(f.bounding_box().min.Z - 13) < 1e-5
            and abs(f.bounding_box().max.Z - 13) < 1e-5
        ]
        area = 0
        for face in top:
            for bearing in shoulder:
                common = face.intersect(bearing)
                if common:
                    area += sum(f.area for f in common.faces())
        areas[style] = area
    assert areas["original"] == pytest.approx(1092.9042795, abs=0.002)
    assert areas["full-height"] == pytest.approx(822.9183841, abs=0.002)
    assert areas["full-height"] < areas["original"]


@pytest.mark.parametrize(
    "nx,ny",
    [
        # Portable runs keep the smallest, largest and both extreme aspect ratios.
        (x, y) if {x, y} <= {1, 5} else pytest.param(x, y, marks=pytest.mark.slow)
        for x in range(1, 6)
        for y in range(1, 6)
    ],
)
def test_all_full_height_ordered_sizes_through_five_cells(nx, ny):
    tile = make_tile(Tile(nx, ny, Interface(joint_style="full-height"), hole_diameter=None))
    assert tile.is_valid and len(tile.solids()) == 1
    assert tuple(tile.bounding_box().size) == pytest.approx(
        (60 * nx + 6, 60 * ny + 6, 13), abs=1e-5
    )
    assert tile.is_inside(Vector(60 * nx + 3, 30, 12))
    assert not tile.is_inside(Vector(3, 30, 12))


@pytest.mark.parametrize(
    "family",
    [
        "plate",
        "vertical-tile-bracket",
        "vertical-stop",
        "lock-45",
        "support",
        "support-bit",
        "support-end",
    ],
)
def test_non_edge_interfaces_and_support_rails_do_not_change_with_style(family):
    original = (
        Accessory("vertical-tile-bracket", nx=2)
        if family == "vertical-tile-bracket"
        else Accessory("vertical-stop", nx=2, height=60)
        if family == "vertical-stop"
        else Accessory(family)
    )
    full = replace(original, interface=Interface(joint_style="full-height"))
    a, b = make_accessory(original), make_accessory(full)
    assert a.volume == pytest.approx(b.volume, abs=1e-7)
    assert a.area == pytest.approx(b.area, abs=1e-7)
    assert accessory_datums(original) == accessory_datums(full)


@pytest.mark.parametrize("style", ["full-height", "original"])
def test_style_survives_layout_and_exported_manifest(style, tmp_path):
    interface = Interface(joint_style=style)
    build = BuildVolume(150, 140, 50)
    job = layout_job(
        exact_layout(121, 137, build, interface=interface, hole_diameter=None),
        build,
    )
    assert all(
        d.parameters["interface"]["joint_style"] == style and style in d.name for d in job.designs
    )
    shape = Compound([d.shape.moved(Location(f)) for d in job.designs for f in d.assembly_frames])
    assert tuple(shape.bounding_box().size) == pytest.approx((121, 137, 13), abs=1e-5)
    design = tile_design(Tile(interface=interface, hole_diameter=None))
    manifest = json.loads(
        export_job(Job([design], build, "part"), tmp_path / style, stl=False).read_text()
    )
    assert manifest["joint_styles"] == [style]
    assert manifest["designs"][0]["joint_style"] == style
    comp = manifest["designs"][0]["compatibility"]
    assert comp["original_tile_edge_dimensions"] == (style == "original")
    assert comp["experimental"] == (style == "full-height")
    assert comp["original_x_attachment_dimensions"]
    assert style in accessory_design(Accessory("edge-x", interface=interface)).name
    assert set(tile_sizes(BuildVolume(306, 306, 50), interface)) == {
        (x, y) for x in range(1, 6) for y in range(1, 6)
    }


def test_cli_explicit_full_height_and_default_original_exports(tmp_path, capsys):
    for style in ("full-height", "original"):
        args = [
            "part",
            "--build-width-mm",
            "150",
            "--build-depth-mm",
            "150",
            "--build-height-mm",
            "50",
            "--output",
            str(tmp_path / style),
            "--no-stl",
        ]
        if style == "full-height":
            args += ["--joint-style", "full-height"]
        assert main(args) == 0
        stderr = capsys.readouterr().err
        assert ("0.146 mm" in stderr) == (style == "full-height")
        manifest = json.loads((tmp_path / style / "manifest.json").read_text())
        assert manifest["joint_styles"] == [style]
