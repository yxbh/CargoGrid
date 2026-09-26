import pytest
from build123d import Box

from cargo_grid import catalogue

# Tests using these expensive module fixtures are collected first, so their long chains start
# at the beginning of a run instead of becoming its tail. A group name keeps the tests on one
# worker under `--dist loadgroup`, so the fixture is built once. Without one, each test is
# long enough that building a copy per worker finishes sooner than one chain.
EARLY_FIXTURES = {"h2d_plan": "h2d-plan", "review_job": None}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    order = list(EARLY_FIXTURES)

    def rank(item):
        names = getattr(item, "fixturenames", ())
        return next((index for index, name in enumerate(order) if name in names), len(order))

    for item in items:
        index = rank(item)
        if index < len(order) and EARLY_FIXTURES[order[index]]:
            item.add_marker(pytest.mark.xdist_group(EARLY_FIXTURES[order[index]]))
    items.sort(key=rank)


@pytest.fixture
def accessory_metadata_shape(monkeypatch):
    """Exercise design metadata without constructing an unrelated family body."""
    monkeypatch.setattr(catalogue, "make_accessory", lambda spec: Box(2, 3, 4))
