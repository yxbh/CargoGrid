"""Native roof support requests, not a claim of physical support release."""

import json
import os
import subprocess
from dataclasses import replace
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from build123d import Vector

from cargo_grid import BuildVolume, Interface, Tile, make_tile
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, export_job, write_3mf
from cargo_grid.jobs import Job, layout_job, tile_design
from cargo_grid.layout import exact_layout
from cargo_grid.roof_support import RoofSupportSettings, roof_enforcers, validate_roof_job
from cargo_grid.stacking import StackSettings
from cargo_grid.tiles import hole_placements


@pytest.fixture
def support():
    return RoofSupportSettings(0.2, 2, 0, (2, 1), coverage="full")


@pytest.fixture
def bambu(support):
    return BambuSettings(
        (Material("PETG", "PETG", "#778877"), Material("PLA", "PLA", "#DDDDDD")), 0.4, 0.2, support
    )


@pytest.mark.parametrize("nx,ny,expected", [(2, 3, 29), (4, 4, 65)])
def test_full_scope_exact_lattice_and_bores(nx, ny, expected):
    spec = Tile(nx, ny, hole_diameter=10, hole_scope="full")
    holes = hole_placements(spec)
    assert len(holes) == expected and all(h.accepted for h in holes)
    shape = make_tile(spec)
    assert shape.is_valid and len(shape.solids()) == 1
    assert tuple(shape.bounding_box().size) == pytest.approx(
        (nx * 60 + 6, ny * 60 + 6, 13), abs=1e-5
    )
    for hole in holes:
        for z in (1.5, 6.5, 11.5):
            assert not shape.is_inside(Vector(hole.x, hole.y, z))
    expected_volume = 146696.4078426918 if nx == 2 else 391727.15419238823
    assert shape.volume == pytest.approx(expected_volume, abs=0.002)


def test_full_default_interior_override_and_terminated_fillers_are_preserved():
    assert Tile().hole_scope == "full" and Tile().hole_diameter == 10
    assert len(hole_placements(Tile(2, 3, hole_diameter=10, hole_scope="interior"))) == 9
    spec = Tile(2, 3, hole_diameter=10, hole_scope="full", west=False, filler_west=3)
    holes = hole_placements(spec)
    assert all(not h.accepted and "terminated" in h.reason for h in holes if h.x == 0)
    shape = make_tile(spec)
    assert shape.is_valid
    assert tuple(shape.bounding_box().size) == pytest.approx((129, 186, 13), abs=1e-5)
    volumes = roof_enforcers(tile_design(spec), 0.2)
    assert len(volumes) == 4 and {v.roof_side for v in volumes} == {"south"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_gap": -1},
        {"top_gap": float("nan")},
        {"interface_layers": 0},
        {"interface_layers": True},
        {"interface_spacing": float("inf")},
        {"nozzle_map": (1, 1)},
        {"nozzle_map": (1, 3)},
        {"nozzle_map": (1,)},
        {"coverage": "unknown"},
        {"foot_expansion": -0.5},
        {"foot_expansion": float("inf")},
        {"foot_expansion": float("nan")},
    ],
)
def test_invalid_support_parameters(support, kwargs):
    with pytest.raises(ValueError):
        replace(support, **kwargs)


def test_material_roles_must_be_explicit_petg_and_pla(support):
    for materials in (
        (Material("PETG", "PETG", "#ffffff"),),
        (Material("PLA", "PLA", "#ffffff"), Material("PETG", "PETG", "#ffffff")),
    ):
        with pytest.raises(ValueError, match="PETG"):
            BambuSettings(materials, 0.4, 0.2, support)


def test_automatic_support_defaults_omit_custom_assignments(tmp_path):
    support = RoofSupportSettings(0.2, 2, 0)
    assert support.nozzle_map is None
    assert support.native_settings()["support_on_build_plate_only"] == "0"
    assert "filament_map" not in support.native_settings()
    assert "filament_map_mode" not in support.native_settings()
    assert "raft_first_layer_expansion" not in support.native_settings()
    bambu = BambuSettings(
        (Material("PETG", "PETG", "#778877"), Material("PLA", "PLA", "#DDDDDD")),
        0.4,
        0.2,
        support,
    )
    path = tmp_path / "auto.3mf"
    report = write_3mf(
        Job([tile_design(Tile())], BuildVolume(150, 150, 50), "part"), path, bambu=bambu
    )
    assert report["roof_support"]["settings"]["nozzle_map"] is None
    with ZipFile(path) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert settings["support_filament"] == "1" and settings["support_interface_filament"] == "2"
        assert settings["support_on_build_plate_only"] == "0"
        assert "filament_map" not in settings
        assert settings["filament_map_mode"] == "Auto For Match"
        assert "raft_first_layer_expansion" not in settings
        assert not {
            "filament_map",
            "filament_map_mode",
            "support_on_build_plate_only",
            "raft_first_layer_expansion",
        } & set(settings["different_settings_to_system"][0].split(";"))
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        metadata = {m.get("key") for m in config.findall("./plate/metadata")}
        assert not {"filament_maps", "filament_volume_maps"} & metadata
        assert (
            next(
                m.get("value")
                for m in config.findall("./plate/metadata")
                if m.get("key") == "filament_map_mode"
            )
            == "Auto For Match"
        )


@pytest.mark.parametrize("expansion", [None, -1, 0, 1.5])
def test_support_foot_defaults_and_explicit_override(expansion):
    settings = RoofSupportSettings(0.2, 2, 0, foot_expansion=expansion).native_settings()
    if expansion is None or expansion == -1:
        assert "raft_first_layer_expansion" not in settings
    else:
        assert settings["raft_first_layer_expansion"] == f"{expansion:g}"


def test_enforcers_are_actual_roof_footprints_and_nonprinting(support):
    design = tile_design(Tile(2, 3, hole_diameter=10, hole_scope="full"))
    before = design.shape.volume
    job = Job([design], BuildVolume(200, 220, 50), "part")
    validate_roof_job(job, support, 0.2)
    volumes = roof_enforcers(design, 0.2, coverage="full")
    assert len(volumes) == 7
    assert {(v.roof_side, v.roof_index) for v in volumes} == {
        ("west", 1),
        ("west", 2),
        ("west", 3),
        ("south", 1),
        ("south", 2),
    }
    for volume in volumes:
        box = volume.shape.bounding_box()
        assert volume.subtype == "support_enforcer" and volume.role == "roof-enforcer"
        assert volume.shape.is_valid and len(volume.shape.solids()) == 1
        assert box.min.Z == pytest.approx(9.2)
        assert box.max.Z == pytest.approx(11.2)
        center = 30 + 60 * (volume.roof_index - 1)
        if volume.roof_side == "west":
            assert box.min.X >= 1 - 1e-5 and box.max.X <= 5.1 + 1e-5
            assert (box.min.Y + box.max.Y) / 2 == pytest.approx(center)
            cut_center = Vector(3, center, 10.2)
        else:
            assert box.min.Y >= 1 - 1e-5 and box.max.Y <= 5 + 1e-5
            assert center - 16 < box.min.X < box.max.X < center + 16
            cut_center = Vector(center, 3, 10.2)
        assert not volume.shape.is_inside(cut_center)
    assert design.shape.volume == before


def test_native_modifier_types_rotation_and_dual_nozzle_settings(bambu, tmp_path):
    design = tile_design(Tile(1, 2, hole_diameter=10, hole_scope="full"))
    job = Job([design], BuildVolume(170, 100, 50, margin=10), "part")
    path = tmp_path / "support.3mf"
    report = write_3mf(job, path, bambu=bambu)
    assert report["roof_support"]["enforcer_count"] == 4
    assert {(t["side"], t["roof_count"]) for t in report["roof_support"]["targets"]} == {
        ("west", 2),
        ("south", 1),
    }
    assert report["plates"][0]["print_rotation"] == 90
    assert sum(v["printed_part"] for v in report["plates"][0]["volumes"]) == 1
    with ZipFile(path) as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        parts = config.findall("./object/part")
        assert [p.get("subtype") for p in parts].count("normal_part") == 1
        assert [p.get("subtype") for p in parts].count("support_enforcer") == 4
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert settings["nozzle_diameter"] == ["0.4", "0.4"]
        assert settings["filament_map"] == ["2", "1"]
        assert settings["support_filament"] == "1" and settings["support_interface_filament"] == "2"
        assert settings["support_type"] == "normal(manual)"
        assert "raft_first_layer_expansion" not in settings
        assert len(settings["different_settings_to_system"]) == 4
        overrides = set(settings["different_settings_to_system"][0].split(";"))
        assert {"enable_support", "support_type", "support_interface_filament"} <= overrides
        plate_metadata = {m.get("key"): m.get("value") for m in config.findall("./plate/metadata")}
        assert plate_metadata["filament_map_mode"] == "Manual"
        assert plate_metadata["filament_maps"] == "2 1"
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
        objects = {o.get("id"): o for o in model.findall("./{*}resources/{*}object")}
        for part in parts:
            if part.get("subtype") != "support_enforcer":
                continue
            points = objects[part.get("id")].findall("./{*}mesh/{*}vertices/{*}vertex")
            xs = [float(v.get("x")) for v in points]
            ys = [float(v.get("y")) for v in points]
            assert min(xs) < 0  # original Y coordinates rotated into negative X
            name = next(m.get("value") for m in part.findall("metadata") if m.get("key") == "name")
            if "_west_roof_" in name:
                assert min(ys) >= 1 - 1e-5 and max(ys) <= 5.1 + 1e-5
            else:
                assert min(xs) >= -5 - 1e-5 and max(xs) <= -1 + 1e-5
                assert min(ys) > 14 and max(ys) < 46


@pytest.mark.parametrize("nozzle_map", [None, (2, 1)])
def test_original_only_and_stacking_rejected_before_export(bambu, tmp_path, nozzle_map):
    bambu = replace(bambu, roof_support=replace(bambu.roof_support, nozzle_map=nozzle_map))
    design = tile_design(Tile())
    job = Job([design], BuildVolume(150, 150, 50), "part")
    for function, path in ((write_3mf, tmp_path / "bad.3mf"), (export_job, tmp_path / "bad-job")):
        with pytest.raises(ValueError, match="stacked"):
            function(job, path, bambu=bambu, stack=StackSettings(2, 1, 0.2, 1, 1, 2))
        assert not path.exists()
    full = tile_design(Tile(interface=Interface(joint_style="full-height")))
    with pytest.raises(ValueError, match="original"):
        write_3mf(Job([full], job.build, "part"), tmp_path / "full.3mf", bambu=bambu)
    assert not (tmp_path / "full.3mf").exists()


def test_layout_skips_terminated_west_edge_and_reports_untargeted_tile(bambu, tmp_path):
    build = BuildVolume(150, 150, 50)
    layout = exact_layout(240, 120, build, hole_diameter=10, hole_scope="full")
    job = layout_job(layout, build)
    before = [d.shape.volume for d in job.designs]
    result = write_3mf(job, tmp_path / "layout.3mf", bambu=bambu)
    assert result["roof_support"]["enforcer_count"] == 2
    assert len(result["roof_support"]["untargeted_designs"]) == 1
    assert [d.shape.volume for d in job.designs] == before


@pytest.mark.parametrize("west,south,edges", [(False, True, {"south"}), (True, False, {"west"})])
@pytest.mark.parametrize("coverage", ["critical", "full"])
def test_either_retained_female_edge_is_supported(west, south, edges, coverage, support):
    design = tile_design(Tile(2, 1, west=west, south=south, hole_diameter=10, hole_scope="full"))
    validate_roof_job(Job([design], BuildVolume(150, 150, 50), "part"), support, 0.2)
    assert {v.roof_side for v in roof_enforcers(design, 0.2, coverage)} == edges


def test_only_male_edges_are_not_roof_support_targets(support):
    design = tile_design(Tile(west=False, south=False))
    assert roof_enforcers(design, 0.2) == []
    with pytest.raises(ValueError, match="no retained original female pocket roofs"):
        validate_roof_job(Job([design], BuildVolume(150, 150, 50), "part"), support, 0.2)


def test_non_tile_job_is_rejected_before_creating_output(bambu, tmp_path):
    from cargo_grid.accessories import Accessory
    from cargo_grid.catalogue import accessory_design

    job = Job([accessory_design(Accessory("edge-x"))], BuildVolume(150, 150, 50), "catalogue")
    with pytest.raises(ValueError, match="catalogues and extras are not supported"):
        export_job(job, tmp_path / "unsupported", bambu=bambu)
    assert not (tmp_path / "unsupported").exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--roof-support"],
        ["--roof-top-gap-mm", ".2"],
        ["--roof-nozzle-slots", "2", "1"],
        ["--roof-foot-expansion-mm", "0"],
        [
            "--roof-support",
            "--roof-top-gap-mm",
            ".2",
            "--roof-interface-layer-count",
            "2",
            "--roof-interface-spacing-mm",
            "0",
            "--roof-nozzle-slots",
            "2",
            "1",
        ],
    ],
)
def test_incomplete_cli_support_or_scope_fails_early(tmp_path, extra):
    output = tmp_path / "new"
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
                str(output),
                *extra,
            ]
        )
    assert caught.value.code == 2 and not output.exists()


@pytest.mark.parametrize("nozzle_map", [None, (2, 1)])
@pytest.mark.native
def test_native_bambu_roundtrip_keeps_nonprinting_roof_enforcers(bambu, tmp_path, nozzle_map):
    executable = os.environ.get("CARGO_GRID_BAMBU")
    if not executable:
        pytest.skip("CARGO_GRID_BAMBU not supplied; actual native importer not run")
    bambu = replace(bambu, roof_support=replace(bambu.roof_support, nozzle_map=nozzle_map))
    design = tile_design(Tile(1, 1, hole_diameter=10, hole_scope="full"))
    write_3mf(Job([design], BuildVolume(150, 150, 50), "part"), tmp_path / "input.3mf", bambu=bambu)
    result = subprocess.run(
        [
            executable,
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
            str(tmp_path / "roundtrip.3mf"),
            str(tmp_path / "input.3mf"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with ZipFile(tmp_path / "roundtrip.3mf") as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        types = [p.get("subtype") for p in config.findall("./object/part")]
        assert types.count("normal_part") == 1 and types.count("support_enforcer") == 3
        metadata = {m.get("key"): m.get("value") for m in config.findall("./plate/metadata")}
        assert metadata["filament_map_mode"] == ("Manual" if nozzle_map else "Auto For Match")
        if nozzle_map:
            assert metadata["filament_maps"] == "2 1"
