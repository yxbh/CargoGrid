import importlib.util
import json
import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from cargo_grid.cli import H2D_PROFILES, parser

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location(
        "makerworld_exports", ROOT / "tools/makerworld_exports.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_variants_cover_each_published_set_and_nozzle_once():
    variants = _tool().variants()
    assert len(variants) == (2 * 2 + 2) * len(H2D_PROFILES)
    assert len({variant.name for variant in variants}) == len(variants)
    for variant in variants:
        args = parser().parse_args([*variant.arguments, "--output", "unused"])
        profiles = H2D_PROFILES[args.nozzle_diameter_mm]
        assert args.h2d_dual_safe and args.bambu and args.no_stl
        assert args.layer_height_mm == profiles.layer_height_mm
        assert variant.name.split("_")[:2] == ["CargoGrid", "H2D"]
        if args.command == "extras" and args.recipe == "pull-handle":
            assert variant.name == (
                f"CargoGrid_H2D_Pull-Handle-Set_{args.nozzle_diameter_mm:g}mm-Nozzle"
            )
            assert not args.roof_support and args.roof_support_mode is None
            assert [material[0] for material in args.material] == [profiles.petg]
            assert args.solid_bottom_thickness_mm == 0
            continue
        if args.command == "extras" and args.recipe == "adjustable-stop":
            assert variant.name == (
                f"CargoGrid_H2D_Adjustable-Stop_{args.nozzle_diameter_mm:g}mm-Nozzle"
            )
            assert args.roof_support and args.roof_support_mode == "auto"
            assert [material[0] for material in args.material] == [profiles.petg, profiles.pla]
            assert args.solid_bottom_thickness_mm == 0
            continue
        assert args.roof_support and args.roof_support_mode == "auto"
        assert [material[0] for material in args.material] == [profiles.petg, profiles.pla]
        assert variant.name.endswith(f"_Auto-Support_{args.nozzle_diameter_mm:g}mm-Nozzle")
        assert ("_Solid-Bottom-1.92mm_" in variant.name) == (args.solid_bottom_thickness_mm > 0)
        assert ("Zeekr" in variant.name) == (args.command == "extras")


def _fake_generate(variant, scratch):
    folder = scratch / variant.name
    folder.mkdir()
    (folder / "job.3mf").write_text(f"new {variant.name}")
    plates = [{"items": [{"quantity": 2}]}, {"items": [{"quantity": 1}]}]
    (folder / "manifest.json").write_text(json.dumps({"export": {"plates": plates}}))
    return folder


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


def test_set_filter_builds_only_the_chosen_sets(monkeypatch, tmp_path, capsys):
    tool = _tool()
    monkeypatch.setattr(tool, "generate", _fake_generate)
    other = next(v for v in tool.variants() if v.set_name != "Pull-Handle-Set")
    untouched = tool.destinations(tmp_path, other)["job.3mf"]
    untouched.write_text("old")
    assert tool.main(["--output", str(tmp_path), "--set", "Pull-Handle-Set"]) == 0
    handles = [v for v in tool.variants() if v.set_name == "Pull-Handle-Set"]
    assert len(handles) == len(H2D_PROFILES)
    expected = {
        path.name for variant in handles for path in tool.destinations(tmp_path, variant).values()
    }
    assert {path.name for path in tmp_path.iterdir()} == expected | {untouched.name}
    assert untouched.read_text() == "old"
    capsys.readouterr()
    assert tool.main(["--output", str(tmp_path), "--dry-run", "--set", "Full-Catalogue"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 2 * len(H2D_PROFILES)
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

    def generate(variant, scratch):
        if variant.name == broken:
            raise RuntimeError("generation failed")
        return _fake_generate(variant, scratch)

    monkeypatch.setattr(tool, "generate", generate)
    with pytest.raises(RuntimeError, match="generation failed"):
        tool.main(["--output", str(tmp_path)])
    assert not any(tmp_path.iterdir())


def _project(path, plates=2):
    settings = {
        "printer_settings_id": "Bambu Lab H2D 0.8 nozzle",
        "print_settings_id": "0.32mm Balanced Strength @BBL H2D 0.8 nozzle",
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
        return code

    monkeypatch.setattr(tool, "_run", fake_run)
    assert tool.slice_plate(tmp_path / "bambu", tmp_path / "job.3mf", 9, tmp_path) == expected


def test_bambu_problems_name_the_plate_and_petg_nozzle(monkeypatch, tmp_path):
    tool = _tool()
    source = tmp_path / "job.3mf"
    settings = _project(source)
    monkeypatch.setattr(tool, "effective_settings", lambda project, bambu, work: settings)
    seen = []

    def fake_slice(bambu, project, plate, work):
        mapping = json.loads(ZipFile(project).read("Metadata/project_settings.config"))[
            "filament_map"
        ]
        seen.append((tuple(mapping), plate))
        return "conflict" if (mapping[0], plate) == ("2", 2) else None

    monkeypatch.setattr(tool, "slice_plate", fake_slice)
    problems = tool.bambu_problems(source, tmp_path / "bambu", tmp_path / "check", jobs=2)
    assert problems == ["plate 2 with PETG on the right nozzle: conflict"]
    assert sorted(seen) == [(("1", "2"), 1), (("1", "2"), 2), (("2", "1"), 1), (("2", "1"), 2)]


def test_bambu_check_runs_before_anything_is_written(monkeypatch, tmp_path, capsys):
    tool = _tool()
    monkeypatch.setattr(tool, "generate", _fake_generate)
    bambu = tmp_path / "BambuStudio"
    bambu.write_text("")
    output = tmp_path / "out"
    output.mkdir()
    refused = tool.variants()[1].name

    def fake_problems(project, executable, work, jobs):
        assert executable == bambu.resolve() and project.name == "job.3mf"
        return ["plate 9 with PETG on the left nozzle: conflict"] if refused in str(work) else []

    monkeypatch.setattr(tool, "bambu_problems", fake_problems)
    with pytest.raises(RuntimeError, match=f"{refused}: plate 9 with PETG on the left"):
        tool.main(["--output", str(output), "--bambu-studio", str(bambu)])
    assert not any(output.iterdir())
    monkeypatch.setattr(tool, "bambu_problems", lambda *args: [])
    assert tool.main(["--output", str(output), "--bambu-studio", str(bambu)]) == 0
    assert len(list(output.iterdir())) == 2 * len(tool.variants())
    assert "Bambu Studio check, every plate sliced" in capsys.readouterr().out


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
