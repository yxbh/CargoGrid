"""Plus- and X-shaped posts that hold box corners on the mat, in physical millimetres.

A cross post is a column whose arms run along the grid lines. A box corner sits in any quadrant,
against two arm faces, so one post holds up to four box corners where boxes meet. The tee drops
one arm for posts beside a mat edge or along a single row of boxes; the corner keeps two arms for
one box corner. The X post turns the four arms onto the diagonals for loads placed at 45 degrees.

The post has no base plate: the arms stand straight on the mat, so a box corner rests flat on the
mat right up to the post instead of riding up on a plate. Two mounts are available:

- ``pegs`` puts a round peg under the centre and under each arm tip. Tips sit half a unit from the
  centre, which is the full round-hole pattern's spacing, so the post fits at every half-unit
  point: cell corners, edge midpoints and over X sockets, where the centre peg only stands in the
  open socket and the tip pegs hold the post. That is twice as many positions along each grid
  line as an X-plug post. The centre peg is the nominal hole size; the tip pegs leave a gap of
  0.15 mm a side, about the X plug's 0.14 mm lobe-tip gap, so they locate the post without
  several snug pegs fighting each other across a tile seam.
- ``x-plug`` uses the shared X plug under the centre, for tiles without round holes. It fits over
  X sockets only.

An X-plug X post is the cross turned 45 degrees, with its arms over the plug's lobes. A peg X post
can't simply turn: its tips reach the diagonal holes half a unit away along both grid directions,
so its arms are longer, and it fits over X sockets and at edge midpoints but not at cell corners,
where only its centre peg would find a hole.

Arms are 14 mm thick, so box faces sit 7 mm either side of an arm's centre line. Their tops lean
in by 3 mm over the top 12 mm, which guides a box lowered from above into place. Arm thickness,
the lead-in, heights and peg size stay in physical millimetres. Unit size sets only the arm reach
and the X plug; tile thickness sets insertion depth.

Source frame: the post centre is on the Z axis, the seating plane is Z=0 and the pegs or plug
point down.
"""

from dataclasses import dataclass
from math import hypot
from typing import ClassVar, Literal

from build123d import Axis, Edge, Face, Location, Part, Solid, Vector, Wire
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer

from cargo_grid.interfaces import horizontal_edges, make_plug, prism, rectangle, x_profile
from cargo_grid.parameters import DEFAULT_HOLE_DIAMETER_MM, Interface, Tile, positive
from cargo_grid.rods import PEG_LEAD_IN_MM, UNDERSIDE_CLEARANCE_MM
from cargo_grid.tiles import hole_placements

CROSS_POST_FAMILY = "cross-post"
ARM_LAYOUTS = {
    "cross": ((1, 0), (0, 1), (-1, 0), (0, -1)),
    "tee": ((1, 0), (0, 1), (-1, 0)),
    "corner": ((1, 0), (0, 1)),
    "x": ((1, 1), (-1, 1), (-1, -1), (1, -1)),
}
MOUNTS = ("pegs", "x-plug")
CROSS_POST_HEIGHTS_MM = (60.0, 120.0)
MIN_HEIGHT_MM = 30.0
MAX_HEIGHT_MM = 240.0
ARM_THICKNESS_MM = 14.0
LEAD_IN_INSET_MM = 3.0
LEAD_IN_HEIGHT_MM = 12.0
MIN_PEG_DIAMETER_MM = 9.0
MAX_PEG_DIAMETER_MM = 10.5
# Tip pegs only locate the post; this gap per side is about the X plug's 0.14 mm lobe-tip gap.
TIP_PEG_CLEARANCE_MM = 0.15
PLUG_ROOT_RADIUS_MM = 1.0


@dataclass(frozen=True)
class CrossPost:
    """One post. ``peg_diameter_mm`` is the centre peg, defaulting to the 10 mm hole size.

    Tip pegs are ``2 * TIP_PEG_CLEARANCE_MM`` smaller. X-plug posts have no pegs.
    """

    arms: Literal["cross", "tee", "corner", "x"] = "cross"
    mount: Literal["pegs", "x-plug"] = "pegs"
    height_mm: float = 60.0
    peg_diameter_mm: float | None = None
    interface: Interface = Interface()
    family: ClassVar[str] = CROSS_POST_FAMILY

    def __post_init__(self) -> None:
        if self.arms not in ARM_LAYOUTS:
            raise ValueError("cross post arms must be cross, tee, corner or x")
        if self.mount not in MOUNTS:
            raise ValueError("cross post mount must be pegs or x-plug")
        positive("cross post height", self.height_mm)
        if not MIN_HEIGHT_MM <= self.height_mm <= MAX_HEIGHT_MM:
            raise ValueError(
                f"cross post height must be between {MIN_HEIGHT_MM:g} and {MAX_HEIGHT_MM:g} mm"
            )
        if not isinstance(self.interface, Interface):
            raise ValueError("interface must be an Interface")
        if self.interface.solid_bottom_mm:
            raise ValueError(
                "a solid bottom applies only to tiles, edge/corner pieces and ramps; "
                "cross posts are unchanged"
            )
        if self.mount == "x-plug":
            if self.peg_diameter_mm is not None:
                raise ValueError("x-plug cross posts have no pegs; omit the peg diameter")
        else:
            if self.interface.fit_offset:
                raise ValueError(
                    "peg-mounted cross posts use the peg diameter, not the X socket fit offset"
                )
            diameter = (
                DEFAULT_HOLE_DIAMETER_MM if self.peg_diameter_mm is None else self.peg_diameter_mm
            )
            positive("cross post peg diameter", diameter)
            if not MIN_PEG_DIAMETER_MM <= diameter <= MAX_PEG_DIAMETER_MM:
                raise ValueError(
                    f"cross post peg diameter must be between {MIN_PEG_DIAMETER_MM:g} and "
                    f"{MAX_PEG_DIAMETER_MM:g} mm"
                )
            object.__setattr__(self, "peg_diameter_mm", float(diameter))
            if not peg_sites_accepted(self.interface, diameter):
                raise ValueError(
                    f"{self.interface.pitch:g} mm units leave no full-pattern round holes at "
                    "every half-unit point; use the x-plug mount"
                )
        object.__setattr__(self, "height_mm", float(self.height_mm))
        object.__setattr__(self, "interface", normalized_interface(self.interface))


def normalized_interface(interface: Interface) -> Interface:
    """Equal interfaces can differ in int/float spelling; keep one identity for every caller."""
    return Interface(
        float(interface.pitch),
        float(interface.height),
        float(interface.fit_offset),
        interface.joint_style,
        float(interface.solid_bottom_mm),
    )


def peg_sites_accepted(
    interface: Interface = Interface(), diameter: float = DEFAULT_HOLE_DIAMETER_MM
) -> bool:
    """Whether full-pattern tiles keep a round hole at every peg of a post on any half-unit point.

    A post centred on the shared corner of a 2x2 tile reaches the four surrounding edge
    midpoints; every other position, including the X post's diagonal tips, uses the same corner
    and midpoint sites.
    """
    p = interface.pitch
    sites = {
        (hole.x, hole.y): hole.accepted
        for hole in hole_placements(Tile(2, 2, interface, diameter, hole_scope="full"))
    }
    return all(
        sites.get(site, False)
        for site in ((p, p), (p / 2, p), (3 * p / 2, p), (p, p / 2), (p, 3 * p / 2))
    )


def tip_centers(spec: CrossPost) -> list[tuple[float, float]]:
    """Arm-tip centres: half a unit out, or on the diagonal holes for a peg X post."""
    half = spec.interface.pitch / 2
    return [
        (
            float(dx * half / (1 if spec.mount == "pegs" else hypot(dx, dy))),
            float(dy * half / (1 if spec.mount == "pegs" else hypot(dx, dy))),
        )
        for dx, dy in ARM_LAYOUTS[spec.arms]
    ]


def tip_distance_mm(spec: CrossPost) -> float:
    """Distance from the post centre to each arm tip."""
    x, y = tip_centers(spec)[0]
    return hypot(x, y)


def arm_reach_mm(spec: CrossPost) -> float:
    return tip_distance_mm(spec) + ARM_THICKNESS_MM / 2


def peg_insertion_mm(interface: Interface = Interface()) -> float:
    return interface.height - UNDERSIDE_CLEARANCE_MM


def peg_centers(spec: CrossPost) -> list[tuple[float, float, float]]:
    if spec.mount != "pegs":
        return []
    return [(0.0, 0.0, 0.0)] + [(x, y, 0.0) for x, y in tip_centers(spec)]


def _arm_face(tip: tuple[float, float]) -> Face:
    """Plan outline of one arm: a slot from the centre to the tip with round ends."""
    radius = ARM_THICKNESS_MM / 2
    x, y = tip
    length = hypot(x, y)
    ux, uy = x / length, y / length
    nx, ny = -uy * radius, ux * radius
    start, end = Vector(nx, ny, 0), Vector(x + nx, y + ny, 0)
    far, near = Vector(x - nx, y - ny, 0), Vector(-nx, -ny, 0)
    return Face(
        Wire(
            [
                Edge.make_line(start, end),
                Edge.make_three_point_arc(end, Vector(x + ux * radius, y + uy * radius, 0), far),
                Edge.make_line(far, near),
                Edge.make_three_point_arc(near, Vector(-ux * radius, -uy * radius, 0), start),
            ]
        )
    )


def plan_face(spec: CrossPost) -> Face:
    """Arm outlines joined at the centre, with sharp inner corners where box corners seat."""
    arms = [_arm_face(tip) for tip in tip_centers(spec)]
    faces = arms[0].fuse(*arms[1:]).clean().faces()
    if len(faces) != 1:
        raise ValueError("cross post plan must be one face")
    return Face(faces[0].wrapped)


def _body(spec: CrossPost) -> Part:
    height = spec.height_mm
    body = Part([Solid.extrude(plan_face(spec), (0, 0, height))])
    (top,) = [
        face
        for face in body.faces()
        if face.center().Z > height - 1e-6 and face.normal_at().Z > 0.5
    ]
    # The lead-in leans every top edge inward; its long side runs down the arm faces.
    operation = BRepFilletAPI_MakeChamfer(body.wrapped)
    for edge in horizontal_edges(body, height):
        operation.Add(LEAD_IN_INSET_MM, LEAD_IN_HEIGHT_MM, edge.wrapped, top.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("cross post lead-in chamfer failed")
    result = Part([Solid(operation.Shape())])
    if not result.is_valid or len(result.solids()) != 1:
        raise ValueError("cross post lead-in produced invalid geometry")
    return result


def tip_peg_diameter_mm(spec: CrossPost) -> float | None:
    if spec.peg_diameter_mm is None:
        return None
    return spec.peg_diameter_mm - 2 * TIP_PEG_CLEARANCE_MM


def _pegs(spec: CrossPost) -> list[Solid]:
    depth = peg_insertion_mm(spec.interface)
    pegs = []
    for index, (x, y, _) in enumerate(peg_centers(spec)):
        radius = (spec.peg_diameter_mm if index == 0 else tip_peg_diameter_mm(spec)) / 2
        pegs.append(
            Solid.make_cylinder(radius, depth - PEG_LEAD_IN_MM).moved(
                Location((x, y, PEG_LEAD_IN_MM - depth))
            )
        )
        pegs.append(
            Solid.make_cone(radius - PEG_LEAD_IN_MM, radius, PEG_LEAD_IN_MM).moved(
                Location((x, y, -depth))
            )
        )
    return pegs


def _with_plug(body: Part, spec: CrossPost) -> Part:
    """Fuse the X plug, rounding its roots only under the arms.

    The arm faces cross the plug outline at angles that change with unit size, so the root
    round is solved around the whole outline against a flat plate and then trimmed to the arm
    outline. The lobe tops between the arms stay flush with the mat.
    """
    interface = spec.interface
    plug = make_plug(interface).rotate(Axis.X, 180)
    half = arm_reach_mm(spec) + 1
    plate = prism(rectangle(-half, -half, 2 * half, 2 * half), PLUG_ROOT_RADIUS_MM)
    mounted = plate.fuse(plug)
    outline = x_profile(interface, plug=True).outer_wire()
    roots = [
        edge
        for edge in horizontal_edges(mounted, 0)
        if outline.distance_to(Vector(edge.center().X, edge.center().Y, 0)) < 1e-5
    ]
    mounted = mounted.fillet(PLUG_ROOT_RADIUS_MM, roots)
    under_arms = prism(plan_face(spec), interface.plug_depth + PLUG_ROOT_RADIUS_MM).moved(
        Location((0, 0, -interface.plug_depth))
    )
    rounded_roots = Part(mounted.intersect(under_arms).solids())
    return Part(body.fuse(plug, rounded_roots).clean().solids())


def make_cross_post(spec: CrossPost = CrossPost()) -> Part:
    """Upright source frame: post centre on the Z axis, seating plane Z=0, pegs or plug below."""
    body = _body(spec)
    if spec.mount == "pegs":
        part = Part(body.fuse(*_pegs(spec)).clean().solids())
    else:
        part = _with_plug(body, spec)
    if not part.is_valid or len(part.solids()) != 1 or part.volume <= 0:
        raise ValueError(f"{spec.arms} cross post: invalid or disconnected geometry")
    return part


def cross_post_datums(spec: CrossPost) -> dict:
    """Nominal mating datums; no physical-fit assertions."""
    interface = spec.interface
    datums = {
        "post_center": (0.0, 0.0, 0.0),
        "seating_plane_z": 0.0,
        "arms": spec.arms,
        "arm_directions": [list(direction) for direction in ARM_LAYOUTS[spec.arms]],
        "arm_thickness_mm": ARM_THICKNESS_MM,
        "arm_reach_mm": arm_reach_mm(spec),
        "box_face_offset_from_arm_centre_line_mm": ARM_THICKNESS_MM / 2,
        "lead_in_mm": {"inset": LEAD_IN_INSET_MM, "height": LEAD_IN_HEIGHT_MM},
        "height_above_mat_mm": spec.height_mm,
        "mount": spec.mount,
        "physical_fit_verified": False,
    }
    if spec.mount == "pegs":
        datums.update(
            positions=(
                "over X sockets and at edge midpoints"
                if spec.arms == "x"
                else "every half-unit point: cell corners, edge midpoints and over X sockets"
            ),
            peg_centers=peg_centers(spec),
            peg_diameter_mm=spec.peg_diameter_mm,
            tip_peg_diameter_mm=tip_peg_diameter_mm(spec),
            tip_peg_clearance_per_side_mm=TIP_PEG_CLEARANCE_MM,
            peg_insertion_mm=peg_insertion_mm(interface),
            peg_lead_in_mm=PEG_LEAD_IN_MM,
            tip_peg_distance_mm=tip_distance_mm(spec),
        )
    else:
        datums.update(
            positions="over X sockets at cell centres",
            x_plug_center=(0.0, 0.0, 0.0),
            plug_depth_mm=interface.plug_depth,
            plug_root_radius_mm=PLUG_ROOT_RADIUS_MM,
        )
    return datums
