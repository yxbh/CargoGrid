"""Slow CI partition coverage and once-per-run CAD fixture snapshots."""

import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace

import pytest
import slow_shards
from build123d import Box
from shared_fixture import shared_fixture

from cargo_grid.jobs import Design, Job
from cargo_grid.parameters import BuildVolume

ROOT = Path(__file__).resolve().parents[1]


def _collect(*arguments):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            slow_shards.SLOW_SELECTION,
            *arguments,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        text=True,
        capture_output=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.startswith("tests/")]


def test_slow_shards_cover_the_full_selection_exactly_once():
    complete = _collect()
    shards = [_collect("--slow-shard", str(shard)) for shard in (1, 2)]
    assert len(complete) == len(set(complete))
    assert all(shards)
    assert not set(shards[0]) & set(shards[1])
    assert set(shards[0]) | set(shards[1]) == set(complete)
    assert sum(map(len, shards)) == len(complete)
    for index, shard in enumerate(shards):
        assert {node.split("::")[0] for node in shard} == set(slow_shards.SLOW_SHARDS[index])


@pytest.mark.parametrize("problem", ["missing-file", "duplicate-file", "split-group", "not-slow"])
def test_slow_shards_reject_incomplete_or_incompatible_selections(problem, monkeypatch):
    first, second = ROOT / "tests/one.py", ROOT / "tests/two.py"
    files = (("tests/one.py",), ("tests/two.py",))
    if problem == "missing-file":
        files = (("tests/one.py",), ())
    elif problem == "duplicate-file":
        files = (("tests/one.py",), ("tests/one.py", "tests/two.py"))
    monkeypatch.setattr(slow_shards, "SLOW_SHARDS", files)

    class Item:
        def __init__(self, path):
            self.path = path

        def get_closest_marker(self, name):
            return pytest.mark.slow.mark if name == "slow" and problem != "not-slow" else None

        def iter_markers(self, name):
            return [pytest.mark.xdist_group("shared").mark] if problem == "split-group" else []

    config = SimpleNamespace(rootpath=ROOT, getoption=lambda name: 1)
    hook = slow_shards.pytest_collection_modifyitems(config, [Item(first), Item(second)])
    next(hook)
    with pytest.raises(pytest.UsageError):
        next(hook)


def _snapshot_worker(path):
    def build():
        with path.with_suffix(".built").open("x"):
            pass
        return Job(
            [
                Design(
                    "shared box",
                    Box(2, 3, 4) - Box(1, 1, 1),
                    {"witness": (1, 2)},
                    assembly_frames=[(3, 4, 5)],
                )
            ],
            BuildVolume(20, 20, 20),
            "snapshot-test",
        )

    job = shared_fixture(path, build)
    design = job.designs[0]
    return (
        design.shape.is_valid,
        len(design.shape.solids()),
        design.shape.volume,
        design.size,
        design.parameters,
        design.assembly_frames,
        job.kind,
    )


def test_parallel_workers_build_one_fixture_and_restore_independent_cad(tmp_path):
    path = tmp_path / "shared.pickle"
    with ProcessPoolExecutor(2, mp_context=get_context("spawn")) as pool:
        values = list(pool.map(_snapshot_worker, [path] * 4))
    assert all(value == values[0] for value in values)
    assert values[0] == (
        True,
        1,
        pytest.approx(23, abs=1e-12),
        pytest.approx((2, 3, 4), abs=1e-12),
        {"witness": (1, 2)},
        [(3, 4, 5)],
        "snapshot-test",
    )
    assert path.with_suffix(".built").is_file()
    assert not path.with_suffix(".tmp").exists()


def test_failed_shared_fixture_build_does_not_publish_a_partial_snapshot(tmp_path):
    path = tmp_path / "shared.pickle"

    def fail():
        raise RuntimeError("fixture construction failed")

    with pytest.raises(RuntimeError, match="fixture construction failed"):
        shared_fixture(path, fail)
    assert not path.exists() and not path.with_suffix(".tmp").exists()
    assert shared_fixture(path, lambda: {"ready": True}) == {"ready": True}
