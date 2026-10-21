from hashlib import sha256
from pathlib import Path

import pytest

SOURCE_DIGESTS = pytest.StashKey[dict[Path, str]]()


def pytest_sessionstart(session):
    root = session.config.rootpath
    session.config.stash[SOURCE_DIGESTS] = {
        path: sha256(path.read_bytes()).hexdigest()
        for folder in (root / "src/cargo_grid", root / "tests")
        for path in folder.rglob("*.py")
    }


def pytest_addoption(parser):
    parser.addoption("--ci-partition", help="slow runner partition INDEX/COUNT (zero-based)")
    parser.addoption(
        "--input-cache", choices=("on", "off"), default="on", help="reuse source-keyed test inputs"
    )
    parser.addoption(
        "--input-cache-dir", type=Path, help="test input store (default: .local/test-inputs)"
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    shared = ("h2d_plan", "x2d_plan", "wide_perimeter_designs", "expansion_set_job")

    def rank(item):
        return next((i for i, name in enumerate(shared) if name in item.fixturenames), len(shared))

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
    items.sort(key=rank)
    return result


@pytest.fixture(scope="session")
def input_store(pytestconfig):
    from input_cache import InputStore

    root = pytestconfig.getoption("--input-cache-dir")
    store = InputStore(
        root if root is not None else pytestconfig.rootpath / ".local/test-inputs",
        enabled=pytestconfig.getoption("--input-cache") == "on",
        sources_at_start=pytestconfig.stash[SOURCE_DIGESTS],
    )
    yield store
    store.close()


@pytest.fixture
def fresh_geometry(input_store):
    enabled = input_store.enabled
    input_store.enabled = False
    try:
        yield
    finally:
        input_store.enabled = enabled


@pytest.fixture(scope="module", autouse=True)
def geometry_inputs(request):
    if request.node.get_closest_marker("geometry_inputs") is None:
        yield
        return
    from geometry_inputs import GeometryInputs

    with pytest.MonkeyPatch.context() as monkeypatch:
        GeometryInputs(request.getfixturevalue("input_store")).install(monkeypatch, request.module)
        yield


@pytest.fixture
def artifact_inputs(input_store, request):
    root = request.config.rootpath
    sources = [
        *sorted((root / "src/cargo_grid").rglob("*.py")),
        root / "tests/geometry_inputs.py",
        Path(request.module.__file__),
    ]

    def load(kind, parameters, create):
        return input_store.artifacts(kind, parameters, sources, create)

    return load


@pytest.fixture
def accessory_metadata_shape(monkeypatch):
    """Exercise design metadata without constructing an unrelated family body."""
    from build123d import Box

    from cargo_grid import catalogue

    monkeypatch.setattr(catalogue, "make_accessory", lambda spec: Box(2, 3, 4))
