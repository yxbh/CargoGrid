"""X-plug pull handle, with or without a front strap bar, in physical millimetres.

The handle gives a hand-sized grip for sliding an assembled floor in and out of a vehicle
tray. Its body is the intersection of a front-view profile (posts, gussets and hand opening)
and a side-view profile (posts that flare straight down to the seating edges), with only the
remaining intersection edges rounded in 3D.

The posts, grip and hand opening are sized for a hand, so unit size doesn't scale them. The
footprint is the fewest whole cells that cover at least 120 x 60 mm, with an X plug in every
cell; tile thickness sets the plug depth like other X attachments.
"""

from dataclasses import dataclass
from math import asin, atan2, ceil, cos, degrees, hypot, sin
from typing import ClassVar

from build123d import Axis, CenterOf, Face, Location, Part, Solid, Wire
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet

from cargo_grid.interfaces import horizontal_edges, make_plug, prism, rectangle
from cargo_grid.parameters import Interface

PULL_HANDLE_FAMILY = "pull-handle"
MIN_WIDTH_MM = 120.0
MIN_DEPTH_MM = 60.0
BASE_MM = 4.1
POST_WIDTH_MM = 16.0
HAND_CLEARANCE_MM = 40.0
GRIP_DEPTH_MM = 24.0
GRIP_HEIGHT_MM = 18.0
FLARE_NECK_MM = 25.0
GUSSET_WIDTH_MM = 10.0
GUSSET_HEIGHT_MM = 14.0
NECK_RADIUS_MM = 10.0
WINDOW_TOP_RADIUS_MM = 8.0
GUSSET_KNEE_RADIUS_MM = 6.0
WINDOW_FOOT_RADIUS_MM = 4.0
OUTER_EDGE_RADIUS_MM = 6.0
WINDOW_EDGE_RADIUS_MM = 3.0
SEAT_EDGE_RADIUS_MM = 1.5
PLUG_ROOT_RADIUS_MM = 2.0
STRAP_SLOT_WIDTH_MM = 30.0
STRAP_SLOT_HEIGHT_MM = 8.0
STRAP_BAR_WIDTH_MM = 52.0
STRAP_BAR_DEPTH_MM = 12.0
STRAP_BAR_THICKNESS_MM = 8.0
STRAP_BAR_CENTRE_OFFSET_MM = 25.0
STRAP_BAR_TOP_RADIUS_MM = 3.0
NO_STRAP_BAR_PRINT_ROTATION_X = 180.0
# Provisional margin against tipping while the strap-bar handle prints on its front; not measured.
MIN_FRONT_REST_MARGIN_MM = 5.0

_SHARP_EDGE_DEGREES = 5.0
_EPSILON = 1e-6


@dataclass(frozen=True)
class PullHandle:
    """A hand-sized pull handle; ``strap_bar`` adds a front bar with a slot for a strap or rope."""

    strap_bar: bool = True
    interface: Interface = Interface()
    family: ClassVar[str] = PULL_HANDLE_FAMILY

    def __post_init__(self) -> None:
        if not isinstance(self.strap_bar, bool):
            raise ValueError("pull handle strap_bar must be true or false")
        if not isinstance(self.interface, Interface):
            raise ValueError("interface must be an Interface")
        if self.interface.solid_bottom_mm:
            raise ValueError(
                "a solid bottom applies only to tiles, edge/corner pieces and ramps; "
                "pull handles are unchanged"
            )
        # Equal interfaces can differ in int/float spelling; keep one identity for every caller.
        interface = self.interface
        object.__setattr__(
            self,
            "interface",
            Interface(
                float(interface.pitch),
                float(interface.height),
                float(interface.fit_offset),
                interface.joint_style,
            ),
        )


def _cells(minimum: float, pitch: float) -> int:
    return max(1, ceil(minimum / pitch - 1e-9))


def width_cells(interface: Interface = Interface()) -> int:
    return _cells(MIN_WIDTH_MM, interface.pitch)


def depth_cells(interface: Interface = Interface()) -> int:
    return _cells(MIN_DEPTH_MM, interface.pitch)


def width_mm(interface: Interface = Interface()) -> float:
    return width_cells(interface) * interface.pitch


def depth_mm(interface: Interface = Interface()) -> float:
    return depth_cells(interface) * interface.pitch


def grip_top_mm() -> float:
    return BASE_MM + HAND_CLEARANCE_MM + GRIP_HEIGHT_MM


def grip_front_y_mm(interface: Interface = Interface()) -> float:
    return (depth_mm(interface) - GRIP_DEPTH_MM) / 2


def strap_bar_top_mm() -> float:
    return BASE_MM + STRAP_SLOT_HEIGHT_MM + STRAP_BAR_THICKNESS_MM


def strap_bar_front_y_mm(interface: Interface = Interface()) -> float:
    return depth_mm(interface) / 2 - STRAP_BAR_CENTRE_OFFSET_MM


def plug_centers(interface: Interface = Interface()) -> list[tuple[float, float, float]]:
    pitch = interface.pitch
    return [
        ((i + 0.5) * pitch, (j + 0.5) * pitch, 0.0)
        for i in range(width_cells(interface))
        for j in range(depth_cells(interface))
    ]


def _tangent_tilt(lower: tuple[float, float], radius: float, grip, grip_radius: float) -> float:
    """Rest-normal tilt b for a face tangent to the grip arc and one lower arc in the YZ view.

    The outward normal is (-cos b, sin b): b=0 faces front and b=90 degrees faces up.
    """
    dy, dz = grip[0] - lower[0], grip[1] - lower[1]
    return atan2(dy, dz) + asin((radius - grip_radius) / hypot(dy, dz))


def _front_rest(interface: Interface) -> tuple[float, list[tuple[tuple[float, float], float]]]:
    """Front rest tilt and the two YZ arcs (centre, radius) it touches: grip, then lower edge."""
    front = grip_front_y_mm(interface)
    grip = (
        (front + OUTER_EDGE_RADIUS_MM, grip_top_mm() - OUTER_EDGE_RADIUS_MM),
        OUTER_EDGE_RADIUS_MM,
    )
    bar = (
        (
            strap_bar_front_y_mm(interface) + STRAP_BAR_TOP_RADIUS_MM,
            strap_bar_top_mm() - STRAP_BAR_TOP_RADIUS_MM,
        ),
        STRAP_BAR_TOP_RADIUS_MM,
    )
    # The seat edge rounds the corner between the seat (along +Y) and the flare towards the neck.
    flare = hypot(front, FLARE_NECK_MM)
    bisector = (1 + front / flare, FLARE_NECK_MM / flare)
    corner = atan2(FLARE_NECK_MM, front)
    offset = SEAT_EDGE_RADIUS_MM / sin(corner / 2) / hypot(*bisector)
    seat = ((bisector[0] * offset, bisector[1] * offset), SEAT_EDGE_RADIUS_MM)
    tilt, lower = max(
        (_tangent_tilt(centre, radius, grip[0], grip[1]), (centre, radius))
        for centre, radius in (bar, seat)
    )
    if not 0 < tilt < atan2(front, FLARE_NECK_MM):
        raise ValueError("pull handle front rest face is outside its contact edges")
    return tilt, [grip, lower]


def print_rotation_x(strap_bar: bool, interface: Interface = Interface()) -> float:
    """X rotation that lays each version on its least-support resting face.

    Without the strap bar the flat grip top goes on the bed. With it, the handle lies on its
    front against the rounded top-front grip edge and the next convex edge below it: the strap
    bar's top-front edge, or the front seat edge on deeper handles. These edges run along X, so
    the rest face is a common tangent in the side (YZ) view. Of the candidate tangents through
    the grip, the hull face is the one tilted furthest toward the top.
    """
    if not strap_bar:
        return NO_STRAP_BAR_PRINT_ROTATION_X
    return 90.0 + degrees(_front_rest(interface)[0])


def front_rest_margin_mm(shape: Part, interface: Interface = Interface()) -> float:
    """How far the centre of mass sits inside the strap-bar handle's front rest contacts.

    Measured in the bed plane across the two contact lines; negative means it would tip over.
    """
    tilt, arcs = _front_rest(interface)
    normal = (-cos(tilt), sin(tilt))
    along = (sin(tilt), cos(tilt))
    contacts = [
        along[0] * (y + radius * normal[0]) + along[1] * (z + radius * normal[1])
        for (y, z), radius in arcs
    ]
    centre = shape.center(CenterOf.MASS)
    position = along[0] * centre.Y + along[1] * centre.Z
    return min(position - min(contacts), max(contacts) - position)


def _face(points, to3d) -> Face:
    return Face(Wire.make_polygon([to3d(*point) for point in points], close=True))


def _rounded(face: Face, corners) -> Face:
    for radius, targets in corners:
        vertices = [
            vertex
            for vertex in face.vertices()
            if any(
                abs(vertex.X - x) + abs(vertex.Y - y) + abs(vertex.Z - z) < _EPSILON
                for x, y, z in targets
            )
        ]
        if len(vertices) != len(targets):
            raise ValueError(f"pull handle profile: expected {len(targets)} corners to round")
        face = face.fillet_2d(radius, vertices)
    return face


def _front_profile(interface: Interface) -> Face:
    def xz(x, z):
        return (x, 0.0, z)

    width, top = width_mm(interface), grip_top_mm()
    outer = _face([(0, 0), (width, 0), (width, top), (0, top)], xz)
    left, right = POST_WIDTH_MM, width - POST_WIDTH_MM
    knee, ceiling = BASE_MM + GUSSET_HEIGHT_MM, BASE_MM + HAND_CLEARANCE_MM
    window = _face(
        [
            (left + GUSSET_WIDTH_MM, BASE_MM),
            (right - GUSSET_WIDTH_MM, BASE_MM),
            (right, knee),
            (right, ceiling),
            (left, ceiling),
            (left, knee),
        ],
        xz,
    )
    window = _rounded(
        window,
        [
            (WINDOW_TOP_RADIUS_MM, [xz(left, ceiling), xz(right, ceiling)]),
            (GUSSET_KNEE_RADIUS_MM, [xz(left, knee), xz(right, knee)]),
            (
                WINDOW_FOOT_RADIUS_MM,
                [xz(left + GUSSET_WIDTH_MM, BASE_MM), xz(right - GUSSET_WIDTH_MM, BASE_MM)],
            ),
        ],
    )
    faces = outer.cut(window).faces()
    if len(faces) != 1:
        raise ValueError("pull handle front profile must stay one face")
    return Face(faces[0].wrapped)


def _side_profile(interface: Interface) -> Face:
    def yz(y, z):
        return (0.0, y, z)

    depth, top = depth_mm(interface), grip_top_mm()
    front = grip_front_y_mm(interface)
    back = front + GRIP_DEPTH_MM
    tower = _face(
        [
            (0, 0),
            (depth, 0),
            (back, FLARE_NECK_MM),
            (back, top),
            (front, top),
            (front, FLARE_NECK_MM),
        ],
        yz,
    )
    return _rounded(tower, [(NECK_RADIUS_MM, [yz(front, FLARE_NECK_MM), yz(back, FLARE_NECK_MM)])])


def _sharp_edges(part: Part) -> list:
    sharp = []
    for edge in part.edges():
        faces = [face for face in part.faces() if any(e.is_same(edge) for e in face.edges())]
        if len(faces) != 2:
            continue
        point = edge.position_at(0.5)
        if faces[0].normal_at(point).get_angle(faces[1].normal_at(point)) > _SHARP_EDGE_DEGREES:
            sharp.append(edge)
    return sharp


def _round(part: Part, radii: dict, what: str) -> Part:
    if not radii:
        raise ValueError(f"pull handle {what}: no edges selected")
    operation = BRepFilletAPI_MakeFillet(part.wrapped)
    for edge, radius in radii.items():
        operation.Add(radius, edge.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise ValueError(f"pull handle {what}: fillet failed")
    result = Part([Solid(operation.Shape())])
    if not result.is_valid or len(result.solids()) != 1:
        raise ValueError(f"pull handle {what}: fillet produced invalid geometry")
    return result


def _body(interface: Interface) -> Part:
    width, depth = width_mm(interface), depth_mm(interface)
    front = Part([Solid.extrude(_front_profile(interface), (0, depth, 0))])
    side = Part([Solid.extrude(_side_profile(interface), (width, 0, 0))])
    solids = front.intersect(side).solids()
    if len(solids) != 1:
        raise ValueError("pull handle body must be one solid")
    body = Part([solids[0]])
    top = grip_top_mm()

    def outer(box) -> bool:
        return box.max.Z > _EPSILON and (
            box.max.X < _EPSILON or box.min.X > width - _EPSILON or box.min.Z > top - _EPSILON
        )

    def window(box) -> bool:
        return (
            box.max.Z > _EPSILON
            and box.min.X > POST_WIDTH_MM - 1e-3
            and box.max.X < width - POST_WIDTH_MM + 1e-3
        )

    def seat(box) -> bool:
        return box.max.Z < _EPSILON

    # The seat perimeter is solved together with the outer corners: on shallow flares a
    # separate seat pass can't blend into the R6 ends of the flare edges.
    edges = _sharp_edges(body)
    body = _round(
        body,
        {
            **{edge: OUTER_EDGE_RADIUS_MM for edge in edges if outer(edge.bounding_box())},
            **{edge: SEAT_EDGE_RADIUS_MM for edge in edges if seat(edge.bounding_box())},
        },
        "outer corners, grip top and seat perimeter",
    )
    edges = [edge for edge in _sharp_edges(body) if window(edge.bounding_box())]
    body = _round(body, dict.fromkeys(edges, WINDOW_EDGE_RADIUS_MM), "hand opening")
    if _sharp_edges(body):
        raise ValueError("pull handle body still has unrounded edges")
    return body


def _strap_bar(interface: Interface) -> Part:
    width = width_mm(interface)
    height = STRAP_SLOT_HEIGHT_MM + STRAP_BAR_THICKNESS_MM
    bar = prism(
        rectangle(
            (width - STRAP_BAR_WIDTH_MM) / 2,
            strap_bar_front_y_mm(interface),
            STRAP_BAR_WIDTH_MM,
            STRAP_BAR_DEPTH_MM,
        ),
        height,
    ).moved(Location((0, 0, BASE_MM)))
    # The slot cutter overshoots the bar's front, back and underside to avoid coplanar faces.
    slot = prism(
        rectangle(
            (width - STRAP_SLOT_WIDTH_MM) / 2,
            strap_bar_front_y_mm(interface) - 1,
            STRAP_SLOT_WIDTH_MM,
            STRAP_BAR_DEPTH_MM + 2,
        ),
        STRAP_SLOT_HEIGHT_MM + 0.5,
    ).moved(Location((0, 0, BASE_MM - 0.5)))
    bar = bar.cut(slot)
    return bar.fillet(STRAP_BAR_TOP_RADIUS_MM, horizontal_edges(bar, BASE_MM + height))


def make_pull_handle(spec: PullHandle = PullHandle()) -> Part:
    """Source frame: southwest body corner at the origin, seating face on Z=0, plugs below."""
    interface = spec.interface
    part = _body(interface)
    if spec.strap_bar:
        part = part.fuse(_strap_bar(interface)).clean()
    centers = plug_centers(interface)
    plug = make_plug(interface).rotate(Axis.X, 180)
    for center in centers:
        part = part.fuse(plug.moved(Location(center)))
    root_limit = interface.pitch * 23 / 60
    roots = [
        edge
        for edge in horizontal_edges(part, 0)
        if any(
            abs(edge.center().X - x) < root_limit and abs(edge.center().Y - y) < root_limit
            for x, y, _ in centers
        )
    ]
    part = part.fillet(PLUG_ROOT_RADIUS_MM, roots).clean()
    if not part.is_valid or len(part.solids()) != 1 or part.volume <= 0:
        raise ValueError("pull handle: invalid or disconnected geometry")
    if spec.strap_bar:
        margin = front_rest_margin_mm(part, interface)
        if margin < MIN_FRONT_REST_MARGIN_MM:
            raise ValueError(
                f"the strap-bar pull handle wouldn't rest steadily on its front for printing "
                f"with {interface.height:g} mm tiles: its centre of mass is {margin:.1f} mm "
                f"inside its resting edges, and at least {MIN_FRONT_REST_MARGIN_MM:g} mm is "
                "needed; use a thinner tile setting"
            )
    return part


def pull_handle_datums(spec: PullHandle = PullHandle()) -> dict:
    interface = spec.interface
    datums = {
        "cells": (width_cells(interface), depth_cells(interface)),
        "footprint_mm": (width_mm(interface), depth_mm(interface)),
        "x_plug_centers": plug_centers(interface),
        "seating_plane_z": 0.0,
        "plug_depth_mm": interface.plug_depth,
        "hand_opening_mm": {
            "width": width_mm(interface) - 2 * POST_WIDTH_MM,
            "height": HAND_CLEARANCE_MM,
        },
        "grip_mm": {"depth": GRIP_DEPTH_MM, "height": GRIP_HEIGHT_MM, "top_z": grip_top_mm()},
        "strap_slot_mm": None,
    }
    if spec.strap_bar:
        datums["strap_slot_mm"] = {
            "width": STRAP_SLOT_WIDTH_MM,
            "height": STRAP_SLOT_HEIGHT_MM,
            "front_y": strap_bar_front_y_mm(interface),
            "depth": STRAP_BAR_DEPTH_MM,
        }
    return datums
