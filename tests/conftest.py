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
    parser.addoption(
        "--input-cache", choices=("on", "off"), default="on", help="reuse source-keyed test inputs"
    )
    parser.addoption(
        "--input-cache-dir", type=Path, help="test input store (default: .local/test-inputs)"
    )


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
