import pytest
from build123d import Box

from cargo_grid import catalogue

# Tests sharing one expensive module fixture form an xdist group, so `--dist loadgroup` runs
# them on one worker and builds the fixture once. Groups are also collected first, so their
# long chains start at the beginning of a run instead of becoming its tail.
SHARED_FIXTURE_GROUPS = {"h2d_plan": "h2d-plan", "review_job": "zeekr-review-job"}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    order = list(SHARED_FIXTURE_GROUPS)

    def group(item):
        names = getattr(item, "fixturenames", ())
        return next((index for index, name in enumerate(order) if name in names), len(order))

    for item in items:
        index = group(item)
        if index < len(order):
            item.add_marker(pytest.mark.xdist_group(SHARED_FIXTURE_GROUPS[order[index]]))
    items.sort(key=group)


@pytest.fixture
def accessory_metadata_shape(monkeypatch):
    """Exercise design metadata without constructing an unrelated family body."""
    monkeypatch.setattr(catalogue, "make_accessory", lambda spec: Box(2, 3, 4))
