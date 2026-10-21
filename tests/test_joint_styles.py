"""The roofed joint contract and its serialized identity."""

import json

import pytest
from build123d import Compound, Location, Vector, export_step, import_step

from cargo_grid import BuildVolume, Interface, Tile, make_tile
from cargo_grid.accessories import Accessory
from cargo_grid.catalogue import accessory_design, tile_sizes
from cargo_grid.cli import parser
from cargo_grid.export import export_job
from cargo_grid.jobs import Job, layout_job, tile_design
from cargo_grid.layout import exact_layout

pytestmark = pytest.mark.geometry_inputs


def test_default_contract_and_invalid_styles():
    interface = Interface()
    assert interface.joint_style == "original"
    assert interface.reference_defaults and interface.reference_socket_dimensions
    assert interface.compatibility()["geometry_warning"] is None
    assert not interface.compatibility()["experimental"]
    for value in ("full-height", "full", "reference", "", None, 1):
        with pytest.raises(ValueError, match="joint_style"):
            Interface(joint_style=value)
    for command in ("part", "catalogue"):
        args = [command, "--output", "outputs/example"]
        assert parser().parse_args(args + ["--joint-style", "original"]).joint_style == "original"
        with pytest.raises(SystemExit):
            parser().parse_args(args + ["--joint-style", "full-height"])


def test_actual_roofed_sections_and_male_top_planes(tmp_path):
    shape = make_tile(Tile(2, 3, hole_diameter=None))
    for z in (10.3, 11, 12, 12.9):
        for point in ((123, 30, z), (30, 183, z)):
            assert not shape.is_inside(Vector(*point))
        for point in ((3, 30, z), (30, 3, z)):
            assert shape.is_inside(Vector(*point))
    assert shape.is_valid and len(shape.solids()) == 1
    path = tmp_path / "roofed.step"
    assert export_step(shape, path)
    restored = import_step(path)
    assert restored.is_valid
    assert not restored.is_inside(Vector(123, 30, 12))
    assert restored.is_inside(Vector(3, 30, 12))


def test_style_survives_layout_and_exported_manifest(tmp_path):
    build = BuildVolume(150, 140, 50)
    job = layout_job(exact_layout(121, 137, build, hole_diameter=None), build)
    assert all(d.parameters["interface"]["joint_style"] == "original" for d in job.designs)
    assembly = Compound(
        [d.shape.moved(Location(frame)) for d in job.designs for frame in d.assembly_frames]
    )
    assert tuple(assembly.bounding_box().size) == pytest.approx((121, 137, 13), abs=1e-5)
    manifest = json.loads(
        export_job(
            Job([tile_design(Tile(hole_diameter=None))], build, "part"),
            tmp_path / "job",
            stl=False,
        ).read_text()
    )
    assert manifest["joint_styles"] == ["original"]
    compatibility = manifest["designs"][0]["compatibility"]
    assert compatibility["original_tile_edge_dimensions"]
    assert compatibility["original_x_attachment_dimensions"]
    assert not compatibility["experimental"]
    assert "original" in accessory_design(Accessory("edge-x")).name
    assert set(tile_sizes(BuildVolume(306, 306, 50))) == {
        (x, y) for x in range(1, 6) for y in range(1, 6)
    }
