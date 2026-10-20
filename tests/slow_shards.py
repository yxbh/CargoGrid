"""Whole-file slow shards, balanced from the measured geometry and fixture costs."""

from pathlib import Path

import pytest

SLOW_SELECTION = "slow and not native and not reference"
SLOW_SHARDS = (
    (
        "tests/test_auto_roof_support.py",
        "tests/test_edge_seams.py",
        "tests/test_edge_variants.py",
        "tests/test_print_orientation.py",
        "tests/test_ramp.py",
        "tests/test_solid_bottom.py",
        "tests/test_zeekr_extras.py",
        "tests/test_zeekr_rear_review.py",
    ),
    (
        "tests/test_00_zeekr_rear_export.py",
        "tests/test_adjustable_stop.py",
        "tests/test_h2d_catalogue.py",
        "tests/test_joint_styles.py",
        "tests/test_pull_handles.py",
        "tests/test_vertical_tile_bracket.py",
        "tests/test_x2d_catalogue.py",
    ),
)


def pytest_addoption(parser):
    parser.addoption(
        "--slow-shard",
        type=int,
        choices=(1, 2),
        help="run one whole-file shard of the slow, non-native, non-reference selection",
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    yield
    shard = config.getoption("--slow-shard")
    if shard is None:
        return
    if any(
        item.get_closest_marker("slow") is None
        or item.get_closest_marker("native") is not None
        or item.get_closest_marker("reference") is not None
        for item in items
    ):
        raise pytest.UsageError(f'--slow-shard requires -m "{SLOW_SELECTION}"')
    configured = [name for files in SLOW_SHARDS for name in files]
    if len(configured) != len(set(configured)):
        raise pytest.UsageError("a slow test file appears in more than one shard")
    paths = {item: Path(item.path).relative_to(config.rootpath).as_posix() for item in items}
    missing = set(paths.values()) - set(configured)
    if missing:
        raise pytest.UsageError(
            f"assign new slow test files to a shard: {', '.join(sorted(missing))}"
        )
    groups = {}
    for item in items:
        owner = next(index for index, files in enumerate(SLOW_SHARDS) if paths[item] in files)
        for mark in item.iter_markers("xdist_group"):
            name = mark.kwargs.get("name", mark.args[0] if mark.args else "default")
            if groups.setdefault(name, owner) != owner:
                raise pytest.UsageError(f"xdist group {name} spans slow shards")
    selected = set(SLOW_SHARDS[shard - 1])
    kept = [item for item in items if paths[item] in selected]
    removed = [item for item in items if paths[item] not in selected]
    config.hook.pytest_deselected(items=removed)
    items[:] = kept
