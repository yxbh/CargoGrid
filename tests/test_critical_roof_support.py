"""Critical masks reduce requested coverage without pretending masks equal adhesion."""

import json
from dataclasses import asdict, replace
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from cargo_grid import BuildVolume, Tile
from cargo_grid.cli import main, parser
from cargo_grid.export import BambuSettings, Material, write_3mf
from cargo_grid.jobs import Job, tile_design
from cargo_grid.roof_support import RoofSupportSettings, roof_enforcers


def test_critical_is_preferred_but_full_remains_explicit():
    settings = RoofSupportSettings(0.2, 2, 0, (2, 1))
    assert settings.coverage == "critical"
    full = replace(settings, coverage="full")
    assert full.native_settings() == settings.native_settings()
    assert asdict(full)["coverage"] == "full"
    with pytest.raises(ValueError, match="coverage"):
        replace(settings, coverage=None)


@pytest.mark.parametrize("nx,ny", [(1, 1), (2, 3), (4, 4)])
def test_actual_critical_mask_clipping_and_unmodified_model(nx, ny):
    design = tile_design(Tile(nx, ny, hole_diameter=10, hole_scope="full"))
    before = design.shape.volume
    full = roof_enforcers(design, 0.2, "full")
    critical = roof_enforcers(design, 0.2, "critical")
    assert len(full) == ny + 2 * nx and len(critical) == 2 * (nx + ny)
    assert sum(v.shape.volume for v in critical) < 0.4 * sum(v.shape.volume for v in full)
    for index, volume in enumerate(critical):
        assert volume.subtype == "support_enforcer"
        assert volume.shape.is_valid and len(volume.shape.solids()) == 1
        box = volume.shape.bounding_box()
        center = 30 + 60 * (volume.roof_index - 1)
        expected_along = (center - 9, center - 6) if index % 2 == 0 else (center + 6, center + 9)
        along = (box.min.Y, box.max.Y) if volume.roof_side == "west" else (box.min.X, box.max.X)
        assert along == pytest.approx(expected_along, abs=1e-5)
        assert (box.min.Z, box.max.Z) == pytest.approx((9.2, 11.2))
        if volume.roof_side == "west":
            assert box.min.X >= 1 - 1e-5 and box.max.X <= 5.1 + 1e-5
        else:
            assert box.min.Y >= 1 - 1e-5 and box.max.Y <= 5 + 1e-5
        remainder = volume.shape
        for whole in full:
            if (whole.roof_side, whole.roof_index) == (volume.roof_side, volume.roof_index):
                remainder = remainder.cut(whole.shape)
                if remainder is None:
                    break
        assert remainder is None or remainder.volume < 1e-5
    assert design.shape.volume == before


def test_critical_tracks_custom_grid_and_leaves_terminated_edge_empty():
    from cargo_grid import Interface

    spec = Tile(1, 2, Interface(pitch=65), hole_diameter=10, hole_scope="full")
    masks = roof_enforcers(tile_design(spec), 0.2)
    for index, mask in enumerate(masks):
        box = mask.shape.bounding_box()
        along = box.min.Y if mask.roof_side == "west" else box.min.X
        center = 32.5 + 65 * (mask.roof_index - 1)
        assert along == pytest.approx(center - 9 if index % 2 == 0 else center + 6)
    remaining = roof_enforcers(tile_design(replace(spec, west=False)), 0.2)
    assert len(remaining) == 2 and {v.roof_side for v in remaining} == {"south"}
    assert roof_enforcers(tile_design(replace(spec, west=False, south=False)), 0.2) == []


@pytest.mark.parametrize("coverage", ["critical", "full"])
def test_roof_masks_span_coarser_profile_layer_planes_without_changing_xy(coverage):
    design = tile_design(Tile(2, 1, hole_diameter=10, hole_scope="full"))
    fine = roof_enforcers(design, 0.2, coverage)
    coarse = roof_enforcers(design, 0.4, coverage)
    assert len(fine) == len(coarse)
    for a, b in zip(fine, coarse):
        assert a.name == b.name and a.shape.volume == pytest.approx(b.shape.volume)
        box_a, box_b = a.shape.bounding_box(), b.shape.bounding_box()
        assert tuple(box_a.min) == pytest.approx(tuple(box_b.min))
        assert tuple(box_a.max) == pytest.approx(tuple(box_b.max))
        assert box_a.min.Z < 10.2 < 10.6 < box_a.max.Z


@pytest.mark.parametrize("coverage,count", [("critical", 6), ("full", 4)])
def test_exported_masks_and_manifest_distinguish_coverages(tmp_path, coverage, count):
    design = tile_design(Tile(1, 2, hole_diameter=10, hole_scope="full"))
    roof = RoofSupportSettings(0.2, 2, 0, (2, 1), coverage)
    bambu = BambuSettings(
        (Material("PETG", "PETG", "#778877"), Material("PLA", "PLA", "#DDDDDD")), 0.4, 0.2, roof
    )
    path = tmp_path / f"{coverage}.3mf"
    result = write_3mf(Job([design], BuildVolume(150, 150, 50), "part"), path, bambu=bambu)
    assert result["roof_support"]["settings"]["coverage"] == coverage
    assert result["roof_support"]["enforcer_count"] == count
    assert sum(t["roof_count"] for t in result["roof_support"]["targets"]) == 3
    assert result["roof_support"]["critical_coverage_experimental"] == (coverage == "critical")
    with ZipFile(path) as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        assert (
            len(
                [
                    p
                    for p in config.findall("./object/part")
                    if p.get("subtype") == "support_enforcer"
                ]
            )
            == count
        )
        assert (
            len([p for p in config.findall("./object/part") if p.get("subtype") == "normal_part"])
            == 1
        )


def test_coverage_without_support_is_not_silently_ignored(tmp_path):
    for coverage in ("critical", "full"):
        with pytest.raises(SystemExit) as caught:
            main(
                [
                    "part",
                    "--build-width-mm",
                    "150",
                    "--build-depth-mm",
                    "150",
                    "--build-height-mm",
                    "50",
                    "--output",
                    str(tmp_path / coverage),
                    "--roof-coverage",
                    coverage,
                ]
            )
        assert caught.value.code == 2 and not (tmp_path / coverage).exists()


@pytest.mark.parametrize("nozzle_slots", [None, (2, 1)])
def test_cli_implicit_critical_and_explicit_full(tmp_path, nozzle_slots):
    base = [
        "part",
        "--build-width-mm",
        "150",
        "--build-depth-mm",
        "150",
        "--build-height-mm",
        "50",
        "--no-stl",
        "--bambu",
        "--material",
        "PETG",
        "PETG",
        "#778877",
        "--material",
        "PLA",
        "PLA",
        "#DDDDDD",
        "--nozzle-diameter-mm",
        ".4",
        "--layer-height-mm",
        ".2",
        "--roof-support",
        "--roof-top-gap-mm",
        ".2",
        "--roof-interface-layer-count",
        "2",
        "--roof-interface-spacing-mm",
        "0",
    ]
    if nozzle_slots:
        base += ["--roof-nozzle-slots", *map(str, nozzle_slots)]
    for coverage in ("critical", "full"):
        command = base + ["--output", str(tmp_path / coverage)]
        if coverage == "full":
            command += ["--roof-coverage", "full", "--roof-foot-expansion-mm", "0"]
        assert main(command) == 0
        report = json.loads((tmp_path / coverage / "manifest.json").read_text())
        assert report["export"]["roof_support"]["settings"]["coverage"] == coverage
        assert report["export"]["roof_support"]["settings"]["nozzle_slots"] == (
            list(nozzle_slots) if nozzle_slots else None
        )
        assert report["export"]["roof_support"]["settings"]["foot_expansion"] == (
            0 if coverage == "full" else None
        )
    assert (
        parser()
        .parse_args(
            [
                "part",
                "--build-width-mm",
                "150",
                "--build-depth-mm",
                "150",
                "--build-height-mm",
                "50",
                "--output",
                "ignored",
            ]
        )
        .roof_support
        is False
    )
