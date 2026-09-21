"""Projected-footprint packing and explicit-placement collision checks."""

from math import inf, nan

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


def test_projected_packer_handles_reordered_concave_shapes_and_mixed_rotations():
    footprints = [
        ProjectedFootprint(Polygon(((0, 0), (14, 0), (14, 4), (4, 4), (4, 12), (0, 12)))),
        ProjectedFootprint(box(0, 0, 12, 5)),
        ProjectedFootprint(box(0, 0, 6, 10)),
    ]
    for selected in (footprints, list(reversed(footprints))):
        placements = pack_projected_footprints(
            selected,
            (0, 0, 20, 16),
            gap=2,
            search_gap=1.5,
            grid=1,
        )
        assert {placement.plate for placement in placements} == {0}
        assert {placement.rotation for placement in placements} == {0, 90}
        assert minimum_projected_clearance(selected, placements, plate=0) >= 2 - 1e-6
        assert placements == pack_projected_footprints(
            selected,
            (0, 0, 20, 16),
            gap=2,
            search_gap=1.5,
            grid=1,
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


def test_explicit_placements_apply_physical_and_per_plate_envelopes():
    placements = [
        PrintPlacement(0, 5, 5, 0),
        PrintPlacement(1, 70, 5, 90),
    ]
    sizes = [(20, 10, 3), (10, 20, 3)]
    physical = BuildVolume(100, 50, 10, margin=5)
    constrained = BuildVolume(40, 50, 10, margin=5)
    assert (
        _explicit_placements(
            placements,
            sizes,
            physical,
            2,
            plate_builds={0: constrained},
        )
        == placements
    )
    with pytest.raises(ValueError, match="plate build envelope"):
        _explicit_placements(
            [PrintPlacement(0, 20, 5, 0), placements[1]],
            sizes,
            physical,
            2,
            plate_builds={0: constrained},
        )
    with pytest.raises(ValueError, match="build envelope"):
        _explicit_placements(
            [PrintPlacement(0, 5, 5, 0), PrintPlacement(1, 85, 5, 90)],
            sizes,
            physical,
            2,
            plate_builds={0: constrained},
        )


@pytest.mark.parametrize(
    "placement,size,message",
    [
        (PrintPlacement(0, nan, 0, 0), (10, 10, 3), "coordinates must be finite"),
        (PrintPlacement(0, 0, inf, 0), (10, 10, 3), "coordinates must be finite"),
        (PrintPlacement(0, 0, 0, 45), (10, 10, 3), "0/90 rotations"),
        (PrintPlacement(-1, 0, 0, 0), (10, 10, 3), "nonnegative plates"),
        (PrintPlacement(0, 0, 0, 0), (nan, 10, 3), "part dimension"),
    ],
)
def test_explicit_placements_reject_malformed_inputs(placement, size, message):
    with pytest.raises(ValueError, match=message):
        _explicit_placements([placement], [size], BuildVolume(30, 30, 10), 2)


def test_projected_clearance_cannot_bypass_a_plate_envelope():
    footprint = ProjectedFootprint(box(0, 0, 15, 10))
    with pytest.raises(ValueError, match="projected footprint exceeds its plate build envelope"):
        _explicit_placements(
            [PrintPlacement(0, 0, 0, 0)],
            [(10, 10, 3)],
            BuildVolume(30, 30, 10),
            2,
            [footprint],
            {0: 2},
            {0: BuildVolume(12, 30, 10)},
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


def test_job_preserves_legacy_positional_field_order():
    design = Design("box", Box(10, 10, 3), {})
    placement = PrintPlacement(0, 0, 0, 0)
    footprint = ProjectedFootprint(box(0, 0, 10, 10))
    job = Job(
        [design],
        BuildVolume(30, 30, 10),
        "catalogue",
        [{"reason": "legacy"}],
        (10, 10),
        3,
        [placement],
        {0: "Legacy plate"},
        {0: {"filament_map_mode": "Manual"}},
        {"name": "legacy policy"},
        [footprint],
        {0: 2},
    )
    assert job.placement_policy == {"name": "legacy policy"}
    assert job.projected_footprints == [footprint]
    assert job.projected_footprint_clearances == {0: 2}
    assert job.plate_builds == {}


def test_job_rejects_non_placement_entries_before_accessing_plate():
    with pytest.raises(ValueError, match="PrintPlacement instances"):
        Job(
            [Design("box", Box(10, 10, 3), {})],
            BuildVolume(30, 30, 10),
            "catalogue",
            print_placements=[None],
        )


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"plate_builds": {0: BuildVolume(20, 20, 10)}}, "require explicit"),
        (
            {
                "print_placements": [PrintPlacement(0, 0, 0, 0)],
                "plate_builds": {1: BuildVolume(20, 20, 10)},
            },
            "no placed design",
        ),
        (
            {
                "print_placements": [PrintPlacement(0, 0, 0, 0)],
                "plate_builds": {0: BuildVolume(31, 20, 10)},
            },
            "cannot exceed",
        ),
        (
            {
                "print_placements": [PrintPlacement(0, 0, 0, 0)],
                "plate_builds": {"0": BuildVolume(20, 20, 10)},
            },
            "nonnegative integer",
        ),
        (
            {
                "print_placements": [PrintPlacement(0, 0, 0, 0)],
                "plate_builds": {0: object()},
            },
            "BuildVolume",
        ),
    ],
)
def test_job_rejects_invalid_plate_build_mappings(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Job(
            [Design("box", Box(10, 10, 3), {})],
            BuildVolume(30, 30, 10),
            "catalogue",
            **kwargs,
        )


def test_prepared_export_revalidates_mutated_plate_mapping_and_placement():
    job = Job(
        [Design("box", Box(10, 10, 3), {})],
        BuildVolume(30, 30, 10),
        "catalogue",
        print_placements=[PrintPlacement(0, 0, 0, 0)],
        plate_builds={0: BuildVolume(20, 30, 10)},
    )
    assert _PreparedProject(job, None, None).placements == job.print_placements
    job.print_placements[0] = PrintPlacement(0, 15, 0, 0)
    with pytest.raises(ValueError, match="plate build envelope"):
        _PreparedProject(job, None, None)
    job.print_placements[0] = PrintPlacement(0, 0, 0, 0)
    job.plate_builds[1] = BuildVolume(20, 30, 10)
    with pytest.raises(ValueError, match="no placed design"):
        _PreparedProject(job, None, None)


def test_exact_normal_part_bounds_override_smaller_projected_hint():
    job = Job(
        [Design("wide", Box(15, 10, 3), {})],
        BuildVolume(30, 30, 10),
        "catalogue",
        print_placements=[PrintPlacement(0, 0, 0, 0)],
        projected_footprints=[ProjectedFootprint(box(0, 0, 10, 10))],
        projected_footprint_clearances={0: 1},
        plate_builds={0: BuildVolume(12, 30, 10)},
    )
    with pytest.raises(ValueError, match="plate build envelope"):
        _PreparedProject(job, None, None)


def test_explicit_placement_count_still_matches_prepared_batches():
    job = Job(
        [Design("box", Box(10, 10, 3), {}, quantity=2)],
        BuildVolume(30, 30, 10),
        "part",
        print_placements=[PrintPlacement(0, 0, 0, 0)],
    )
    with pytest.raises(ValueError, match="packed batches"):
        _PreparedProject(job, None, None)
