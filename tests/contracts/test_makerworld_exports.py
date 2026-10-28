import importlib.util
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from zipfile import ZipFile

import pytest

from cargo_grid.cli import parser
from cargo_grid.printers import H2D, PRINTERS, X2D
from cargo_grid.roof_support import BAMBU_SUPPORT_DEFAULTS
from cargo_grid.sliced_extrusion import SUPPORT_REACH_ALLOWANCE_MM, feature_bounds

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _private_state(monkeypatch, tmp_path):
    monkeypatch.setenv("CARGO_GRID_MAKERWORLD_STATE", str(tmp_path / "state"))


def _bambu_bundle(folder):
    contents = folder / "BambuStudio.app" / "Contents"
    (contents / "Resources" / "profiles" / "BBL" / "machine").mkdir(parents=True)
    (contents / "Resources" / "printers").mkdir()
    (contents / "MacOS").mkdir()
    executable = contents / "MacOS" / "BambuStudio"
    executable.write_text("bambu")
    return executable


def _tool():
    spec = importlib.util.spec_from_file_location(
        "makerworld_exports", ROOT / "tools/makerworld_exports.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_variants_cover_each_published_set_and_nozzle_once():
    variants = _tool().variants()
    assert len(variants) == (2 * 2 + 2) * len(H2D.profiles)
    assert len({variant.name for variant in variants}) == len(variants)
    for variant in variants:
        args = parser().parse_args([*variant.arguments, "--output", "unused"])
        profiles = H2D.profiles[args.nozzle_diameter_mm]
        assert args.printer == "h2d" and args.bambu and args.no_stl
        assert not any(option.startswith("--build-") for option in variant.arguments)
        assert args.layer_height_mm == profiles.layer_height_mm
        assert variant.name.split("_")[:2] == ["CargoGrid", "H2D"]
        assert args.auto_roof_support
        assert [material[0] for material in args.material] == [profiles.petg, profiles.pla]
        titles = {"pull-handle": "Pull-Handle-Set", "adjustable-stop": "Adjustable-Stop"}
        if args.command == "extras" and args.recipe in titles:
            assert variant.name == (
                f"CargoGrid_H2D_{titles[args.recipe]}_{args.nozzle_diameter_mm:g}mm-Nozzle"
            )
            assert args.solid_bottom_thickness_mm == 0
            continue
        assert variant.name.endswith(f"_Auto-Support_{args.nozzle_diameter_mm:g}mm-Nozzle")
        assert ("_Solid-Bottom-1.92mm_" in variant.name) == (args.solid_bottom_thickness_mm > 0)
        assert ("Zeekr" in variant.name) == (args.command == "extras")


def _fake_generate(variant, scratch, *, packing_workers=1):
    folder = scratch / variant.name
    folder.mkdir()
    (folder / "job.3mf").write_text(f"new {variant.name}")
    plates = [{"items": [{"quantity": 2}]}, {"items": [{"quantity": 1}]}]
    (folder / "manifest.json").write_text(json.dumps({"export": {"plates": plates}}))
    return folder


def test_automatic_capacity_reserves_cores_and_memory(monkeypatch):
    tool = _tool()
    monkeypatch.setattr(tool.os, "sched_getaffinity", lambda pid: set(range(18)), raising=False)
    monkeypatch.setattr(tool, "_available_memory_bytes", lambda: 16 * 1024**3)
    assert tool._process_capacity() == (16, 4)
    monkeypatch.setattr(tool, "_available_memory_bytes", lambda: 1024**3)
    assert tool._process_capacity() == (16, 1)


@pytest.mark.parametrize(
    ("override", "capacity", "jobs", "workers"),
    [
        (None, (16, 32), 16, 1),
        (None, (16, 6), 6, 1),
        (4, (16, 32), 4, 4),
        (2, (16, 6), 2, 2),
        (20, (16, 6), 20, 1),
    ],
)
def test_variant_and_search_workers_share_one_budget(
    monkeypatch, tmp_path, capsys, override, capacity, jobs, workers
):
    tool = _tool()
    observed = []

    def generate(variant, scratch, *, packing_workers):
        observed.append(packing_workers)
        return _fake_generate(variant, scratch)

    monkeypatch.setattr(tool, "_process_capacity", lambda: capacity)
    monkeypatch.setattr(tool, "generate", generate)
    arguments = ["--output", str(tmp_path), "--printer", "h2d", "--printer", "x2d"]
    if override is not None:
        arguments += ["--jobs", str(override)]
    assert tool.main(arguments) == 0
    assert observed == [workers] * 24
    assert (
        f"Generating 24 variants with {min(jobs, 24)} jobs; "
        f"at most {workers} search workers per variant"
    ) in capsys.readouterr().out


def test_generation_passes_a_private_process_quota_without_mutating_the_environment(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    tool = _tool()
    name = tool.PACKING_WORKERS_ENV
    monkeypatch.setenv(name, "7")
    observed = []

    def run(command, **kwargs):
        observed.append(kwargs["env"][name])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(tool.subprocess, "run", run)
    tool.generate(tool.variants()[0], tmp_path, packing_workers=3)
    assert observed == ["3"] and os.environ[name] == "7"


def test_published_sets_use_the_same_writer_and_shared_worker_budget(monkeypatch, tmp_path):
    tool = _tool()
    selected = [variant for variant in tool.variants() if variant.set_name == "Adjustable-Stop"]
    observed = []

    def generate(variant, scratch, *, packing_workers):
        observed.append((variant.name, packing_workers))
        return _fake_generate(variant, scratch)

    monkeypatch.setattr(tool, "_process_capacity", lambda: (2, 4))
    monkeypatch.setattr(tool, "generate", generate)
    assert tool.main(["--output", str(tmp_path), "--jobs", "1", "--set", "Adjustable-Stop"]) == 0
    assert observed == [(variant.name, 2) for variant in selected]
    assert {path.name for path in tmp_path.iterdir()} == {
        path.name for variant in selected for path in tool.destinations(tmp_path, variant).values()
    }


def test_weekly_groups_cover_every_published_variant_once():
    tool = _tool()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    printers = json.loads(re.search(r"printer: (\[.*\])", workflow)[1])
    sets = json.loads(re.search(r"set: (\[.*\])", workflow)[1])
    assert len(printers) * len(sets) == 8
    actual = [
        variant.name
        for printer in printers
        for title in sets
        for variant in tool.variants((printer,))
        if variant.set_name == title
    ]
    assert sorted(actual) == sorted(variant.name for variant in tool.variants(("h2d", "x2d")))


def test_weekly_fingerprints_use_the_public_set_route(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import publish_guard

    tool = _tool()
    monkeypatch.setitem(sys.modules, "makerworld_exports", tool)
    monkeypatch.setattr(tool, "_process_capacity", lambda: (2, 4))
    monkeypatch.setattr(tool, "generate", _fake_generate)
    publish_guard.record_fingerprints(
        SimpleNamespace(printer="x2d", set="Adjustable-Stop", output=tmp_path)
    )
    result = json.loads((tmp_path / "fingerprints.json").read_text())
    expected = {
        path.name
        for variant in tool.variants(("x2d",))
        if variant.set_name == "Adjustable-Stop"
        for path in tool.destinations(tmp_path, variant).values()
    }
    assert result["fingerprints"].keys() == expected
    assert set(result) == {"commit", "environment", "fingerprints"}


def test_writes_named_files_and_replaces_only_on_request(monkeypatch, tmp_path, capsys):
    tool = _tool()
    monkeypatch.setattr(tool, "generate", _fake_generate)
    first = tool.variants()[0]
    stale = tool.destinations(tmp_path, first)["job.3mf"]
    stale.write_text("old")
    with pytest.raises(SystemExit) as error:
        tool.main(["--output", str(tmp_path), "--jobs", "2"])
    assert error.value.code == 2
    assert "pass --replace" in capsys.readouterr().err
    assert stale.read_text() == "old"

    assert tool.main(["--output", str(tmp_path), "--jobs", "2", "--replace"]) == 0
    assert stale.read_text() == f"new {first.name}"
    expected = {
        path.name
        for variant in tool.variants()
        for path in tool.destinations(tmp_path, variant).values()
    }
    assert {path.name for path in tmp_path.iterdir()} == expected
    assert f"{first.name}: 2 plates, 3 parts" in capsys.readouterr().out


def test_every_variant_belongs_to_one_named_set():
    tool = _tool()
    variants = tool.variants()
    assert {variant.set_name for variant in variants} == set(tool.SET_NAMES)
    for variant in variants:
        assert variant.name.split("_")[2] == variant.set_name


def test_x2d_variants_use_the_shared_sets_and_both_official_profile_pairs():
    variants = _tool().variants(("x2d",))
    assert len(variants) == 12
    assert len({v.name for v in variants}) == 12
    for variant in variants:
        args = parser().parse_args([*variant.arguments, "--output", "unused"])
        profile = X2D.profiles[args.nozzle_diameter_mm]
        assert args.printer == "x2d"
        assert not any(option.startswith("--build-") for option in variant.arguments)
        assert args.layer_height_mm == profile.layer_height_mm
        assert args.auto_roof_support
        assert [m[0] for m in args.material] == [profile.petg, profile.pla]
        assert variant.name.split("_")[:2] == ["CargoGrid", "X2D"]
        assert args.no_stl and args.bambu


def test_printer_selection_composes_with_set_selection(monkeypatch, tmp_path, capsys):
    tool = _tool()
    monkeypatch.setattr(tool, "generate", _fake_generate)
    assert (
        tool.main(
            [
                "--output",
                str(tmp_path),
                "--printer",
                "x2d",
                "--printer",
                "x2d",
                "--set",
                "Pull-Handle-Set",
            ]
        )
        == 0
    )
    variants = [v for v in tool.variants(("x2d",)) if v.set_name == "Pull-Handle-Set"]
    assert {p.name for p in tmp_path.iterdir()} == {
        p.name for variant in variants for p in tool.destinations(tmp_path, variant).values()
    }
    capsys.readouterr()
    assert (
        tool.main(
            [
                "--output",
                str(tmp_path),
                "--printer",
                "h2d",
                "--printer",
                "x2d",
                "--set",
                "Adjustable-Stop",
                "--dry-run",
            ]
        )
        == 0
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 4
    assert {line.split("_")[1] for line in lines} == {"H2D", "X2D"}


def test_set_filter_builds_only_the_chosen_sets(monkeypatch, tmp_path, capsys):
    tool = _tool()
    monkeypatch.setattr(tool, "generate", _fake_generate)
    other = next(v for v in tool.variants() if v.set_name != "Pull-Handle-Set")
    untouched = tool.destinations(tmp_path, other)["job.3mf"]
    untouched.write_text("old")
    assert tool.main(["--output", str(tmp_path), "--set", "Pull-Handle-Set"]) == 0
    handles = [v for v in tool.variants() if v.set_name == "Pull-Handle-Set"]
    assert len(handles) == len(H2D.profiles)
    expected = {
        path.name for variant in handles for path in tool.destinations(tmp_path, variant).values()
    }
    assert {path.name for path in tmp_path.iterdir()} == expected | {untouched.name}
    assert untouched.read_text() == "old"
    capsys.readouterr()
    assert tool.main(["--output", str(tmp_path), "--dry-run", "--set", "Full-Catalogue"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 2 * len(H2D.profiles)
    assert all(line.startswith("CargoGrid_H2D_Full-Catalogue_") for line in listed)


def test_adjustable_stop_set_filter_writes_only_both_nozzles(monkeypatch, tmp_path):
    tool = _tool()
    monkeypatch.setattr(tool, "generate", _fake_generate)
    assert tool.main(["--output", str(tmp_path), "--set", "Adjustable-Stop"]) == 0
    assert {path.name for path in tmp_path.iterdir()} == {
        path.name
        for variant in tool.variants()
        if variant.set_name == "Adjustable-Stop"
        for path in tool.destinations(tmp_path, variant).values()
    }


def test_failed_variant_leaves_the_output_folder_untouched(monkeypatch, tmp_path):
    tool = _tool()
    broken = tool.variants()[-1].name

    def generate(variant, scratch, **kwargs):
        if variant.name == broken:
            raise RuntimeError("generation failed")
        return _fake_generate(variant, scratch)

    monkeypatch.setattr(tool, "generate", generate)
    with pytest.raises(RuntimeError, match="generation failed"):
        tool.main(["--output", str(tmp_path)])
    assert not any(tmp_path.iterdir())


def _project(path, plates=2, printer=H2D):
    settings = {
        "printer_model": printer.model,
        "printer_settings_id": printer.profiles[0.8].printer,
        "print_settings_id": printer.profiles[0.8].process,
        "filament_settings_id": ["PETG", "PLA"],
        "nozzle_diameter": ["0.8", "0.8"],
    }
    plate = (
        '<plate><metadata key="plater_id" value="{n}" />'
        '<metadata key="filament_map_mode" value="Auto For Match" /></plate>'
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("Metadata/project_settings.config", json.dumps(settings))
        archive.writestr(
            "Metadata/model_settings.config",
            "<config>" + "".join(plate.format(n=n + 1) for n in range(plates)) + "</config>",
        )
        archive.writestr("3D/3dmodel.model", "<model />")
    return settings


def test_check_copy_fixes_the_nozzle_map_without_touching_the_project(tmp_path):
    tool = _tool()
    source = tmp_path / "job.3mf"
    settings = _project(source, plates=3)
    before = source.read_bytes()
    target = tmp_path / "check.3mf"
    assert tool.check_copy(source, settings, ("2", "1"), target) == 3
    assert source.read_bytes() == before
    with ZipFile(target) as archive:
        copied = json.loads(archive.read("Metadata/project_settings.config"))
        plates = archive.read("Metadata/model_settings.config").decode()
        assert archive.read("3D/3dmodel.model") == b"<model />"
    assert copied["filament_map_mode"] == "Manual" and copied["filament_map"] == ["2", "1"]
    assert copied["extruder_nozzle_stats"] == ["Standard#1", "Standard#1"]
    assert copied["flush_volumes_matrix"] == ["0", "280", "280", "0"] * 2
    assert plates.count('value="Manual"') == 3 and plates.count('"filament_maps" value="2 1"') == 3
    assert "Auto For Match" not in plates


@pytest.mark.parametrize(
    "code,log,expected",
    [
        (0, "[error] Invalid T command (T1001).\n", None),
        (
            154,
            "[error] gcode path conflicts found between WipeTower and tile_4x2\n"
            "[error] plate 9: found slicing result conflict!\n",
            "gcode path conflicts found between WipeTower and tile_4x2",
        ),
        (
            154,
            "[error] plate 15: found gcode unprintable! error_code = 1\n",
            "found gcode unprintable! error_code = 1",
        ),
        (3, "nothing useful\n", "Bambu Studio exited with code 3"),
    ],
)
def test_slice_plate_reports_what_bambu_refuses(monkeypatch, tmp_path, code, log, expected):
    tool = _tool()

    def fake_run(bambu, arguments, path):
        path.write_text(log)
        if code == 0:
            target = tmp_path / "sliced-9.3mf"
            _project(target)
            with ZipFile(target, "a") as archive:
                archive.writestr(
                    "Metadata/plate_9.gcode",
                    "M83\nG0 X30 Y10 Z0.2\n; FEATURE: Support\n; LINE_WIDTH: 0.5\nG1 X40 E1\n",
                )
        return code

    monkeypatch.setattr(tool, "_run", fake_run)
    assert tool.slice_plate(tmp_path / "bambu", tmp_path / "job.3mf", 9, tmp_path) == expected


@pytest.mark.parametrize(
    "move,expected",
    [
        ("G1 X40 Y10 E1", (29.8, 9.8, 0.2, 40.2, 10.2, 0.2)),
        ("G2 X40 Y10 I5 J0 E1", (29.8, 9.8, 0.2, 40.2, 15.2, 0.2)),
        ("G3 X40 Y10 I5 J0 E1", (29.8, 4.8, 0.2, 40.2, 10.2, 0.2)),
        ("G2 X30 Y10 I5 J0 E1", (29.8, 4.8, 0.2, 40.2, 15.2, 0.2)),
    ],
)
def test_all_layer_extrusion_bounds_include_width_and_arc_extrema(move, expected):
    gcode = f"M83\nG0 X30 Y10 Z0.2\n; FEATURE: Outer wall\n; LINE_WIDTH: 0.4\n{move}\n"
    assert feature_bounds(gcode)["Outer wall"] == pytest.approx(expected)


def test_extrusion_bounds_ignore_startup_travel_and_retraction_and_track_absolute_e():
    gcode = (
        "M82\nG0 X-50 Y-50 Z0.2\nG1 X-30 E5\nG92 E0\n"
        "; FEATURE: Custom\nG1 X-20 E10\n"
        "G0 X30 Y10\n; FEATURE: Prime tower\n; LINE_WIDTH: 0.4\n"
        "G1 X40 E11\nG1 X100 E10\nG0 X200\n"
    )
    assert feature_bounds(gcode) == {
        "Prime tower": pytest.approx((29.8, 9.8, 0.2, 40.2, 10.2, 0.2))
    }


@pytest.mark.parametrize("x,passes", [(20.75, True), (20.74, False)])
def test_common_reach_check_uses_printed_width_not_just_the_centerline(tmp_path, x, passes):
    path = tmp_path / "sliced.3mf"
    with ZipFile(path, "w") as archive:
        archive.writestr("Metadata/project_settings.config", '{"printer_model":"Bambu Lab X2D"}')
        archive.writestr(
            "Metadata/plate_1.gcode",
            f"M83\nG0 X{x} Y10 Z0.2\n; FEATURE: Outer wall\n; LINE_WIDTH: 0.5\nG1 Y20 E1\n",
        )
    problem = _tool().extrusion_problem(path)
    assert (problem is None) == passes
    if not passes:
        assert "exceed X2D common reach/bed" in problem


def _sliced_project(path, gcode, printer=X2D):
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "Metadata/project_settings.config", json.dumps({"printer_model": printer.model})
        )
        archive.writestr("Metadata/plate_1.gcode", gcode)


@pytest.mark.parametrize("feature", ["Support", "Support interface", "Support transition"])
@pytest.mark.parametrize("x,passes", [(20.25, True), (20.249, False)])
def test_support_only_common_reach_allowance_has_an_exact_boundary(tmp_path, feature, x, passes):
    tool = _tool()
    assert SUPPORT_REACH_ALLOWANCE_MM == 0.5
    path = tmp_path / "sliced.3mf"
    _sliced_project(
        path,
        f"M83\nG0 X{x} Y10 Z0.2\n; FEATURE: {feature}\n; LINE_WIDTH: 0.5\nG1 Y20 E1\n",
    )
    assert (tool.extrusion_problem(path) is None) == passes


@pytest.mark.parametrize(
    "feature", ["Outer wall", "Inner wall", "Prime tower", "Brim", "Support interface extra"]
)
def test_support_allowance_does_not_hide_any_other_feature_overrun(tmp_path, feature):
    path = tmp_path / "sliced.3mf"
    _sliced_project(
        path,
        "M83\nG0 X20.25 Y10 Z0.2\n; FEATURE: Support\n; LINE_WIDTH: 0.5\nG1 Y20 E1\n"
        f"G0 X20.74 Y10\n; FEATURE: {feature}\nG1 Y20 E1\n",
    )
    problem = _tool().extrusion_problem(path)
    assert problem is not None and problem.startswith(feature)


@pytest.mark.parametrize(
    "move",
    [
        "X256 Y10 Z0.2",
        "X40 Y0.24 Z0.2",
        "X40 Y255.76 Z0.2",
        "X40 Y10 Z-0.01",
        "X40 Y10 Z256.01",
    ],
)
def test_support_allowance_never_extends_the_bed_or_height(tmp_path, move):
    path = tmp_path / "sliced.3mf"
    _sliced_project(path, f"M83\nG0 {move}\n; FEATURE: Support\n; LINE_WIDTH: 0.5\nG1 X50 E1\n")
    assert _tool().extrusion_problem(path) is not None


@pytest.mark.parametrize(
    "x,passes", [(24.75, True), (24.749, False), (325.25, True), (325.251, False)]
)
def test_support_allowance_applies_to_both_shared_x_edges_without_moving_h2d(tmp_path, x, passes):
    path = tmp_path / "sliced.3mf"
    _sliced_project(
        path,
        f"M83\nG0 X{x} Y10 Z0.2\n; FEATURE: Support\n; LINE_WIDTH: 0.5\nG1 Y20 E1\n",
        printer=H2D,
    )
    assert (_tool().extrusion_problem(path) is None) == passes


@pytest.mark.parametrize("rear,passes", [(47.5, True), (47.3, False)])
def test_rectangular_tower_check_uses_its_actual_reserved_envelope(tmp_path, rear, passes):
    path = tmp_path / "sliced.3mf"
    with ZipFile(path, "w") as archive:
        archive.writestr("Metadata/project_settings.config", '{"printer_model":"Bambu Lab X2D"}')
        archive.writestr(
            "Metadata/plate_1.gcode",
            "M83\nG0 X40 Y45 Z0.4\n; FEATURE: Prime tower\n; LINE_WIDTH: 0.8\n"
            "G1 Y47 E1\nG0 X100 Y100\n; FEATURE: Outer wall\nG1 X110 E1\n",
        )
    problem = _tool().extrusion_problem(path, (25.75, 12, 63.25, rear))
    assert (problem is None) == passes
    if not passes:
        assert "exceed reserved footprint" in problem


@pytest.mark.parametrize("printer", PRINTERS.values(), ids=PRINTERS)
@pytest.mark.parametrize("failed_slot", ["1", "2"])
def test_bambu_problems_name_the_plate_and_petg_nozzle(monkeypatch, tmp_path, printer, failed_slot):
    tool = _tool()
    source = tmp_path / "job.3mf"
    settings = _project(source, printer=printer)
    monkeypatch.setattr(tool, "effective_settings", lambda project, bambu, work: settings)
    seen = []

    def fake_slice(bambu, project, plate, work, reserved_tower=None):
        assert reserved_tower == ((25.75, 12, 63.25, 48) if plate == 2 else None)
        mapping = json.loads(ZipFile(project).read("Metadata/project_settings.config"))[
            "filament_map"
        ]
        seen.append((tuple(mapping), plate))
        return "conflict" if (mapping[0], plate) == (failed_slot, 2) else None

    monkeypatch.setattr(tool, "slice_plate", fake_slice)
    problems = tool.bambu_problems(
        source,
        tmp_path / "bambu",
        tmp_path / "check",
        jobs=2,
        tower_bounds={2: (25.75, 12, 63.25, 48)},
    )
    nozzle = printer.nozzles[int(failed_slot) - 1].name
    assert problems == [f"plate 2 with PETG on the {nozzle} nozzle: conflict"]
    assert sorted(seen) == [(("1", "2"), 1), (("1", "2"), 2), (("2", "1"), 1), (("2", "1"), 2)]


def test_bambu_check_runs_before_anything_is_written(monkeypatch, tmp_path, capsys):
    tool = _tool()
    monkeypatch.setattr(tool, "generate", _fake_generate)
    bambu = _bambu_bundle(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    refused = tool.variants()[1].name
    nozzle = H2D.nozzles[0].name

    def fake_check(project, work, slicer, tower_bounds=None):
        assert slicer.bambu == bambu.resolve() and project.name == "job.3mf"
        return tool.Check(
            [f"plate 9 with PETG on the {nozzle} nozzle: conflict"] if refused in str(work) else []
        )

    monkeypatch.setattr(tool, "bambu_check", fake_check)
    with pytest.raises(RuntimeError, match=f"{refused}: plate 9 with PETG on the {nozzle}"):
        tool.main(["--output", str(output), "--bambu-studio", str(bambu)])
    assert not any(output.iterdir())
    monkeypatch.setattr(tool, "bambu_check", lambda *args, **kwargs: tool.Check([]))
    assert tool.main(["--output", str(output), "--bambu-studio", str(bambu)]) == 0
    assert len(list(output.iterdir())) == 2 * len(tool.variants())
    assert "Bambu Studio check, every plate sliced" in capsys.readouterr().out


def test_bambu_check_passes_rectangular_reservations_from_the_manifest(monkeypatch, tmp_path):
    tool = _tool()

    def generate(variant, scratch, **kwargs):
        folder = _fake_generate(variant, scratch)
        manifest = folder / "manifest.json"
        data = json.loads(manifest.read_text())
        data["export"]["prime_tower"] = {
            "process_settings": {"prime_tower_width": "18"},
            "plates": [{"plate": 1, "reserved_extrusion_bounds_mm": [25.75, 12, 63.25, 48]}],
        }
        manifest.write_text(json.dumps(data))
        return folder

    def check(project, work, slicer, tower_bounds):
        assert tower_bounds == {1: (25.75, 12, 63.25, 48)}
        return tool.Check([])

    monkeypatch.setattr(tool, "generate", generate)
    monkeypatch.setattr(tool, "bambu_check", check)
    bambu = _bambu_bundle(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    assert tool.main(["--output", str(output), "--bambu-studio", str(bambu)]) == 0


def _sliceable_generate(plates):
    def generate(variant, scratch, **kwargs):
        folder = _fake_generate(variant, scratch)
        _project(folder / "job.3mf", plates=plates)
        return folder

    return generate


def _fake_bambu(monkeypatch, tool, tmp_path):
    monkeypatch.setattr(
        tool, "effective_settings", lambda project, bambu, work: _project(work / "settings.3mf")
    )
    bambu = _bambu_bundle(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    return bambu, output


def test_each_variant_is_checked_as_soon_as_it_is_generated(monkeypatch, tmp_path):
    tool = _tool()
    first, last = [v for v in tool.variants() if v.set_name == "Adjustable-Stop"]
    checked = threading.Event()
    generate = _sliceable_generate(1)

    def generate_in_order(variant, scratch, **kwargs):
        if variant == last:
            assert checked.wait(10), "the first variant was not checked during generation"
        return generate(variant, scratch)

    def check(project, work, slicer, tower_bounds):
        checked.set()
        return tool.Check([])

    monkeypatch.setattr(tool, "generate", generate_in_order)
    monkeypatch.setattr(tool, "bambu_check", check)
    bambu, output = _fake_bambu(monkeypatch, tool, tmp_path)
    arguments = ["--output", str(output), "--set", "Adjustable-Stop", "--jobs", "2"]
    assert tool.main([*arguments, "--bambu-studio", str(bambu)]) == 0


def test_slice_jobs_are_independent_of_generation_jobs(monkeypatch, tmp_path):
    tool = _tool()
    together = threading.Barrier(4, timeout=10)

    def slice_plate(bambu, project, plate, work, reserved_tower=None):
        together.wait()
        return None

    monkeypatch.setattr(tool, "_process_capacity", lambda: (2, 8))
    monkeypatch.setattr(tool, "generate", _sliceable_generate(2))
    monkeypatch.setattr(tool, "slice_plate", slice_plate)
    bambu, output = _fake_bambu(monkeypatch, tool, tmp_path)
    arguments = ["--output", str(output), "--set", "Adjustable-Stop", "--jobs", "1"]
    assert tool.main([*arguments, "--bambu-studio", str(bambu), "--slice-jobs", "4"]) == 0


def test_slices_wait_for_memory_that_generation_has_reserved(monkeypatch, tmp_path, capsys):
    tool = _tool()
    lock = threading.Lock()
    running, peak, generating = [0], [0], []

    def generate(variant, scratch, **kwargs):
        with lock:
            generating.append(running[0])
        return _sliceable_generate(3)(variant, scratch)

    def slice_plate(bambu, project, plate, work, reserved_tower=None):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.02)
        with lock:
            running[0] -= 1

    # One job with two search workers reserves all three memory slots while it builds.
    monkeypatch.setattr(tool, "_process_capacity", lambda: (16, 3))
    monkeypatch.setattr(tool, "generate", generate)
    monkeypatch.setattr(tool, "slice_plate", slice_plate)
    bambu, output = _fake_bambu(monkeypatch, tool, tmp_path)
    arguments = ["--output", str(output), "--set", "Adjustable-Stop", "--jobs", "1"]
    assert tool.main([*arguments, "--bambu-studio", str(bambu)]) == 0
    assert generating == [0, 0] and 1 <= peak[0] <= 3
    assert "at most 3 Bambu Studio processes at once" in capsys.readouterr().out


def test_failed_generation_stops_waiting_slices_and_writes_nothing(monkeypatch, tmp_path):
    tool = _tool()
    first, last = [v for v in tool.variants() if v.set_name == "Adjustable-Stop"]
    sliced = []

    def generate(variant, scratch, **kwargs):
        if variant == last:
            raise RuntimeError("generation failed")
        return _sliceable_generate(2)(variant, scratch)

    monkeypatch.setattr(tool, "_process_capacity", lambda: (16, 3))
    monkeypatch.setattr(tool, "generate", generate)
    monkeypatch.setattr(tool, "slice_plate", lambda *args, **kwargs: sliced.append(args))
    bambu, output = _fake_bambu(monkeypatch, tool, tmp_path)
    arguments = ["--output", str(output), "--set", "Adjustable-Stop", "--jobs", "1"]
    errors = []
    runner = threading.Thread(
        target=lambda: errors.append(
            pytest.raises(RuntimeError, tool.main, [*arguments, "--bambu-studio", str(bambu)])
        ),
        daemon=True,
    )
    runner.start()
    runner.join(20)
    assert not runner.is_alive(), "waiting slices kept the run open"
    assert "generation failed" in str(errors[0].value)
    assert not sliced and not any(output.iterdir())


def test_capacity_lets_slices_use_only_unreserved_slots():
    tool = _tool()
    capacity = tool.Capacity(3)
    capacity.reserve(2)
    started = threading.Event()

    def second():
        with capacity.slot():
            started.set()

    with capacity.slot():
        waiter = threading.Thread(target=second, daemon=True)
        waiter.start()
        assert not started.wait(0.1)
        capacity.reserve(1)
        assert started.wait(5)
    waiter.join(5)
    capacity.reserve(3)
    capacity.close()
    with pytest.raises(RuntimeError, match="stopped"):
        with capacity.slot():
            pass


def _meshed_project(path, meshes, printer=H2D, header="CargoGrid"):
    """A project with one placed object per plate; each mesh is a vertex x value."""
    settings = _project(path, plates=len(meshes), printer=printer)
    resources, items, objects, plates = [], [], [], []
    for number, mesh in enumerate(meshes, 1):
        part, whole = 2 * number - 1, 2 * number
        resources.append(
            f'<object id="{part}" type="model"><mesh><vertices><vertex x="{mesh}" y="0" z="0"/>'
            f'</vertices></mesh></object><object id="{whole}" type="model"><components>'
            f'<component objectid="{part}" /></components></object>'
        )
        items.append(f'<item objectid="{whole}" transform="1 0 0 0 1 0 0 0 1 {number} 0 0" />')
        objects.append(
            f'<object id="{whole}"><metadata key="name" value="part {number}" /></object>'
        )
        plates.append(
            f'<plate><metadata key="plater_id" value="{number}" />'
            '<metadata key="filament_map_mode" value="Auto For Match" /><model_instance>'
            f'<metadata key="object_id" value="{whole}" /></model_instance></plate>'
        )
    with ZipFile(path, "w") as archive:
        archive.writestr("Metadata/project_settings.config", json.dumps(settings))
        archive.writestr(
            "Metadata/model_settings.config", f"<config>{''.join(objects + plates)}</config>"
        )
        archive.writestr(
            "3D/3dmodel.model",
            f'<model><metadata name="Designer">{header}</metadata><resources>{"".join(resources)}'
            f"</resources><build>{''.join(items)}</build></model>",
        )
    return settings


def test_plate_keys_change_only_with_what_decides_that_plate(tmp_path):
    tool = _tool()

    def keys(meshes, reservations=None, checker="checker", header="CargoGrid", effective=None):
        path = tmp_path / "job.3mf"
        settings = _meshed_project(path, meshes, header=header)
        return tool.plate_keys(path, effective or settings, reservations or {}, checker)

    base = keys(["1", "2", "3"])
    assert len({key for values in base.values() for key in values}) == 6
    changed = keys(["1", "2.5", "3"])
    for mapping in tool.NOZZLE_MAPS:
        assert [a == b for a, b in zip(base[mapping], changed[mapping])] == [True, False, True]
    moved = keys(["1", "2", "3"], reservations={3: (1, 2, 3, 4)})
    assert [a == b for a, b in zip(base[("1", "2")], moved[("1", "2")])] == [True, True, False]
    for other in (
        keys(["1", "2", "3"], checker="other"),
        keys(["1", "2", "3"], header="Other"),
        keys(["1", "2"]),
        keys(["1", "2", "3"], effective={**_project(tmp_path / "x.3mf"), "layer_height": "0.24"}),
    ):
        assert not set(base[("1", "2")]) & set(other[("1", "2")])


def test_model_digests_split_objects_and_items_and_refuse_unknown_structure():
    import io

    tool = _tool()
    model = (
        b'<model><resources><object id="1"><mesh><v x="1"/></mesh></object>'
        b'<object xmlns="ns" id="2"><components><component objectid="1" /></components>'
        b'</object></resources><build><item objectid="2" transform="t" /></build></model>'
    )

    class Trickle(io.BytesIO):
        def read(self, size=-1):
            return super().read(3)

    rest, objects, components, items = tool.model_digests(io.BytesIO(model))
    assert tool.model_digests(Trickle(model)) == (rest, objects, components, items)
    assert set(objects) == {"1", "2"} and components == {"1": [], "2": ["1"]}
    assert list(items) == ["2"]
    for broken in (
        model.replace(b"/></build>", b"><x/></item></build>"),
        model.replace(b"</object></resources>", b"</resources>"),
        model.replace(b'<object id="1">', b'<objects id="1">'),
        model.replace(b'id="2"', b'id="1"'),
    ):
        with pytest.raises(ValueError):
            tool.model_digests(io.BytesIO(broken))


def _cached_run(monkeypatch, tool, tmp_path, meshes_by_variant, problems=None):
    """Run the Adjustable-Stop set; return (variant name, nozzle folder, plate) slices made."""
    sliced = []
    lock = threading.Lock()

    def generate(variant, scratch, **kwargs):
        folder = _fake_generate(variant, scratch)
        _meshed_project(folder / "job.3mf", meshes_by_variant(variant))
        return folder

    def slice_plate(bambu, project, plate, work, reserved_tower=None):
        with lock:
            sliced.append((work.parent.name.removesuffix("-check"), work.name, plate))
        return (problems or {}).get(plate)

    monkeypatch.setattr(tool, "generate", generate)
    monkeypatch.setattr(tool, "slice_plate", slice_plate)
    bambu, output = _fake_bambu(monkeypatch, tool, tmp_path)
    return sliced, [
        "--output",
        str(output),
        "--replace",
        "--set",
        "Adjustable-Stop",
        "--bambu-studio",
        str(bambu),
    ]


def test_identical_plates_are_sliced_once_per_run_and_reused_by_later_runs(
    monkeypatch, tmp_path, capsys
):
    tool = _tool()
    first, last = [v for v in tool.variants() if v.set_name == "Adjustable-Stop"]
    meshes = {first: ["1", "2"], last: ["1", "3"]}
    sliced, arguments = _cached_run(monkeypatch, tool, tmp_path, lambda variant: meshes[variant])
    assert tool.main(arguments) == 0
    assert len(sliced) == 6 and len(set(sliced)) == 6
    assert sorted(plate for _, _, plate in sliced) == [1, 1, 2, 2, 2, 2]
    shared = re.findall(r"(\d+) shared with another variant", capsys.readouterr().out)
    assert sum(map(int, shared)) == 2

    sliced.clear()
    assert tool.main(arguments) == 0
    assert sliced == []
    assert capsys.readouterr().out.count("(4 plate slices: 4 from the slice cache)") == 2

    meshes[last] = ["1", "4"]
    assert tool.main(arguments) == 0
    assert sorted(sliced) == [
        (last.name, "petg-nozzle-1", 2),
        (last.name, "petg-nozzle-2", 2),
    ]
    entries = list((tmp_path / "state" / "slice-cache").glob("*.json"))
    assert len(entries) == 8
    record = json.loads(entries[0].read_text())
    assert record["result"] == "clean" and record["key"] == entries[0].stem


def test_refused_plates_are_sliced_again_and_never_cached(monkeypatch, tmp_path):
    tool = _tool()
    sliced, arguments = _cached_run(
        monkeypatch, tool, tmp_path, lambda variant: ["1", "2"], problems={2: "conflict"}
    )
    for expected in ([1, 1, 2, 2], [2, 2]):
        sliced.clear()
        with pytest.raises(RuntimeError, match="plate 2 with PETG"):
            tool.main(arguments)
        assert sorted(plate for _, _, plate in sliced) == expected
    assert len(list((tmp_path / "state" / "slice-cache").glob("*.json"))) == 2


def test_no_slice_cache_slices_every_plate_and_writes_no_cache(monkeypatch, tmp_path, capsys):
    tool = _tool()
    sliced, arguments = _cached_run(monkeypatch, tool, tmp_path, lambda variant: ["1", "2"])
    assert tool.main(arguments) == 0
    sliced.clear()
    before = sorted(path.name for path in (tmp_path / "state" / "slice-cache").iterdir())
    assert tool.main([*arguments, "--no-slice-cache"]) == 0
    assert len(sliced) == 8
    assert sorted(path.name for path in (tmp_path / "state" / "slice-cache").iterdir()) == before
    assert "from the slice cache" not in capsys.readouterr().out.split("every plate sliced (4")[-1]


def test_unreadable_model_falls_back_to_slicing_every_plate(monkeypatch, tmp_path, capsys):
    tool = _tool()
    sliced, arguments = _cached_run(monkeypatch, tool, tmp_path, lambda variant: ["1", "2"])
    monkeypatch.setattr(
        tool, "plate_fingerprints", lambda project: (_ for _ in ()).throw(ValueError("odd model"))
    )
    assert tool.main(arguments) == 0
    assert tool.main(arguments) == 0
    assert len(sliced) == 16
    assert "slicing every plate without the cache: odd model" in capsys.readouterr().out
    assert not (tmp_path / "state" / "slice-cache").exists()


def test_checker_identity_covers_the_executable_profiles_and_check_code(tmp_path):
    import ast

    tool = _tool()
    bambu = _bambu_bundle(tmp_path)
    profile = bambu.parents[1] / "Resources" / "profiles" / "BBL" / "machine" / "H2D.json"
    profile.write_text("{}")
    printer = bambu.parents[1] / "Resources" / "printers" / "O1D.json"
    printer.write_text("{}")
    identities = {tool.checker_identity(bambu)}
    for path, text in ((bambu, "bambu 2"), (profile, '{"a": 1}'), (printer, '{"b": 1}')):
        path.write_text(text)
        identities.add(tool.checker_identity(bambu))
    profile.rename(profile.with_name("Other.json"))
    identities.add(tool.checker_identity(bambu))
    assert len(identities) == 5

    covered = {module.__name__ for module in tool.CHECK_MODULES}
    pending = ["cargo_grid.sliced_extrusion", "cargo_grid.printers"]
    while pending:
        name = pending.pop()
        assert name in covered
        source = ROOT / "src" / Path(*name.split(".")).with_suffix(".py")
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("cargo_grid."):
                pending.append(node.module)


def test_bambu_studio_must_be_an_existing_file(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        _tool().main(["--output", str(tmp_path), "--bambu-studio", str(tmp_path / "missing")])
    assert error.value.code == 2
    assert "--bambu-studio must be the Bambu Studio executable" in capsys.readouterr().err


@pytest.mark.native
def test_native_bambu_check_slices_a_real_zeekr_set(tmp_path):
    executable = os.environ.get("CARGO_GRID_BAMBU")
    if not executable:
        pytest.skip("Set CARGO_GRID_BAMBU to explicitly enable local Bambu Studio CLI checks")
    tool = _tool()
    variant = next(v for v in tool.variants() if "Zeekr" in v.name and "Solid" not in v.name)
    folder = tool.generate(variant, tmp_path)
    problems = tool.bambu_problems(
        folder / "job.3mf", Path(executable).resolve(strict=True), tmp_path / "check", jobs=4
    )
    assert problems == []


@pytest.mark.native
@pytest.mark.parametrize("printer", PRINTERS.values(), ids=PRINTERS)
@pytest.mark.parametrize("nozzle", [0.8, 0.4])
def test_native_system_presets_match_the_audited_printer_and_support_defaults(printer, nozzle):
    executable = os.environ.get("CARGO_GRID_BAMBU")
    if not executable:
        pytest.skip("Set CARGO_GRID_BAMBU to explicitly enable local Bambu Studio profile checks")
    tool = _tool()
    root = tool._profiles(Path(executable).resolve(strict=True))
    profiles = printer.profiles[nozzle]
    machine = tool._resolve(root, "machine", profiles.printer)
    assert machine["printer_model"] == printer.model
    assert [float(v) for v in machine["nozzle_diameter"]] == [nozzle, nozzle]
    assert float(machine["printable_height"]) == printer.build.z
    assert [float(v) for v in machine["extruder_printable_height"]] == [
        n.height_mm for n in printer.nozzles
    ]
    assert machine["extruder_type"] == [n.extruder_type for n in printer.nozzles]
    areas = [
        [tuple(map(float, point.split("x"))) for point in polygon.split(",")]
        for polygon in machine["extruder_printable_area"]
    ]
    assert [(min(x for x, y in area), max(x for x, y in area)) for area in areas] == [
        n.x_mm for n in printer.nozzles
    ]
    assert all(min(y for x, y in area) == 0 for area in areas)
    assert all(max(y for x, y in area) == printer.build.y for area in areas)
    process = tool._resolve(root, "process", profiles.process)
    assert profiles.printer in process["compatible_printers"]
    assert float(process["layer_height"]) == profiles.layer_height_mm
    assert {key: process[key] for key in BAMBU_SUPPORT_DEFAULTS} == BAMBU_SUPPORT_DEFAULTS
    filaments = [tool._resolve(root, "filament", name) for name in (profiles.petg, profiles.pla)]
    assert all(profiles.printer in p["compatible_printers"] for p in filaments)
    assert max(float(p["filament_prime_volume"][0]) for p in filaments) == (
        printer.tower_prime_volume_mm3
    )


@pytest.mark.native
@pytest.mark.parametrize("printer,model_id", [(H2D, "O1D"), (X2D, "N6")])
def test_native_nozzle_names_follow_the_resource_and_physical_extruder_map(printer, model_id):
    executable = os.environ.get("CARGO_GRID_BAMBU")
    if not executable:
        pytest.skip("Set CARGO_GRID_BAMBU to enable local Bambu printer-resource checks")
    tool = _tool()
    root = tool._profiles(Path(executable).resolve(strict=True))
    resource = json.loads((root.parent.parent / "printers" / f"{model_id}.json").read_text())
    names = resource["00.00.00.00"]["tool_head_display_names"]
    machine = tool._resolve(root, "machine", printer.profiles[0.8].printer)
    assert tuple(n.name for n in printer.nozzles) == tuple(
        names[physical]["nozzle"][2].removesuffix(" nozzle")
        for physical in machine["physical_extruder_map"]
    )
    assert [slot for slot, n in enumerate(printer.nozzles, 1) if n.is_master] == [
        int(machine["master_extruder_id"])
    ]
