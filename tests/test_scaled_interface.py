import json

import pytest
from build123d import Axis, Location

from cargo_grid import BuildVolume, Interface, Tile, make_tile
from cargo_grid.accessories import (
    PANEL_BOTTOM_MM,
    Accessory,
    accessory_datums,
    make_accessory,
)
from cargo_grid.catalogue import accessory_design
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, _checked_step_roundtrip, write_3mf
from cargo_grid.interfaces import make_plug, x_profile
from cargo_grid.jobs import Job
from cargo_grid.meshes import checked_mesh


def volume(shape):
    return sum(solid.volume for solid in shape.solids()) if shape and shape.solids() else 0


@pytest.mark.parametrize(
    "unit,thickness",
    [(30, 13), (90, 13), (60, 8), (60, 18), (30, 8)],
)
def test_unit_and_thickness_scale_independent_interface_axes(unit, thickness):
    interface = Interface(unit, thickness)
    scale = unit / 60
    socket = x_profile(interface)
    plug = make_plug(interface)
    tile = make_tile(Tile(1, 1, interface, hole_diameter=None))

    assert tuple(socket.bounding_box().size)[:2] == pytest.approx(
        (44.55634918610404 * scale,) * 2,
        abs=1e-6,
    )
    assert plug.bounding_box().size.Z == pytest.approx(thickness - 0.2)
    assert tuple(tile.bounding_box().size) == pytest.approx(
        (unit * 1.1, unit * 1.1, thickness),
        abs=1e-5,
    )
    adjacent = tile.moved(Location((unit, 0, 0)))
    assert volume(tile.intersect(adjacent)) < 1e-7
    assert tile.distance_to(adjacent) < 1e-6


@pytest.mark.parametrize("unit", [30, 90])
def test_scaled_representative_accessory_families_are_valid(unit):
    interface = Interface(unit, 13)
    specs = [
        Accessory("edge-x", interface=interface),
        Accessory("edge-y", interface=interface),
        Accessory("corner-in", variant=2, interface=interface),
        Accessory("corner-out", variant=3, interface=interface),
        Accessory("plate", interface=interface),
        Accessory(
            "vertical-tile-bracket",
            nx=1,
            ny=1,
            panel_height_cells=2,
            interface=interface,
        ),
        Accessory("ramp", interface=interface),
        Accessory("vertical-stop", nx=1, ny=1, height=60, interface=interface),
        Accessory("support", interface=interface),
        Accessory("support-end", variant=1, interface=interface),
    ]
    specs.append(
        Accessory(
            "lock-45",
            nx=2 if unit == 30 else 1,
            ny=2 if unit == 30 else 1,
            interface=interface,
        )
    )
    for spec in specs:
        shape = make_accessory(spec)
        assert shape.is_valid and len(shape.solids()) == 1, spec
        assert shape.volume > 0, spec


@pytest.mark.parametrize("unit,thickness", [(30, 13), (90, 13), (60, 8), (60, 18)])
def test_scaled_wall_interface_rotates_in_local_xz_plane(unit, thickness):
    interface = Interface(unit, thickness)
    spec = Accessory(
        "vertical-tile-bracket",
        nx=1,
        ny=1,
        panel_height_cells=2,
        interface=interface,
    )
    bracket = make_accessory(spec)
    wall = make_tile(Tile(1, 2, interface, hole_diameter=None))
    top_outward = (
        wall.rotate(Axis.Z, 180)
        .rotate(Axis.X, -90)
        .moved(Location((unit, unit - thickness, PANEL_BOTTOM_MM)))
    )
    assert volume(bracket.intersect(top_outward)) < 1e-7
    datums = accessory_datums(spec)
    assert datums["panel_seat_y"] == unit - thickness
    assert datums["panel_plug_tip_y"] == pytest.approx(unit - 0.2)
    centers = datums["panel_plug_centers"]
    assert centers == [
        (unit / 2, unit - thickness, PANEL_BOTTOM_MM + unit / 2),
        (unit / 2, unit - thickness, PANEL_BOTTOM_MM + 1.5 * unit),
    ]


def test_combined_scaled_geometry_step_mesh_and_bambu_transform(tmp_path):
    interface = Interface(30, 8)
    tile = make_tile(Tile(1, 1, interface, hole_diameter=None))
    restored, _, _, _, _ = _checked_step_roundtrip(tile, tmp_path / "tile.step")
    assert restored.is_valid
    _, _, mesh = checked_mesh(tile)
    assert mesh["closed_oriented_manifold"]

    design = accessory_design(
        Accessory(
            "vertical-tile-bracket",
            nx=1,
            ny=1,
            panel_height_cells=2,
            interface=interface,
        )
    )
    settings = BambuSettings((Material("PETG", "PETG", "#637b70"),), 0.4, 0.2)
    result = write_3mf(
        Job([design], BuildVolume(150, 150, 100), "part"),
        tmp_path / "scaled.3mf",
        bambu=settings,
    )
    item = result["plates"][0]["items"][0]
    assert item["source_to_project_transform"]["rotation_y_degrees"] == -90
    assert item["object_settings"] == {
        "enable_support": "1",
        "support_type": "normal(auto)",
    }


def test_cli_exposes_unit_and_thickness_and_rejects_removed_alpha_names(tmp_path, capsys):
    output = tmp_path / "scaled"
    assert (
        main(
            [
                "part",
                "--unit-size-mm",
                "30",
                "--tile-thickness-mm",
                "8",
                "--build-width-mm",
                "100",
                "--build-depth-mm",
                "100",
                "--build-height-mm",
                "50",
                "--no-holes",
                "--no-stl",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    design = json.loads((output / "manifest.json").read_text())["designs"][0]
    assert design["parameters"]["interface"] == {
        "pitch": 30,
        "height": 8,
        "fit_offset": 0,
        "joint_style": "original",
    }
    assert design["size_mm"] == pytest.approx((33, 33, 8), abs=1e-5)

    capsys.readouterr()
    for old in ("--grid-pitch-mm", "--tile-height-mm"):
        with pytest.raises(SystemExit) as caught:
            main(
                [
                    "part",
                    old,
                    "30",
                    "--build-width-mm",
                    "100",
                    "--build-depth-mm",
                    "100",
                    "--build-height-mm",
                    "50",
                    "--output",
                    str(tmp_path / old.removeprefix("--")),
                ]
            )
        assert caught.value.code == 2
        message = capsys.readouterr().err
        assert f"unrecognized arguments: {old} 30" in message
        assert "; use" not in message


def test_explicit_custom_dimension_limits_are_clear():
    with pytest.raises(ValueError, match="unit size >= 30"):
        Interface(29, 13)
    with pytest.raises(ValueError, match="tile thickness >= 6"):
        Interface(60, 5)
    with pytest.raises(ValueError, match="unit size <= 60"):
        Accessory("lock-45", nx=2, ny=2, interface=Interface(90, 13))
