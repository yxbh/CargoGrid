"""Shared tile construction and default circular-hole keep-out checks."""

from copy import deepcopy
from dataclasses import dataclass
from math import hypot

from build123d import Axis, Face, Location, Part, Solid, Vector

from cargo_grid.interfaces import (
    dovetail_face,
    full_height_part,
    horizontal_edges,
    prism,
    rectangle,
    socket_entry_tool,
    tile_join_tool,
    underside_fillet,
    x_profile,
)
from cargo_grid.parameters import Interface, Tile


@dataclass(frozen=True)
class HolePlacement:
    x: float
    y: float
    accepted: bool
    reason: str


def socket_centers(tile: Tile) -> list[tuple[float, float]]:
    p = tile.interface.pitch
    return [((i + 0.5) * p, (j + 0.5) * p) for i in range(tile.nx) for j in range(tile.ny)]


def hole_placements(tile: Tile) -> list[HolePlacement]:
    if tile.hole_diameter is None:
        return []
    p = tile.interface.pitch
    candidates = [
        *((i * p, (j + 0.5) * p) for i in range(1, tile.nx) for j in range(tile.ny)),
        *(((i + 0.5) * p, j * p) for i in range(tile.nx) for j in range(1, tile.ny)),
        *((i * p, j * p) for i in range(1, tile.nx) for j in range(1, tile.ny)),
    ]
    if tile.hole_scope == "full":
        candidates = [
            (i * p / 2, j * p / 2)
            for i in range(2 * tile.nx + 1)
            for j in range(2 * tile.ny + 1)
            if not (i % 2 and j % 2)
        ]
    # Protect the actual top opening and a material band, rather than its bbox.
    protected = x_profile(
        tile.interface,
        offset=3 + tile.minimum_web + tile.interface.fit_offset,
    )
    result = []
    for x, y in candidates:
        w, d = tile.body_size
        if tile.hole_scope == "full" and (
            (x == 0 and not tile.west)
            or (x == w and not tile.east)
            or (y == 0 and not tile.south)
            or (y == d and not tile.north)
        ):
            result.append(HolePlacement(x, y, False, "terminated perimeter/filler keep-out"))
            continue
        clearance = min(
            protected.distance_to(Vector(x - cx, y - cy, 0)) for cx, cy in socket_centers(tile)
        )
        inside = any(
            protected.is_inside(Vector(x - cx, y - cy, 0)) for cx, cy in socket_centers(tile)
        )
        r = tile.hole_diameter / 2
        safe = (
            not inside
            and clearance >= r
            and (tile.hole_scope == "full" or min(x, y, w - x, d - y) >= 6.1 + r + tile.minimum_web)
        )
        # Neighbour holes must also retain the declared material band.
        safe = safe and all(
            hypot(x - ox, y - oy) >= 2 * r + tile.minimum_web
            for ox, oy in candidates
            if (ox, oy) != (x, y)
        )
        result.append(HolePlacement(x, y, safe, "" if safe else "socket/join/material keep-out"))
    return result


def _join_tool(
    interface: Interface,
    x: float,
    y: float,
    angle: float,
    depth: float,
) -> Face:
    return dovetail_face(interface, depth=depth).rotate(Axis.Z, angle).moved(Location((x, y, 0)))


def make_tile(tile: Tile = Tile()) -> Part:
    w, d = tile.body_size
    p, h = tile.interface.pitch, tile.interface.height
    body = rectangle(
        -tile.filler_west,
        -tile.filler_south,
        w + tile.filler_west + tile.filler_east,
        d + tile.filler_south + tile.filler_north,
    )

    tools: dict[tuple[float, bool], Part] = {}

    def tool(x: float, y: float, angle: float, depth: float, male: bool) -> Part:
        key = (depth, male)
        if key not in tools:
            tools[key] = tile_join_tool(tile.interface, depth=depth, male=male)
        # Located OCP shapes share topology; each Boolean needs an independent tool.
        solid = deepcopy(tools[key])
        return solid.rotate(Axis.Z, angle).moved(Location((x, y, 0)))

    male_tools = []
    female_tools = []
    male_faces = []
    female_faces = []
    for i in range(tile.nx):
        if tile.north:
            male_faces.append(
                _join_tool(tile.interface, (i + 0.5) * p, d, 0, tile.interface.male_join_depth)
            )
            if tile.interface.joint_style == "original":
                male_tools.append(
                    tool(
                        (i + 0.5) * p,
                        d,
                        0,
                        tile.interface.male_join_depth,
                        True,
                    )
                )
        if tile.south:
            female_faces.append(
                _join_tool(
                    tile.interface,
                    (i + 0.5) * p,
                    0,
                    0,
                    tile.interface.male_join_depth,
                )
            )
            if tile.interface.joint_style == "original":
                female_tools.append(
                    tool(
                        (i + 0.5) * p,
                        0,
                        0,
                        tile.interface.male_join_depth,
                        False,
                    )
                )
    for j in range(tile.ny):
        if tile.east:
            male_faces.append(
                _join_tool(
                    tile.interface,
                    w,
                    (j + 0.5) * p,
                    -90,
                    tile.interface.male_join_depth,
                )
            )
            if tile.interface.joint_style == "original":
                male_tools.append(
                    tool(
                        w,
                        (j + 0.5) * p,
                        -90,
                        tile.interface.male_join_depth,
                        True,
                    )
                )
        if tile.west:
            female_faces.append(
                _join_tool(
                    tile.interface,
                    0,
                    (j + 0.5) * p,
                    -90,
                    tile.interface.female_join_depth,
                )
            )
            if tile.interface.joint_style == "original":
                female_tools.append(
                    tool(
                        0,
                        (j + 0.5) * p,
                        -90,
                        tile.interface.female_join_depth,
                        False,
                    )
                )
    if tile.interface.joint_style == "full-height":
        part = full_height_part(
            body,
            male_faces,
            female_faces,
            h,
            interface_blend_radius=tile.interface.tile_join_blend_radius,
        )
    else:
        body = body.fillet_2d(1, body.vertices())
        part = prism(body, h)
        part = part.fillet(1, horizontal_edges(part, h))
        if male_tools:
            part = part.fuse(*male_tools, tol=1e-7)
        if female_tools:
            part = part.cut(*female_tools)
    part = part.clean()
    part = underside_fillet(part)
    if tile.interface.joint_style == "full-height":
        cutter = socket_entry_tool(tile.interface)
        part = part.cut(*(cutter.moved(Location((cx, cy, 0))) for cx, cy in socket_centers(tile)))
    elif tile.interface.unit_scale != 1:
        cutter = socket_entry_tool(tile.interface)
        part = part.cut(*(cutter.moved(Location((cx, cy, 0))) for cx, cy in socket_centers(tile)))
    else:
        profile = x_profile(tile.interface, offset=tile.interface.fit_offset)
        cutter = prism(profile, h + 2)
        part = part.cut(*(cutter.moved(Location((cx, cy, -1))) for cx, cy in socket_centers(tile)))
        entry_wires = [
            profile.outer_wire().moved(Location((cx, cy, h))) for cx, cy in socket_centers(tile)
        ]
        entry_edges = [
            e
            for e in horizontal_edges(part, h)
            if any(wire.distance_to(e.center()) < 1e-5 for wire in entry_wires)
        ]
        part = part.fillet(tile.interface.socket_entry_radius, entry_edges)
    holes = hole_placements(tile)
    cutters = [
        Solid.make_cylinder(tile.hole_diameter / 2, h + 2).moved(Location((hole.x, hole.y, -1)))
        for hole in holes
        if hole.accepted
    ]
    if tile.hole_scope == "full" and cutters:
        part = part.cut(*cutters).clean()
    else:
        for cutter in cutters:
            part = part.cut(cutter)
    part.label = f"tile_{tile.nx}x{tile.ny}_{tile.interface.joint_style}"
    if not part.is_valid or len(part.solids()) != 1:
        raise ValueError(f"{part.label}: invalid or disconnected geometry")
    return part
