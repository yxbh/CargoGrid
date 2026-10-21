"""Persistent inputs retain geometry and bytes, never passing test outcomes."""

import json
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path

import numpy as np
import pytest
from build123d import Box, Color, Location
from input_cache import InputCacheError, InputStore

from cargo_grid.jobs import Design
from cargo_grid.meshes import checked_mesh

SOURCES = [Path(__file__)]
DEPENDENCIES = {"kernel": "test"}


def store_at(path, *, enabled=True):
    return InputStore(path, enabled=enabled, dependencies=DEPENDENCIES)


def test_keys_include_complete_parameters_source_bytes_schema_and_toolchain(tmp_path, monkeypatch):
    import input_cache

    @dataclass(frozen=True)
    class Parameters:
        width: float
        holes: tuple[float, ...]

    @dataclass(frozen=True)
    class PrivateParameter:
        _radius: float

    source = tmp_path / "geometry.py"
    source.write_text("radius = 2\n")
    store = store_at(tmp_path / "cache")
    parameters = Parameters(60, (10, 20))
    key = store.key("tile", parameters, [source])
    assert store.key("tile", Parameters(60, (10, 20)), [source]) == key
    assert store.key("tile", Parameters(60, (10, 21)), [source]) != key
    assert store.key("rod", parameters, [source]) != key
    assert store.key("private", PrivateParameter(2), [source]) != store.key(
        "private", PrivateParameter(3), [source]
    )
    assert store.key("tile", (1, 2), [source]) != store.key("tile", [1, 2], [source])
    assert store.key("tile", {"a": 1, "b": 2}, [source]) == store.key(
        "tile", {"b": 2, "a": 1}, [source]
    )
    source.write_text("radius = 3\n")
    assert store.key("tile", parameters, [source]) != key
    source.write_text("radius = 2\n")
    other = InputStore(store.root, dependencies={"kernel": "changed"})
    assert other.key("tile", parameters, [source]) != key
    monkeypatch.setattr(input_cache, "SCHEMA", input_cache.SCHEMA + 1)
    assert store.key("tile", parameters, [source]) != key
    for invalid in (float("nan"), float("inf"), object(), {1: "not a string key"}):
        with pytest.raises((ValueError, TypeError)):
            store.key("tile", invalid, [source])


def test_active_run_rejects_source_edits_instead_of_miskeying_loaded_code(tmp_path):
    from hashlib import sha256

    source = tmp_path / "geometry.py"
    source.write_text("radius = 2\n")
    snapshots = {source: sha256(source.read_bytes()).hexdigest()}
    store = InputStore(tmp_path / "cache", dependencies=DEPENDENCIES, sources_at_start=snapshots)
    store.key("box", (), [source])
    source.write_text("radius = 3\n")
    with pytest.raises(InputCacheError, match="source changed.*restart pytest"):
        store.key("box", (), [source])
    assert not store.root.exists()


def test_inputs_changed_inside_a_builder_are_not_published(tmp_path):
    source = tmp_path / "geometry.py"
    source.write_text("radius = 2\n")
    store = store_at(tmp_path / "cache")
    key = store.key("geometry:box", (), [source])

    def create():
        source.write_text("radius = 3\n")
        return Box(2, 3, 4)

    with pytest.raises(InputCacheError, match="changed during construction"):
        store.geometry("box", (), [source], create)
    assert not (store.root / key).exists()


def test_geometry_hits_restore_independent_shapes_and_immutable_export_meshes(tmp_path):
    store = store_at(tmp_path / "cache")
    fresh = Box(2, 3, 4) - Box(1, 1, 1)
    fresh.label = "owned geometry"
    fresh.color = Color(0.19, 0.39, 0.5, 0.8)
    fresh.bounding_box()
    expected = checked_mesh(deepcopy(fresh))
    calls = []

    def create():
        calls.append(True)
        fresh.construction_only = lambda: None
        return fresh

    first = store.geometry("box", (2, 3, 4), SOURCES, create)
    second = store.geometry("box", (2, 3, 4), SOURCES, create)
    assert calls == [True]
    assert first is not second and first.wrapped is not second.wrapped
    assert not hasattr(first, "construction_only")
    assert first.label == "owned geometry"
    assert tuple(first.color) == tuple(second.color) == tuple(fresh.color)
    assert first.is_valid and len(first.solids()) == 1
    assert first.volume == pytest.approx(23, abs=1e-12)
    actual = store.checked_mesh(second)
    assert np.array_equal(actual[0], expected[0])
    assert np.array_equal(actual[1], expected[1])
    assert actual[2] == expected[2]
    for array in actual[:2]:
        assert not array.flags.writeable
        with pytest.raises(ValueError):
            array.setflags(write=True)
    first.move(Location((100, 0, 0)))
    assert tuple(second.bounding_box().size) == pytest.approx((2, 3, 4), abs=1e-12)
    assert second.bounding_box().max.X == pytest.approx(1, abs=1e-12)
    moved = store.checked_mesh(first)
    assert moved[0][:, 0].min() >= 99
    assert actual[0][:, 0].max() == 1


def test_copied_geometry_keeps_exact_mesh_but_edits_do_not_reuse_it(tmp_path):
    store = store_at(tmp_path / "cache")
    source = store.geometry("box", (), SOURCES, lambda: Box(2, 3, 4))
    expected = store.checked_mesh(source)
    copied = deepcopy(source)
    assert np.array_equal(store.checked_mesh(copied)[0], expected[0])
    copied.move(Location((0, 10, 0)))
    assert store.checked_mesh(copied)[0][:, 1].min() == pytest.approx(8.5)
    assert expected[0][:, 1].max() == pytest.approx(1.5)


def _cache_worker(arguments):
    cache, marker = map(Path, arguments)
    store = store_at(cache)

    def build():
        with marker.open("x"):
            pass
        return Box(2, 3, 4)

    shape = store.geometry("parallel box", (), SOURCES, build)
    shape.move(Location((100, 0, 0)))
    other = store.geometry("parallel box", (), SOURCES, build)
    return other.is_valid, other.volume, tuple(other.bounding_box().size)


def test_concurrent_first_access_builds_once_and_restores_independent_cad(tmp_path):
    arguments = (str(tmp_path / "cache"), str(tmp_path / "built"))
    with ProcessPoolExecutor(2, mp_context=get_context("spawn")) as pool:
        values = list(pool.map(_cache_worker, [arguments] * 4))
    for valid, volume, size in values:
        assert valid
        assert volume == pytest.approx(24, abs=1e-12)
        assert size == pytest.approx((2, 3, 4), abs=1e-12)
    assert (tmp_path / "built").is_file()
    assert not list((tmp_path / "cache").glob(".building-*"))


def test_failed_geometry_build_never_publishes_and_a_later_retry_runs(tmp_path):
    store = store_at(tmp_path / "cache")

    def fail():
        raise RuntimeError("construction failed")

    key = store.key("geometry:box", (), SOURCES)
    with pytest.raises(RuntimeError, match="construction failed"):
        store.geometry("box", (), SOURCES, fail)
    assert not (store.root / key).exists()
    assert not list(store.root.glob(".building-*"))
    assert store.geometry("box", (), SOURCES, lambda: Box(2, 3, 4)).volume == pytest.approx(
        24, abs=1e-12
    )


@pytest.mark.parametrize("damage", ["missing-file", "bytes", "manifest", "metadata"])
def test_corrupt_entries_fail_instead_of_silently_rebuilding(tmp_path, damage):
    store = store_at(tmp_path / "cache")
    shape = store.geometry("box", (), SOURCES, lambda: Box(2, 3, 4))
    entry = store.root / shape._test_input_mesh[0]
    if damage == "missing-file":
        (entry / "faces.npy").unlink()
    elif damage == "bytes":
        (entry / "vertices.npy").write_bytes(b"corrupt")
    elif damage == "manifest":
        (entry / "entry.json").write_text("{}")
    else:
        record = json.loads((entry / "entry.json").read_text())
        record["label"] = "changed metadata"
        (entry / "entry.json").write_text(json.dumps(record))

    def forbidden():
        raise AssertionError("a corrupt hit must not run a replacement builder")

    with pytest.raises(InputCacheError):
        store.geometry("box", (), SOURCES, forbidden)


def test_artifact_inputs_are_source_keyed_checked_and_not_cached_assertions(tmp_path):
    store = store_at(tmp_path / "cache")
    source = tmp_path / "writer.py"
    source.write_text("format_version = 1")
    calls = []

    def create(destination):
        calls.append(True)
        (destination / "project.3mf").write_bytes(b"test bytes")
        (destination / "entry.json").write_text('{"nested":true}')

    first = store.artifacts("export", {"copies": 2}, [source], create)
    assert store.artifacts("export", {"copies": 2}, [source], create) == first
    assert calls == [True]
    assert (first / "project.3mf").read_bytes() == b"test bytes"
    assert json.loads((first / "entry.json").read_text()) == {"nested": True}
    source.write_text("format_version = 2")
    assert store.artifacts("export", {"copies": 2}, [source], create) != first
    assert calls == [True, True]
    (first / "project.3mf").write_bytes(b"changed")
    source.write_text("format_version = 1")
    with pytest.raises(InputCacheError, match="checksum"):
        store.artifacts("export", {"copies": 2}, [source], create)


def test_cache_off_builds_fresh_geometry_and_export_inputs(tmp_path):
    store = store_at(tmp_path / "cache", enabled=False)
    calls = []

    def build():
        calls.append(True)
        return Box(2, 3, 4)

    assert store.geometry("box", (), SOURCES, build) is not store.geometry(
        "box", (), SOURCES, build
    )
    assert calls == [True, True]

    def export(destination):
        (destination / "project.3mf").write_bytes(b"fresh")

    first = store.artifacts("export", (), SOURCES, export)
    second = store.artifacts("export", (), SOURCES, export)
    assert first != second
    assert (first / "project.3mf").read_bytes() == (second / "project.3mf").read_bytes() == b"fresh"
    store.close()
    assert not first.exists() and not second.exists()


def test_design_metadata_and_assembly_frames_are_independent_typed_inputs(tmp_path):
    store = store_at(tmp_path / "cache")
    calls = []

    def create():
        calls.append(True)
        return Design(
            "test",
            Box(2, 3, 4),
            {"witness": (1, 2)},
            assembly_frames=[(3, 4, 5)],
            mating_datums={"nested": [(1.0, 2.0)]},
        )

    first = store.design("box", (), SOURCES, create)
    first.parameters["witness"] = "changed"
    first.assembly_frames.append((10, 20, 30))
    first.mating_datums["nested"].clear()
    second = store.design("box", (), SOURCES, create)
    assert calls == [True]
    assert second.parameters == {"witness": (1, 2)}
    assert second.assembly_frames == [(3, 4, 5)]
    assert second.mating_datums == {"nested": [(1.0, 2.0)]}


def test_failed_artifact_build_and_failed_publication_leave_no_entry(tmp_path, monkeypatch):
    store = store_at(tmp_path / "cache")

    def partial(destination):
        (destination / "partial.3mf").write_bytes(b"incomplete")
        raise RuntimeError("export failed")

    with pytest.raises(RuntimeError, match="export failed"):
        store.artifacts("export", (), SOURCES, partial)
    key = store.key("artifacts:export", (), SOURCES)
    assert not (store.root / key).exists()
    assert not list(store.root.glob(".building-*"))

    def complete(destination):
        (destination / "project.3mf").write_bytes(b"complete")

    def fail_publication(*args):
        raise OSError("publication failed")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_publication)
        with pytest.raises(OSError, match="publication failed"):
            store.artifacts("export", (), SOURCES, complete)
    assert not (store.root / key).exists()
    assert not list(store.root.glob(".building-*"))
    assert (store.artifacts("export", (), SOURCES, complete) / "project.3mf").read_bytes() == (
        b"complete"
    )
