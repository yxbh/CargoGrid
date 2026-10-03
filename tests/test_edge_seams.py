"""Perimeter seams open no more of the mat than a tile-to-tile seam; print fit is unverified.

Edge and corner faces that meet a tile or a neighbouring perimeter piece use the tile's R1
rounds, so each assembled seam is checked against two tiles meeting in the same place, with
the same hole pattern and solid bottom. The measurement is the band area left open through
the whole height, as in a top-down view of the assembled parts.
"""

from functools import lru_cache

import pytest
from build123d import Axis, GeomType, Location, Vector
from local_geometry import through_open_area
from OCP.BRepAdaptor import BRepAdaptor_Surface

from cargo_grid import Interface, Tile, make_tile
from cargo_grid.accessories import (
    EDGE_BODY_RADIUS_MM,
    EDGE_MATING_RADIUS_MM,
    Accessory,
    make_accessory,
)

OUTWARD = 10.0
# Seams meet another piece along ~60 mm (tile-facing) or 10 mm (strip ends). Bands stop 1 mm
# short of the neighbouring corner rounds, as the reference bands do.
STACKED = "tiles stacked in Y"
SIDE = "tiles side by side in X"


@lru_cache(maxsize=None)
def tile(thickness: float, holes: bool):
    return make_tile(Tile(1, 1, Interface(solid_bottom_mm=thickness), 10 if holes else None))


@lru_cache(maxsize=None)
def piece(family: str, thickness: float, holes: bool, variant: int = 1):
    return make_accessory(
        Accessory(
            family,
            variant=variant,
            interface=Interface(solid_bottom_mm=thickness),
            complete_edge_holes=holes,
        )
    )


def seam_shapes(name, thickness, holes):
    t = tile(thickness, holes)
    strip = piece("edge-x", thickness, holes)
    if name == "tile + male edge-x":
        return [t, strip], (0.9, 59.1, -2, 2), STACKED
    if name == "tile + female edge-y":
        below = t.moved(Location((0, -60, 0)))
        return [below, piece("edge-y", thickness, holes)], (0.9, 59.1, -2, 2), STACKED
    if name == "edge-x end + edge-x end":
        right = strip.moved(Location((60, 0, 0)))
        return [strip, right], (58, 62, -OUTWARD + 1, -1), SIDE
    if name == "corner-in v1 + edge-x":
        right = strip.moved(Location((60, 60, 0)))
        corner = piece("corner-in", thickness, holes, 1)
        return [corner, right], (58, 62, 60 - OUTWARD + 1, 59), SIDE
    if name == "corner-out v6 + edge-x":
        right = strip.moved(Location((60, 0, 0)))
        corner = piece("corner-out", thickness, holes, 6)
        return [corner, right], (58, 62, -OUTWARD + 1, -1), SIDE
    if name == "corner-out v1 half + west edge-x":
        west = strip.rotate(Axis.Z, -90)
        corner = piece("corner-out", thickness, holes, 1)
        return [corner, west], (-OUTWARD + 1, -1, -2, 2), STACKED
    raise ValueError(name)


def reference_area(kind, band, thickness, holes):
    """Two tiles meeting on the same seam line with a mirrored band of the same length."""
    t = tile(thickness, holes)
    x0, x1, y0, y1 = band
    if kind == STACKED:
        # A mirrored band keeps the same distance from the neighbouring hole and corner.
        lo, hi = (x0, x1) if x0 >= 0 else (-x1, -x0)
        return through_open_area([t.moved(Location((0, -60, 0))), t], lo, hi, y0, y1)
    offset = 60 * round((x0 + x1) / 2 / 60)
    local_lo, local_hi = y0 - 60 * round(y1 / 60), y1 - 60 * round(y1 / 60)
    lo, hi = (-local_hi, -local_lo) if local_hi <= 0 else (local_lo, local_hi)
    right = t.moved(Location((60, 0, 0)))
    return through_open_area([t, right], x0 - offset + 60, x1 - offset + 60, lo, hi)


SEAMS = (
    "tile + male edge-x",
    "tile + female edge-y",
    "edge-x end + edge-x end",
    "corner-in v1 + edge-x",
    "corner-out v6 + edge-x",
    "corner-out v1 half + west edge-x",
)
# Portable runs keep every seam in the user-reported case (plated, full holes) and the plain
# unplated case; the other two combinations of each seam run in the slow tier.
PORTABLE = {(1.92, True), (0.0, False)}


@pytest.mark.parametrize(
    "thickness,holes",
    [
        case if case in PORTABLE else pytest.param(*case, marks=pytest.mark.slow)
        for case in ((0.0, False), (0.0, True), (1.92, False), (1.92, True))
    ],
)
@pytest.mark.parametrize("seam", SEAMS)
def test_perimeter_seam_is_no_more_open_than_a_tile_seam(seam, thickness, holes):
    shapes, band, kind = seam_shapes(seam, thickness, holes)
    assert all(shape.is_valid and len(shape.solids()) == 1 for shape in shapes)
    opening = through_open_area(shapes, *band)
    reference = reference_area(kind, band, thickness, holes)
    assert opening <= reference + 0.01, (seam, opening, reference)
    if thickness and holes:
        # The solid bottom closes blind holes too, so nothing is open through the seam.
        assert reference < 0.01


def test_seam_measurement_sees_an_r3_notch():
    """The measurement is not blind: R3 strip ends leave a visible opening, R1 ends don't."""
    from cargo_grid.interfaces import prism, rectangle

    body = prism(rectangle(0, -OUTWARD, 60, OUTWARD), 13)
    r3 = body.fillet(3, body.edges().filter_by(Axis.Z))
    r1 = body.fillet(1, body.edges().filter_by(Axis.Z))
    right = Location((60, 0, 0))
    wide = through_open_area([r3, r3.moved(right)], 58, 62, -OUTWARD + 1, -1)
    narrow = through_open_area([r1, r1.moved(right)], 58, 62, -OUTWARD + 1, -1)
    assert narrow < 0.01 and wide > 1


def _radius_near(shape, point):
    for face in shape.faces():
        if face.geom_type in (GeomType.CYLINDER, GeomType.TORUS) and face.distance_to(point) < 1e-6:
            surface = BRepAdaptor_Surface(face.wrapped)
            if face.geom_type == GeomType.CYLINDER:
                return surface.Cylinder().Radius()
            return surface.Torus().MinorRadius()
    return None


@pytest.mark.parametrize("thickness", [0.0, 1.92])
def test_mating_faces_use_tile_r1_and_free_faces_keep_r3(thickness):
    top = 13 + thickness
    strip = piece("edge-x", thickness, False)
    assert tuple(strip.bounding_box().size) == pytest.approx((60, OUTWARD + 6, top), abs=1e-5)
    s = 2**-0.5  # 45 degrees around each round
    r1_points = (
        Vector(2.5, -1 + s, top - 1 + s),  # tile-facing top, clear of the joint tool wall
        Vector(2.5, -1 + s, 1 - s),  # tile-facing underside
        Vector(1 - s, -1 + s, top / 2),  # strip end meets the tile-facing face
        Vector(1 - s, -OUTWARD + 1 - s, top / 2),  # strip end meets the outward face
        Vector(1 - s, -5, top - 1 + s),  # strip end top
        Vector(1 - s, -5, 1 - s),  # strip end underside
    )
    for point in r1_points:
        assert _radius_near(strip, point) == pytest.approx(EDGE_MATING_RADIUS_MM), point
    for point in (
        Vector(30, -OUTWARD + 3 - 3 * s, top - 3 + 3 * s),  # free outward top
        Vector(30, -OUTWARD + 3 - 3 * s, 3 - 3 * s),  # free outward underside
    ):
        assert _radius_near(strip, point) == pytest.approx(EDGE_BODY_RADIUS_MM), point


@pytest.mark.parametrize(
    "family,variant",
    [
        ("edge-x", 1),
        ("edge-y", 1),
        *(("corner-in", v) for v in range(1, 5)),
        *(("corner-out", v) for v in range(1, 7)),
    ],
)
def test_plated_perimeter_bodies_are_valid_with_closed_floors(family, variant):
    from build123d import Plane, section

    thickness = 1.92
    plated = piece(family, thickness, True, variant)
    standard = piece(family, 0.0, True, variant)
    assert plated.is_valid and len(plated.solids()) == 1 and plated.volume > 0
    a, b = plated.bounding_box(), standard.bounding_box()
    assert (a.min.X, a.min.Y, a.max.X, a.max.Y) == pytest.approx(
        (b.min.X, b.min.Y, b.max.X, b.max.Y), abs=1e-5
    )
    assert a.min.Z == pytest.approx(0, abs=1e-6) and a.size.Z == pytest.approx(13 + thickness)
    faces = section(plated, section_by=Plane.XY.offset(thickness / 2)).faces()
    assert len(faces) == 1 and not faces[0].inner_wires()
