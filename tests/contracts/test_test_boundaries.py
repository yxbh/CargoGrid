"""The default collection is small; the full command retains all portable contracts."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _collect(*arguments):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *arguments],
        cwd=ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if line.startswith("tests/")]


def test_default_and_full_portable_collections_have_distinct_complete_boundaries():
    default = _collect()
    complete = _collect("tests", "-m", "not native")
    assert default and complete
    assert len(default) == len(set(default))
    assert len(complete) == len(set(complete))
    assert all(node.startswith("tests/contracts/") for node in default)
    assert set(default) < set(complete)
    assert any(node.startswith("tests/test_geometry.py::") for node in complete)
    assert any(node.startswith("tests/test_adjustable_stop.py::") for node in complete)
    assert any(node.startswith("tests/test_zeekr_contours.py::") for node in complete)
    assert any(node.startswith("tests/test_cached_geometry.py::") for node in complete)


def test_slow_runner_partitions_cover_every_collected_test_once():
    from ci_partition import partition_files

    arguments = ("tests", "-m", "slow and not native")
    complete = set(_collect(*arguments))
    first = set(_collect(*arguments, "--ci-partition=0/2"))
    second = set(_collect(*arguments, "--ci-partition=1/2"))
    assert first and second
    assert first.isdisjoint(second)
    assert first | second == complete
    paths = {node.split("::", 1)[0] for node in complete}
    allocations = partition_files(paths | {"tests/test_new_slow_family.py"}, 2)
    assert allocations[0].isdisjoint(allocations[1])
    assert set.union(*allocations) == paths | {"tests/test_new_slow_family.py"}
    assert allocations == partition_files(
        set(reversed(sorted(paths))) | {"tests/test_new_slow_family.py"}, 2
    )
