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
