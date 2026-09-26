import os
from pathlib import Path

import pytest
from build123d import Compound, Location, Vector

from cargo_grid import BuildVolume, Tile, make_tile, tiles
from cargo_grid.export import _checked_step_roundtrip
from cargo_grid.interfaces import make_plug, prism, rectangle, x_profile
from cargo_grid.jobs import Design, Job, layout_job
from cargo_grid.layout import exact_layout
from cargo_grid.parameters import Interface
from cargo_grid.tiles import hole_placements


def test_prism_wrapper_has_real_positive_volume():
    shape = prism(rectangle(0, 0, 10, 20), 3)
    assert shape.is_valid
    assert shape.volume == pytest.approx(600)


def test_analytic_socket_and_plug_dimensions():
    face = x_profile()
    assert tuple(face.bounding_box().size)[:2] == pytest.approx((44.556349, 44.556349), abs=1e-6)
    assert face.is_inside(Vector(13.63, 0, 0))
    assert not face.is_inside(Vector(13.65, 0, 0))
    plug = make_plug()
    assert plug.is_valid and len(plug.solids()) == 1
    assert plug.bounding_box().size.Z == pytest.approx(12.8, abs=1e-6)


@pytest.mark.parametrize("nx,ny", [(1, 1), (2, 3), (4, 3), (5, 1)])
def test_tile_validity_dimensions_and_step_roundtrip(nx, ny, tmp_path):
    shape = make_tile(Tile(nx, ny))
    assert shape.is_valid and len(shape.solids()) == 1
    assert tuple(shape.bounding_box().size) == pytest.approx(
        (60 * nx + 6, 60 * ny + 6, 13), abs=1e-5
    )
    path = tmp_path / "tile.step"
    restored, _, _, _, _ = _checked_step_roundtrip(shape, path)
    assert restored.is_valid and len(restored.solids()) == 1
    assert tuple(restored.bounding_box().size) == pytest.approx(
        tuple(shape.bounding_box().size), abs=1e-5
    )


def test_interior_hole_centers_and_actual_void_radii():
    tile = Tile(2, 3, hole_diameter=10, hole_scope="interior")
    holes = hole_placements(tile)
    assert len(holes) == 9 and all(h.accepted for h in holes)
    assert {(h.x, h.y) for h in holes} >= {(60, 60), (60, 120), (60, 30)}
    shape = make_tile(tile)
    for hole in holes:
        for z in (1, 6.5, 12):
            assert not shape.is_inside(Vector(hole.x + 4.99, hole.y, z))
            assert shape.is_inside(Vector(hole.x + 5.01, hole.y, z))
    assert all(not h.accepted for h in hole_placements(Tile(2, 2, hole_diameter=60)))


def test_memoised_hole_placements_match_a_fresh_classification_and_stay_private():
    cases = [
        Tile(3, 2),
        Tile(2, 1, hole_scope="interior"),
        Tile(1, 1, Interface(pitch=30, height=8)),
        Tile(2, 2, hole_diameter=None),
    ]
    for tile in cases:
        fresh = list(tiles._hole_placements.__wrapped__(repr(tile), tile))
        first = hole_placements(tile)
        assert first == fresh
        first.append("caller-owned")
        assert hole_placements(tile) == fresh
    # Equal int and float units print differently in manifests, so they must not share entries.
    as_int = hole_placements(Tile(2, 2, Interface(pitch=60), hole_scope="interior"))
    as_float = hole_placements(Tile(2, 2, Interface(pitch=60.0), hole_scope="interior"))
    assert as_int == as_float
    assert [repr(h.x) for h in as_int] != [repr(h.x) for h in as_float]


@pytest.mark.parametrize("nx,ny,expected", [(1, 1, 8), (2, 1, 13), (2, 3, 29), (4, 4, 65)])
def test_default_tile_uses_full_ten_millimeter_pattern(nx, ny, expected):
    tile = Tile(nx, ny)
    holes = hole_placements(tile)
    assert tile.hole_diameter == 10
    assert tile.hole_scope == "full"
    assert len(holes) == expected
    assert all(hole.accepted for hole in holes)
    assert Tile(nx, ny, hole_diameter=None).hole_diameter is None


@pytest.mark.parametrize(
    "width,depth,filler",
    [
        (120, 120, "balanced"),
        (121, 137, "positive"),
        (179.99, 241, "negative"),
    ],
)
def test_exact_layout_actual_geometry_bounds_and_disjointness(width, depth, filler):
    build = BuildVolume(150, 140, 30, margin=2)
    job = layout_job(exact_layout(width, depth, build, distribution=filler), build)
    shapes = [d.shape.moved(Location(frame)) for d in job.designs for frame in d.assembly_frames]
    bounds = Compound(shapes).bounding_box()
    assert tuple(bounds.min) == pytest.approx((0, 0, 0), abs=1e-5)
    assert tuple(bounds.max) == pytest.approx((width, depth, 13), abs=1e-5)
    for design in job.designs:
        assert build.placement(design.size) is not None
    for i, a in enumerate(shapes):
        for b in shapes[i + 1 :]:
            intersection = a.intersect(b)
            assert intersection is None or sum(s.volume for s in intersection.solids()) < 0.001


def test_service_rejects_invalid_job_quantities():
    shape = prism(rectangle(0, 0, 10, 10), 2)
    for quantity in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            Design("test", shape, {}, quantity=quantity)
    with pytest.raises(ValueError):
        Job([], BuildVolume(100, 100, 100), "part")


@pytest.mark.skipif(
    not os.environ.get("CARGO_GRID_REFERENCE"), reason="local reference not supplied"
)
@pytest.mark.reference
def test_local_functional_interoperability():
    from cargo_grid.validation import compare_reference

    report = compare_reference(Path(os.environ["CARGO_GRID_REFERENCE"]))
    assert report["joint_style"] == "original"
    assert report["functional_envelope_pass"], report["joining_mating_min_clearance_mm"]
    assert report["engagement_flanks_and_plug_pass"]
    assert report["not_checked"]
