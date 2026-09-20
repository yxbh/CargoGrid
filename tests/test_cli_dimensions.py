"""Named printer dimensions are required, unambiguous and preserve geometry."""

import json
import re
from pathlib import Path

import pytest
from build123d import import_step

from cargo_grid.cli import LEGACY_OPTION_REPLACEMENTS, main, parser

OPTIONS = ("--build-width-mm", "--build-depth-mm", "--build-height-mm")
COMPLETE = [OPTIONS[0], "150", OPTIONS[1], "150", OPTIONS[2], "50"]


@pytest.mark.parametrize("command", ["part", "layout", "catalogue"])
@pytest.mark.parametrize("supplied", [(), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2)])
def test_missing_build_axes_are_named_in_errors(command, supplied, tmp_path, capsys):
    args = [command, "--output", str(tmp_path / "job")]
    if command == "layout":
        args += ["--layout-width-mm", "120", "--layout-depth-mm", "60"]
    for index in supplied:
        args += [OPTIONS[index], "150"]
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    message = capsys.readouterr().err.split("error:", 1)[1]
    assert "required" in message
    for index, option in enumerate(OPTIONS):
        assert (option in message) == (index not in supplied)
    assert not (tmp_path / "job").exists()


@pytest.mark.parametrize("axis", range(3))
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-a-number"])
def test_build_values_report_axis_and_units(axis, value, tmp_path, capsys):
    args = list(COMPLETE)
    args[2 * axis + 1] = value
    with pytest.raises(SystemExit) as error:
        main(["part", *args, "--output", str(tmp_path / "job")])
    assert error.value.code == 2
    message = capsys.readouterr().err.split("error:", 1)[1]
    assert OPTIONS[axis] in message and "millimeters" in message and "greater than 0" in message
    assert not (tmp_path / "job").exists()


@pytest.mark.parametrize("other_dimensions", [[], COMPLETE])
def test_removed_ambiguous_option_is_not_an_abbreviation(tmp_path, capsys, other_dimensions):
    legacy = "--" + "build"
    with pytest.raises(SystemExit) as error:
        main(
            [
                "part",
                *other_dimensions,
                legacy,
                "150",
                "150",
                "50",
                "--output",
                str(tmp_path / "job"),
            ]
        )
    message = capsys.readouterr().err
    assert error.value.code == 2 and "unrecognized option" in message
    assert all(option in message for option in OPTIONS)
    assert not (tmp_path / "job").exists()


@pytest.mark.parametrize("command", ["part", "layout", "catalogue"])
def test_help_explains_dimensions_and_multivalue_order(command, capsys):
    with pytest.raises(SystemExit) as error:
        parser().parse_args([command, "--help"])
    assert error.value.code == 0
    text = capsys.readouterr().out
    normalized = " ".join(text.split())
    for option in OPTIONS:
        assert option in text
    for explanation in (
        "left-right",
        "front-back",
        "maximum print height",
        "no default",
        "--build-reserve-width-mm",
        "X_MM Y_MM WIDTH_MM DEPTH_MM",
    ):
        assert explanation in normalized
    assert not re.search(r"--build(?:[ =,]|$)", text)


def test_brace_bore_help_describes_standard_not_trial_variants(capsys):
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["part", "--help"])
    assert error.value.code == 0
    match = re.search(
        r"^\s+--bore-diameter-mm MM\s+(.*?)(?=\n\s+--|\Z)",
        capsys.readouterr().out,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    description = " ".join(match.group(1).split())
    assert "standard/default 10 mm" in description
    assert "diametral" in description
    assert "10.2" not in description and "10.4" not in description


def test_named_dimensions_generate_the_same_two_by_one_contract(tmp_path):
    output = tmp_path / "job"
    assert (
        main(
            [
                "part",
                *COMPLETE,
                "--width-cells",
                "2",
                "--depth-cells",
                "1",
                "--copy-count",
                "2",
                "--no-stl",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    manifest = json.loads((output / "manifest.json").read_text())
    assert {key: manifest["build"][key] for key in ("x", "y", "z")} == {"x": 150, "y": 150, "z": 50}
    design = manifest["designs"][0]
    assert design["quantity"] == 2
    assert design["parameters"]["interface"]["joint_style"] == "original"
    assert design["parameters"]["hole_diameter"] == 10
    assert design["parameters"]["hole_scope"] == "full"
    shape = import_step(output / f"{design['name']}.step")
    assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
    assert tuple(shape.bounding_box().size) == pytest.approx((126, 66, 13), abs=1e-5)


@pytest.mark.parametrize(
    "legacy,replacement",
    [
        ("--cells", "--width-cells COUNT --depth-cells COUNT"),
        ("--margin", "--build-margin-mm MM"),
        ("--pitch", "--unit-size-mm MM"),
        ("--height", "--tile-thickness-mm MM"),
        ("--grid-pitch-mm", "--unit-size-mm MM"),
        ("--tile-height-mm", "--tile-thickness-mm MM"),
        ("--quantity", "--copy-count COUNT"),
        ("--footprint", "--layout-width-mm MM --layout-depth-mm MM"),
    ],
)
def test_removed_parameter_names_give_actionable_migrations(legacy, replacement, tmp_path, capsys):
    command = "layout" if legacy == "--footprint" else "part"
    values = ["120", "60"] if legacy in {"--cells", "--footprint"} else ["1"]
    with pytest.raises(SystemExit) as error:
        main(
            [
                command,
                *COMPLETE,
                legacy,
                *values,
                "--output",
                str(tmp_path / "job"),
            ]
        )
    message = " ".join(capsys.readouterr().err.split())
    assert error.value.code == 2
    assert f"unrecognized option {legacy}" in message
    assert replacement in message


@pytest.mark.parametrize("legacy,replacement", LEGACY_OPTION_REPLACEMENTS.items())
def test_every_removed_option_has_an_actionable_migration(legacy, replacement, capsys):
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["part", legacy])
    message = " ".join(capsys.readouterr().err.split())
    assert error.value.code == 2
    assert f"unrecognized option {legacy}" in message
    assert replacement in message


def test_partial_two_axis_dimensions_name_the_missing_count(tmp_path, capsys):
    for supplied, missing in (
        ("--width-cells", "--depth-cells"),
        ("--depth-cells", "--width-cells"),
    ):
        with pytest.raises(SystemExit) as error:
            main(
                [
                    "part",
                    *COMPLETE,
                    supplied,
                    "2",
                    "--output",
                    str(tmp_path / supplied.removeprefix("--")),
                ]
            )
        assert error.value.code == 2
        assert missing in capsys.readouterr().err


@pytest.mark.parametrize(
    "family,option",
    [
        ("ramp", "--depth-cells"),
        ("edge-x", "--width-cells"),
        ("corner-in", "--length-cells"),
        ("plate", "--stop-height-mm"),
        ("tile", "--variant-number"),
        ("support", "--connector-length-mm"),
    ],
)
def test_family_specific_dimensions_reject_irrelevant_flags(family, option, tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(
            [
                "part",
                *COMPLETE,
                "--family",
                family,
                option,
                "1",
                "--output",
                str(tmp_path / family),
            ]
        )
    assert error.value.code == 2
    assert option in capsys.readouterr().err


def test_no_stale_build_triples_in_maintained_commands():
    root = Path(__file__).resolve().parents[1]
    files = [root / "README.md", root / "AGENTS.md", root / ".github/workflows/ci.yml"]
    files += list((root / "docs").glob("*.md")) + list((root / "examples").glob("*.py"))
    for path in files:
        assert not re.search(r"--build(?:[ =,`\"']|$)", path.read_text()), path


@pytest.mark.parametrize("join", [None, "female", "male"])
def test_ramp_join_cli_keeps_width_and_slope_run_separate(join, tmp_path):
    output = tmp_path / "ramp"
    flags = [] if join is None else ["--ramp-join", join]
    assert (
        main(
            [
                "part",
                *COMPLETE,
                "--family",
                "ramp",
                "--width-cells",
                "2",
                *flags,
                "--no-stl",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    design = json.loads((output / "manifest.json").read_text())["designs"][0]
    assert design["parameters"].get("ramp_join", "female") == (join or "female")
    if join != "male":
        assert "ramp_join" not in design["parameters"]
    shape = import_step(output / f"{design['name']}.step")
    assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
    bounds = shape.bounding_box()
    assert tuple(bounds.size) == pytest.approx((120, 56 if join == "male" else 50, 13), abs=1e-5)
    assert bounds.min.Z == pytest.approx(0, abs=1e-5)
    assert bounds.min.Y == pytest.approx(-6 if join == "male" else 0, abs=1e-5)


@pytest.mark.parametrize("family", ["tile", "plate", "edge-x", "vertical-tile-bracket"])
@pytest.mark.parametrize("join", ["female", "male"])
def test_ramp_join_rejects_other_part_families(family, join, tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(
            [
                "part",
                *COMPLETE,
                "--family",
                family,
                "--ramp-join",
                join,
                "--output",
                str(tmp_path / "invalid"),
            ]
        )
    assert error.value.code == 2
    assert f"--ramp-join does not apply to {family}" in capsys.readouterr().err
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("width", ["0", "-1", "1.5"])
def test_ramp_width_requires_positive_whole_cells(width, tmp_path):
    with pytest.raises(SystemExit) as error:
        main(
            [
                "part",
                *COMPLETE,
                "--family",
                "ramp",
                "--ramp-join",
                "male",
                "--width-cells",
                width,
                "--output",
                str(tmp_path / "invalid"),
            ]
        )
    assert error.value.code == 2
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("command", ["layout", "catalogue"])
def test_ramp_join_is_not_a_catalogue_or_layout_option(command, tmp_path, capsys):
    extra = ["--layout-width-mm", "60", "--layout-depth-mm", "60"] if command == "layout" else []
    with pytest.raises(SystemExit) as error:
        main(
            [
                command,
                *COMPLETE,
                *extra,
                "--ramp-join",
                "male",
                "--output",
                str(tmp_path / "invalid"),
            ]
        )
    assert error.value.code == 2
    assert "unrecognized arguments: --ramp-join male" in capsys.readouterr().err
    assert not (tmp_path / "invalid").exists()
