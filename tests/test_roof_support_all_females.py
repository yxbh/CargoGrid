"""Locate retained female ceilings and removable-support access openings."""

import pytest

from cargo_grid import Interface, Tile
from cargo_grid.accessories import Accessory
from cargo_grid.catalogue import accessory_design
from cargo_grid.jobs import tile_design
from cargo_grid.roof_support import female_roofs

EXPECTED = {
    "tile": ({("west", 1), ("south", 1), ("south", 2)}, {(0, 30), (30, 0), (90, 0)}),
    "edge-y": ({("south", 1), ("south", 2)}, {(30, 0), (90, 0)}),
    "corner-in v4": ({("west", 1), ("south", 1)}, {(0, 30), (30, 0)}),
    "corner-out v3": ({("west", 1), ("south", 1)}, {(60, 30), (30, 60)}),
    "corner-out v2 half": ({("south", 1)}, {(30, 0)}),
    "ramp": ({("south", 1), ("south", 2)}, set()),
}


@pytest.mark.parametrize("thickness", [0.0, 1.92])
@pytest.mark.parametrize("kind", list(EXPECTED))
def test_retained_female_roof_faces_and_occupied_openings(kind, thickness):
    interface = Interface(solid_bottom_mm=thickness)
    if kind == "tile":
        design = tile_design(Tile(2, 1, interface))
    else:
        specs = {
            "edge-y": Accessory("edge-y", nx=2, interface=interface),
            "corner-in v4": Accessory("corner-in", variant=4, interface=interface),
            "corner-out v3": Accessory("corner-out", variant=3, interface=interface),
            "corner-out v2 half": Accessory("corner-out", variant=2, interface=interface),
            "ramp": Accessory("ramp", nx=2, interface=interface),
        }
        design = accessory_design(specs[kind])
    roofs = female_roofs(design)
    sides, openings = EXPECTED[kind]
    assert {(roof.side, roof.index) for roof in roofs} == sides
    assert {point for roof in roofs for point in roof.occupied_openings} == openings
    for roof in roofs:
        assert roof.faces
        for face in roof.faces:
            box = face.bounding_box()
            assert box.min.Z == pytest.approx(interface.female_opening_height)
            assert box.max.Z == pytest.approx(interface.female_opening_height)
            assert face.normal_at().Z < -0.99
