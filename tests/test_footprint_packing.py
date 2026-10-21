"""Projected-footprint packing and explicit-placement collision checks."""

from math import inf, nan
from types import SimpleNamespace
from zipfile import ZipFile

import numpy as np
import pytest
from build123d import Align, Box
from shapely.geometry import Polygon, box

from cargo_grid import catalogue
from cargo_grid.export import (
    BambuSettings,
    Material,
    _explicit_placements,
    _PreparedProject,
    export_job,
)
from cargo_grid.footprints import (
    ProjectedFootprint,
    minimum_projected_clearance,
    oriented_footprint,
    pack_projected_footprints,
    placed_footprint,
    projected_mesh_footprint,
)
from cargo_grid.jobs import Design, Job
from cargo_grid.packing import PrintPlacement, packing_score
from cargo_grid.parameters import BuildVolume
from cargo_grid.plates import PlateGroup, plan_plates
from cargo_grid.printers import H2D, X2D


@pytest.fixture(scope="module")
def wide_perimeter_designs():
    return [
        catalogue.accessory_design(spec)
        for spec in catalogue.accessory_variants(H2D.build)
        if spec.family in {"edge-x", "edge-y", "corner-in", "corner-out"}
        and spec.edge_outward == 30
    ]


@pytest.mark.slow
@pytest.mark.parametrize(
    "printer,layer,max_cells,rectangle_plates",
    [(X2D, 0.32, 4, 3), (X2D, 0.24, 4, 3), (H2D, 0.32, 5, 2)],
    ids=["x2d-0.32", "x2d-0.24", "h2d-0.32"],
)
def test_real_wide_perimeter_family_uses_multi_plate_outline_packing(
    wide_perimeter_designs, printer, layer, max_cells, rectangle_plates
):
    plan = plan_plates(
        [
            PlateGroup(
                "30mm edges and corners - complete holes",
                [
                    design
                    for design in wide_perimeter_designs
                    if design.parameters["nx"] <= max_cells
                ],
                4,
                projected=True,
                max_layout_attempts=catalogue.WIDE_PERIMETER_PACKING_ATTEMPTS,
            )
        ],
        printer.common_build(),
        size=lambda design: design.bambu_size,
        auto_roof_support=True,
        layer_height_mm=layer,
        tower_width_mm=(
            printer.catalogue_support.tower_width_mm if printer.catalogue_support else None
        ),
    )
    assert not plan.unfit and len(plan.designs) == 2 * max_cells + 10
    record = plan.projected_packing["30mm edges and corners - complete holes"]
    assert plan.plate_count == 2 and record["rectangle_plates"] == rectangle_plates
    assert record["maximum_layout_attempts"] == (
        catalogue.WIDE_PERIMETER_PACKING_ATTEMPTS if rectangle_plates > 2 else 32
    )
    assert set(plan.projected_clearances) <= set(range(plan.plate_count))
    job = Job(
        plan.designs,
        printer.build,
        "catalogue",
        print_placements=plan.placements,
        projected_footprints=plan.projected_footprints,
        projected_footprint_clearances=plan.projected_clearances,
        plate_builds=plan.plate_builds,
    )
    assert _PreparedProject(job, None, None).placements == plan.placements


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


def test_projected_tile_rounds_preserve_all_non_degenerate_triangle_centres():
    from shapely import contains_xy

    from cargo_grid.jobs import tile_design
    from cargo_grid.meshes import checked_mesh
    from cargo_grid.parameters import Tile

    design = tile_design(Tile(1, 4))
    vertices, faces, _ = checked_mesh(design.bambu_shape)
    footprint = projected_mesh_footprint(vertices, faces)
    triangles = vertices[faces, :2]
    first, second = triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    selected = triangles[np.abs(first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]) > 1e-6]
    centres = selected.mean(axis=1)
    assert footprint.geometry.is_valid
    assert footprint.size == pytest.approx(design.bambu_size[:2], abs=1e-6)
    assert contains_xy(footprint.geometry.buffer(1e-9), centres[:, 0], centres[:, 1]).all()


def test_restart_budget_and_seed_are_fixed_and_repeatable():
    from cargo_grid.footprints import PACKING_SEARCH_ATTEMPTS, _packing_attempts

    footprints = [ProjectedFootprint(box(0, 0, w, d)) for w, d in ((8, 8), (9, 4), (5, 6))]
    rasters = {
        (i, 0): SimpleNamespace(width=footprint.size[0], depth=footprint.size[1])
        for i, footprint in enumerate(footprints)
    }
    attempts = list(_packing_attempts(footprints, rasters, PACKING_SEARCH_ATTEMPTS))
    assert PACKING_SEARCH_ATTEMPTS == 32 and len(attempts) == 32
    assert attempts == list(_packing_attempts(footprints, rasters, PACKING_SEARCH_ATTEMPTS))
    assert len({tuple(order) for order, _, _, _ in attempts}) > 1
    assert {seed for _, _, seed, _ in attempts} == {None, 0, 90, 180, 270}
    assert all(
        sorted(order) == [0, 90, 180, 270]
        for _, _, _, preferences in attempts
        if preferences
        for order in preferences.values()
    )


def test_packing_score_prefers_plate_count_then_compact_last_plate():
    sizes = [(5, 5, 1)] * 3
    compact = [PrintPlacement(0, 0, 0, 0), PrintPlacement(0, 7, 0, 0), PrintPlacement(1, 0, 0, 0)]
    spread = [PrintPlacement(0, 0, 0, 0), PrintPlacement(1, 0, 0, 0), PrintPlacement(1, 7, 0, 0)]
    one_plate = [
        PrintPlacement(0, 0, 0, 0),
        PrintPlacement(0, 7, 0, 0),
        PrintPlacement(0, 14, 0, 0),
    ]
    assert packing_score(compact, sizes) == (2, 25)
    assert packing_score(spread, sizes) == (2, 60)
    assert (
        packing_score(one_plate, sizes)
        < packing_score(compact, sizes)
        < packing_score(spread, sizes)
    )


def test_later_over_budget_attempts_do_not_discard_a_verified_layout(monkeypatch):
    from cargo_grid import footprints as module

    original = module._pack_order

    def limited(*args, **kwargs):
        if len(kwargs["rotations"]) == 4:
            raise module._CandidateBudgetExceeded("projected-footprint candidate budget")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_pack_order", limited)
    footprints = [ProjectedFootprint(box(0, 0, 8, 8)) for _ in range(2)]
    placements = pack_projected_footprints(footprints, (0, 0, 20, 10), gap=2, search_gap=2)
    assert {p.plate for p in placements} == {0}
    assert minimum_projected_clearance(footprints, placements, plate=0) >= 2 - 1e-6


@pytest.mark.parametrize("adaptive", [False, True])
def test_process_workers_produce_identical_multi_plate_placements(adaptive):
    footprints = (
        [ProjectedFootprint(box(0, 0, 10, 10))] * 2
        if adaptive
        else [
            ProjectedFootprint(Polygon([(0, 0), (14, 0), (14, 4), (4, 4), (4, 12), (0, 12)]))
            for _ in range(4)
        ]
    )
    serial = pack_projected_footprints(
        footprints, (0, 0, 18, 14), gap=2, search_gap=1.25, workers=1, max_layout_attempts=64
    )
    parallel = pack_projected_footprints(
        footprints, (0, 0, 18, 14), gap=2, search_gap=1.25, workers=2, max_layout_attempts=64
    )
    assert parallel == serial and {p.plate for p in parallel} == {0, 1}


def test_repeated_nested_exports_are_byte_identical_including_zip_metadata(tmp_path, monkeypatch):
    def generate(folder, workers):
        monkeypatch.setenv("CARGO_GRID_PACKING_WORKERS", str(workers))
        aligned = (Align.MIN, Align.MIN, Align.MIN)
        corner = Box(14, 4, 3, align=aligned) + Box(4, 12, 3, align=aligned)
        designs = [Design(name, corner, {}) for name in ("first", "second")]
        build = BuildVolume(18, 14, 10)
        plan = plan_plates(
            [PlateGroup("Corners", designs, 2, projected=True, max_layout_attempts=64)],
            build,
            size=lambda d: d.size,
        )
        assert plan.plate_count == 1 and plan.projected_clearances == {0: 2}
        job = Job(
            plan.designs,
            build,
            "catalogue",
            print_placements=plan.placements,
            projected_footprints=plan.projected_footprints,
            projected_footprint_clearances=plan.projected_clearances,
            plate_builds=plan.plate_builds,
        )
        settings = BambuSettings((Material("PETG", "PETG", "#637b70"),), 0.8, 0.32)
        return export_job(job, tmp_path / folder, stl=False, bambu=settings)

    first, second = [generate(folder, workers) for folder, workers in (("first", 1), ("second", 2))]
    assert first.read_bytes() == second.read_bytes()
    archives = [path.parent / "job.3mf" for path in (first, second)]
    assert archives[0].read_bytes() == archives[1].read_bytes()
    with ZipFile(archives[0]) as archive:
        assert {entry.date_time for entry in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_cad_envelope_sizes_are_not_replaced_by_smaller_mesh_bounds():
    footprints = [ProjectedFootprint(box(0, 0, 8.2, 8.2))] * 2
    placements = pack_projected_footprints(
        footprints,
        (0, 0, 19, 10),
        gap=0.5,
        search_gap=0.25,
        bounding_sizes=[(9.1, 9.1)] * 2,
    )
    assert {p.plate for p in placements} == {0, 1}
    assert all(p.x + 9.1 <= 19 + 1e-6 and p.y + 9.1 <= 10 + 1e-6 for p in placements)
    with pytest.raises(ValueError, match="must match"):
        pack_projected_footprints(
            footprints, (0, 0, 19, 10), gap=0.5, search_gap=0.25, bounding_sizes=[(9.1, 9.1)]
        )
    with pytest.raises(ValueError, match="must contain"):
        pack_projected_footprints(
            footprints, (0, 0, 19, 10), gap=0.5, search_gap=0.25, bounding_sizes=[(8, 8)] * 2
        )


@pytest.mark.parametrize(("width", "obstacles"), [(19, ()), (30, ((19.05, 0, 30, 10),))])
def test_refinement_keeps_cad_bounds_inside_the_envelope_and_clear_of_obstacles(width, obstacles):
    from cargo_grid.footprints import _refine_clearance

    footprints = [ProjectedFootprint(box(0, 0, 8.2, 8.2))] * 2
    placements = {0: (0, 0, 0), 1: (9, 0, 0)}
    options = dict(
        width=width,
        depth=10,
        target=1.5,
        grid=1,
        tolerance=1e-6,
        max_refinements=20,
        obstacles=obstacles,
    )
    assert _refine_clearance(footprints, placements, **options)[1][0] == 10
    with pytest.raises(ValueError, match="could not refine"):
        _refine_clearance(footprints, placements, bounding_sizes=[(9.1, 9.1)] * 2, **options)


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


@pytest.mark.parametrize("bounds,required_rotation", [((0, 0, 18, 14), 270), ((0, 0, 20, 16), 180)])
def test_projected_packer_retries_reversed_asymmetric_footprints(bounds, required_rotation):
    footprint = ProjectedFootprint(Polygon(((0, 0), (14, 0), (14, 4), (4, 4), (4, 12), (0, 12))))
    footprints = [footprint, footprint]
    placements = pack_projected_footprints(footprints, bounds, gap=2, search_gap=1.25)
    assert required_rotation in {placement.rotation for placement in placements}
    assert {placement.plate for placement in placements} == {0}
    assert minimum_projected_clearance(footprints, placements, plate=0) >= 2 - 1e-6
    assert placements == pack_projected_footprints(footprints, bounds, gap=2, search_gap=1.25)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_oriented_footprint_normalizes_all_quarter_turns(rotation):
    footprint = ProjectedFootprint(Polygon(((0, 0), (14, 0), (14, 4), (4, 4), (4, 12), (0, 12))))
    oriented = oriented_footprint(footprint, rotation)
    assert oriented.geometry.bounds[:2] == pytest.approx((0, 0))
    assert oriented.size == pytest.approx((14, 12) if rotation % 180 == 0 else (12, 14))
    assert oriented.geometry.area == pytest.approx(footprint.geometry.area)


def test_projected_packer_refuses_an_oversized_part_before_raster_search(monkeypatch):
    from cargo_grid import footprints as module

    def unexpected(*args, **kwargs):
        raise AssertionError("rasterized a group larger than the plate")

    monkeypatch.setattr(module, "_rasterize", unexpected)
    with pytest.raises(ValueError, match="could not fit"):
        pack_projected_footprints(
            [ProjectedFootprint(box(0, 0, 11, 11))],
            (0, 0, 10, 10),
            gap=1,
            search_gap=1,
        )


def test_projected_packer_carries_large_total_area_across_single_part_plates():
    footprints = [ProjectedFootprint(box(0, 0, 9, 9))] * 3
    placements = pack_projected_footprints(footprints, (2, 3, 12, 13), gap=1, search_gap=1)
    assert [p.plate for p in placements] == [0, 1, 2]
    assert all(p.x >= 2 and p.y >= 3 for p in placements)
    assert placements == pack_projected_footprints(footprints, (2, 3, 12, 13), gap=1, search_gap=1)


def test_projected_packer_nests_quarter_turned_shapes_on_every_plate():
    corner = ProjectedFootprint(Polygon(((0, 0), (14, 0), (14, 4), (4, 4), (4, 12), (0, 12))))
    footprints = [corner] * 4
    placements = pack_projected_footprints(footprints, (0, 0, 18, 14), gap=2, search_gap=1.25)
    assert {p.plate for p in placements} == {0, 1}
    assert 270 in {p.rotation for p in placements}
    for plate in (0, 1):
        assert sum(p.plate == plate for p in placements) == 2
        assert minimum_projected_clearance(footprints, placements, plate=plate) >= 2 - 1e-6
    assert placements == pack_projected_footprints(
        footprints, (0, 0, 18, 14), gap=2, search_gap=1.25
    )


def test_projected_packer_fills_space_after_a_larger_piece_is_deferred():
    footprints = [
        ProjectedFootprint(box(0, 0, 12, 8)),
        ProjectedFootprint(box(0, 0, 12, 8)),
        ProjectedFootprint(box(0, 0, 4, 4)),
    ]
    placements = pack_projected_footprints(footprints, (0, 0, 20, 10), gap=2, search_gap=2)
    assert placements[0].plate == placements[2].plate == 0
    assert placements[1].plate == 1
    assert minimum_projected_clearance(footprints, placements, plate=0) >= 2 - 1e-6


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
        (PrintPlacement(0, 0, 0, 45), (10, 10, 3), "0/90/180/270 rotations"),
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


def test_projected_packing_keeps_bounding_boxes_out_of_obstacles():
    footprints = [ProjectedFootprint(box(0, 0, 40, 40)) for _ in range(3)]
    placements = pack_projected_footprints(
        footprints, (10, 10, 130, 130), gap=2, search_gap=1.5, obstacles=((0, 0, 60, 60),)
    )
    for footprint, placement in zip(footprints, placements):
        x0, y0, _, _ = placed_footprint(footprint, placement).bounds
        assert x0 >= 60 - 1e-6 or y0 >= 60 - 1e-6
    with pytest.raises(ValueError, match="could not fit"):
        pack_projected_footprints(
            footprints, (10, 10, 130, 130), gap=2, search_gap=1.5, obstacles=((0, 0, 130, 130),)
        )


def test_folded_exclusions_remain_strict_when_outer_bounds_allow_numerical_tolerance():
    from cargo_grid.parameters import Exclusion
    from cargo_grid.plates import _projected_bounds

    build = BuildVolume(20, 10, 5, exclusions=(Exclusion(0, 0, 1, 10), Exclusion(10, 0, 10, 10)))
    bounds, obstacles = _projected_bounds(build)
    placements = pack_projected_footprints(
        [ProjectedFootprint(box(0, 0, 9, 9))],
        bounds,
        gap=1,
        search_gap=1,
        bounding_sizes=[(9 + 1e-7, 9)],
        obstacles=obstacles,
    )
    p = placements[0]
    assert p.rotation in (90, 270)
    assert build.contains_box(p.x, p.y, 9, 9 + 1e-7, 1)
