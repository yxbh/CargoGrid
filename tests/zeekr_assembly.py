"""Assemble ordinary generator parts with the nine rear contour pieces for tests."""

from copy import deepcopy
from dataclasses import replace

from cargo_grid.accessories import accessory_datums, tile_matched_perimeter
from cargo_grid.catalogue import accessory_design
from cargo_grid.jobs import tile_design
from cargo_grid.parameters import Tile
from cargo_grid.vehicles.zeekr_7x_contours import (
    TILE_MODULES,
    ContourParameters,
    rear_panel_designs,
)


def assemble_rear_panel():
    parameters = ContourParameters()
    tiles, north = [], []
    tile_sources, north_sources = {}, {}
    x = parameters.field_x_min_mm
    for index, cells in enumerate(TILE_MODULES):
        if cells not in tile_sources:
            tile_sources[cells] = tile_design(
                Tile(cells, 4, parameters.interface, hole_diameter=10, hole_scope="full")
            )
            spec = tile_matched_perimeter(
                "edge-y",
                nx=cells,
                outward=30,
                interface=parameters.interface,
                hole_diameter=10,
                hole_scope="full",
            )
            north_sources[cells] = accessory_design(spec)
            north_sources[cells].mating_datums = accessory_datums(spec)
        tiles.append(
            replace(
                tile_sources[cells],
                shape=deepcopy(tile_sources[cells].shape),
                name=f"zeekr_rear_test_tile_{index + 1}_{cells}x4",
                assembly_frames=[(x, parameters.field_assembly_y_min_mm, 0)],
            )
        )
        north.append(
            replace(
                north_sources[cells],
                shape=deepcopy(north_sources[cells].shape),
                name=f"zeekr_rear_north_{index + 1}_{cells}cell",
                assembly_frames=[(x, parameters.field_assembly_y_max_mm, 0)],
            )
        )
        x += cells * parameters.interface.pitch
    sides, south = rear_panel_designs(parameters)
    return tiles + north + south + sides
