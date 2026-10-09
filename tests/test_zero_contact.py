"""Intentional PETG/PLA contact is scoped, dense, synchronized and persistent."""

import json
import os
import subprocess
from dataclasses import replace
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from cargo_grid import BuildVolume, Tile
from cargo_grid.cli import H2D_PROFILES, main
from cargo_grid.export import BambuSettings, Material, write_3mf
from cargo_grid.jobs import Job, tile_design
from cargo_grid.roof_support import (
    BAMBU_SUPPORT_DEFAULT_PROCESSES,
    BAMBU_SUPPORT_DEFAULTS,
    RoofSupportSettings,
)

CONTACT = {
    "support_top_z_distance": "0",
    "independent_support_layer_height": "0",
    "support_interface_spacing": "0",
}
INHERITED_SUPPORT = {
    "support_filament",
    "support_interface_top_layers",
    "support_on_build_plate_only",
}


def test_default_contact_overrides_only_changed_settings():
    settings = RoofSupportSettings()
    native = settings.native_settings()
    assert settings.contact_mode == "zero-contact"
    assert {key: native[key] for key in CONTACT} == CONTACT
    assert set(CONTACT) <= settings.process_override_keys
    assert native["support_object_xy_distance"] == "0.4"
    assert "support_object_xy_distance" in settings.process_override_keys
    assert native["support_interface_filament"] == "2"
    assert INHERITED_SUPPORT.isdisjoint(native)
    assert INHERITED_SUPPORT.isdisjoint(settings.process_override_keys)
    assert (
        not {
            "support_expansion",
            "support_threshold_angle",
            "bridge_no_support",
            "support_speed",
            "enable_prime_tower",
            "flush_into_support",
            "filament_map",
            "filament_map_mode",
            "raft_first_layer_expansion",
        }
        & native.keys()
    )


@pytest.mark.parametrize("changes", [{"interface_layers": 1}, {"interface_spacing": 0.2}])
def test_zero_contact_rejects_insufficient_or_sparse_interface(changes):
    with pytest.raises(ValueError, match="at least two dense"):
        replace(RoofSupportSettings(), **changes)
    assert (
        RoofSupportSettings(interface_layers=3).native_settings()["support_interface_top_layers"]
        == "3"
    )


def test_explicit_gapped_mode_does_not_force_synchronized_contact():
    settings = RoofSupportSettings(0.2, 1, 0.5)
    assert settings.contact_mode == "gapped"
    assert "support_top_z_distance" not in settings.native_settings()
    assert "support_object_xy_distance" not in settings.native_settings()
    assert "support_expansion" not in settings.native_settings()
    assert "support_interface_spacing" not in settings.native_settings()
    assert "independent_support_layer_height" not in settings.native_settings()


@pytest.mark.parametrize("mode", ["painted", "auto"])
@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, {}),
        ({"interface_layers": 3}, {"support_interface_top_layers": "3"}),
        (
            {"top_gap": 0.3, "interface_layers": 1, "interface_spacing": 0.6},
            {
                "support_top_z_distance": "0.3",
                "support_interface_top_layers": "1",
                "support_interface_spacing": "0.6",
            },
        ),
        ({"foot_expansion": 0}, {"raft_first_layer_expansion": "0"}),
    ],
)
def test_default_filtering_preserves_requested_nondefault_values(mode, changes, expected):
    settings = RoofSupportSettings(mode=mode, **changes)
    native = settings.native_settings()
    assert {key: native[key] for key in expected} == expected
    assert expected.keys() <= settings.process_override_keys
    assert all(BAMBU_SUPPORT_DEFAULTS.get(key) != value for key, value in native.items())
    assert "support_filament" not in native


def test_audited_support_processes_match_the_supported_h2d_pairs():
    assert BAMBU_SUPPORT_DEFAULT_PROCESSES == {profile.process for profile in H2D_PROFILES.values()}


@pytest.mark.parametrize("mode", ["painted", "auto"])
@pytest.mark.parametrize(
    "settings",
    [RoofSupportSettings(), RoofSupportSettings(top_gap=0.2, interface_spacing=0.5)],
    ids=["zero-contact", "gapped"],
)
def test_unknown_process_keeps_all_requested_support_values(mode, settings):
    settings = replace(settings, mode=mode)
    native = settings.native_settings(process_profile="0.16mm Optimal @BBL X1C")
    assert native["support_on_build_plate_only"] == "0"
    assert native["support_interface_top_layers"] == "2"
    assert native["support_top_z_distance"] == f"{settings.top_gap:g}"
    assert native["support_interface_spacing"] == f"{settings.interface_spacing:g}"
    overrides = settings.native_process_override_keys(process_profile="0.16mm Optimal @BBL X1C")
    assert set(native) == overrides
    if settings.contact_mode == "gapped":
        assert native["support_expansion"] == "0"
        assert native["support_object_xy_distance"] == "0.35"
        assert "independent_support_layer_height" not in native
    else:
        assert native["independent_support_layer_height"] == "0"
        assert native["support_object_xy_distance"] == "0.4"
        assert "support_expansion" not in native


@pytest.fixture(scope="module")
def profile_test_tile():
    return tile_design(Tile(1, 1))


@pytest.mark.parametrize(
    "process_profile",
    [None, *(profile.process for profile in H2D_PROFILES.values()), "0.16mm Optimal @BBL X1C"],
)
def test_export_filters_only_audited_process_defaults(process_profile, profile_test_tile, tmp_path):
    support = RoofSupportSettings(top_gap=0.2, interface_spacing=0.5)
    bambu = BambuSettings(
        (Material("model", "PETG", "#778877"), Material("interface", "PLA", "#dddddd")),
        0.4,
        0.2,
        support,
        printer_settings_id="Bambu Lab X1 Carbon 0.4 nozzle" if process_profile else None,
        print_settings_id=process_profile,
    )
    project = tmp_path / "gapped.3mf"
    write_3mf(Job([profile_test_tile], BuildVolume(150, 150, 50), "part"), project, bambu=bambu)
    with ZipFile(project) as archive:
        native = json.loads(archive.read("Metadata/project_settings.config"))
    overrides = set(native["different_settings_to_system"][0].split(";"))
    requested = {
        "support_top_z_distance": "0.2",
        "support_interface_spacing": "0.5",
        "support_interface_top_layers": "2",
        "support_on_build_plate_only": "0",
        "support_expansion": "0",
        "support_object_xy_distance": "0.35",
    }
    if process_profile is None or process_profile in BAMBU_SUPPORT_DEFAULT_PROCESSES:
        assert requested.keys().isdisjoint(native)
        assert requested.keys().isdisjoint(overrides)
    else:
        assert {key: native[key] for key in requested} == requested
        assert requested.keys() <= overrides
    assert "support_filament" not in native
    assert "independent_support_layer_height" not in native


def test_zero_contact_selects_the_passing_side_clearance_witness():
    # Project-owned 2x1 / 0.32-layer slice: PLA end cap versus south PETG wall.
    # Full declared widths are conservative; this is not a physical-print claim.
    wall_y = 5.689
    declared_widths = (0.82, 0.82)
    interface_end_y = {0.35: 4.892, 0.40: 4.842}

    def declared_gap(clearance):
        return wall_y - interface_end_y[clearance] - sum(declared_widths) / 2

    assert declared_gap(0.35) == pytest.approx(-0.023)
    assert declared_gap(0.40) == pytest.approx(0.027)
    settings = RoofSupportSettings()
    chosen = float(settings.native_settings()["support_object_xy_distance"])
    assert declared_gap(chosen) > 0
    assert "support_object_xy_distance" in settings.process_override_keys


def test_same_material_or_unknown_pair_cannot_request_roof_zero_contact():
    for kind in ("PETG", "ABS", "PLA-CF"):
        with pytest.raises(ValueError, match="PETG slot 1.*PLA slot 2"):
            BambuSettings(
                (Material("model", "PETG", "#778877"), Material("interface", kind, "#dddddd")),
                0.8,
                0.32,
                RoofSupportSettings(),
            )


def test_cli_defaults_encode_contact_intent_and_preserve_markers(tmp_path):
    target = tmp_path / "job"
    assert (
        main(
            [
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
                "model",
                "PETG",
                "#778877",
                "--material",
                "interface",
                "PLA",
                "#dddddd",
                "--nozzle-diameter-mm",
                ".8",
                "--layer-height-mm",
                ".32",
                "--roof-support",
                "--output",
                str(target),
            ]
        )
        == 0
    )
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["export"]["roof_support"]["contact_mode"] == "zero-contact"
    with ZipFile(target / "job.3mf") as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert {key: settings[key] for key in CONTACT} == CONTACT
        assert set(CONTACT) <= set(settings["different_settings_to_system"][0].split(";"))
        assert INHERITED_SUPPORT.isdisjoint(settings)
        assert INHERITED_SUPPORT.isdisjoint(settings["different_settings_to_system"][0].split(";"))


def test_general_same_material_project_gets_no_contact_override(tmp_path):
    settings = BambuSettings((Material("model", "PETG", "#778877"),), 0.8, 0.32)
    path = tmp_path / "general.3mf"
    write_3mf(Job([tile_design(Tile())], BuildVolume(150, 150, 50), "part"), path, bambu=settings)
    with ZipFile(path) as archive:
        config = json.loads(archive.read("Metadata/project_settings.config"))
        assert not set(CONTACT) & config.keys()
        assert "support_object_xy_distance" not in config


@pytest.mark.native
def test_native_roundtrip_preserves_zero_contact_and_all_markers(tmp_path):
    executable = os.environ.get("CARGO_GRID_BAMBU")
    if not executable:
        pytest.skip("CARGO_GRID_BAMBU not supplied")
    path = tmp_path / "input.3mf"
    settings = BambuSettings(
        (Material("model", "PETG", "#778877"), Material("interface", "PLA", "#dddddd")),
        0.8,
        0.32,
        RoofSupportSettings(),
    )
    write_3mf(Job([tile_design(Tile())], BuildVolume(150, 150, 50), "part"), path, bambu=settings)
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
            str(path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with ZipFile(tmp_path / "roundtrip.3mf") as archive:
        config = json.loads(archive.read("Metadata/project_settings.config"))
        assert {key: config[key] for key in CONTACT} == CONTACT
        assert config["support_object_xy_distance"] == "0.4"
        assert "support_object_xy_distance" in config["different_settings_to_system"][0].split(";")
        assert set(CONTACT) <= set(config["different_settings_to_system"][0].split(";"))
        model = ET.fromstring(archive.read("Metadata/model_settings.config"))
        modes = [
            m.get("value")
            for m in model.findall("./plate/metadata")
            if m.get("key") == "filament_map_mode"
        ]
        assert modes == ["Auto For Match"]
