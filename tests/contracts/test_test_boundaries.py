"""The default collection is small; the full command retains all portable contracts."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _collect(*selections):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import contextlib
import io
import json
import sys

import pytest


class Collection:
    def pytest_collection_finish(self, session):
        self.nodes = [item.nodeid for item in session.items]


collections = []
for arguments in json.loads(sys.argv[1]):
    collector = Collection()
    with contextlib.redirect_stdout(io.StringIO()) as output:
        result = pytest.main(["--collect-only", "-q", *arguments], plugins=[collector])
    if result:
        sys.stderr.write(output.getvalue())
        sys.exit(result)
    collections.append(collector.nodes)
print(json.dumps(collections))
""",
            json.dumps(selections),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_default_and_full_portable_collections_have_distinct_complete_boundaries():
    default, complete = _collect((), ("tests", "-m", "not native"))
    assert default and complete
    assert len(default) == len(set(default))
    assert len(complete) == len(set(complete))
    assert all(node.startswith("tests/contracts/") for node in default)
    assert set(default) < set(complete)
    assert any(node.startswith("tests/test_geometry.py::") for node in complete)
    assert any(node.startswith("tests/test_adjustable_stop.py::") for node in complete)
    assert any(node.startswith("tests/test_zeekr_contours.py::") for node in complete)


def test_planned_shards_cover_every_collected_test_once():
    from test_selection import shards

    complete = set(_collect(("tests", "-m", "not native"))[0])
    collection = [{"nodeid": node, "slow": False} for node in complete]
    plan = {"selected": sorted(complete)}
    parts = list(map(set, shards(plan, collection, None, count=4)))
    assert all(parts)
    assert sum(map(len, parts)) == len(set.union(*parts))
    assert set.union(*parts) == complete
    paths = {node.split("::", 1)[0] for node in complete}
    for path in paths:
        assert sum(any(node.startswith(path + "::") for node in part) for part in parts) == 1
    assert shards(plan, list(reversed(collection)), None, count=4) == [
        sorted(part) for part in parts
    ]
    new = {"nodeid": "tests/test_new_slow_family.py::test_new", "slow": True}
    extended = shards(
        {"selected": [*plan["selected"], new["nodeid"]]}, [*collection, new], None, count=4
    )
    assert set.union(*map(set, extended)) == complete | {new["nodeid"]}


def test_grouped_workers_construct_a_shared_plan_once(tmp_path):
    module = tmp_path / "test_shared_plan.py"
    module.write_text(
        """
import os
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def expansion_set_job():
    Path(__file__).with_name(f"built-{os.getpid()}").touch()
    return "shared"


@pytest.mark.parametrize("case", range(6))
def test_plan(expansion_set_job, case):
    assert expansion_set_job == "shared"
"""
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "conftest",
            str(module),
            "-n",
            "2",
            "--dist",
            "loadgroup",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTEST_ADDOPTS": "",
            "PYTHONPATH": os.pathsep.join(
                (str(ROOT / "tests"), str(ROOT / "tools"), os.environ.get("PYTHONPATH", ""))
            ),
        },
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "6 passed" in result.stdout
    assert len(list(tmp_path.glob("built-*"))) == 1, result.stdout + result.stderr
