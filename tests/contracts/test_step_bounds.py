"""Precise bounds live for one STEP check, not across attempts or exports."""

from collections import Counter

import pytest
from build123d import Box, Location, PrecisionMode, import_step
from OCP.Precision import Precision

from cargo_grid import export

MODES = [
    PrecisionMode.AVERAGE,
    PrecisionMode.LEAST,
    PrecisionMode.GREATEST,
    PrecisionMode.SESSION,
]


def _step_probe(monkeypatch, accept_attempt):
    source = Box(20, 30, 2)
    modes, restored = [], []
    calls = Counter()
    original_export = export.export_step
    original_import = export.import_step

    def watch_bounds(shape):
        original_bounds = shape.bounding_box

        def measured(*args, **kwargs):
            assert args == () and kwargs == {}  # Keep the default precise query.
            calls[id(shape)] += 1
            return original_bounds()

        monkeypatch.setattr(shape, "bounding_box", measured)

    def write(shape, path, *, precision_mode):
        modes.append(precision_mode)
        before = type(shape).bounding_box(shape)
        volume, area = shape.volume, shape.area
        written = original_export(shape, path, precision_mode=precision_mode)
        after = type(shape).bounding_box(shape)
        assert tuple(after.min) == tuple(before.min)
        assert tuple(after.max) == tuple(before.max)
        assert shape.volume == volume and shape.area == area
        assert shape.is_valid and len(shape.solids()) == 1
        return written

    def read(path):
        shape = original_import(path)
        if accept_attempt is None or len(restored) + 1 < accept_attempt:
            # Same size and volume, but displaced beyond the existing bounds tolerance.
            shape = shape.moved(Location((0.01, 0, 0)))
        restored.append(shape)
        watch_bounds(shape)
        return shape

    watch_bounds(source)
    monkeypatch.setattr(export, "export_step", write)
    monkeypatch.setattr(export, "import_step", read)
    return source, modes, restored, calls


@pytest.mark.parametrize("accept_attempt", [1, 2, 4])
def test_step_measures_source_once_and_each_precision_reimport_once(
    monkeypatch, tmp_path, accept_attempt
):
    source, modes, restored, calls = _step_probe(monkeypatch, accept_attempt)
    path = tmp_path / "checked.step"
    result, mode, delta, budget, bounds_delta = export._checked_step_roundtrip(source, path)

    assert modes == MODES[:accept_attempt]
    assert mode == ("average", "least", "greatest", "session")[accept_attempt - 1]
    assert result is restored[-1]
    assert calls == {id(shape): 1 for shape in [source, *restored]}
    assert delta <= budget == max(1e-6, source.area * Precision.Confusion_s())
    assert bounds_delta <= 1e-5
    actual = import_step(path)
    assert actual.is_valid and len(actual.solids()) == 1 and actual.volume > 0
    assert actual.volume == pytest.approx(1200)
    assert tuple(actual.bounding_box().min) == pytest.approx((-10, -15, -1), abs=1e-5)
    assert tuple(actual.bounding_box().max) == pytest.approx((10, 15, 1), abs=1e-5)


def test_step_rejects_all_bad_reimports_without_reusing_an_attempts_bounds(monkeypatch, tmp_path):
    source, modes, restored, calls = _step_probe(monkeypatch, accept_attempt=None)
    with pytest.raises(ValueError, match="STEP roundtrip failed") as failure:
        export._checked_step_roundtrip(source, tmp_path / "rejected.step")

    assert modes == MODES
    assert calls == {id(shape): 1 for shape in [source, *restored]}
    for mode in ("average", "least", "greatest", "session"):
        assert f"{mode}:valid=True,solids=1," in str(failure.value)
    assert str(failure.value).count("bounds=0.01") == 4


def test_step_remeasures_mutated_source_on_the_next_export(monkeypatch, tmp_path):
    source, modes, restored, calls = _step_probe(monkeypatch, accept_attempt=1)
    export._checked_step_roundtrip(source, tmp_path / "first.step")
    source.move(Location((7, 11, 13)))
    _, mode, delta, budget, bounds_delta = export._checked_step_roundtrip(
        source, tmp_path / "moved.step"
    )

    assert modes == [PrecisionMode.AVERAGE, PrecisionMode.AVERAGE]
    assert calls[id(source)] == 2
    assert all(calls[id(shape)] == 1 for shape in restored)
    assert mode == "average" and delta <= budget and bounds_delta <= 1e-5
    first = import_step(tmp_path / "first.step")
    moved = import_step(tmp_path / "moved.step")
    assert tuple(moved.bounding_box().min - first.bounding_box().min) == pytest.approx((7, 11, 13))
    assert moved.is_valid and len(moved.solids()) == 1 and moved.volume > 0
