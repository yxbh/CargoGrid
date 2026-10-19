"""Audited printer envelopes and profile pairs, without inferring printed fit."""

import json
import os
from dataclasses import replace
from math import isfinite
from pathlib import Path
from subprocess import run
from zipfile import ZipFile

import pytest
from build123d import Box

from cargo_grid.export import (
    BambuSettings,
    Material,
    _bambu_project_settings,
    _validate_request,
    write_3mf,
)
from cargo_grid.jobs import Design, Job, tile_design
from cargo_grid.packing import PrintPlacement
from cargo_grid.parameters import BuildVolume, Exclusion, Tile
from cargo_grid.plates import dual_nozzle_tower_reservation
from cargo_grid.printers import H2D, PRINTERS, X2D, bambu_flush_settings, printer_for_common_build
from cargo_grid.roof_support import RoofSupportSettings


@pytest.mark.parametrize("filaments", [1, 2, 3])
@pytest.mark.parametrize("nozzles", [1, 2])
def test_flush_defaults_have_complete_finite_data_for_every_nozzle(filaments, nozzles):
    settings = bambu_flush_settings(filaments, nozzles)
    matrix = list(map(float, settings["flush_volumes_matrix"]))
    assert len(matrix) == nozzles * filaments**2
    assert matrix == [
        0 if row == column else 280
        for _ in range(nozzles)
        for row in range(filaments)
        for column in range(filaments)
    ]
    assert settings["flush_volumes_vector"] == ["140"] * (filaments * 2)
    assert settings["flush_multiplier"] == ["1"] * nozzles
    assert settings["flush_multiplier_fast"] == ["1.2"] * nozzles
    assert all(isfinite(float(value)) for values in settings.values() for value in values)


@pytest.mark.parametrize("value", [0, -1, 2.5, True])
@pytest.mark.parametrize("key", ["filament_count", "nozzle_count"])
def test_flush_defaults_reject_invalid_dimensions(key, value):
    with pytest.raises(ValueError, match="positive integer"):
        bambu_flush_settings(**({"filament_count": 2, "nozzle_count": 2} | {key: value}))


@pytest.mark.parametrize("printer", [H2D, X2D, None], ids=["h2d", "x2d", "generic"])
def test_dual_nozzle_project_initializes_purge_tables_without_mapping_caches(printer):
    settings = BambuSettings(
        (Material("PETG", "PETG", "#688197"), Material("PLA", "PLA", "#0A2989")),
        0.8,
        0.32,
        printer=printer,
        machine_nozzle_count=2 if printer is None else 1,
    )
    job = Job([Design("box", Box(10, 10, 2), {})], BuildVolume(256, 256, 256), "part")
    project = _bambu_project_settings(job, settings)
    expected = bambu_flush_settings(2, 2)
    assert {key: project[key] for key in expected} == expected
    assert len(project["nozzle_diameter"]) == 2
    assert project["filament_map_mode"] == "Auto For Match"
    assert (
        not {
            "filament_map",
            "filament_map_2",
            "filament_nozzle_map",
            "filament_volume_map",
            "extruder_nozzle_stats",
        }
        & project.keys()
    )
    assert not set(expected) & set(project["different_settings_to_system"][0].split(";"))


@pytest.mark.native
@pytest.mark.parametrize("printer", PRINTERS.values(), ids=PRINTERS)
@pytest.mark.parametrize("nozzle", [0.8, 0.4])
def test_native_direct_load_preserves_complete_dual_nozzle_purge_tables(printer, nozzle, tmp_path):
    executable = os.environ.get("CARGO_GRID_BAMBU")
    if not executable:
        pytest.skip("Set CARGO_GRID_BAMBU for direct native purge-table loading")
    profile = printer.profiles[nozzle]
    settings = BambuSettings(
        (Material(profile.petg, "PETG", "#688197"), Material(profile.pla, "PLA", "#0A2989")),
        nozzle,
        profile.layer_height_mm,
        printer=printer,
        printer_settings_id=profile.printer,
        print_settings_id=profile.process,
    )
    job = Job([Design("box", Box(10, 10, 2), {})], printer.build, "part")
    project = tmp_path / "direct-load.3mf"
    write_3mf(job, project, bambu=settings)
    exported = tmp_path / "resolved.json"
    arguments = [
        str(Path(executable).resolve()),
        "--datadir",
        str(tmp_path / "datadir"),
        "--debug",
        "1",
        "--arrange",
        "0",
        "--orient",
        "0",
        "--export-settings",
        str(exported),
        str(project),
    ]
    with (tmp_path / "native.log").open("w") as log:
        result = run(arguments, stdout=log, stderr=log)
    assert result.returncode == 0, (tmp_path / "native.log").read_text()
    restored = json.loads(exported.read_text())
    for key, expected in bambu_flush_settings(2, 2).items():
        assert restored[key] == expected
        assert all(isfinite(float(value)) for value in restored[key])
    assert restored["filament_map_mode"] == "Auto For Match"


def test_h2d_shared_build_keeps_the_existing_envelope():
    assert H2D.build == BuildVolume(350, 320, 325)
    assert H2D.common_build() == BuildVolume(
        350,
        320,
        320,
        margin=5,
        exclusions=(Exclusion(0, 0, 30, 320), Exclusion(320, 0, 30, 320)),
    )
    assert H2D.placement_policy() == {
        "name": "H2D dual-nozzle safe",
        "common_reach_mm": {"min_x": 25, "max_x": 325, "min_y": 0, "max_y": 320, "max_z": 320},
        "common_model_inset_mm": 5,
    }


def test_x2d_models_use_the_common_reach_not_the_whole_256mm_bed():
    assert X2D.build == BuildVolume(256, 256, 261)
    build = X2D.common_build()
    assert build.contains_box(25.5, 5, 225.5, 246, 256)
    assert not build.contains_box(25.49, 5, 225.5, 246, 256)
    assert not build.contains_box(25.5, 4.99, 225.5, 246, 256)
    assert not build.contains_box(25.5, 5, 225.51, 246, 256)
    assert not build.contains_box(25.5, 5, 225.5, 246.01, 256)
    assert not build.contains_box(25.5, 5, 225.5, 246, 256.01)
    assert build.placement((186, 246, 13)) is not None
    assert build.placement((246, 246, 13)) is None
    assert build.placement((66, 306, 13)) is None


@pytest.mark.parametrize("printer", PRINTERS.values(), ids=PRINTERS)
def test_supported_profile_pairs_and_build_lookup(printer):
    assert {nozzle: p.layer_height_mm for nozzle, p in printer.profiles.items()} == {
        0.8: 0.32,
        0.4: 0.24,
    }
    assert printer_for_common_build(printer.common_build()) is printer
    assert all(p.printer.startswith(printer.model) for p in printer.profiles.values())
    assert printer_for_common_build(printer.build) is None


def test_profile_names_keep_printer_specific_bambu_spelling():
    assert H2D.profiles[0.4].pla == "Bambu PLA Basic @BBL H2D"
    assert X2D.profiles[0.4].pla == "Bambu PLA Basic @BBL X2D 0.4 nozzle"
    assert X2D.profiles[0.8].process == "0.32mm Balanced Quality @BBL X2D 0.8 nozzle"
    assert X2D.profiles[0.4].process == "0.24mm Standard @BBL X2D"
    assert H2D.tower_prime_volume_mm3 == 30
    assert X2D.tower_prime_volume_mm3 == 45
    assert [(n.name, n.extruder_type, n.x_mm, n.height_mm, n.is_master) for n in H2D.nozzles] == [
        ("left", "Direct Drive", (0, 325), 320, False),
        ("right", "Direct Drive", (25, 350), 325, True),
    ]
    assert [(n.name, n.extruder_type, n.x_mm, n.height_mm, n.is_master) for n in X2D.nozzles] == [
        ("main", "Direct Drive", (0, 256), 261, True),
        ("auxiliary", "Bowden", (20.5, 256), 256, False),
    ]
    assert H2D.shared_x_mm == (25, 325) and H2D.shared_height_mm == 320
    assert X2D.shared_x_mm == (20.5, 256) and X2D.shared_height_mm == 256


def test_shared_reach_is_derived_from_nozzle_records_without_parallel_aliases():
    auxiliary = replace(X2D.nozzles[1], x_mm=(24, 250), height_mm=250)
    printer = replace(X2D, nozzles=(X2D.nozzles[0], auxiliary))
    assert printer.shared_x_mm == (24, 250) and printer.shared_height_mm == 250
    for name in (
        "common_x_mm",
        "common_height_mm",
        "extruder_types",
        "nozzle_names",
        "master_extruder_id",
    ):
        assert not hasattr(printer, name)
    with pytest.raises(ValueError, match="exactly one master"):
        replace(printer, nozzles=(replace(printer.nozzles[0], is_master=False), auxiliary))


@pytest.mark.parametrize(
    "printer", [*PRINTERS.values(), replace(X2D, name="Test")], ids=(*PRINTERS, "unregistered")
)
def test_tower_manifest_names_the_selected_printer(printer, tmp_path):
    profile = printer.profiles[0.8]
    tower = dual_nozzle_tower_reservation(printer, 0.32, 10)
    job = Job(
        [Design("box", Box(10, 10, 10), {})],
        printer.build,
        "part",
        print_placements=[PrintPlacement(0, 30, 80, 0)],
        prime_tower=tower.reach,
        prime_tower_positions={0: tower.origin},
        prime_tower_clearances={0: tower.clearance},
    )
    settings = BambuSettings(
        (Material(profile.petg, "PETG", "#637B70"), Material(profile.pla, "PLA", "#DDDDDD")),
        0.8,
        0.32,
        printer_settings_id=profile.printer,
        print_settings_id=profile.process,
        machine_nozzle_count=2,
        printer=printer,
    )
    path = tmp_path / "printer.3mf"
    result = write_3mf(job, path, bambu=settings)
    assert f"On the {printer.name} " in result["prime_tower"]["note"]
    with ZipFile(path) as archive:
        project = json.loads(archive.read("Metadata/project_settings.config"))
    assert project["extruder_type"] == [nozzle.extruder_type for nozzle in printer.nozzles]


def test_x2d_catalogue_rectangle_is_a_process_override_without_changing_support_or_prime_volume():
    job = Job(
        [Design("test", Box(20, 20, 10), {})], X2D.build, "catalogue", prime_tower_width_mm=18
    )
    settings = BambuSettings(
        (Material("PETG", "PETG", "#688197"), Material("PLA", "PLA", "#0A2989")),
        0.4,
        0.24,
        roof_support=RoofSupportSettings(mode="auto"),
        printer=X2D,
        machine_nozzle_count=2,
    )
    project = _bambu_project_settings(job, settings)
    assert project["prime_tower_width"] == "18" and project["prime_tower_rib_wall"] == "0"
    changed = project["different_settings_to_system"][0].split(";")
    assert {"prime_tower_width", "prime_tower_rib_wall"} <= set(changed)
    assert (
        not {
            "support_threshold_angle",
            "raft_first_layer_expansion",
            "filament_prime_volume",
            "prime_tower_brim_width",
            "wipe_tower_rotation_angle",
        }
        & project.keys()
    )


def test_printer_catalogue_policies_are_data_not_singleton_identity():
    options = {
        "auto_roof_support": True,
        "solid_bottom_mm": 0.0,
        "layer_height_mm": 0.32,
        "hole_diameter": 10.0,
        "hole_scope": "full",
    }
    assert H2D.catalogue_support_tuning(**options) is None
    printer = replace(X2D, name="Test")
    assert printer.diagonal_fallback and printer.omit_unfit
    assert printer.catalogue_support_tuning(**options) == X2D.catalogue_support
    assert "Test common reach" in printer.common_fit_reason()
    assert not H2D.diagonal_fallback and not H2D.omit_unfit
    assert "make it with part" in H2D.common_fit_reason()


def test_rectangular_export_validation_follows_printer_capability():
    job = Job([tile_design(Tile(1, 1))], X2D.build, "catalogue", prime_tower_width_mm=18)
    settings = BambuSettings(
        (Material("PETG", "PETG", "#688197"), Material("PLA", "PLA", "#0A2989")),
        0.8,
        0.32,
        roof_support=RoofSupportSettings(mode="auto"),
        printer=replace(X2D, name="Test"),
        machine_nozzle_count=2,
    )
    _validate_request(job, settings, None)
    for printer in (H2D, replace(X2D, catalogue_support=None), None):
        with pytest.raises(ValueError, match="rectangular prime towers"):
            _validate_request(job, replace(settings, printer=printer), None)


def test_bambu_settings_rejects_a_printer_model_string():
    with pytest.raises(ValueError, match="printer must be DualNozzlePrinter"):
        BambuSettings((Material("PETG", "PETG", "#688197"),), 0.8, 0.32, printer=X2D.model)
