import pytest


def pytest_addoption(parser):
    parser.addoption("--ci-partition", help="slow runner partition INDEX/COUNT (zero-based)")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    shared = (
        "h2d_plan",
        "x2d_plan",
        "wide_perimeter_designs",
        "expansion_set_job",
        "assembled_parts",
    )

    def rank(item):
        return next((i for i, name in enumerate(shared) if name in item.fixturenames), len(shared))

    # xdist resets workers' dist option; loadgroup preserves their scheduler mode.
    grouped = config.getoption("loadgroup", default=False)
    if grouped:
        for item in items:
            group = rank(item)
            if group < len(shared):
                item.add_marker(pytest.mark.xdist_group(shared[group]))
    result = yield
    value = config.getoption("--ci-partition")
    if value is not None:
        from ci_partition import partition_files

        parts = value.split("/")
        if len(parts) != 2 or not all(part.isdecimal() for part in parts):
            raise pytest.UsageError("--ci-partition requires INDEX/COUNT")
        index, count = map(int, parts)
        if count < 1 or not 0 <= index < count:
            raise pytest.UsageError("--ci-partition requires 0 <= INDEX < COUNT")
        if any(item.get_closest_marker("slow") is None for item in items):
            raise pytest.UsageError("--ci-partition requires a slow-only selection")
        paths = {item.path.relative_to(config.rootpath).as_posix() for item in items}
        selected = partition_files(paths, count)[index]
        kept = []
        rejected = []
        for item in items:
            target = (
                kept if item.path.relative_to(config.rootpath).as_posix() in selected else rejected
            )
            target.append(item)
        config.hook.pytest_deselected(items=rejected)
        items[:] = kept
    if grouped:
        items.sort(key=rank)
    return result


@pytest.fixture
def accessory_metadata_shape(monkeypatch):
    """Exercise design metadata without constructing an unrelated family body."""
    from build123d import Box

    from cargo_grid import catalogue

    monkeypatch.setattr(catalogue, "make_accessory", lambda spec: Box(2, 3, 4))
