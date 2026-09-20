"""Projected-footprint packing and explicit-placement collision checks."""

import numpy as np
import pytest
from build123d import Box
from shapely.geometry import Polygon, box

from cargo_grid.export import _explicit_placements, _PreparedProject
from cargo_grid.footprints import (
    ProjectedFootprint,
    minimum_projected_clearance,
    pack_projected_footprints,
    projected_mesh_footprint,
)
from cargo_grid.jobs import Design, Job
from cargo_grid.packing import PrintPlacement
from cargo_grid.parameters import BuildVolume


def test_projected_mesh_footprint_unions_all_height_triangles():
    vertices = np.array(
        [
            (0, 0, 0),
            (10, 0, 0),
            (0, 10, 0),
            (20, 20, 5),
            (30, 20, 5),
            (20, 30, 5),
        ]
    )
    footprint = projected_mesh_footprint(vertices, np.array(((0, 1, 2), (3, 4, 5))))
    assert footprint.geometry.area == pytest.approx(100)
    assert footprint.size == pytest.approx((30, 30))


def test_projected_mesh_footprint_unions_large_triangle_sets_in_bounded_chunks():
    vertices = np.array(((0, 0, 0), (10, 0, 0), (0, 10, 0)))
    faces = np.tile(np.array(((0, 1, 2),)), (4097, 1))
    footprint = projected_mesh_footprint(vertices, faces)
    assert footprint.geometry.area == pytest.approx(50)
    assert footprint.size == pytest.approx((10, 10))


def test_projected_packer_is_deterministic_and_bounded():
    footprints = [
        ProjectedFootprint(box(0, 0, 8, 8)),
        ProjectedFootprint(box(0, 0, 8, 8)),
    ]
    placements = pack_projected_footprints(
        footprints,
        (2, 3, 24, 13),
        gap=2,
        search_gap=2,
        grid=1,
    )
    assert placements == pack_projected_footprints(
        footprints,
        (2, 3, 24, 13),
        gap=2,
        search_gap=2,
        grid=1,
    )
    assert minimum_projected_clearance(footprints, placements, plate=0) >= 2 - 1e-6
    with pytest.raises(ValueError, match="candidate budget"):
        pack_projected_footprints(
            footprints,
            (2, 3, 24, 13),
            gap=2,
            search_gap=2,
            grid=1,
            max_candidate_positions=1,
        )


def test_explicit_shape_clearance_validates_concave_aabb_overlap():
    concave = ProjectedFootprint(Polygon(((0, 0), (20, 0), (20, 5), (5, 5), (5, 20), (0, 20))))
    square = ProjectedFootprint(box(0, 0, 10, 10))
    sizes = [(20, 20, 3), (10, 10, 3)]
    placements = [PrintPlacement(0, 0, 0, 0), PrintPlacement(0, 10, 10, 0)]
    build = BuildVolume(30, 30, 10)
    with pytest.raises(ValueError, match="overlaps"):
        _explicit_placements(placements, sizes, build, 4)
    assert (
        _explicit_placements(
            placements,
            sizes,
            build,
            4,
            [concave, square],
            {0: 4},
        )
        == placements
    )
    with pytest.raises(ValueError, match="projected-footprint clearance"):
        _explicit_placements(
            [placements[0], PrintPlacement(0, 8, 8, 0)],
            sizes,
            build,
            4,
            [concave, square],
            {0: 4},
        )


def test_prepared_export_recomputes_actual_footprints_instead_of_trusting_hints():
    concave_hint = ProjectedFootprint(Polygon(((0, 0), (20, 0), (20, 5), (5, 5), (5, 20), (0, 20))))
    square_hint = ProjectedFootprint(box(0, 0, 10, 10))
    job = Job(
        [
            Design("large", Box(20, 20, 3), {}),
            Design("small", Box(10, 10, 3), {}),
        ],
        BuildVolume(30, 30, 10),
        "catalogue",
        print_placements=[
            PrintPlacement(0, 0, 0, 0),
            PrintPlacement(0, 10, 10, 0),
        ],
        projected_footprints=[concave_hint, square_hint],
        projected_footprint_clearances={0: 4},
    )
    with pytest.raises(ValueError, match="projected-footprint clearance"):
        _PreparedProject(job, None, None)


def test_job_requires_footprints_for_every_shape_nested_plate_design():
    footprint = ProjectedFootprint(box(0, 0, 10, 10))
    with pytest.raises(ValueError, match="every design footprint"):
        Job(
            [
                Design("first", Box(10, 10, 3), {}),
                Design("second", Box(10, 10, 3), {}),
            ],
            BuildVolume(30, 30, 10),
            "catalogue",
            print_placements=[
                PrintPlacement(0, 0, 0, 0),
                PrintPlacement(0, 15, 0, 0),
            ],
            projected_footprints=[footprint, None],
            projected_footprint_clearances={0: 4},
        )
