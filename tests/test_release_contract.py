"""Maintained CLI, manifest, early-failure and distribution boundaries."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from build123d import Box

from cargo_grid import BuildVolume, Tile, __version__
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, export_job, write_3mf
from cargo_grid.jobs import Design, Job, tile_design
from cargo_grid.roof_support import RoofSupportSettings
from cargo_grid.stacking import StackSettings


@pytest.fixture
def block_job():
    return Job([Design("block", Box(20, 30, 2), {})], BuildVolume(100, 100, 40), "diagnostic")


@pytest.fixture
def bambu():
    return BambuSettings(
        (Material("PETG", "PETG", "#778877"), Material("PLA", "PLA", "#dddddd")),
        0.4,
        0.2,
    )


def test_version_and_module_entrypoint(tmp_path):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    version = subprocess.run(
        [sys.executable, "-m", "cargo_grid", "--version"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert version.stdout.strip() == f"cargo-grid {__version__}"
    help_result = subprocess.run(
        [sys.executable, "-m", "cargo_grid", "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert all(
        command in help_result.stdout
        for command in ("part", "layout", "catalogue", "compare-reference")
    )
    normalized_help = " ".join(help_result.stdout.split())
    assert "full 10 mm round-hole tiles are the defaults" in normalized_help
    assert "roof support remains off" in normalized_help


def test_manifest_version_design_mode_and_limits(block_job, tmp_path):
    report = json.loads(export_job(block_job, tmp_path / "job", stl=False).read_text())
    assert report["schema_version"] == 1
    assert report["generator"] == {"name": "cargo-grid", "version": __version__}
    assert report["design_mode"] == {
        "workflow": "diagnostic",
        "joint_styles": [],
        "hole_scopes": [],
        "roof_coverage": None,
        "stacked": False,
    }
    assert report["physical_fit_verified"] is False
    assert report["export"]["physical_print_verified"] is False
    assert "roof_support_with_stacking" in report["unsupported_combinations"]
    with ZipFile(tmp_path / "job/job.3mf") as archive:
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
        version = next(
            item.text
            for item in model.findall("{*}metadata")
            if item.get("name") == "CargoGridVersion"
        )
        assert version == __version__


@pytest.mark.parametrize(
    "name", ["", "../outside", "a/b", "a\\b", "NUL", "COM1.txt", "a?", "name."]
)
def test_unsafe_design_names_fail_before_writing(block_job, tmp_path, name):
    block_job.designs[0].name = name
    for function, output in ((export_job, tmp_path / "job"), (write_3mf, tmp_path / "job.3mf")):
        with pytest.raises(ValueError, match="portable filenames"):
            function(block_job, output)
        assert not output.exists()


def test_duplicate_casefolded_filenames_are_rejected(block_job, tmp_path):
    block_job.designs.append(Design("BLOCK", block_job.designs[0].shape, {}))
    with pytest.raises(ValueError, match="duplicate"):
        export_job(block_job, tmp_path / "job")
    assert not (tmp_path / "job").exists()


@pytest.mark.parametrize("gap", [-1, float("nan"), float("inf")])
def test_mutated_invalid_gap_fails_before_outputs(block_job, tmp_path, gap):
    block_job.part_gap = gap
    with pytest.raises(ValueError, match="part gap"):
        export_job(block_job, tmp_path / "job")
    assert not (tmp_path / "job").exists()


def test_standalone_3mf_and_file_output_are_never_overwritten(block_job, tmp_path):
    path = tmp_path / "existing.3mf"
    path.write_bytes(b"user-owned content")
    for function in (write_3mf, export_job):
        with pytest.raises(ValueError, match="already exists|not empty"):
            function(block_job, path)
        assert path.read_bytes() == b"user-owned content"


def test_invalid_stack_api_is_rejected_before_step_exports(block_job, bambu, tmp_path):
    stack = StackSettings(2, 1, 0.2, 1, 1, 2)
    with pytest.raises(ValueError, match="Bambu"):
        export_job(block_job, tmp_path / "without-backend", stack=stack)
    with pytest.raises(ValueError, match="tiles"):
        export_job(block_job, tmp_path / "non-tile", bambu=bambu, stack=stack)
    assert not (tmp_path / "without-backend").exists()
    assert not (tmp_path / "non-tile").exists()


def test_tile_only_catalogue_still_rejects_roof_support(bambu, tmp_path):
    settings = BambuSettings(bambu.materials, 0.4, 0.2, RoofSupportSettings(0.2, 2, 0))
    job = Job([tile_design(Tile())], BuildVolume(150, 150, 50), "catalogue")
    with pytest.raises(ValueError, match="tile-only part or layout"):
        export_job(job, tmp_path / "catalogue", bambu=settings)
    assert not (tmp_path / "catalogue").exists()


@pytest.mark.parametrize("gap", ["0", "-13", "nan", "inf"])
def test_auto_stack_invalid_gap_is_a_cli_error_not_traceback(tmp_path, capsys, gap):
    with pytest.raises(SystemExit) as error:
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
                str(tmp_path / "job"),
                "--bambu",
                "--nozzle-diameter-mm",
                ".4",
                "--layer-height-mm",
                ".2",
                "--material",
                "PETG",
                "PETG",
                "#778877",
                "--material",
                "PLA",
                "PLA",
                "#dddddd",
                "--stack-count",
                "auto",
                "--stack-gap-mm",
                gap,
                "--stack-interface-thickness-mm",
                ".2",
                "--stack-material-slots",
                "1",
                "1",
                "2",
            ]
        )
    assert error.value.code == 2
    assert "stack gap" in capsys.readouterr().err
    assert not (tmp_path / "job").exists()


def test_reference_report_refuses_existing_path(tmp_path, capsys):
    output = tmp_path / "reference-report.json"
    output.write_text("preserve me")
    with pytest.raises(SystemExit) as error:
        main(
            [
                "compare-reference",
                "--reference-file",
                str(tmp_path / "not-supplied.3mf"),
                "--output",
                str(output),
            ]
        )
    assert error.value.code == 2 and "already exists" in capsys.readouterr().err
    assert output.read_text() == "preserve me"


def distribution_checker():
    source = Path(__file__).resolve().parents[1] / "tools/check_distributions.py"
    spec = importlib.util.spec_from_file_location("distribution_check", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "path",
    [
        "",
        "outputs/private.3mf",
        ".venv/config",
        "cargo_grid/__pycache__/a.pyc",
        "uv.lock",
        "result.json",
        "../outside.py",
        "/outside.py",
        "cargo_grid/model.step",
    ],
)
def test_distribution_gate_rejects_runtime_and_generated_files(path):
    module = distribution_checker()
    with pytest.raises(ValueError):
        module.check_path(path)


def test_distribution_metadata_rejects_stale_readme():
    module = distribution_checker()
    text = (
        f"Name: cargo-grid\nVersion: {__version__}\nLicense-Expression: MIT\n"
        "Requires-Python: >=3.12\nDescription-Content-Type: text/markdown\n\nOld README"
    )
    with pytest.raises(ValueError, match="README does not match"):
        module.check_metadata(text, __version__)


def test_distribution_gate_rejects_stale_module_bytes(tmp_path):
    module = distribution_checker()
    metadata = (
        f"Name: cargo-grid\nVersion: {__version__}\nLicense-Expression: MIT\n"
        "Requires-Python: >=3.12\nDescription-Content-Type: text/markdown\n\n"
        + (module.ROOT / "README.md").read_text()
    )
    path = tmp_path / "stale.whl"
    prefix = f"cargo_grid-{__version__}.dist-info/"
    with ZipFile(path, "w") as archive:
        for source in (module.ROOT / "src/cargo_grid").rglob("*.py"):
            name = source.relative_to(module.ROOT / "src").as_posix()
            archive.writestr(
                name, b"# stale\n" if source.name == "_version.py" else source.read_bytes()
            )
        archive.writestr(prefix + "METADATA", metadata)
        archive.writestr(prefix + "WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr(prefix + "RECORD", "")
        archive.writestr(
            prefix + "entry_points.txt", "[console_scripts]\ncargo-grid = cargo_grid.cli:main\n"
        )
        archive.writestr(prefix + "licenses/LICENSE", (module.ROOT / "LICENSE").read_bytes())
    with pytest.raises(ValueError, match="module contents differ"):
        module.check_wheel(path, __version__)


def duration_checker():
    source = Path(__file__).resolve().parents[1] / "tools/check_test_durations.py"
    spec = importlib.util.spec_from_file_location("duration_check", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _junit_report(path, cases, session=100.0):
    suite = ET.Element("testsuite", time=str(session))
    for classname, name, seconds, skipped in cases:
        case = ET.SubElement(suite, "testcase", classname=classname, name=name, time=str(seconds))
        if skipped:
            ET.SubElement(case, "skipped")
    suites = ET.Element("testsuites")
    suites.append(suite)
    ET.ElementTree(suites).write(path)


@pytest.mark.parametrize(
    "seconds,allowlist,status,message",
    [
        (59.0, {}, 0, None),
        (61.0, {}, 1, "::error title=Test over duration budget::tests/test_ramp.py::test_x[1]"),
        (61.0, {"tests/test_ramp.py::test_x[1]": (90.0, "reviewed")}, 0, "[allowlisted]"),
        (91.0, {"tests/test_ramp.py::test_x[1]": (90.0, "reviewed")}, 1, "over its 90 s limit"),
    ],
)
def test_duration_budget_uses_node_ids_and_reviewed_limits(
    seconds, allowlist, status, message, tmp_path, monkeypatch, capsys
):
    module = duration_checker()
    report = tmp_path / "junit.xml"
    _junit_report(
        report,
        [
            ("tests.test_ramp", "test_x[1]@shared-plan", seconds, False),
            ("tests.test_ramp", "test_skipped", 500.0, True),
        ],
    )
    monkeypatch.setattr(module, "ALLOWLIST", allowlist)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(sys, "argv", ["check", str(report), "--root", str(root)])
    assert module.main() == status
    output = capsys.readouterr().out
    assert "test_skipped" not in output
    if message:
        assert message in output


def test_duration_budget_warns_for_long_sessions_and_stale_allowlist(tmp_path, monkeypatch, capsys):
    module = duration_checker()
    report = tmp_path / "junit.xml"
    _junit_report(report, [("tests.test_ramp", "test_x", 1.0, False)], session=900.0)
    monkeypatch.setattr(module, "ALLOWLIST", {"tests/test_ramp.py::gone": (90.0, "reviewed")})
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sys, "argv", ["check", str(report), "--target-seconds", "780"])
    assert module.main() == 0
    output = capsys.readouterr().out
    assert "::warning title=Portable test time::pytest session took 900 s" in output
    assert "::warning title=Stale duration allowlist entry::tests/test_ramp.py::gone" in output
