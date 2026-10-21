"""Geometric support/release checks, independent of physical print outcomes."""

from collections import Counter
from math import nan

import pytest
from build123d import Location, Vector

from cargo_grid.jobs import Design, tile_design
from cargo_grid.parameters import BuildVolume, Exclusion, Tile
from cargo_grid.stacking import StackSettings, stack_volumes


@pytest.fixture(scope="module")
def tile():
    return tile_design(Tile())


@pytest.fixture(scope="module")
def settings():
    return StackSettings(2, 1, 0.2, 1, 1, 2)


@pytest.fixture(scope="module")
def sandwich(tile, settings):
    return stack_volumes(tile, settings, BuildVolume(150, 150, 50))


@pytest.mark.parametrize(
    "updates",
    [
        {"count": 0},
        {"count": True},
        {"count": 1.5},
        {"gap": 0},
        {"gap": nan},
        {"gap": 0.4},
        {"interface_thickness": 0},
        {"interface_thickness": nan},
        {"model_slot": 0},
        {"support_slot": True},
        {"interface_slot": 1},
    ],
)
def test_invalid_stack_parameters(updates):
    values = {
        "count": 2,
        "gap": 1,
        "interface_thickness": 0.2,
        "model_slot": 1,
        "support_slot": 1,
        "interface_slot": 2,
    }
    values.update(updates)
    with pytest.raises(ValueError):
        StackSettings(**values)


def test_maximum_count_includes_all_gaps_and_reservations(settings):
    assert settings.maximum_count(13, BuildVolume(100, 100, 27)) == 2
    assert settings.maximum_count(13, BuildVolume(100, 100, 26.999)) == 1
    assert settings.maximum_count(13, BuildVolume(100, 100, 29, reserve_z=2)) == 2
    assert settings.maximum_count(13, BuildVolume(100, 100, 12.9)) == 0
    assert settings.maximum_count(13, BuildVolume(100, 100, 41)) == 3


def test_actual_stack_exact_height_and_overflow(tile, settings):
    volumes = stack_volumes(tile, settings, BuildVolume(150, 150, 27))
    assert min(v.shape.bounding_box().min.Z for v in volumes) == pytest.approx(0, abs=1e-6)
    assert max(v.shape.bounding_box().max.Z for v in volumes) == pytest.approx(27, abs=1e-6)
    with pytest.raises(ValueError, match="maximum"):
        stack_volumes(tile, settings, BuildVolume(150, 150, 26.999))


def test_xy_margin_and_exclusion_constraints(tile, settings):
    with pytest.raises(ValueError, match="fit"):
        stack_volumes(tile, settings, BuildVolume(70, 70, 50, margin=3))
    with pytest.raises(ValueError, match="fit"):
        stack_volumes(
            tile,
            settings,
            BuildVolume(
                100,
                100,
                50,
                exclusions=(Exclusion(0, 0, 100, 50),),
            ),
        )


def test_single_tile_has_no_sacrificial_material(tile):
    volumes = stack_volumes(tile, StackSettings(1, 1, 0.2, 1, 1, 2), BuildVolume(150, 150, 13))
    assert len(volumes) == 1
    assert volumes[0].role == "model" and volumes[0].slot == 1
    assert volumes[0].shape.volume == pytest.approx(tile.shape.volume)


def test_non_tile_stack_rejected(tile, settings):
    other = Design("accessory", tile.shape, {})
    with pytest.raises(ValueError, match="tiles"):
        stack_volumes(other, settings, BuildVolume(150, 150, 50))


def test_stack_parts_are_named_connected_disjoint_and_correctly_assigned(sandwich):
    assert len({v.name for v in sandwich}) == len(sandwich)
    roles = Counter(v.role for v in sandwich)
    assert roles["model"] == 2
    assert roles["release-lower"] and roles["release-upper"] and roles["support-base"]
    for volume in sandwich:
        assert volume.shape.is_valid
        assert len(volume.shape.solids()) == 1
        assert volume.shape.volume > 1e-6
        assert volume.slot == (2 if volume.role.startswith("release-") else 1)
    for index, a in enumerate(sandwich):
        for b in sandwich[index + 1 :]:
            removed_volume = a.shape.volume - a.shape.cut(b.shape).volume
            assert removed_volume < 1e-4, f"{a.name} overlaps {b.name}"


def test_model_geometry_is_unchanged_at_each_height(tile, sandwich):
    normalized = tile.shape.moved(Location(-tile.shape.bounding_box().min))
    models = [v for v in sandwich if v.role == "model"]
    for level, volume in enumerate(models):
        restored = volume.shape.moved(Location((0, 0, -14 * level)))
        assert restored.volume == pytest.approx(normalized.volume, abs=1e-5)
        assert normalized.volume - normalized.cut(restored).volume == pytest.approx(
            normalized.volume,
            abs=1e-5,
        )


def test_x_socket_is_not_bridged_by_sacrificial_rectangle(sandwich):
    sacrificial = [v for v in sandwich if v.role != "model"]
    for x, y in ((30, 30), (31, 31), (29, 31), (31, 29)):
        for z in (10.5, 12.9, 13.1, 13.5, 13.9, 14.1):
            assert not any(v.shape.is_inside(Vector(x, y, z)) for v in sacrificial)


def test_both_release_layers_contact_the_intended_tile(sandwich):
    models = [v.shape for v in sandwich if v.role == "model"]
    for role, model in (("release-lower", models[0]), ("release-upper", models[1])):
        release = [v.shape for v in sandwich if v.role == role]
        assert release
        assert all(shape.distance_to(model) < 1e-5 for shape in release)
    bases = [v.shape for v in sandwich if v.role == "support-base"]
    assert all(
        any(base.distance_to(v.shape) < 1e-5 for v in sandwich if v.role.startswith("release-"))
        for base in bases
    )


def test_same_material_base_cannot_touch_either_tile_directly(sandwich):
    models = [v for v in sandwich if v.role == "model"]
    for base in (v for v in sandwich if v.role == "support-base"):
        for model in models:
            if base.slot == model.slot:
                assert base.shape.distance_to(model.shape) > 1e-5, (
                    f"{base.name} directly contacts {model.name} without a release layer"
                )
