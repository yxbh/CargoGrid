"""Cache-scoped planning, output comparisons and portable-budget contracts."""

import copy
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile, ZipInfo

import publish_guard
import pytest
import test_selection as selection
from publish_guard import build, compare, manifest_changes, merge_fingerprints
from test_selection import FORMAT_VERSION, digest, environment, load_map, write_json


@pytest.fixture
def cached_mapping(tmp_path):
    return {
        "version": FORMAT_VERSION,
        "commit": "a" * 40,
        "environment": {**environment(tmp_path), "lock": digest(b"locked")},
        "snapshot": {"files": {}, "python": {}, "config": {}},
        "tests": {},
        "fixtures": {},
    }


@pytest.mark.parametrize("kind", ["contracts", "mixed", "geometry", "empty", "weekly"])
def test_ci_planning_routes_only_complete_contract_selections_into_checks(
    tmp_path, monkeypatch, cached_mapping, kind
):
    nodes = [
        "tests/contracts/test_example.py::test_first",
        "tests/contracts/test_example.py::test_second",
        "tests/test_geometry.py::test_geometry",
    ]
    selected = {
        "contracts": nodes[:2],
        "mixed": nodes,
        "geometry": nodes[2:],
        "empty": [],
        "weekly": nodes,
    }[kind]
    collection = [{"nodeid": node, "slow": False} for node in nodes]
    plan = {
        **copy.deepcopy(cached_mapping),
        "mode": "full" if kind == "weekly" else "selected",
        "selected": selected,
        "changed": [],
        "publish_guard": False,
        "collection": collection,
        "shards": [[node] for node in selected],
    }
    calls = []
    monkeypatch.setattr(selection, "ROOT", tmp_path)
    monkeypatch.setattr(selection, "load_map", lambda path: (cached_mapping, None))
    monkeypatch.setattr(selection, "collect", lambda *args: calls.append("collect") or collection)

    def make_plan(root, baseline, actual_collection, **kwargs):
        assert calls == ["collect"] and actual_collection == collection
        calls.append("plan")
        return copy.deepcopy(plan)

    monkeypatch.setattr(selection, "make_plan", make_plan)
    monkeypatch.setitem(sys.modules, "makerworld_exports", None)
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    (tmp_path / "uv.lock").write_bytes(b"locked")
    args = SimpleNamespace(
        mode="weekly" if kind == "weekly" else "selected",
        baseline=tmp_path / "baseline",
        output=tmp_path / "plan",
    )
    selection.ci_plan(args)
    result = json.loads((args.output / "plan.json").read_text())
    outputs = dict(line.split("=", 1) for line in github_output.read_text().splitlines())
    assert result["selected"] == selected and result["collection"] == collection
    assert result["contracts_only"] == (kind == "contracts")
    assert outputs["contracts_only"] == str(kind == "contracts").lower()
    assert result["shards"] == ([selected] if kind == "contracts" else plan["shards"])
    assert result["weekly"] == (kind == "weekly")
    assert (args.output / "uv.lock").read_bytes() == b"locked"


def test_ci_completion_modules_import_without_cad_or_site_packages():
    tools = str(Path(selection.__file__).parent)
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            f"import sys; sys.path.insert(0, {tools!r}); "
            "import test_selection, publish_guard; "
            "assert not any(n == 'build123d' or n.startswith('OCP') for n in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("damage", ["missing", "version", "lock"])
def test_cached_maps_reject_missing_files_wrong_versions_and_lock_drift(
    tmp_path, cached_mapping, damage
):
    if damage != "missing":
        mapping = {**cached_mapping, "version": 0} if damage == "version" else cached_mapping
        write_json(tmp_path / "map.json", mapping)
        (tmp_path / "uv.lock").write_bytes(b"wrong" if damage == "lock" else b"locked")
    mapping, error = selection.cached_map(tmp_path)
    assert mapping is None and "cannot use" in error


def test_cached_baseline_keeps_the_same_repo_guard_and_requires_main_lock_and_environment(
    tmp_path, monkeypatch, cached_mapping
):
    main, overlay = copy.deepcopy(cached_mapping), copy.deepcopy(cached_mapping)
    overlay["commit"] = "previous-pr-head"
    args = SimpleNamespace(
        main=tmp_path / "main",
        pr=tmp_path / "pr",
        output=tmp_path / "baseline",
        same_repo="true",
    )
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setattr(selection, "snapshot", lambda root: main["snapshot"])
    for folder, mapping in ((args.main, main), (args.pr, overlay)):
        write_json(folder / "map.json", mapping)
        (folder / "uv.lock").write_bytes(b"locked")

    def choose():
        selection.ci_baseline(args)
        assert (args.output / "uv.lock").read_bytes() == b"locked"
        return json.loads((args.output / "map.json").read_text())

    assert choose()["commit"] == overlay["commit"]
    args.same_repo = "false"
    assert choose()["commit"] == main["commit"]
    args.same_repo = "true"
    changed = {**overlay, "environment": {**overlay["environment"], "python": [3, 12, 0]}}
    write_json(args.pr / "map.json", changed)
    assert choose()["commit"] == main["commit"]
    write_json(args.pr / "map.json", overlay)
    (args.pr / "uv.lock").write_bytes(b"wrong")
    assert choose()["commit"] == main["commit"]
    args.main = tmp_path / "missing-main"
    selection.ci_baseline(args)
    mapping, error = load_map(args.output / "map.json")
    assert mapping is None and error
    assert "pinned=false" in (tmp_path / "outputs").read_text().splitlines()[-2:]


def test_dependency_configuration_change_does_not_pin_the_cached_lock(
    tmp_path, monkeypatch, cached_mapping
):
    write_json(tmp_path / "main/map.json", cached_mapping)
    (tmp_path / "main/uv.lock").write_bytes(b"locked")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setattr(selection, "snapshot", lambda root: {"config": {"project": "changed"}})
    selection.ci_baseline(
        SimpleNamespace(
            main=tmp_path / "main",
            pr=tmp_path / "pr",
            output=tmp_path / "baseline",
            same_repo="false",
        )
    )
    assert "pinned=false" in (tmp_path / "outputs").read_text().splitlines()


def test_publish_comparison_keeps_zip_metadata_and_manifest_fields_visible(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    for folder, value in ((old, "before"), (new, "after")):
        with ZipFile(folder / "sample.3mf", "w") as zipped:
            zipped.writestr("3D/model", value)
        (folder / "sample.manifest.json").write_text(json.dumps({"designs": [{"fit": value}]}))
    changes = compare(old, new)
    assert changes[0]["changed_members"] == ["3D/model"]
    assert changes[1]["fields"] == [
        {"path": "designs[0].fit", "before": "before", "after": "after"}
    ]
    assert list(manifest_changes({"a": None}, {"b": 1})) == [
        {"path": "a", "before": None, "after_absent": True},
        {"path": "b", "before_absent": True, "after": 1},
    ]
    (new / "sample.3mf").write_bytes((old / "sample.3mf").read_bytes())
    with ZipFile(new / "sample.3mf", "a") as zipped:
        zipped.comment = b"changed ZIP metadata"
    assert compare(old, new)[0]["zip_metadata_only"]
    assert list(manifest_changes(True, 1)) == [{"path": "", "before": True, "after": 1}]


def test_publish_report_names_mesh_metadata_and_placement_changes(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    for folder, x, setting in ((old, 0, "1"), (new, 1, "2")):
        with ZipFile(folder / "sample.3mf", "w") as archive:
            archive.writestr(
                "3D/3dmodel.model",
                '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                f'<resources><object id="1"><mesh><vertices><vertex x="{x}" y="0" z="0"/>'
                "</vertices></mesh></object></resources>"
                f'<build><item objectid="1" transform="{x}"/></build></model>',
            )
            archive.writestr(
                "Metadata/model_settings.config",
                '<config><object id="1"><metadata key="name" value="Named part"/>'
                f'<metadata key="extruder" value="{setting}"/></object></config>',
            )
    changed = compare(old, new)[0]["changed_objects"]
    assert {entry["id"] for entry in changed} == {"object:1", "placement:0"}
    assert all(entry["after"]["name"] == "Named part" for entry in changed)


def test_weekly_parts_need_complete_inventory_and_exact_reviewed_fingerprints(
    tmp_path, cached_mapping
):
    part = {
        "commit": cached_mapping["commit"],
        "environment": cached_mapping["environment"],
        "fingerprints": {
            f"variant-{index}{suffix}": "before"
            for index in range(publish_guard.PUBLISHED_PAIRS)
            for suffix in (".3mf", ".manifest.json")
        },
    }
    path, report = tmp_path / "fingerprints.json", tmp_path / "report.json"
    write_json(path, part)
    with pytest.raises(ValueError, match="fingerprints changed"):
        merge_fingerprints([path], None, "", report)
    approval = json.loads(report.read_text())["approval_digest"]
    baseline = merge_fingerprints([path], None, approval, report)
    part["fingerprints"]["variant-0.3mf"] = "after"
    write_json(path, part)
    with pytest.raises(ValueError, match="fingerprints changed"):
        merge_fingerprints([path], baseline, "", report)
    approval = json.loads(report.read_text())["approval_digest"]
    assert (
        merge_fingerprints([path], baseline, approval, report)["fingerprints"]
        == part["fingerprints"]
    )
    with pytest.raises(ValueError, match="duplicate"):
        merge_fingerprints([path, path], None, "", report)
    with pytest.raises(ValueError, match=f"all {publish_guard.PUBLISHED_PAIRS}"):
        merge_fingerprints([], None, "", report)


def test_publish_source_isolation_uses_each_archived_source_not_the_editable_install(tmp_path):
    for label in ("baseline", "candidate"):
        source = tmp_path / label
        (source / "src/cargo_grid").mkdir(parents=True)
        (source / "tools").mkdir()
        (source / "src/cargo_grid/__init__.py").write_text(f"PROVENANCE = {label!r}\n")
        (source / "tools/makerworld_exports.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "from cargo_grid import PROVENANCE\n"
            "output = Path(sys.argv[sys.argv.index('--output') + 1])\n"
            "(output / 'source.txt').write_text(PROVENANCE)\n"
        )
        output = tmp_path / f"{label}-output"
        build(source, output, 1)
        assert (output / "source.txt").read_text() == label


@pytest.mark.parametrize("environment_changed", [False, True])
def test_publish_guard_snapshots_both_commits_and_rejects_dependency_drift(
    tmp_path, monkeypatch, environment_changed
):
    source = tmp_path / "source.py"
    source.write_text("committed")
    state = {"head": "head-before", "built": 0}

    def git(root, *arguments):
        if arguments[0] == "rev-parse":
            return state["head"].encode()
        if arguments[0] == "diff":
            return b"source.py" if state["built"] else b""
        if arguments[0] == "merge-base":
            return b"base"
        assert arguments[0] == "archive"
        assert arguments[1] in {"base", "head-before"}
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as packed:
            member = tarfile.TarInfo("source.py")
            member.size = len(b"committed")
            packed.addfile(member, io.BytesIO(b"committed"))
        return archive.getvalue()

    def build(source, output, jobs):
        output.mkdir()
        content = (source / "source.py").read_text()
        for index in range(24):
            with ZipFile(output / f"variant-{index}.3mf", "w") as archive:
                archive.writestr(ZipInfo("payload"), content)
            write_json(output / f"variant-{index}.manifest.json", {"source": content})
        state["built"] += 1
        (tmp_path / "source.py").write_text("edits during the build")
        state["head"] = "head-after"

    monkeypatch.setattr(publish_guard, "ROOT", tmp_path)
    monkeypatch.setattr(publish_guard, "git", git)
    monkeypatch.setattr(publish_guard, "build", build)
    # Each archived source declares its own inventory; these stand-ins declare 24.
    monkeypatch.setattr(publish_guard, "declared_pairs", lambda source: 24)
    monkeypatch.setattr(
        publish_guard,
        "environment",
        lambda root: {"changed": environment_changed and state["built"] == 2},
    )
    output = tmp_path / "report"
    arguments = SimpleNamespace(base="origin/main", output=output, jobs=1)
    if environment_changed:
        with pytest.raises(ValueError, match="dependencies changed"):
            publish_guard.guard(arguments)
        assert not (output / "report.json").exists()
    else:
        assert publish_guard.guard(arguments) == 0
        report = json.loads((output / "report.json").read_text())
        assert report["head"] == "head-before"
        assert report["base"] == "base"
        assert not report["changes"]
    with pytest.raises(ValueError, match="commit source changes"):
        publish_guard.guard(arguments)
    assert state["built"] == 2


def test_duration_gate_applies_to_portable_cases_without_reclassifying_slow_cases(
    tmp_path, monkeypatch
):
    from check_test_durations import main

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_part.py").touch()
    report, plan = tmp_path / "report.xml", tmp_path / "plan.json"
    write_json(
        plan,
        {
            "collection": [
                {"nodeid": "tests/test_part.py::test_portable", "slow": False},
                {"nodeid": "tests/test_part.py::test_slow", "slow": True},
            ],
            "selected": ["tests/test_part.py::test_portable", "tests/test_part.py::test_slow"],
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_test_durations.py",
            str(report),
            "--root",
            str(tmp_path),
            "--selection-plan",
            str(plan),
        ],
    )
    for portable, expected in ((130, 1), (10, 0)):
        report.write_text(
            '<testsuites><testsuite time="500">'
            f'<testcase classname="tests.test_part" name="test_portable" time="{portable}"/>'
            '<testcase classname="tests.test_part" name="test_slow" time="900"/>'
            "</testsuite></testsuites>"
        )
        assert main() == expected
