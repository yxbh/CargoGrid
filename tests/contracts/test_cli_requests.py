"""CLI parameter resolution does not construct CAD or invoke an exporter."""

import pytest

from cargo_grid import cli
from cargo_grid.accessories import Accessory
from cargo_grid.parameters import Interface, Tile
from cargo_grid.rods import Rod, RodBrace

INTERFACE = Interface(pitch=30, height=8)


@pytest.mark.parametrize(
    "family,options,expected",
    [
        ("tile", [], Tile(1, 1, INTERFACE, 10)),
        (
            "ramp",
            ["--width-cells", "2", "--ramp-join", "male"],
            Accessory("ramp", nx=2, interface=INTERFACE, ramp_join="male"),
        ),
        (
            "rod",
            ["--rod-height-mm", "240", "--peg-diameter-mm", "10.2"],
            Rod(240, 10.2, 8),
        ),
        (
            "rod-brace",
            ["--brace-spacing-mm", "120", "--bore-diameter-mm", "10.2"],
            RodBrace(120, 10.2),
        ),
    ],
)
def test_part_request_resolves_a_narrow_spec_without_building_geometry(
    family, options, expected, monkeypatch
):
    def unexpected(*args, **kwargs):
        pytest.fail("parameter resolution constructed geometry")

    monkeypatch.setattr(cli, "tile_design", unexpected)
    monkeypatch.setattr(cli, "accessory_design", unexpected)
    args = cli.parser().parse_args(["part", "--family", family, *options, "--output", "unused"])
    assert cli._part_specification(args, INTERFACE, 10) == expected
