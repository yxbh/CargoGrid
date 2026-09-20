"""Catalogue packing invariants and Bambu's finite native plate capacity."""

import os
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from math import inf, nan, sqrt
from pathlib import Path
from zipfile import ZipFile

import pytest
from build123d import Box
from test_export import _project_facts

from cargo_grid.export import BambuSettings, Material, write_3mf
from cargo_grid.jobs import Design, Job
from cargo_grid.packing import PrintPlacement, pack_sizes
from cargo_grid.parameters import BuildVolume, Exclusion


def _catalogue_bounds(cells):
    sizes = [(60 * x + 6, 60 * y + 6, 13) for x in range(1, cells + 1) for y in range(1, cells + 1)]
    for n in range(1, cells + 1):
        sizes.extend(((60 * n, 16, 13), (60 * n, 10, 13), (45, 60 * n + 5, 25)))
    sizes.extend((x, y, 13) for x, y in ((60, 66), (66, 66), (66, 60), (60, 60)))
    sizes.extend(
        (x, y, 13)
        for x, y in (
            (16, 60 + 5 * sqrt(2)),
            (60 + 5 * sqrt(2), 10),
            (70, 70),
            (10, 60 + 5 * sqrt(2)),
            (60 + 5 * sqrt(2), 16),
            (70, 70),
        )
    )
    sizes.extend(((45, 120, 25), (45, 120, 25), (45, 125, 25), (45, 125, 25)))
    sizes.extend((45, length + 5, 25) for length in (20, 30, 40, 50))
    sizes.extend(
        (60 * x, 60 * y, 62.8)
        for x, y in (
            (1, 1),
            (1, 2),
            (2, 1),
            (2, 2),
            (3, 1),
            (3, 2),
            (1, 1),
            (2, 2),
        )
    )
    sizes.extend((60 * x, 60 * y, 16.9) for x, y in ((1, 1), (1, 2), (2, 2)))
    return sizes


def _assert_placements(sizes, placements, build, gap):
    assert len(placements) == len(sizes)
    rectangles = []
    for size, placement in zip(sizes, placements):
        assert placement.plate >= 0
        assert placement.rotation in (0, 90)
        w, d = size[:2] if placement.rotation == 0 else size[1::-1]
        x, y = placement.x, placement.y
        assert x >= build.margin - 1e-6
        assert y >= build.margin - 1e-6
        assert x + w <= build.x - build.margin - build.reserve_x + 1e-6
        assert y + d <= build.y - build.margin - build.reserve_y + 1e-6
        assert size[2] <= build.z - build.reserve_z + 1e-6
        for area in build.exclusions:
            assert (
                x + w <= area.x + 1e-6
                or x >= area.x + area.width - 1e-6
                or y + d <= area.y + 1e-6
                or y >= area.y + area.depth - 1e-6
            )
        rectangles.append((placement.plate, x, y, x + w, y + d))
    for index, a in enumerate(rectangles):
        for b in rectangles[index + 1 :]:
            if a[0] != b[0]:
                continue
            assert (
                a[3] + gap <= b[1] + 1e-6
                or b[3] + gap <= a[1] + 1e-6
                or a[4] + gap <= b[2] + 1e-6
                or b[4] + gap <= a[2] + 1e-6
            ), (a, b)


@pytest.mark.parametrize(
    "cells,build,expected_count",
    [
        (4, BuildVolume(246, 246, 120), 57),
        (5, BuildVolume(325, 320, 325, margin=5), 69),
    ],
)
def test_reference_and_larger_catalogue_patterns_fit_native_limit(cells, build, expected_count):
    sizes = _catalogue_bounds(cells)
    assert len(sizes) == expected_count
    placements = pack_sizes(sizes, build, gap=2)
    _assert_placements(sizes, placements, build, 2)
    assert max(p.plate for p in placements) < 36
    assert len({p.plate for p in placements}) < len(sizes)
    assert placements == pack_sizes(sizes, build, gap=2)


def test_requested_gap_changes_exact_fit_boundary():
    sizes = [(20, 20, 13), (20, 20, 13)]
    exact = pack_sizes(sizes, BuildVolume(42, 20, 20), gap=2)
    assert exact[0].plate == exact[1].plate
    _assert_placements(sizes, exact, BuildVolume(42, 20, 20), 2)
    smaller = pack_sizes(sizes, BuildVolume(41.99, 20, 20), gap=2)
    assert smaller[0].plate != smaller[1].plate


def test_rotation_and_lower_margin_are_preserved():
    build = BuildVolume(74, 34, 20, margin=2, exclusions=(Exclusion(0, 0, 1, 1),))
    sizes = [(30, 70, 13)]
    placements = pack_sizes(sizes, build)
    assert placements[0].rotation == 90
    _assert_placements(sizes, placements, build, 2)


def test_exclusions_and_upper_reservations_are_not_used_for_packing():
    build = BuildVolume(
        120,
        100,
        30,
        margin=5,
        reserve_x=10,
        reserve_y=10,
        reserve_z=5,
        exclusions=(Exclusion(45, 5, 20, 80), Exclusion(0, 0, 2, 2)),
    )
    sizes = [(35, 30, 25)] * 6
    placements = pack_sizes(sizes, build, gap=3)
    _assert_placements(sizes, placements, build, 3)


def test_compact_fallback_fits_all_10mm_perimeter_bounds_on_one_h2d_plate():
    sizes = [
        (60, 66, 13),
        (66, 66, 13),
        (66, 60, 13),
        (60, 60, 13),
        (16, 60 + 5 * sqrt(2), 13),
        (60 + 5 * sqrt(2), 10, 13),
        (70, 70, 13),
        (10, 60 + 5 * sqrt(2), 13),
        (60 + 5 * sqrt(2), 16, 13),
        (70, 70, 13),
        *((60 * length, 16, 13) for length in range(1, 6)),
        *((60 * length, 10, 13) for length in range(1, 6)),
    ]
    build = BuildVolume(
        350,
        320,
        320,
        margin=5,
        exclusions=(Exclusion(0, 0, 30, 320), Exclusion(320, 0, 30, 320)),
    )
    placements = pack_sizes(sizes, build, gap=10)
    assert {placement.plate for placement in placements} == {0}
    _assert_placements(sizes, placements, build, 10)
    assert placements == pack_sizes(sizes, build, gap=10)

    blocked = pack_sizes([*sizes, (290, 310, 13)], build, gap=10)
    assert max(placement.plate for placement in blocked) >= 1
    _assert_placements([*sizes, (290, 310, 13)], blocked, build, 10)


def test_compact_fallback_keeps_existing_layout_when_plate_count_is_equal():
    sizes = [(60, 30, 13), (20, 20, 13), (40, 70, 13)]
    build = BuildVolume(100, 100, 30, margin=4)
    assert pack_sizes(sizes, build, gap=2) == [
        PrintPlacement(0, 46, 4, 90),
        PrintPlacement(0, 46, 66, 0),
        PrintPlacement(0, 4, 4, 0),
    ]


def test_input_identity_order_and_unpacked_mode():
    sizes = [(60, 30, 13), (20, 20, 13), (40, 70, 13)]
    build = BuildVolume(100, 100, 30, margin=4)
    placements = pack_sizes(sizes, build, pack=False)
    assert [p.plate for p in placements] == [0, 1, 2]
    _assert_placements(sizes, placements, build, 2)


@pytest.mark.parametrize(
    "size",
    [
        (0, 20, 13),
        (-1, 20, 13),
        (nan, 20, 13),
        (20, inf, 13),
        (20, 20, 0),
        (101, 101, 13),
        (20, 20, 21),
    ],
)
def test_invalid_or_unplaceable_sizes_fail(size):
    with pytest.raises(ValueError):
        pack_sizes([size], BuildVolume(100, 100, 20))


@pytest.mark.parametrize("gap", [-1, nan, inf])
def test_invalid_gap_fails(gap):
    with pytest.raises(ValueError):
        pack_sizes([(20, 20, 13)], BuildVolume(100, 100, 20), gap=gap)


def test_core_packing_retains_every_item_beyond_native_capacity():
    sizes = [(20, 20, 13)] * 37
    placements = pack_sizes(sizes, BuildVolume(20, 20, 20))
    assert len(placements) == 37
    assert {p.plate for p in placements} == set(range(37))


def test_native_capacity_error_occurs_before_writing_archive(tmp_path):
    cube = Box(20, 20, 13)
    job = Job(
        [Design(f"full_plate_{i}", cube, {}) for i in range(37)],
        BuildVolume(20, 20, 20),
        "catalogue",
    )
    settings = BambuSettings((Material("Diagnostic model", "PETG", "#778877"),), 0.4, 0.2)
    path = tmp_path / "too-many-plates.3mf"
    with pytest.raises(ValueError, match="36"):
        write_3mf(job, path, bambu=settings)
    assert not path.exists()
    core = tmp_path / "all-core-geometry.3mf"
    write_3mf(job, core)
    with ZipFile(core) as archive:
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
        assert len(model.findall("./{*}build/{*}item")) == 37


@pytest.mark.native
def test_native_packed_catalogue_preserves_every_object(tmp_path):
    executable = os.environ.get("CARGO_GRID_BAMBU")
    if not executable:
        pytest.skip("Set CARGO_GRID_BAMBU to enable actual native packed-catalogue verification")
    binary = Path(executable).resolve(strict=True)
    sizes = _catalogue_bounds(4)
    job = Job(
        [Design(f"catalogue_box_{i}", Box(*size), {}) for i, size in enumerate(sizes)],
        BuildVolume(246, 246, 120),
        "catalogue",
    )
    settings = BambuSettings((Material("Diagnostic model", "PETG", "#778877"),), 0.4, 0.2)
    source, output = tmp_path / "packed.3mf", tmp_path / "roundtrip.3mf"
    result = write_3mf(job, source, bambu=settings)
    before_settings, before_plates, before_volumes = _project_facts(source)
    assert len(before_plates) <= 36
    assert any(len(plate[2]) > 1 for plate in before_plates)
    assert sum(len(plate[2]) for plate in before_plates) == 57
    assert sum(plate["quantity"] for plate in result["plates"]) == 57
    with (tmp_path / "bambu.log").open("w") as log:
        completed = subprocess.run(
            [
                str(binary),
                "--datadir",
                str(tmp_path / "settings"),
                "--debug",
                "2",
                "--arrange",
                "0",
                "--orient",
                "0",
                "--info",
                "--export-3mf",
                str(output),
                str(source),
            ],
            cwd=tmp_path,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=180,
            check=False,
        )
    assert completed.returncode == 0, (tmp_path / "bambu.log").read_text()
    after_settings, after_plates, after_volumes = _project_facts(output)
    assert after_plates == before_plates
    assert after_settings["printable_area"] == before_settings["printable_area"]
    assert Counter(v[:-1] for v in after_volumes) == Counter(v[:-1] for v in before_volumes)
    for before, after in zip(before_volumes, after_volumes):
        assert after[-1] == pytest.approx(before[-1], abs=0.001)
