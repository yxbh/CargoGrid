"""Round openings and fixed PETG/PLA support requests."""

import pytest
from build123d import Vector
from local_geometry import prepared_contains

from cargo_grid import BuildVolume, Tile, make_tile
from cargo_grid.export import (
    BambuSettings,
    Material,
    _checked_step_roundtrip,
    _StepSource,
    write_3mf,
)
from cargo_grid.jobs import Job, tile_design
from cargo_grid.roof_support import RoofSupportSettings
from cargo_grid.stacking import StackSettings
from cargo_grid.tiles import hole_placements


@pytest.mark.parametrize("nx,ny,expected", [(2, 3, 29), (4, 4, 65)])
def test_full_scope_exact_lattice_and_bores(nx, ny, expected, tmp_path):
    spec = Tile(nx, ny)
    holes = hole_placements(spec)
    assert len(holes) == expected and all(h.accepted for h in holes)
    shape = make_tile(spec)
    assert shape.is_valid and len(shape.solids()) == 1
    bounds = shape.bounding_box()
    assert tuple(bounds.size) == pytest.approx((nx * 60 + 6, ny * 60 + 6, 13), abs=1e-5)
    contains = prepared_contains((shape,))
    for hole in holes:
        for z in (1.5, 6.5, 11.5):
            assert not contains(Vector(hole.x, hole.y, z))
    expected_volume = 146696.4078426918 if nx == 2 else 391727.15419238823
    assert shape.volume == pytest.approx(expected_volume, abs=0.002)
    if (nx, ny) == (2, 3):
        from cargo_grid.prepared import Bounds

        source = _StepSource.measure(shape, Bounds(tuple(bounds.min), tuple(bounds.max)))
        restored, _, _, _, _ = _checked_step_roundtrip(
            shape, tmp_path / "tile.step", _source=source
        )
        assert restored.is_valid and len(restored.solids()) == 1
        assert tuple(restored.bounding_box().size) == pytest.approx(tuple(bounds.size), abs=1e-5)


def test_full_default_interior_override_and_terminated_fillers_are_preserved():
    assert Tile().hole_scope == "full" and Tile().hole_diameter == 10
    assert len(hole_placements(Tile(2, 3, hole_scope="interior"))) == 9
    spec = Tile(2, 3, west=False, filler_west=3)
    assert all(
        not h.accepted and "terminated" in h.reason for h in hole_placements(spec) if h.x == 0
    )
    shape = make_tile(spec)
    assert shape.is_valid
    assert tuple(shape.bounding_box().size) == pytest.approx((129, 186, 13), abs=1e-5)


def test_material_roles_must_be_explicit_petg_and_pla():
    for materials in (
        (Material("PETG", "PETG", "#ffffff"),),
        (Material("PLA", "PLA", "#ffffff"), Material("PETG", "PETG", "#ffffff")),
    ):
        with pytest.raises(ValueError, match="PETG"):
            BambuSettings(materials, 0.4, 0.24, RoofSupportSettings())


def test_stacking_is_rejected_before_export(tmp_path):
    settings = BambuSettings(
        (Material("model", "PETG", "#ffffff"), Material("interface", "PLA", "#dddddd")),
        0.8,
        0.32,
        RoofSupportSettings(),
    )
    path = tmp_path / "bad.3mf"
    with pytest.raises(ValueError, match="stacked"):
        write_3mf(
            Job([tile_design(Tile())], BuildVolume(150, 150, 50), "part"),
            path,
            bambu=settings,
            stack=StackSettings(2, 1, 0.32, 1, 1, 2),
        )
    assert not path.exists()
