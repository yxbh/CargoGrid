"""Selection contracts use tiny source trees, not CargoGrid geometry."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_selection import (
    FORMAT_VERSION,
    analyse,
    benchmark_snapshot,
    dependencies,
    load_map,
    merge_parts,
    select,
    shards,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    "src/example/__init__.py": "from .body import VALUE as PUBLIC\n",
    "src/example/body.py": """from functools import lru_cache
VALUE = 10
DERIVED = VALUE + 1
class Settings:
    height: int = VALUE
@lru_cache
def cached():
    return DERIVED
def body():
    return cached()
def unrelated():
    return 42
""",
    "tests/test_body.py": """from example import PUBLIC as ALIAS
from example.body import body, Settings
def test_body():
    assert body()
def test_alias():
    assert ALIAS
def test_settings():
    assert Settings()
""",
    "README.md": "A document.\n",
}


def state(sources):
    return {
        "files": {path: source for path, source in sources.items()},
        "python": {
            path: analyse(source, path) for path, source in sources.items() if path.endswith(".py")
        },
        "config": {},
    }


def record(functions, *, fixtures=(), data=(), process=False, outcome="passed"):
    return {
        "functions": functions,
        "fixtures": list(fixtures),
        "data": list(data),
        "process": process,
        "outcome": outcome,
        "duration": 1.0,
    }


@pytest.fixture
def mapping():
    return {
        "version": FORMAT_VERSION,
        "commit": "base",
        "environment": {"packages": {"coverage": "7"}},
        "snapshot": state(SOURCES),
        "fixtures": {
            "shared": record({"src/example/body.py": ["body"]}),
        },
        "tests": {
            "tests/contracts/test_always.py::test_contract": record({}),
            "tests/test_body.py::test_body": record(
                {"src/example/body.py": ["body", "cached"]},
            ),
            "tests/test_body.py::test_alias": record({"tests/test_body.py": ["test_alias"]}),
            "tests/test_body.py::test_settings": record({"tests/test_body.py": ["test_settings"]}),
            "tests/test_peer.py::test_first": record(
                {"src/example/body.py": ["cached"]}, fixtures=["shared"]
            ),
            "tests/test_peer.py::test_second": record({}, fixtures=["shared"], data=["README.md"]),
            "tests/test_unrelated.py::test_other": record({"src/example/body.py": ["unrelated"]}),
            "tests/test_process.py::test_child": record({}, process=True),
            "tests/test_failed.py::test_failed": record({}, outcome="failed"),
        },
    }


def selection(mapping, sources, extra=(), **kwargs):
    collection = [{"nodeid": node, "slow": False} for node in [*mapping["tests"], *extra]]
    return select(mapping, state(sources), collection, **kwargs)


def test_changed_function_selects_users_shared_fixture_peers_failures_and_unknowns(mapping):
    sources = {
        **SOURCES,
        "src/example/body.py": SOURCES["src/example/body.py"].replace(
            "return DERIVED", "return DERIVED + 1"
        ),
    }
    result = selection(mapping, sources, ["tests/test_new.py::test_new"])
    assert result["mode"] == "selected"
    assert set(result["selected"]) == {
        "tests/contracts/test_always.py::test_contract",
        "tests/test_body.py::test_body",
        "tests/test_peer.py::test_first",
        "tests/test_peer.py::test_second",
        "tests/test_process.py::test_child",
        "tests/test_failed.py::test_failed",
        "tests/test_new.py::test_new",
    }
    assert result["publish_guard"]


@pytest.mark.parametrize(
    "before,after,expected",
    [
        ("VALUE = 10", "VALUE = 12", "tests/test_body.py::test_alias"),
        ("height: int = VALUE", "height: int = 20", "tests/test_body.py::test_settings"),
        ("def cached():", "def cached(value=1):", "tests/test_body.py::test_body"),
        ("@lru_cache", "@lru_cache(maxsize=10)", "tests/test_body.py::test_body"),
        (
            "def body():\n    return cached()",
            "def body():\n    return added()\ndef added():\n    return 3",
            "tests/test_body.py::test_body",
        ),
        ("def cached():\n    return DERIVED", "", "tests/test_body.py::test_body"),
    ],
)
def test_constants_aliases_fields_defaults_decorators_new_and_deleted_functions(
    mapping, before, after, expected
):
    sources = {
        **SOURCES,
        "src/example/body.py": SOURCES["src/example/body.py"].replace(before, after),
    }
    assert expected in selection(mapping, sources)["selected"]


def test_doc_data_and_test_changes_do_not_select_the_publish_guard(mapping):
    docs = selection(mapping, {**SOURCES, "README.md": "Changed document.\n"})
    assert "tests/test_peer.py::test_second" in docs["selected"]
    assert not docs["publish_guard"]
    sources = {
        **SOURCES,
        "tests/test_body.py": SOURCES["tests/test_body.py"].replace(
            "assert ALIAS", "assert ALIAS > 0"
        ),
    }
    assert "tests/test_body.py::test_alias" in selection(mapping, sources)["selected"]
    assert not selection(mapping, sources)["publish_guard"]


def test_comments_and_formatting_do_not_change_function_dependencies(mapping):
    sources = {
        **SOURCES,
        "src/example/body.py": "# comment\n"
        + SOURCES["src/example/body.py"].replace("return 42", "return (42)"),
    }
    result = selection(mapping, sources)
    assert "tests/test_unrelated.py::test_other" not in result["selected"]


@pytest.mark.parametrize(
    "path",
    [
        "tests/conftest.py",
        "tests/nested/conftest.py",
        ".github/workflows/ci.yml",
        "tools/test_selection.py",
        "tests/selection.py",
        "tools/ci_tests.py",
        "tools/publish_guard.py",
        "src/example/input.json",
    ],
)
def test_infrastructure_and_package_data_force_full(mapping, path):
    result = selection(mapping, {**SOURCES, path: ""})
    assert result["mode"] == "full"
    assert set(result["selected"]) == set(mapping["tests"])
    assert result["publish_guard"]


def test_environment_configuration_and_missing_map_force_full(mapping):
    collection = [{"nodeid": node, "slow": False} for node in mapping["tests"]]
    assert select(None, state(SOURCES), collection)["mode"] == "full"
    assert selection(mapping, SOURCES, current_environment={"packages": {}})["mode"] == "full"
    current = state(SOURCES)
    current["config"] = {"project": {"dependencies": ["new-package"]}}
    assert select(mapping, current, collection)["mode"] == "full"


def test_unresolved_side_effects_are_not_silently_deselected(mapping):
    sources = {**SOURCES, "src/example/body.py": SOURCES["src/example/body.py"] + "\nregister()\n"}
    assert selection(mapping, sources)["mode"] == "full"


@pytest.mark.parametrize("import_path", ["tests.helpers", "helpers"])
def test_test_helper_constants_follow_package_and_pythonpath_imports(mapping, import_path):
    sources = {
        **SOURCES,
        "tests/helpers.py": "LIMIT = 10\n",
        "tests/test_body.py": f"from {import_path} import LIMIT as bound\n"
        "def test_alias():\n    assert bound\n",
    }
    mapping["snapshot"] = state(sources)
    latest = {**sources, "tests/helpers.py": "LIMIT = 11\n"}
    assert "tests/test_body.py::test_alias" in selection(mapping, latest)["selected"]


def test_decorator_replacement_selects_static_callers_even_without_original_body_coverage(mapping):
    mapping["tests"]["tests/test_body.py::test_body"]["functions"] = {
        "tests/test_body.py": ["test_body"]
    }
    sources = {
        **SOURCES,
        "src/example/body.py": SOURCES["src/example/body.py"].replace(
            "def body():", "@replacement\ndef body():"
        ),
    }
    assert "tests/test_body.py::test_body" in selection(mapping, sources)["selected"]


def test_a_body_change_does_not_select_unrelated_branches_of_a_shared_factory(mapping):
    original = {
        **SOURCES,
        "src/example/body.py": SOURCES["src/example/body.py"]
        + "\ndef factory(kind):\n    return body() if kind == 'body' else unrelated()\n",
    }
    mapping["snapshot"] = state(original)
    mapping["tests"]["tests/test_unrelated.py::test_other"]["functions"][
        "src/example/body.py"
    ].append("factory")
    current = {
        **original,
        "src/example/body.py": original["src/example/body.py"].replace(
            "return cached()", "return cached() + 1"
        ),
    }
    result = selection(mapping, current)
    assert "tests/test_body.py::test_body" in result["selected"]
    assert "tests/test_unrelated.py::test_other" not in result["selected"]


def test_definition_constants_reach_callers_when_a_decorator_replaces_the_original_body(mapping):
    original = {
        **SOURCES,
        "src/example/body.py": SOURCES["src/example/body.py"].replace(
            "def body():", "@replacement(VALUE)\ndef body():"
        ),
    }
    mapping["snapshot"] = state(original)
    mapping["tests"]["tests/test_body.py::test_body"]["functions"] = {
        "tests/test_body.py": ["test_body"]
    }
    current = {
        **original,
        "src/example/body.py": original["src/example/body.py"].replace("VALUE = 10", "VALUE = 11"),
    }
    assert "tests/test_body.py::test_body" in selection(mapping, current)["selected"]


def test_property_getter_and_setter_both_contribute_to_the_function_digest():
    source = (
        "class Value:\n"
        "    @property\n    def data(self):\n        return 1\n"
        "    @data.setter\n    def data(self, value):\n        self._data = value\n"
    )
    before = analyse(source, "src/example.py")
    after = analyse(source.replace("return 1", "return 2"), "src/example.py")
    assert before["functions"]["Value.data"]["digest"] != after["functions"]["Value.data"]["digest"]


def test_repeated_module_side_effects_are_not_deduplicated(mapping):
    original = {**SOURCES, "src/registry.py": "register()\n"}
    mapping["snapshot"] = state(original)
    current = {**original, "src/registry.py": "register()\nregister()\n"}
    assert selection(mapping, current)["mode"] == "full"


def test_measurements_need_an_exact_baseline_and_do_not_mutate_it(mapping):
    original = state({**SOURCES, "src/cargo_grid/cli.py": "def parser():\n    return 1\n"})
    mapping["snapshot"] = original
    changed = benchmark_snapshot(mapping, original, mapping["environment"], "cli")
    assert changed != original
    assert original == state({**SOURCES, "src/cargo_grid/cli.py": "def parser():\n    return 1\n"})
    assert (
        changed["python"]["src/cargo_grid/cli.py"]["functions"]["parser"]["digest"]
        != original["python"]["src/cargo_grid/cli.py"]["functions"]["parser"]["digest"]
    )
    with pytest.raises(ValueError, match="exact current tree"):
        benchmark_snapshot(mapping, changed, mapping["environment"], "cli")
    with pytest.raises(ValueError, match="environment"):
        benchmark_snapshot(mapping, original, {}, "cli")


def test_shared_lazy_fixture_dependencies_include_transitive_peers_and_data(mapping):
    mapping["tests"]["tests/test_peer.py::test_first"]["fixtures"].append("another")
    mapping["tests"]["tests/test_last.py::test_last"] = record(
        {"src/example/body.py": ["unrelated"]}, fixtures=["another"], process=True
    )
    records = dependencies(mapping)
    for node in (
        "tests/test_peer.py::test_first",
        "tests/test_peer.py::test_second",
        "tests/test_last.py::test_last",
    ):
        assert "unrelated" in records[node]["functions"]["src/example/body.py"]
        assert "README.md" in records[node]["data"]
        assert records[node]["process"]


def test_published_functions_use_their_own_snapshot_until_refreshed(mapping):
    mapping["publish"] = {
        "snapshot": state(SOURCES),
        "functions": {"src/example/body.py": ["body"]},
    }
    sources = {
        **SOURCES,
        "src/example/body.py": SOURCES["src/example/body.py"].replace("return 42", "return 43"),
    }
    assert not selection(mapping, sources)["publish_guard"]
    earlier = {
        **SOURCES,
        "src/example/body.py": SOURCES["src/example/body.py"].replace(
            "return cached()", "return later()\ndef later():\n    return 8"
        ),
    }
    mapping["snapshot"] = state(earlier)
    latest = {
        **earlier,
        "src/example/body.py": earlier["src/example/body.py"].replace("return 8", "return 9"),
    }
    assert selection(mapping, latest)["publish_guard"]
    assert not selection(mapping, {**earlier, "README.md": "words"})["publish_guard"]


def test_map_corruption_is_reported_and_requests_a_full_run(tmp_path):
    path = tmp_path / "map.json"
    for content in ("not json", "{}", '{"version": 100}'):
        path.write_text(content)
        mapping, error = load_map(path)
        assert mapping is None and "cannot use dependency map" in error


def test_complete_duration_shards_are_deterministic_and_keep_files_together(mapping):
    collection = [{"nodeid": n, "slow": False} for n in mapping["tests"]]
    plan = {"selected": sorted(mapping["tests"])}
    parts = shards(plan, collection, mapping, count=3)
    assert parts == shards(plan, list(reversed(collection)), mapping, count=3)
    assert set.union(*map(set, parts)) == set(mapping["tests"])
    assert sum(map(len, parts)) == len(mapping["tests"])
    assert sum(any(n.startswith("tests/test_body.py::") for n in part) for part in parts) == 1


def test_merge_rejects_missing_duplicate_stale_and_wrong_environment_parts(tmp_path, mapping):
    plan = {
        "commit": "head",
        "environment": mapping["environment"],
        "snapshot": mapping["snapshot"],
        "selected": sorted(mapping["tests"]),
        "collection": [{"nodeid": n} for n in mapping["tests"]],
    }
    part = {
        "version": FORMAT_VERSION,
        "commit": "head",
        "environment": mapping["environment"],
        "tests": mapping["tests"],
        "fixtures": mapping["fixtures"],
    }
    path = tmp_path / "part.json"
    path.write_text(json.dumps(part))
    assert set(merge_parts([path], plan)["tests"]) == set(mapping["tests"])
    with pytest.raises(ValueError, match="more than one"):
        merge_parts([path, path], plan)
    with pytest.raises(ValueError, match="measurement-only"):
        merge_parts([path], {**plan, "measurement_only": "docs"})
    with pytest.raises(ValueError, match="exactly"):
        merge_parts([], plan)
    for key, value in (("commit", "old"), ("environment", {}), ("version", 100)):
        changed = {**part, key: value}
        path.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="incompatible|another commit"):
            merge_parts([path], plan)


@pytest.fixture(scope="module")
def recorded_example(tmp_path_factory):
    root = tmp_path_factory.mktemp("recorded-selection")
    (root / "src/example").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\npythonpath=src\n")
    (root / "src/example/__init__.py").write_text(
        "from functools import lru_cache\n"
        "@lru_cache\ndef cached():\n    return 10\n"
        "def lazy():\n    return 20\n"
        "def setup():\n    return 30\n"
    )
    (root / "README.md").write_text("tracked input")
    (root / "tests/test_example.py").write_text(
        """import subprocess
import sys
from pathlib import Path
import pytest
from example import cached, lazy, setup

pytestmark = pytest.mark.xdist_group("shared")
@pytest.fixture(scope="module")
def shared():
    return {"setup": setup()}

def test_first(shared):
    assert cached() == 10
    shared["lazy"] = lazy()
    assert Path("README.md").read_text() == "tracked input"
    subprocess.run([sys.executable, "-c", "pass"], check=True)

def test_second(shared):
    assert cached() == 10
    assert shared["setup"] == 30
"""
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Input",
        ],
        check=True,
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "selection",
            "--rootdir",
            str(root),
            "-c",
            str(root / "pytest.ini"),
            "-n",
            "2",
            "--dist",
            "loadgroup",
            "--record-test-map",
            str(root / "parts"),
        ],
        cwd=root,
        env={
            **os.environ,
            "PYTEST_ADDOPTS": "",
            "PYTHONPATH": os.pathsep.join([str(ROOT / "tests"), str(ROOT / "tools")]),
        },
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    parts = [json.loads(path.read_text()) for path in (root / "parts").glob("part-*.json")]
    assert len(parts) == 2
    combined = {"tests": {}, "fixtures": {}}
    for part in parts:
        combined["tests"].update(part["tests"])
        combined["fixtures"].update(part["fixtures"])
    return combined


def test_recorder_credits_cached_functions_to_every_user_and_shared_setup(recorded_example):
    first = recorded_example["tests"]["tests/test_example.py::test_first"]
    second = recorded_example["tests"]["tests/test_example.py::test_second"]
    assert "cached" in first["functions"]["src/example/__init__.py"]
    assert "cached" in second["functions"]["src/example/__init__.py"]
    assert first["process"]
    assert not second["process"]
    for node, dependencies_for_test in dependencies(recorded_example).items():
        assert {"cached", "lazy", "setup"} <= dependencies_for_test["functions"][
            "src/example/__init__.py"
        ]
        assert "README.md" in dependencies_for_test["data"]
        assert dependencies_for_test["process"]
        assert recorded_example["tests"][node]["outcome"] == "passed"


def test_recorder_stores_only_project_relative_paths(recorded_example):
    for record in [*recorded_example["tests"].values(), *recorded_example["fixtures"].values()]:
        assert all(
            not Path(p).is_absolute() and ".." not in Path(p).parts for p in record["functions"]
        )
    assert all(record["duration"] > 0 for record in recorded_example["tests"].values())


def test_ast_analysis_distinguishes_nested_bodies_and_ignores_class_docstrings():
    before = analyse(
        "class A:\n    'words'\n    def f(self):\n        def inner():\n            return 1\n        return inner()\n",
        "src/example/body.py",
    )
    assert {"A.f", "A.f.inner"} == before["functions"].keys()
    after = analyse(
        "class A:\n    'other words'\n    def f(self):\n        def inner():\n            return 1\n        return inner()\n",
        "src/example/body.py",
    )
    assert before == after
