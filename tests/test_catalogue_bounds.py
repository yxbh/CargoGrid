"""Request-local bound reuse, with small real solids and independent fit policies."""

from collections import Counter

import pytest
from build123d import Box

from cargo_grid import catalogue
from cargo_grid.accessories import Accessory
from cargo_grid.jobs import Design
from cargo_grid.parameters import BuildVolume, Interface


@pytest.fixture
def small_catalogue(monkeypatch):
    designs = {
        "tile": Design("tile", Box(10, 10, 5), {}),
        "source": Design(
            "source",
            Box(20, 40, 10),
            {"family": "plate"},
            recommended_print_rotation_x=90,
            apply_orientation_to_bambu=True,
        ),
        "project": Design(
            "project",
            Box(20, 10, 40),
            {"family": "plate"},
            recommended_print_rotation_x=90,
            apply_orientation_to_bambu=True,
        ),
    }
    monkeypatch.setattr(catalogue, "tile_sizes", lambda build, interface: [(1, 1)])
    monkeypatch.setattr(catalogue, "tile_design", lambda spec: designs["tile"])
    monkeypatch.setattr(
        catalogue,
        "accessory_variants",
        lambda build, interface, **hole_pattern: [
            Accessory("plate", nx=1),
            Accessory("plate", nx=2, ny=2),
        ],
    )
    monkeypatch.setattr(
        catalogue, "accessory_design", lambda spec: designs["source" if spec.nx == 1 else "project"]
    )
    return designs


@pytest.mark.parametrize("oriented", [False, True])
def test_catalogue_measures_each_candidate_once_in_the_requested_pose(
    small_catalogue, monkeypatch, oriented
):
    property_name = "bambu_size" if oriented else "size"
    original = getattr(Design, property_name)
    calls = Counter()

    def measured(design):
        calls[design.name] += 1
        return original.fget(design)

    monkeypatch.setattr(Design, property_name, property(measured))
    job = catalogue.catalogue_job(BuildVolume(60, 60, 15), orient_for_bambu=oriented)
    assert calls == {"tile": 1, "source": 1, "project": 1}
    kept, omitted = ("project", "source") if oriented else ("source", "project")
    assert job.designs == [small_catalogue["tile"], small_catalogue[kept]]
    assert len(job.omitted) == 1
    assert job.omitted[0]["name"] == omitted
    assert job.omitted[0]["size_mm"] == pytest.approx((20, 10, 40))
    assert job.omitted[0]["reason"] == "actual bounds exceed usable envelope"


def test_catalogue_bounds_do_not_outlive_a_request(small_catalogue):
    build = BuildVolume(60, 60, 15)
    initial = catalogue.catalogue_job(build, orient_for_bambu=True)
    assert [design.name for design in initial.designs] == ["tile", "project"]

    small_catalogue["source"].recommended_print_rotation_x = 0
    small_catalogue["project"].shape = Box(80, 10, 40)
    changed = catalogue.catalogue_job(build, orient_for_bambu=True)
    assert [design.name for design in changed.designs] == ["tile", "source"]
    assert changed.omitted[0]["size_mm"] == pytest.approx((80, 40, 10))

    larger = catalogue.catalogue_job(BuildVolume(100, 60, 15), orient_for_bambu=True)
    assert len(larger.designs) == 3
    assert larger.omitted == []


def test_catalogue_still_rejects_an_unexpected_tile_fit_failure(small_catalogue):
    small_catalogue["tile"].shape = Box(80, 10, 5)
    with pytest.raises(ValueError, match="unexpected actual-bounds fit failure: tile"):
        catalogue.catalogue_job(BuildVolume(60, 60, 15))


def test_catalogue_sizes_follow_design_identity_not_reused_names(small_catalogue):
    small_catalogue["project"].name = small_catalogue["source"].name
    job, sizes = catalogue._catalogue_job_with_sizes(
        BuildVolume(60, 60, 50),
        interface=Interface(),
        hole_diameter=10,
        hole_scope="full",
        orient_for_bambu=True,
    )
    assert len(job.designs) == len(sizes) == 3
    assert sizes[id(small_catalogue["source"])] == pytest.approx((20, 10, 40))
    assert sizes[id(small_catalogue["project"])] == pytest.approx((20, 40, 10))
