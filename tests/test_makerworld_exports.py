import importlib.util
import json
from pathlib import Path

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


def test_variants_cover_each_set_floor_and_nozzle_once():
    variants = _tool().variants()
    assert len(variants) == 2 * 2 * len(H2D_PROFILES)
    assert len({variant.name for variant in variants}) == len(variants)
    for variant in variants:
        args = parser().parse_args([*variant.arguments, "--output", "unused"])
        profiles = H2D_PROFILES[args.nozzle_diameter_mm]
        assert args.h2d_dual_safe and args.bambu and args.no_stl
        assert args.roof_support and args.roof_support_mode == "auto"
        assert args.layer_height_mm == profiles.layer_height_mm
        assert [material[0] for material in args.material] == [profiles.petg, profiles.pla]
        assert variant.name.endswith(f"_Auto-Support_{args.nozzle_diameter_mm:g}mm-Nozzle")
        assert ("_Solid-Bottom-1.92mm_" in variant.name) == (args.solid_bottom_thickness_mm > 0)
        assert variant.name.split("_")[:2] == ["CargoGrid", "H2D"]
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
