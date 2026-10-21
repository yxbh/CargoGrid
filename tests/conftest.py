import pytest


def pytest_addoption(parser):
    parser.addoption("--ci-partition", help="slow runner partition INDEX/COUNT (zero-based)")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    shared = (
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


@pytest.fixture
def catalogue_stand_ins(monkeypatch):
    """Keep catalogue policy tests on synthetic solids, not complete family geometry."""
    from build123d import Box, Pos

    from cargo_grid import catalogue
    from cargo_grid.jobs import Design, tile_identity
    from cargo_grid.rods import Rod

    real_tile, real_accessory = catalogue.tile_design, catalogue.make_accessory

    def tile(spec):
        name, parameters = tile_identity(spec)
        return Design(
            name,
            Box(spec.nx * 60 + 6, spec.ny * 60 + 6, 13 + spec.interface.solid_bottom_mm),
            parameters,
        )

    def accessory(spec):
        if isinstance(spec, Rod):
            length = spec.above_mat_height_mm + 12
            return Box(10, 10, length) + Pos(0, 0, -length / 2 + 13.6) * Box(18, 18, 3.2)
        return Box(20, 30, 10)

    def keep_real(predicate):
        monkeypatch.setattr(
            catalogue,
            "tile_design",
            lambda spec: real_tile(spec) if predicate(spec) else tile(spec),
        )
        monkeypatch.setattr(
            catalogue,
            "make_accessory",
            lambda spec: real_accessory(spec) if predicate(spec) else accessory(spec),
        )

    keep_real(lambda spec: False)
    return keep_real


@pytest.fixture
def real_catalogue_group(catalogue_stand_ins, group):
    from types import SimpleNamespace

    from test_h2d_catalogue import _design_group

    from cargo_grid.catalogue import accessory_identity_parameters
    from cargo_grid.parameters import Tile

    constructed = []

    def selected(spec):
        if isinstance(spec, Tile):
            keep = group == "Tiles"
        else:
            design = SimpleNamespace(parameters=accessory_identity_parameters(spec))
            keep = _design_group(design)[0] == group
        if keep:
            constructed.append(spec)
        return keep

    catalogue_stand_ins(selected)
    return constructed
