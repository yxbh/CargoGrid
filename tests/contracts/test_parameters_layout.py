import math

import pytest

from cargo_grid.layout import exact_layout
from cargo_grid.parameters import BuildVolume, Exclusion, Interface, Tile


@pytest.mark.parametrize("value", [0, -1, math.inf, math.nan])
def test_invalid_volume(value):
    with pytest.raises(ValueError):
        BuildVolume(value, 100, 100)


def test_invalid_parameters():
    for values in ({"nx": 0}, {"nx": 1.5}, {"nx": True}, {"hole_diameter": -1}, {"filler_west": 3}):
        with pytest.raises(ValueError):
            Tile(**values)
    with pytest.raises(ValueError):
        Interface(pitch=29)
    with pytest.raises(ValueError):
        Interface(height=5)
    with pytest.raises(ValueError):
        BuildVolume(100, 100, 10, margin=50)


def test_rotations_and_exclusions():
    b = BuildVolume(100, 70, 20, exclusions=(Exclusion(0, 0, 10, 70),))
    assert b.placement((60, 80, 13)) == (10, 0, 90)
    assert b.placement((100, 70, 13)) is None
    assert b.placement((10, 10, 21)) is None


def test_exclusion_candidates_cannot_bypass_lower_margins():
    build = BuildVolume(100, 100, 30, margin=5, exclusions=(Exclusion(0, 0, 1, 1),))
    assert build.placement((20, 20, 13)) == (5, 5, 0)


@pytest.mark.parametrize(
    "width,depth", [(120, 120), (121, 120), (121, 137), (179.99, 241), (360.01, 600.5)]
)
@pytest.mark.parametrize("distribution", ["balanced", "positive", "negative"])
def test_layout_exact_logical_coverage(width, depth, distribution):
    layout = exact_layout(width, depth, BuildVolume(150, 140, 30), distribution=distribution)
    rectangles = []
    for piece in layout.pieces:
        t = piece.tile
        x = piece.x - t.filler_west
        y = piece.y - t.filler_south
        w = t.body_size[0] + t.filler_west + t.filler_east
        d = t.body_size[1] + t.filler_south + t.filler_north
        rectangles.append((x, y, x + w, y + d))
    assert min(r[0] for r in rectangles) == pytest.approx(0)
    assert min(r[1] for r in rectangles) == pytest.approx(0)
    assert max(r[2] for r in rectangles) == pytest.approx(width)
    assert max(r[3] for r in rectangles) == pytest.approx(depth)
    assert sum((c - a) * (d - b) for a, b, c, d in rectangles) == pytest.approx(width * depth)
    for i, a in enumerate(rectangles):
        for b in rectangles[i + 1 :]:
            assert min(a[2], b[2]) <= max(a[0], b[0]) or min(a[3], b[3]) <= max(a[1], b[1])


def test_impossible_footprints_fail():
    for w, d in [(59, 200), (200, 59), (0, 100)]:
        with pytest.raises(ValueError):
            exact_layout(w, d, BuildVolume(100, 100, 100))
    with pytest.raises(ValueError, match="partition"):
        exact_layout(200, 200, BuildVolume(63, 63, 100))
