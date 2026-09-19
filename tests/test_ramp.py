"""Approved ramp geometry and joins; print and physical performance remain unverified."""

import json
from dataclasses import replace
from math import atan
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from build123d import Axis, Edge, GeomType, Location, Part, Solid, Vector
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.Precision import Precision

from cargo_grid import BuildVolume, Interface, Tile, make_tile
from cargo_grid.accessories import (
    RAMP_CARRIER_RUN_MM,
    RAMP_FREE_EDGE_RADIUS_MM,
    RAMP_MINIMUM_FLAT_SHELF_MM,
    RAMP_RUN_MM,
    RAMP_SHELF_RADIUS_MM,
    Accessory,
    _ramp,
    _ramp_profile_tip_y,
    _ramp_shelf_radius,
    accessory_datums,
    make_accessory,
)
from cargo_grid.catalogue import accessory_design, accessory_variants
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, _checked_step_roundtrip, write_3mf
from cargo_grid.interfaces import tile_join_tool
from cargo_grid.jobs import Job
from cargo_grid.meshes import checked_mesh


def volume(shape) -> float:
    return sum(solid.volume for solid in shape.solids()) if shape else 0


@pytest.mark.parametrize("cells", range(1, 6))
@pytest.mark.parametrize("ramp_join", ["female", "male"])
def test_ramp_width_run_rise_rounding_step_and_mesh(cells, ramp_join, tmp_path):
    spec = Accessory("ramp", nx=cells, ramp_join=ramp_join)
    shape = make_accessory(spec)
    projection = 6 if ramp_join == "male" else 0
    assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
    assert tuple(shape.bounding_box().min) == pytest.approx((0, -projection, 0), abs=1e-5)
    assert tuple(shape.bounding_box().size) == pytest.approx(
        (60 * cells, RAMP_RUN_MM + projection, 13), abs=1e-5
    )
    radii = [
        BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()
        for face in shape.faces()
        if face.geom_type == GeomType.CYLINDER
    ]
    assert sum(radius == pytest.approx(RAMP_FREE_EDGE_RADIUS_MM) for radius in radii) == 7
    assert sum(radius == pytest.approx(RAMP_SHELF_RADIUS_MM) for radius in radii) == 1
    path = tmp_path / f"ramp-{ramp_join}-{cells}.step"
    restored, _, _, _, _ = _checked_step_roundtrip(shape, path)
    budget = max(1e-6, shape.area * Precision.Confusion_s())
    assert restored.is_valid and len(restored.solids()) == 1
    assert abs(restored.volume - shape.volume) <= budget
    assert tuple(restored.bounding_box().size) == pytest.approx(tuple(shape.bounding_box().size))
    _, _, mesh = checked_mesh(shape)
    assert mesh["closed_oriented_manifold"] and mesh["mesh_volume_mm3"] > 0


@pytest.mark.parametrize("cells", range(1, 6))
def test_ramp_uses_exact_existing_female_join_and_mates_north_tile_edge(cells):
    interface = Interface()
    spec = Accessory("ramp", nx=cells, interface=interface)
    uncut = _ramp(spec, cut_joins=False)
    ramp = make_accessory(spec)
    cutters = [
        tile_join_tool(interface, depth=6.1, male=False).moved(Location(((cell + 0.5) * 60, 0, 0)))
        for cell in range(cells)
    ]
    expected = uncut.cut(*cutters).clean()
    assert volume(expected.cut(ramp)) + volume(ramp.cut(expected)) < 1e-7
    tile = make_tile(Tile(cells, 1, interface)).moved(Location((0, -60, 0)))
    assert volume(ramp.intersect(tile)) < 1e-7
    assert ramp.distance_to(tile) < 1e-7
    datums = accessory_datums(spec)
    assert datums["finished_run"] == 50
    assert datums["ramp_direction"] == "positive Y away from the tile"
    assert datums["mating_tile_edge"] == "north male edge at Y=0"
    assert [join["position"][0] for join in datums["joins"]] == [
        (cell + 0.5) * 60 for cell in range(cells)
    ]
    assert {join["sex"] for join in datums["joins"]} == {"female"}
    assert all(not join["open_through_top"] for join in datums["joins"])
    assert ramp.bounding_box().max.Z == pytest.approx(tile.bounding_box().max.Z)


@pytest.mark.parametrize("cells", range(1, 6))
def test_male_ramp_repeats_exact_shared_tabs_and_mates_both_female_tile_sides(cells):
    interface = Interface()
    spec = Accessory("ramp", nx=cells, ramp_join="male")
    ramp = make_accessory(spec)
    outside = Solid.make_box(cells * 60, 6, 13).moved(Location((0, -6, 0)))
    expected = Part(
        [
            solid
            for cell in range(cells)
            for solid in (
                tile_join_tool(interface, male=True)
                .rotate(Axis.Z, 180)
                .moved(Location(((cell + 0.5) * 60, 0, 0)))
                .intersect(outside)
                .solids()
            )
        ]
    )
    actual = Part(ramp.intersect(outside).solids())
    assert volume(actual.cut(expected)) + volume(expected.cut(actual)) < 1e-7
    body_region = Solid.make_box(cells * 60, 50, 13)
    body = Part(ramp.intersect(body_region).solids())
    original_body = _ramp(spec, cut_joins=False)
    assert volume(body.cut(original_body)) + volume(original_body.cut(body)) < 1e-7
    south_tile = make_tile(Tile(cells, 1, hole_diameter=None))
    south_ramp = ramp.rotate(Axis.Z, 180).moved(Location((cells * 60, 0, 0)))
    west_tile = make_tile(Tile(1, cells, hole_diameter=None))
    west_ramp = ramp.rotate(Axis.Z, 90)
    for tile, placed in ((south_tile, south_ramp), (west_tile, west_ramp)):
        assert volume(tile.intersect(placed)) < 1e-7
        assert tile.distance_to(placed) < 1e-7
    datums = accessory_datums(spec)
    assert datums["finished_run"] == 50
    assert datums["tab_projection"] == 6
    assert datums["overall_depth"] == 56
    assert datums["ramp_join"] == "male"
    assert {join["sex"] for join in datums["joins"]} == {"male"}
    assert {join["angle"] for join in datums["joins"]} == {180}
    assert {join["height"] for join in datums["joins"]} == {10}


@pytest.mark.parametrize("ramp_join", ["female", "male"])
@pytest.mark.parametrize(
    "unit,thickness,fit",
    [(30, 6, 0), (30, 13, -0.2), (60, 8, 0.2), (90, 18, 0), (60, 60, 0)],
)
def test_scaled_ramps_match_real_tiles_keep_run_and_export(
    ramp_join, unit, thickness, fit, tmp_path
):
    interface = Interface(unit, thickness, fit)
    ramp = make_accessory(Accessory("ramp", nx=2, interface=interface, ramp_join=ramp_join))
    assert ramp.is_valid and len(ramp.solids()) == 1 and ramp.volume > 0
    projection = interface.male_join_depth if ramp_join == "male" else 0
    assert tuple(ramp.bounding_box().min) == pytest.approx((0, -projection, 0), abs=1e-5)
    assert tuple(ramp.bounding_box().size) == pytest.approx(
        (2 * unit, 50 + projection, thickness), abs=1e-5
    )
    if ramp_join == "male":
        placements = (
            (
                Tile(2, 1, interface, hole_diameter=None),
                ramp.rotate(Axis.Z, 180).moved(Location((2 * unit, 0, 0))),
            ),
            (Tile(1, 2, interface, hole_diameter=None), ramp.rotate(Axis.Z, 90)),
        )
    else:
        placements = (
            (Tile(2, 1, interface, hole_diameter=None), ramp.moved(Location((0, unit, 0)))),
            (
                Tile(1, 2, interface, hole_diameter=None),
                ramp.rotate(Axis.Z, -90).moved(Location((unit, 2 * unit, 0))),
            ),
        )
    for spec, placed in placements:
        tile = make_tile(spec)
        assert volume(tile.intersect(placed)) < 1e-7
        assert tile.distance_to(placed) < 1e-7
    source_tile = make_tile(Tile(2, 1, interface, hole_diameter=None))
    if ramp_join == "male":
        source_tile = source_tile.rotate(Axis.Z, 180).moved(Location((2 * unit, 0, 0)))
        projection_y = -interface.male_join_depth / 2
        male, receiver = ramp, source_tile
    else:
        source_tile = source_tile.moved(Location((0, -unit, 0)))
        projection_y = interface.male_join_depth / 2
        male, receiver = source_tile, ramp
    ray = Edge.make_line((unit / 2, projection_y, -1), (unit / 2, projection_y, thickness + 1))
    tab_section = male.intersect(ray).edges()
    roof_section = receiver.intersect(ray).edges()
    assert len(tab_section) == len(roof_section) == 1
    tab_top = tab_section[0].bounding_box().max.Z
    roof_bottom = roof_section[0].bounding_box().min.Z
    assert tab_top == pytest.approx(thickness - 3, abs=1e-7)
    assert roof_bottom == pytest.approx(thickness - 2.8, abs=1e-7)
    assert roof_bottom - tab_top == pytest.approx(0.2, abs=1e-7)
    assert roof_section[0].length > 0
    restored, _, _, _, _ = _checked_step_roundtrip(ramp, tmp_path / "scaled-ramp.step")
    assert restored.is_valid and len(restored.solids()) == 1
    _, _, report = checked_mesh(ramp)
    assert report["closed_oriented_manifold"]


@pytest.mark.parametrize("ramp_join", ["female", "male"])
@pytest.mark.parametrize(
    "cells,unit,thickness",
    [(1, 60, 13), (5, 60, 13), (2, 30, 6), (2, 90, 18), (1, 60, 60)],
)
def test_finished_shelf_slope_round_has_broad_radius_and_is_tangent_across_its_width(
    ramp_join, cells, unit, thickness
):
    shape = make_accessory(
        Accessory("ramp", nx=cells, interface=Interface(unit, thickness), ramp_join=ramp_join)
    )
    radius = _ramp_shelf_radius(thickness)
    transitions = []
    for face in shape.faces():
        if face.geom_type != GeomType.CYLINDER:
            continue
        adaptor = BRepAdaptor_Surface(face.wrapped)
        cylinder = adaptor.Cylinder()
        if (
            abs(cylinder.Axis().Direction().X()) > 1 - 1e-9
            and abs(cylinder.Location().Z() - (thickness - radius)) < 1e-7
            and 0 < cylinder.Location().Y() < RAMP_CARRIER_RUN_MM
        ):
            transitions.append((face, adaptor))
    assert len(transitions) == 1
    blend, adaptor = transitions[0]
    assert adaptor.Cylinder().Radius() == pytest.approx(radius, abs=1e-9)
    assert adaptor.Cylinder().Location().Y() >= RAMP_MINIMUM_FLAT_SHELF_MM - 1e-7
    if thickness == 13:
        assert adaptor.LastUParameter() - adaptor.FirstUParameter() == pytest.approx(
            atan(13 / (_ramp_profile_tip_y(13) - 10)), abs=1e-9
        )
        assert radius == 32
        assert adaptor.Cylinder().Location().Y() == pytest.approx(6.284326079358225, abs=1e-7)
    elif thickness == 60:
        assert radius == pytest.approx(15.330127687723184, abs=1e-7)
        assert adaptor.Cylinder().Location().Y() == pytest.approx(2, abs=1e-7)
    for x in (2.01, cells * unit / 2, cells * unit - 2.01):
        axis = adaptor.Cylinder().Axis()
        along_axis = (x - axis.Location().X()) / axis.Direction().X()
        for u in (adaptor.FirstUParameter(), adaptor.LastUParameter()):
            raw = adaptor.Value(u, along_axis)
            point = Vector(raw.X(), raw.Y(), raw.Z())
            neighbors = [
                face
                for face in shape.faces()
                if face.geom_type == GeomType.PLANE and face.distance_to(point) < 1e-7
            ]
            assert len(neighbors) == 1
            assert blend.normal_at(point).dot(neighbors[0].normal_at(point)) == pytest.approx(
                1, abs=1e-9
            )


def test_adjacent_ramps_meet_without_overlap_and_multi_cell_part_avoids_internal_seams():
    one = make_accessory(Accessory("ramp"))
    adjacent = one.moved(Location((60, 0, 0)))
    assert volume(one.intersect(adjacent)) < 1e-8
    assert one.distance_to(adjacent) < 1e-7
    two = make_accessory(Accessory("ramp", nx=2))
    assert two.is_valid and len(two.solids()) == 1
    plane = Solid.make_box(0.02, RAMP_RUN_MM, 13).moved(Location((59.99, 0, 0)))
    assert volume(two.intersect(plane)) > 0


def test_one_cell_production_shape_has_broad_shelf_volume_fixture():
    production = make_accessory(Accessory("ramp"))
    budget = max(1e-6, production.area * Precision.Confusion_s())
    assert abs(production.volume - 25600.803310757157) <= budget


@pytest.mark.parametrize("ramp_join", ["female", "male"])
def test_broad_shelf_preserves_high_joining_rim_and_standard_pocket_roof(ramp_join):
    shape = make_accessory(Accessory("ramp", ramp_join=ramp_join))
    rim = Edge.make_line((2, 0, 13), (58, 0, 13))
    assert sum(edge.length for edge in shape.intersect(rim).edges()) == pytest.approx(56, abs=1e-7)
    if ramp_join == "female":
        for x in (20, 30, 40):
            for y in (1, 3, 5):
                ray = Edge.make_line((x, y, 10), (x, y, 14))
                roof = shape.intersect(ray).edges()
                assert len(roof) == 1
                assert roof[0].bounding_box().min.Z == pytest.approx(10.2, abs=1e-7)
                assert roof[0].bounding_box().max.Z == pytest.approx(13, abs=1e-7)


def test_catalogue_includes_every_ramp_width_that_fits_selected_envelope():
    variants = accessory_variants(BuildVolume(350, 320, 325))
    ramps = [spec for spec in variants if spec.family == "ramp"]
    assert len(variants) == 65
    for sex in ("female", "male"):
        assert [spec.nx for spec in ramps if spec.ramp_join == sex] == [1, 2, 3, 4, 5]
    assert all(spec.ny == 1 for spec in ramps)
    compact = [
        spec.nx
        for spec in accessory_variants(BuildVolume(246, 246, 120))
        if spec.family == "ramp" and spec.ramp_join == "male"
    ]
    assert compact == [1, 2, 3, 4]
    assert not any(
        spec.family == "ramp"
        for spec in accessory_variants(
            BuildVolume(350, 320, 325),
            Interface(joint_style="full-height"),
        )
    )


@pytest.mark.parametrize("ramp_join", ["female", "male"])
def test_cli_and_api_use_plain_width_cells_and_fixed_run(tmp_path, ramp_join):
    output = tmp_path / "ramp"
    assert (
        main(
            [
                "part",
                "--family",
                "ramp",
                "--ramp-join",
                ramp_join,
                "--width-cells",
                "3",
                "--build-width-mm",
                "350",
                "--build-depth-mm",
                "320",
                "--build-height-mm",
                "325",
                "--no-stl",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    manifest = json.loads((output / "manifest.json").read_text())
    design = manifest["designs"][0]
    assert design["parameters"]["family"] == "ramp"
    assert (design["parameters"]["nx"], design["parameters"]["ny"]) == (3, 1)
    projection = 6 if ramp_join == "male" else 0
    assert design["size_mm"] == pytest.approx([180, 50 + projection, 13])
    assert design["mating_datums"]["ramp_join"] == ramp_join
    assert design["mating_datums"]["finished_run"] == 50
    assert design["mating_datums"]["tab_projection"] == projection
    assert design["mating_datums"]["overall_depth"] == 50 + projection
    assert design["compatibility"]["tile_edge_interface_present"]
    assert not design["compatibility"]["x_attachment_interface_present"]
    assert "ramp_3x1" in accessory_design(Accessory("ramp", nx=3)).name


def test_bambu_ramp_scopes_normal_auto_without_changing_orientation_or_other_objects(tmp_path):
    ramp = accessory_design(Accessory("ramp", nx=3))
    plate = accessory_design(Accessory("plate"))
    assert not ramp.apply_orientation_to_bambu
    assert ramp.bambu_size == ramp.size
    assert ramp.bambu_object_settings == {
        "enable_support": "1",
        "support_type": "normal(auto)",
    }
    settings = BambuSettings((Material("PETG", "PETG", "#637b70"),), 0.4, 0.2)
    path = tmp_path / "mixed.3mf"
    report = write_3mf(
        Job([ramp, plate], BuildVolume(350, 320, 325), "catalogue"),
        path,
        bambu=settings,
    )
    items = [item for plate_record in report["plates"] for item in plate_record["items"]]
    ramp_item = next(item for item in items if item["design"] == ramp.name)
    plate_item = next(item for item in items if item["design"] == plate.name)
    assert ramp_item["object_settings"] == ramp.bambu_object_settings
    assert "source_to_project_transform" not in ramp_item
    assert "object_settings" not in plate_item
    with ZipFile(path) as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        project = json.loads(archive.read("Metadata/project_settings.config"))
    by_name = {}
    for obj in config.findall("object"):
        values = {entry.get("key"): entry.get("value") for entry in obj.findall("metadata")}
        by_name[values["name"]] = values
    assert by_name[f"{ramp.name}_batch_1"]["enable_support"] == "1"
    assert by_name[f"{ramp.name}_batch_1"]["support_type"] == "normal(auto)"
    assert "enable_support" not in by_name[f"{plate.name}_batch_2"]
    assert "enable_support" not in project
    with pytest.raises(ValueError, match="object settings"):
        write_3mf(
            Job(
                [replace(ramp, bambu_object_settings={})],
                BuildVolume(350, 320, 325),
                "part",
            ),
            tmp_path / "missing-object-settings.3mf",
            bambu=settings,
        )


def test_ramp_rejects_irrelevant_height_and_unsupported_interfaces():
    custom = make_accessory(Accessory("ramp", interface=Interface(pitch=65, height=18)))
    assert tuple(custom.bounding_box().size) == pytest.approx((65, 50, 18), abs=1e-5)
    with pytest.raises(ValueError, match="original roofed tile-edge joints"):
        Accessory("ramp", interface=Interface(joint_style="full-height"))
    with pytest.raises(ValueError, match="height does not apply"):
        Accessory("ramp", height=120)
    with pytest.raises(ValueError, match="ramp uses nx"):
        Accessory("ramp", ny=2)
    with pytest.raises(ValueError, match="ramp join must be"):
        Accessory("ramp", ramp_join="both")
    with pytest.raises(ValueError, match="ramp join does not apply"):
        Accessory("plate", ramp_join="male")
    with pytest.raises(ValueError, match="original roofed tile-edge joints"):
        Accessory("ramp", ramp_join="male", interface=Interface(joint_style="full-height"))


def test_approved_shape_constants_are_explicit():
    assert RAMP_RUN_MM == 50
    assert RAMP_CARRIER_RUN_MM == 10
    assert RAMP_FREE_EDGE_RADIUS_MM == 2
    assert RAMP_SHELF_RADIUS_MM == 32
    assert RAMP_MINIMUM_FLAT_SHELF_MM == 2
