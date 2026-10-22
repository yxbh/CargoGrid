"""Artifact provenance, output comparisons and portable-budget contracts."""

import copy
import io
import json
from zipfile import ZipFile

import ci_tests
import pytest
from publish_guard import build, compare, manifest_changes, merge_fingerprints
from test_selection import FORMAT_VERSION, digest, environment, load_map, write_json


@pytest.fixture
def artifact_map(tmp_path):
    return {
        "version": FORMAT_VERSION,
        "commit": "a" * 40,
        "environment": {**environment(tmp_path), "lock": digest(b"locked")},
        "snapshot": {"files": {}, "python": {}, "config": {}},
        "tests": {},
        "fixtures": {},
        "ci": {"main_commit": "a" * 40},
    }


def test_artifact_discovery_accepts_only_successful_owned_workflow_runs(monkeypatch):
    artifacts = [
        {"id": number, "expired": number == 1, "workflow_run": {"id": number}}
        for number in range(1, 7)
    ]
    run = {
        "conclusion": "success",
        "path": ".github/workflows/ci.yml",
        "head_repository": {"full_name": "owner/repo"},
        "head_branch": "main",
        "event": "push",
    }
    runs = {
        2: {**run, "conclusion": "failure"},
        3: {**run, "head_repository": {"full_name": "fork/repo"}},
        4: {**run, "head_branch": "feature"},
        5: {**run, "path": ".github/workflows/other.yml"},
        6: run,
    }

    def api(path):
        return json.dumps(
            {"artifacts": artifacts} if "artifacts?" in path else runs[int(path.rsplit("/", 1)[1])]
        ).encode()

    monkeypatch.setattr(ci_tests, "api", api)
    assert [
        item["id"]
        for item in ci_tests.artifact_candidates("owner/repo", "test-map-main", main=True)
    ] == [6]


@pytest.mark.parametrize("corrupt", ["lock", "inventory", "schema"])
def test_artifacts_reject_mismatched_locks_inventory_and_schema(
    tmp_path, monkeypatch, artifact_map, corrupt
):
    archive = io.BytesIO()
    mapping = copy.deepcopy(artifact_map)
    if corrupt == "schema":
        mapping["tests"] = []
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("map.json", json.dumps(mapping))
        zipped.writestr("uv.lock", b"wrong" if corrupt == "lock" else b"locked")
        if corrupt == "inventory":
            zipped.writestr("../unexpected", "data")
    monkeypatch.setattr(ci_tests, "api", lambda path: archive.getvalue())
    with pytest.raises(ValueError, match="lock|inventory|map"):
        ci_tests.download_map("owner/repo", {"id": 1}, tmp_path)


def test_incremental_fetch_requires_same_pr_base_main_environment_and_ancestor(
    tmp_path, monkeypatch, artifact_map
):
    main = artifact_map
    overlay = {
        **main,
        "commit": "merge",
        "ci": {"pr": 7, "base": "base", "head": "previous", "main_commit": main["commit"]},
    }
    candidates = {
        True: [{"id": 1, "run": {"head_sha": main["commit"]}}],
        False: [
            {
                "id": 2,
                "run": {
                    "head_sha": "previous",
                    "head_branch": "feature",
                    "pull_requests": [],
                },
            }
        ],
    }
    monkeypatch.setattr(
        ci_tests, "artifact_candidates", lambda repo, name, main: iter(candidates[main])
    )
    monkeypatch.setattr(ci_tests, "snapshot", lambda root: main["snapshot"])
    monkeypatch.setattr(
        ci_tests,
        "api",
        lambda path: json.dumps(
            {"head": {"ref": "feature", "repo": {"full_name": "owner/repo"}}}
        ).encode(),
    )
    monkeypatch.setattr(ci_tests, "ancestor", lambda root, old, new: old != "unrelated")

    def download(repo, artifact, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "uv.lock").write_bytes(b"locked")
        return copy.deepcopy(main if artifact["id"] == 1 else overlay)

    monkeypatch.setattr(ci_tests, "download_map", download)
    destination = tmp_path / "baseline"

    def fetch():
        ci_tests.fetch_baseline(
            tmp_path, destination, repo="owner/repo", pr=7, base="base", head="current"
        )
        return json.loads((destination / "map.json").read_text())

    assert fetch()["commit"] == "merge"
    for field, value in (
        ("pr", 8),
        ("base", "different"),
        ("head", "unrelated"),
        ("main_commit", "different"),
    ):
        original = overlay["ci"][field]
        overlay["ci"][field] = value
        assert fetch()["commit"] == main["commit"]
        overlay["ci"][field] = original
    overlay["environment"] = {**main["environment"], "python": [3, 12, 1]}
    assert fetch()["commit"] == main["commit"]


def test_missing_or_rejected_artifact_cannot_leave_a_stale_map(tmp_path, monkeypatch, artifact_map):
    destination = tmp_path / "baseline"
    write_json(destination / "map.json", artifact_map)
    monkeypatch.setattr(ci_tests, "artifact_candidates", lambda *args, **kwargs: iter(()))
    ci_tests.fetch_baseline(tmp_path, destination, repo="owner/repo")
    mapping, reason = load_map(destination / "map.json")
    assert mapping is None and "cannot use" in reason
    assert not json.loads((destination / "fetch.json").read_text())["pinned"]


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
    tmp_path, artifact_map
):
    part = {
        "commit": artifact_map["commit"],
        "environment": artifact_map["environment"],
        "snapshot": artifact_map["snapshot"],
        "functions": {"src/example.py": ["build"]},
        "fingerprints": {
            f"variant-{index}{suffix}": "before"
            for index in range(24)
            for suffix in (".3mf", ".manifest.json")
        },
    }
    path, report = tmp_path / "fingerprints.json", tmp_path / "report.json"
    write_json(path, part)
    baseline = merge_fingerprints([path], None, "", report)
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
    with pytest.raises(ValueError, match="all 24"):
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
        "sys.argv",
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
