"""Original accessory bodies surrounding the shared functional interfaces.

Base X plugs point down from their attachment shoulder at Z=0; vertical tile
brackets also carry panel plugs along positive Y. Edging uses the tile's underside datum, Z=0. Physical support rails have
their upper bearing surface at Z=0 and extend down 25 mm; these are not slicer
supports and have no X-shaped attachment plugs.

Tile-facing joins reuse the shared rounded shoulder and ledge construction.
Support joins are a separate, full-height, 5 mm-deep dovetail. Their 5.085 mm
female depth is a nominal reconstruction, not a certified clearance. Support
ends 1/2/3/4 denote X/Xs/Y/Ys respectively. Non-mating outlines, reinforcement,
and lightening apertures are independently constructed, not reference contours.
"""

from dataclasses import asdict, dataclass
from functools import lru_cache
from math import atan, degrees, sqrt, tan
from typing import Literal

from build123d import Axis, Face, GeomType, Location, Part, Solid, Wire
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet

from cargo_grid.interfaces import (
    dovetail_face,
    full_height_part,
    horizontal_edges,
    make_plug,
    prism,
    rectangle,
    tile_join_tool,
    x_profile,
)
from cargo_grid.parameters import DEFAULT_HOLE_DIAMETER_MM, Interface, Tile, count, positive
from cargo_grid.rods import Rod, RodBrace, brace_datums, make_rod, make_rod_brace, rod_datums
from cargo_grid.tiles import hole_placements

FAMILIES = (
    "edge-x",
    "edge-y",
    "corner-in",
    "corner-out",
    "ramp",
    "vertical-tile-bracket",
    "vertical-stop",
    "lock-45",
    "plate",
    "support",
    "support-bit",
    "support-end",
)
SUPPORT_END_NAMES = ("X", "Xs", "Y", "Ys")
VERTICAL_BRACKET_CELLS = ((1, 2), (2, 1), (2, 2))
VERTICAL_BRACKET_CONFIGS = (
    (1, 2, 2),
    (2, 1, 1),
    (2, 2, 2),
    (1, 1, 2),
    (2, 1, 2),
)
VERTICAL_STOP_CELLS = ((1, 1), (1, 2), (2, 1), (2, 2))
VERTICAL_STOP_HEIGHTS_MM = (60.0, 120.0)
RAMP_RUN_MM = 50.0
RAMP_CARRIER_RUN_MM = 10.0
RAMP_PROFILE_TIP_Y_MM = 65.224331674
RAMP_FREE_EDGE_RADIUS_MM = 2.0
RAMP_SHELF_RADIUS_MM = 32.0
RAMP_MINIMUM_FLAT_SHELF_MM = 2.0
BASE_HEIGHT_MM = 4.1
PANEL_BOTTOM_MM = 6.1
BRACKET_BACKING_MM = 4.1
BRACKET_ENVELOPE_MARGIN_MM = 0.1
STOP_WALL_MM = 6.0
EDGE_TOP_RADIUS_MM = 2.0
EDGE_BODY_RADIUS_MM = 3.0
EDGE_OUTWARD_OPTIONS_MM = (10.0, 20.0, 30.0)
STRAIGHT_EDGE_OUTWARD_OPTIONS_MM = (*EDGE_OUTWARD_OPTIONS_MM, 40.0)
EDGE_FAMILIES = ("edge-x", "edge-y", "corner-in", "corner-out")
SUPPORT_TOP_RADIUS_MM = 1.0
SUPPORT_BODY_RADIUS_MM = 3.0
SUPPORT_WINDOW_RADIUS_MM = 2.0
SUPPORT_END_PROFILE_RADII_MM = {2: 0.25}
SUPPORT_END_BODY_RADII_MM = {1: 0.75, 3: 3.0, 4: 3.0}
SUPPORT_END_CAP_RADIUS_MM = 3.0
SUPPORT_END_WINDOW_RADII_MM = {1: 2.5, 2: 2.0, 3: 3.0, 4: 3.0}
SUPPORT_END_HORIZONTAL_WINDOW_RIM_RADII_MM = {1: 1.0, 2: 2.0, 3: 2.0, 4: 2.0}
SUPPORT_END_SLOPED_WINDOW_RIM_RADII_MM = {1: 1.5, 2: 1.5, 3: 1.0, 4: 0.75}
BRACKET_LIP_RADIUS_MM = 1.0
BRACKET_FREE_EDGE_RADIUS_MM = 2.0
BRACKET_FRONT_SLOPE_RADIUS_MM = 3.0
BRACKET_TOP_EXTENSION_MM = {1: 3.0515422, 2: 2.921921}
PANEL_STEM_PROFILE_INSET_MM = 0.08
PANEL_TIP_TRANSITION_MM = 0.1
PANEL_TIP_RADIUS_MM = 2.0
PANEL_ROOT_OVERLAP_MM = 2.0
VERTICAL_STOP_RADIUS_MM = 2.0
BAMBU_PRINT_ROTATIONS = {
    "vertical-tile-bracket": 135.0,
    "lock-45": -135.0,
    "plate": 180.0,
}
BAMBU_OBJECT_SETTINGS = {
    "rod": {"enable_support": "1", "support_type": "normal(auto)"},
    "ramp": {"enable_support": "1", "support_type": "normal(auto)"},
    "vertical-stop": {"enable_support": "1", "support_type": "normal(auto)"},
}


@dataclass(frozen=True)
class BambuPrintPolicy:
    rotation_x: float | None
    rotation_y: float | None
    object_settings: dict[str, str]


@dataclass(frozen=True)
class Accessory:
    """Accessory dimensions in mm; ``nx`` counts straight edge/support cells.

    ``length`` controls support-bit length excluding its projecting join.
    ``height`` is the lock-45 or vertical-stop height above its shoulder, including its base.
    Vertical tile brackets use ``nx, ny`` for base columns/rows.
    ``panel_height_cells`` optionally gives independent upright panel rows;
    when omitted, panel rows equal base rows for backward compatibility.
    Their height follows the tile grid, not ``height``.
    Vertical stops use explicit 1x2, 2x1 or 2x2 mounting cells and an explicit
    60 or 120 mm height. They are filled cargo wedges without wall holes.
    Ramps use ``nx`` cells along a tile edge. Their approved finished run is
    fixed at 50 mm and ``ny`` remains one. ``ramp_join`` defaults to female;
    male tabs project beyond the run toward negative Y.
    ``variant`` selects corner and support-end types. Interface unit size scales
    tile-facing local-plane geometry; tile thickness independently controls
    insertion depth. Comfort radii and the separate support-rail join stay in mm.
    ``edge_outward`` is the outward body width, excluding male tab projection.
    Straight edges also accept 40 mm with original roofed joints; corners do not.
    ``complete_edge_holes`` continues accepted tile-boundary hole sites through
    perimeter parts. ``None`` matches the normal full tile pattern at every
    outward width. Explicit ``False`` keeps the part plain.
    ``edge_hole_diameter`` defaults to the tile's standard 10 mm when
    completion is selected.
    """

    family: str
    nx: int = 1
    ny: int = 1
    variant: int = 1
    length: float = 60
    height: float = 50
    interface: Interface = Interface()
    panel_height_cells: int | None = None
    ramp_join: Literal["female", "male"] = "female"
    edge_outward: float = 10.0
    complete_edge_holes: bool | None = None
    edge_hole_diameter: float | None = None

    def __post_init__(self) -> None:
        if self.family == "lock-90":
            raise ValueError(
                "lock-90 was replaced by vertical-tile-bracket; use nx/ny 1x2, 2x1 or 2x2"
            )
        if self.family not in FAMILIES:
            raise ValueError(f"unknown accessory family: {self.family!r}")
        for name in ("nx", "ny", "variant"):
            count(name, getattr(self, name))
        if self.panel_height_cells is not None:
            count("panel height cells", self.panel_height_cells)
        positive("length", self.length)
        positive("height", self.height)
        if not isinstance(self.interface, Interface):
            raise ValueError("interface must be an Interface")
        if self.ramp_join not in ("female", "male"):
            raise ValueError("ramp join must be female or male")
        if self.family != "ramp" and self.ramp_join != "female":
            raise ValueError(f"ramp join does not apply to {self.family}")
        variants = {"corner-in": 4, "corner-out": 6, "support-end": 4}
        if self.variant > variants.get(self.family, 1):
            raise ValueError(f"invalid variant for {self.family}")
        grids = {
            "plate": {(1, 1), (1, 2), (2, 2)},
            "lock-45": {(1, 1), (2, 2)},
            "vertical-stop": set(VERTICAL_STOP_CELLS),
        }
        if self.family in grids and (self.nx, self.ny) not in grids[self.family]:
            raise ValueError(f"unsupported mounting grid for {self.family}")
        if self.family not in grids and self.ny != 1:
            if self.family != "vertical-tile-bracket":
                raise ValueError(f"{self.family} uses nx, not ny")
        if self.family != "vertical-tile-bracket" and self.panel_height_cells is not None:
            raise ValueError(f"panel height cells do not apply to {self.family}")
        if self.family in variants and self.nx != 1:
            raise ValueError(f"{self.family} uses variant, not nx")
        if self.family == "support-bit" and (self.length < 20 or self.nx != 1):
            raise ValueError("support-bit requires length >= 20 mm and nx=1")
        if self.family.startswith("lock-") and self.height < 12:
            raise ValueError("lock height must be at least 12 mm")
        if self.family == "lock-45" and self.height > self.ny * self.interface.pitch:
            raise ValueError("45-degree lock height must not exceed its base depth")
        if self.family == "lock-45" and self.nx == 2 and self.interface.pitch > 60:
            raise ValueError(
                "2x2 angled stop with R2 free edges supports unit size <= 60 mm; "
                "use the 1x1 stop or a smaller unit size"
            )
        if self.family == "vertical-tile-bracket":
            panel_rows = self.panel_height_cells or self.ny
            if (self.nx, self.ny, panel_rows) not in VERTICAL_BRACKET_CONFIGS:
                raise ValueError(
                    "vertical-tile-bracket supports floor base 1x2 -> wall 1x2, "
                    "2x1 -> 2x1, 2x2 -> 2x2, 1x1 -> 1x2 or 2x1 -> 2x2"
                )
            if panel_rows == self.ny and self.panel_height_cells is not None:
                object.__setattr__(self, "panel_height_cells", None)
            if self.height != 50:
                raise ValueError(
                    "vertical-tile-bracket height follows its cells; accessory height is only for lock-45"
                )
        if self.family == "vertical-stop" and self.height not in VERTICAL_STOP_HEIGHTS_MM:
            raise ValueError("vertical-stop requires explicit accessory height 60 or 120 mm")
        if self.family == "ramp" and self.interface.joint_style != "original":
            raise ValueError("ramp requires original roofed tile-edge joints")
        if self.family == "ramp" and self.height != 50:
            raise ValueError("ramp rise follows tile thickness; accessory height does not apply")
        if self.complete_edge_holes is not None and not isinstance(self.complete_edge_holes, bool):
            raise ValueError("complete edge holes must be true, false or None")
        if self.edge_hole_diameter is not None:
            positive("edge hole diameter", self.edge_hole_diameter)
        if self.family in EDGE_FAMILIES:
            allowed_widths = (
                STRAIGHT_EDGE_OUTWARD_OPTIONS_MM
                if self.family in ("edge-x", "edge-y")
                else EDGE_OUTWARD_OPTIONS_MM
            )
            if self.edge_outward not in allowed_widths:
                raise ValueError(
                    "edge outward projection must be 10, 20 or 30 mm; "
                    "40 mm is supported only for straight edge-x/edge-y parts"
                )
            if self.edge_outward == 40 and self.interface.joint_style != "original":
                raise ValueError("40 mm straight edges require original roofed tile-edge joints")
            object.__setattr__(self, "edge_outward", float(self.edge_outward))
            requested_completion = self.complete_edge_holes
            hole_diameter = self.edge_hole_diameter or DEFAULT_HOLE_DIAMETER_MM
            if requested_completion is None:
                complete = edge_hole_completion_supported(
                    self.family,
                    self.nx,
                    self.variant,
                    self.interface,
                    hole_diameter,
                )
            elif requested_completion:
                complete = True
                if not edge_hole_completion_supported(
                    self.family,
                    self.nx,
                    self.variant,
                    self.interface,
                    hole_diameter,
                ):
                    raise ValueError(
                        f"no accepted {hole_diameter:g} mm full-pattern boundary holes fit "
                        "this interface; select a smaller hole diameter or keep the perimeter plain"
                    )
            else:
                complete = False
                if self.edge_hole_diameter is not None:
                    raise ValueError(
                        "edge hole diameter requires completed edge holes; "
                        "remove it or enable completion"
                    )
            object.__setattr__(self, "complete_edge_holes", complete)
            object.__setattr__(
                self,
                "edge_hole_diameter",
                hole_diameter if complete else None,
            )
        elif (
            self.edge_outward != 10.0
            or self.complete_edge_holes
            or self.edge_hole_diameter is not None
        ):
            raise ValueError(f"edge options do not apply to {self.family}")
        else:
            object.__setattr__(self, "complete_edge_holes", False)
            object.__setattr__(self, "edge_hole_diameter", None)


def _box(x: float, y: float, w: float, d: float, h: float, z: float = 0) -> Part:
    return prism(rectangle(x, y, w, d), h).moved(Location((0, 0, z)))


def _polygon(points: list[tuple[float, float]]) -> Face:
    return Face(Wire.make_polygon([(x, y, 0) for x, y in points], close=True))


def _join(
    x: float,
    y: float,
    angle: float,
    male: bool,
    *,
    support: bool = False,
    depth: float | None = None,
    interface: Interface = Interface(),
) -> dict:
    return {
        "position": (x, y, -25 if support else 0),
        "angle": angle,
        "sex": "male" if male else "female",
        "depth": depth
        if depth is not None
        else (
            (5 if male else 5.085)
            if support
            else interface.male_join_depth
            if male
            else interface.female_join_depth
        ),
        "height": (
            25 if support else interface.male_height if male else interface.female_opening_height
        ),
        "interface": "support-dovetail" if support else "tile-dovetail",
    }


def _edge_plan(spec: Accessory) -> tuple[Face, list[dict]]:
    p = spec.interface.pitch
    outward = spec.edge_outward
    if spec.family in ("edge-x", "edge-y"):
        male = spec.family == "edge-x"
        return rectangle(0, -outward if male else 0, spec.nx * p, outward), [
            _join((i + 0.5) * p, 0, 0, male, interface=spec.interface) for i in range(spec.nx)
        ]
    if spec.family == "corner-in":
        # Square internal elbows replace the source's curved non-mating web.
        v = spec.variant
        right, top = v in (2, 3), v in (1, 2)
        vertical = rectangle(p - outward if right else 0, 0, outward, p)
        horizontal = rectangle(0, p - outward if top else 0, p, outward)
        face = Face(vertical.fuse(horizontal).faces()[0].wrapped)
        return face, [
            _join(p if right else 0, p / 2, -90, right, interface=spec.interface),
            _join(p / 2, p if top else 0, 0, top, interface=spec.interface),
        ]
    v = spec.variant
    if v in (3, 6):
        if v == 3:
            face = _polygon(
                [
                    (0, p),
                    (p, p),
                    (p, 0),
                    (p + outward, 0),
                    (p + outward, p + outward),
                    (0, p + outward),
                ]
            )
            joins = [
                _join(p, p / 2, -90, False, interface=spec.interface),
                _join(p / 2, p, 0, False, interface=spec.interface),
            ]
        else:
            face = _polygon(
                [
                    (-outward, -outward),
                    (p, -outward),
                    (p, 0),
                    (0, 0),
                    (0, p),
                    (-outward, p),
                ]
            )
            joins = [
                _join(0, p / 2, -90, True, interface=spec.interface),
                _join(p / 2, 0, 0, True, interface=spec.interface),
            ]
        return face, joins
    male = v in (1, 5)
    if v in (2, 5):
        if v == 5:
            points = [
                (0, -outward),
                (p + outward, -outward),
                (p, 0),
                (0, 0),
            ]
        else:
            points = [
                (-outward, outward),
                (0, 0),
                (p, 0),
                (p, outward),
            ]
        return _polygon(points), [_join(p / 2, 0, 0, male, interface=spec.interface)]
    if v == 1:
        points = [
            (-outward, 0),
            (0, 0),
            (0, p),
            (-outward, p + outward),
        ]
    else:
        points = [
            (0, 0),
            (outward, -outward),
            (outward, p),
            (0, p),
        ]
    return _polygon(points), [_join(0, p / 2, -90, male, interface=spec.interface)]


def _edge_hole_centers_for(
    family: str,
    nx: int,
    variant: int,
    interface: Interface,
    hole_diameter: float = DEFAULT_HOLE_DIAMETER_MM,
) -> list[tuple[float, float]]:
    if family not in EDGE_FAMILIES:
        raise ValueError(f"edge-hole completion does not apply to {family}")
    span = nx if family in ("edge-x", "edge-y") else 1
    sites = hole_placements(
        Tile(
            nx=span,
            ny=1,
            interface=interface,
            hole_diameter=hole_diameter,
            hole_scope="full",
        )
    )
    boundary = [(site.x, site.y) for site in sites if site.accepted and abs(site.y) < 1e-8]
    if family in ("edge-x", "edge-y"):
        return boundary
    _, joins = _edge_plan(
        Accessory(
            family,
            nx=nx,
            variant=variant,
            interface=interface,
            complete_edge_holes=False,
        )
    )
    centers = {
        (x, join["position"][1]) if join["angle"] == 0 else (join["position"][0], x)
        for join in joins
        for x, _ in boundary
    }
    return sorted(centers)


def edge_hole_completion_supported(
    family: str,
    nx: int = 1,
    variant: int = 1,
    interface: Interface = Interface(),
    hole_diameter: float = DEFAULT_HOLE_DIAMETER_MM,
) -> bool:
    """Whether at least one matching full-pattern boundary site is accepted."""
    return bool(_edge_hole_centers_for(family, nx, variant, interface, hole_diameter))


def tile_matched_perimeter(
    family: str,
    *,
    nx: int = 1,
    variant: int = 1,
    outward: float,
    interface: Interface = Interface(),
    hole_diameter: float | None = DEFAULT_HOLE_DIAMETER_MM,
    hole_scope: Literal["interior", "full"] = "full",
) -> Accessory:
    """Select one perimeter hole mode from the matching tile's accepted sites."""
    if family not in EDGE_FAMILIES:
        raise ValueError(f"tile-matched perimeter does not apply to {family}")
    Tile(interface=interface, hole_diameter=hole_diameter, hole_scope=hole_scope)
    complete = (
        hole_diameter is not None
        and hole_scope == "full"
        and edge_hole_completion_supported(family, nx, variant, interface, hole_diameter)
    )
    return Accessory(
        family,
        nx=nx,
        variant=variant,
        interface=interface,
        edge_outward=outward,
        complete_edge_holes=complete,
        edge_hole_diameter=hole_diameter if complete else None,
    )


def _edge_hole_centers(spec: Accessory) -> list[tuple[float, float]]:
    return _edge_hole_centers_for(
        spec.family,
        spec.nx,
        spec.variant,
        spec.interface,
        spec.edge_hole_diameter or DEFAULT_HOLE_DIAMETER_MM,
    )


def _cut_completed_edge_holes(part: Part, spec: Accessory) -> Part:
    if not spec.complete_edge_holes:
        return part
    cutters = [
        Solid.make_cylinder(
            (spec.edge_hole_diameter or DEFAULT_HOLE_DIAMETER_MM) / 2,
            spec.interface.height + 2,
        ).moved(Location((x, y, -1)))
        for x, y in _edge_hole_centers(spec)
    ]
    return part.cut(*cutters).clean() if cutters else part


def _join_solid(join: dict, *, interface: Interface = Interface()) -> Part:
    if join["interface"] == "tile-dovetail":
        tool = tile_join_tool(interface, depth=join["depth"], male=join["sex"] == "male")
    else:
        face = dovetail_face(depth=join["depth"])
        face = face.fillet_2d(1, face.vertices())
        tool = prism(face, join["height"])
        for x in (-26.5, 22.5):
            tool = tool.cut(_box(x, -5, 4, join["depth"] + 6, 27, -1))
    return tool.rotate(Axis.Z, join["angle"]).moved(Location(join["position"]))


def _apply_joins(part: Part, joins: list[dict], *, interface: Interface = Interface()) -> Part:
    for join in joins:
        tool = _join_solid(join, interface=interface)
        part = part.fuse(tool) if join["sex"] == "male" else part.cut(tool)
    return part.clean()


@lru_cache(maxsize=16)
def _downward_plug(interface: Interface = Interface()) -> Part:
    return make_plug(interface).rotate(Axis.X, 180)


def _mount_centers(spec: Accessory) -> list[tuple[float, float, float]]:
    p = spec.interface.pitch
    return [((x + 0.5) * p, (y + 0.5) * p, 0) for x in range(spec.nx) for y in range(spec.ny)]


def _cross_prism(
    points: list[tuple[float, float]],
    width: float,
    x: float = 0,
) -> Part:
    face = Face(Wire.make_polygon([(x, y, z) for y, z in points], close=True))
    return Part(Solid.extrude(face, (width, 0, 0)).wrapped)


def _mounted_base(spec: Accessory, *, root_radius: float, round_top: bool = True) -> Part:
    w, d = spec.nx * spec.interface.pitch, spec.ny * spec.interface.pitch
    part = _box(0, 0, w, d, BASE_HEIGHT_MM)
    if round_top:
        part = part.fillet(1, horizontal_edges(part, BASE_HEIGHT_MM))
    centers = _mount_centers(spec)
    for center in centers:
        part = part.fuse(_downward_plug(spec.interface).moved(Location(center)))
    root_limit = spec.interface.pitch * 23 / 60
    roots = [
        edge
        for edge in horizontal_edges(part, 0)
        if any(
            abs(edge.center().X - x) < root_limit and abs(edge.center().Y - y) < root_limit
            for x, y, _ in centers
        )
    ]
    return part.fillet(root_radius, roots)


def _plate(spec: Accessory) -> Part:
    width, depth = spec.nx * spec.interface.pitch, spec.ny * spec.interface.pitch
    body = _box(0, 0, width, depth, BASE_HEIGHT_MM)
    operation = BRepFilletAPI_MakeFillet(body.wrapped)
    for edge in body.edges():
        operation.Add(2.0, edge.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("attachment plate coupled R2 body fillet failed")
    part = Part(Solid(operation.Shape()).wrapped)
    centers = _mount_centers(spec)
    for center in centers:
        part = part.fuse(_downward_plug(spec.interface).moved(Location(center)))
    root_limit = spec.interface.pitch * 23 / 60
    roots = [
        edge
        for edge in horizontal_edges(part, 0)
        if any(
            abs(edge.center().X - x) < root_limit and abs(edge.center().Y - y) < root_limit
            for x, y, _ in centers
        )
    ]
    return part.fillet(2, roots).clean()


def _filled_angled_stop(spec: Accessory) -> Part:
    w, d = spec.nx * spec.interface.pitch, spec.ny * spec.interface.pitch
    lean = spec.height - BASE_HEIGHT_MM
    top_front = d - lean - STOP_WALL_MM
    profile = [
        (0, 0),
        (d, 0),
        (d, BASE_HEIGHT_MM),
        (d - lean, spec.height),
        (top_front, spec.height),
        (0, BASE_HEIGHT_MM - 1),
    ]
    if spec.interface.unit_scale == 1:
        envelope = _cross_prism(profile, w)
        operation = BRepFilletAPI_MakeFillet(envelope.wrapped)
        for edge in envelope.edges():
            operation.Add(2.0, edge.wrapped)
        operation.Build()
        if not operation.IsDone():
            raise ValueError("filled angled-stop R2 envelope failed")
        rounded = Part(Solid(operation.Shape()).wrapped)
    else:
        face = Face(Wire.make_polygon([(0, y, z) for y, z in profile], close=True))
        face = face.fillet_2d(2, face.vertices())
        rounded = Part(Solid.extrude(face, (w, 0, 0)).wrapped)
        cap_edges = [
            edge
            for edge in rounded.edges()
            if edge.bounding_box().size.X < 1e-6
            and (abs(edge.bounding_box().min.X) < 1e-6 or abs(edge.bounding_box().min.X - w) < 1e-6)
        ]
        rounded = rounded.fillet(2, cap_edges)
    mounted = _mounted_base(spec, root_radius=1, round_top=False)
    connectors = []
    region_size = spec.interface.pitch * 0.8
    for x, y, _ in _mount_centers(spec):
        region = Solid.make_box(
            region_size,
            region_size,
            spec.interface.height + 0.2,
        ).moved(
            Location(
                (
                    x - region_size / 2,
                    y - region_size / 2,
                    -spec.interface.height,
                )
            )
        )
        connectors.extend(mounted.intersect(region).solids())
    return Part(rounded.fuse(*connectors).clean().solids())


@lru_cache(maxsize=16)
def _legacy_panel_connector(interface: Interface = Interface()) -> Part:
    plate = _mounted_base(Accessory("plate", interface=interface), root_radius=2)
    center = interface.pitch / 2
    domain = prism(
        x_profile(interface, offset=5),
        interface.height + 5,
    ).moved(Location((center, center, -interface.height)))
    return Part(plate.intersect(domain).solids())


@lru_cache(maxsize=16)
def _bidirectional_panel_post(interface: Interface = Interface()) -> Part:
    tip_start = interface.plug_depth - PANEL_TIP_RADIUS_MM
    transition_start = tip_start - PANEL_TIP_TRANSITION_MM
    narrow_profile = x_profile(
        interface,
        plug=True,
        offset=-PANEL_STEM_PROFILE_INSET_MM,
    )
    stem = prism(
        narrow_profile,
        transition_start + PANEL_ROOT_OVERLAP_MM,
    ).moved(Location((0, 0, -PANEL_ROOT_OVERLAP_MM)))
    transition = Part(
        Solid.make_loft(
            [
                narrow_profile.outer_wire().moved(Location((0, 0, transition_start))),
                x_profile(interface, plug=True).outer_wire().moved(Location((0, 0, tip_start))),
            ]
        ).wrapped
    )
    carrier = interface.pitch + 10
    tip_region = Solid.make_box(
        carrier,
        carrier,
        PANEL_TIP_RADIUS_MM + 0.1,
    ).moved(
        Location(
            (
                -carrier / 2,
                -carrier / 2,
                tip_start,
            )
        )
    )
    exact_tip = Part(make_plug(interface).intersect(tip_region).solids())
    result = Part(stem.fuse(transition, exact_tip).clean().solids())
    if not result.is_valid or len(result.solids()) != 1:
        raise ValueError("bidirectional panel post is invalid")
    return result


@lru_cache(maxsize=16)
def _panel_connector(interface: Interface = Interface()) -> Part:
    center = interface.pitch / 2
    return (
        _bidirectional_panel_post(interface)
        .rotate(Axis.X, 180)
        .moved(Location((center, center, 0)))
    )


def _vertical_bracket(spec: Accessory, *, round_lip: bool = True) -> Part:
    panel_rows = spec.panel_height_cells or spec.ny
    if panel_rows != spec.ny:
        return _shallow_vertical_bracket(spec, panel_rows)
    if round_lip:
        return _rounded_original_bracket(spec)
    p = spec.interface.pitch
    w, d = spec.nx * p, spec.ny * p
    seat = d - spec.interface.height
    base = _mounted_base(spec, root_radius=1, round_top=False)
    node = _panel_connector(spec.interface)
    envelope = _legacy_panel_connector(spec.interface)
    # Cover the highest rear backing corner, retaining the exact45-degree bed plane.
    intercept = (
        PANEL_BOTTOM_MM
        + envelope.bounding_box().max.Y
        + BRACKET_BACKING_MM
        + spec.interface.height
        - p
        + BRACKET_ENVELOPE_MARGIN_MM
    )
    wedge = _cross_prism(
        [(0, BASE_HEIGHT_MM), (seat, BASE_HEIGHT_MM), (seat, seat + intercept), (0, intercept)],
        w,
    )
    connectors = [
        node.rotate(Axis.X, 90).moved(Location((column * p, seat, PANEL_BOTTOM_MM + row * p)))
        for row in range(spec.ny)
        for column in range(spec.nx)
    ]
    land = _box(
        0,
        seat,
        w,
        spec.interface.height,
        PANEL_BOTTOM_MM - BASE_HEIGHT_MM,
        BASE_HEIGHT_MM,
    )
    if round_lip:
        # The outermost1mm lies beyond the tile's rounded-edge planar bearing land.
        edges = [
            e
            for e in horizontal_edges(land, PANEL_BOTTOM_MM)
            if abs(e.bounding_box().min.Y - d) < 1e-5 and abs(e.bounding_box().max.Y - d) < 1e-5
        ]
        land = land.fillet(BRACKET_LIP_RADIUS_MM, edges)
    return base.fuse(wedge, *connectors, land).clean()


def _rounded_original_bracket(spec: Accessory) -> Part:
    p = spec.interface.pitch
    width, depth = spec.nx * p, spec.ny * p
    seat = depth - spec.interface.height
    node = _panel_connector(spec.interface)
    envelope = _legacy_panel_connector(spec.interface)
    intercept = (
        PANEL_BOTTOM_MM
        + envelope.bounding_box().max.Y
        + BRACKET_BACKING_MM
        + spec.interface.height
        - p
        + BRACKET_ENVELOPE_MARGIN_MM
    )
    panel_top = seat + intercept + BRACKET_TOP_EXTENSION_MM[spec.ny]
    body = _cross_prism(
        [
            (0, 0),
            (depth, 0),
            (depth, PANEL_BOTTOM_MM),
            (seat, PANEL_BOTTOM_MM),
            (seat, panel_top),
            (0, intercept),
        ],
        width,
    )
    operation = BRepFilletAPI_MakeFillet(body.wrapped)
    for edge in body.edges():
        bounds = edge.bounding_box()
        if (
            abs(bounds.min.Y - seat) < 1e-5
            and abs(bounds.max.Y - seat) < 1e-5
            and abs(bounds.min.Z - PANEL_BOTTOM_MM) < 1e-5
            and abs(bounds.max.Z - PANEL_BOTTOM_MM) < 1e-5
        ):
            continue
        front_to_slope = (
            abs(bounds.min.Y) < 1e-5
            and abs(bounds.max.Y) < 1e-5
            and abs(bounds.min.Z - intercept) < 1e-5
            and abs(bounds.max.Z - intercept) < 1e-5
            and bounds.size.X > width - 1
        )
        bearing_lip = (
            abs(bounds.min.Z - PANEL_BOTTOM_MM) < 1e-5
            and abs(bounds.max.Z - PANEL_BOTTOM_MM) < 1e-5
            and bounds.max.Y >= seat - 1e-5
        ) or (
            abs(bounds.min.Y - depth) < 1e-5
            and abs(bounds.max.Y - depth) < 1e-5
            and bounds.max.Z <= PANEL_BOTTOM_MM + 1e-5
        )
        operation.Add(
            BRACKET_FRONT_SLOPE_RADIUS_MM
            if front_to_slope
            else BRACKET_LIP_RADIUS_MM
            if bearing_lip
            else BRACKET_FREE_EDGE_RADIUS_MM,
            edge.wrapped,
        )
    operation.Build()
    if not operation.IsDone():
        raise ValueError("original tile-bracket coupled body fillet failed")
    rounded_body = Part(Solid(operation.Shape()).wrapped)
    mounted = _mounted_base(spec, root_radius=1, round_top=False)
    floor_connectors = []
    region_size = spec.interface.pitch * 0.8
    for x, y, _ in _mount_centers(spec):
        region = Solid.make_box(
            region_size,
            region_size,
            spec.interface.height + 1.1,
        ).moved(
            Location(
                (
                    x - region_size / 2,
                    y - region_size / 2,
                    -spec.interface.height,
                )
            )
        )
        floor_connectors.extend(mounted.intersect(region).solids())
    panel_connectors = [
        node.rotate(Axis.X, 90).moved(Location((column * p, seat, PANEL_BOTTOM_MM + row * p)))
        for row in range(spec.ny)
        for column in range(spec.nx)
    ]
    return Part(rounded_body.fuse(*floor_connectors, *panel_connectors).clean().solids())


def _rounded_shallow_bracket_body(spec: Accessory, panel_rows: int) -> Part:
    p = spec.interface.pitch
    w, d = spec.nx * p, spec.ny * p
    seat = d - spec.interface.height
    envelope = _legacy_panel_connector(spec.interface)
    intercept = (
        PANEL_BOTTOM_MM
        + envelope.bounding_box().max.Y
        + BRACKET_BACKING_MM
        + spec.interface.height
        - p
        + BRACKET_ENVELOPE_MARGIN_MM
    )
    panel_top = panel_rows * p - spec.interface.height + intercept
    upper_rear = seat - BRACKET_BACKING_MM - BRACKET_ENVELOPE_MARGIN_MM
    body = _cross_prism(
        [
            (0, 0),
            (d, 0),
            (d, PANEL_BOTTOM_MM),
            (seat, PANEL_BOTTOM_MM),
            (seat, panel_top),
            (upper_rear, panel_top),
            (0, intercept),
        ],
        w,
    )
    operation = BRepFilletAPI_MakeFillet(body.wrapped)
    for edge in body.edges():
        bounds = edge.bounding_box()
        if (
            abs(bounds.min.Y - seat) < 1e-5
            and abs(bounds.max.Y - seat) < 1e-5
            and abs(bounds.min.Z - PANEL_BOTTOM_MM) < 1e-5
            and abs(bounds.max.Z - PANEL_BOTTOM_MM) < 1e-5
        ):
            continue
        front_to_slope = (
            abs(bounds.min.Y) < 1e-5
            and abs(bounds.max.Y) < 1e-5
            and abs(bounds.min.Z - intercept) < 1e-5
            and abs(bounds.max.Z - intercept) < 1e-5
            and bounds.size.X > w - 1
        )
        bearing_lip = (
            abs(bounds.min.Z - PANEL_BOTTOM_MM) < 1e-5
            and abs(bounds.max.Z - PANEL_BOTTOM_MM) < 1e-5
            and bounds.max.Y >= seat - 1e-5
        ) or (
            abs(bounds.min.Y - d) < 1e-5
            and abs(bounds.max.Y - d) < 1e-5
            and bounds.max.Z <= PANEL_BOTTOM_MM + 1e-5
        )
        operation.Add(
            BRACKET_FRONT_SLOPE_RADIUS_MM
            if front_to_slope
            else BRACKET_LIP_RADIUS_MM
            if bearing_lip
            else BRACKET_FREE_EDGE_RADIUS_MM,
            edge.wrapped,
        )
    operation.Build()
    if not operation.IsDone():
        raise ValueError("shallow tile-bracket coupled body fillet failed")
    return Part(Solid(operation.Shape()).wrapped)


def _shallow_vertical_bracket(spec: Accessory, panel_rows: int) -> Part:
    p = spec.interface.pitch
    seat = spec.ny * p - spec.interface.height
    rounded_body = _rounded_shallow_bracket_body(spec, panel_rows)
    node = _panel_connector(spec.interface)
    mounted = _mounted_base(spec, root_radius=1, round_top=False)
    floor_connectors = []
    region_size = spec.interface.pitch * 0.8
    for x, y, _ in _mount_centers(spec):
        region = Solid.make_box(
            region_size,
            region_size,
            spec.interface.height + 1.1,
        ).moved(
            Location(
                (
                    x - region_size / 2,
                    y - region_size / 2,
                    -spec.interface.height,
                )
            )
        )
        floor_connectors.extend(mounted.intersect(region).solids())
    panel_connectors = [
        node.rotate(Axis.X, 90).moved(Location((column * p, seat, PANEL_BOTTOM_MM + row * p)))
        for row in range(panel_rows)
        for column in range(spec.nx)
    ]
    return Part(rounded_body.fuse(*floor_connectors, *panel_connectors).clean().solids())


def _vertical_stop_slope_values(
    depth: float,
    height: float,
    radius: float = VERTICAL_STOP_RADIUS_MM,
) -> float:
    a = depth - radius
    b = height - radius - BASE_HEIGHT_MM
    return (a * b + radius * sqrt(a * a + b * b - radius * radius)) / (a * a - radius * radius)


def _vertical_stop_slope(spec: Accessory, radius: float = VERTICAL_STOP_RADIUS_MM) -> float:
    return _vertical_stop_slope_values(
        spec.ny * spec.interface.pitch,
        spec.height,
        radius,
    )


def _typed_print_parameters(spec: Accessory | Rod | RodBrace) -> dict:
    return {"family": spec.family, **asdict(spec)}


def bambu_print_policy(parameters: dict) -> BambuPrintPolicy:
    family = parameters.get("family")
    panel_height_cells = parameters.get("panel_height_cells")
    interface = parameters.get("interface", {})
    pitch = interface.get("pitch", 60)
    thickness = interface.get("height", 13)
    if family == "vertical-tile-bracket" and panel_height_cells is None:
        rows = parameters["ny"]
        seat = rows * pitch - thickness
        slope = (seat + BRACKET_TOP_EXTENSION_MM[rows]) / seat
        rotation_x = 180 - degrees(atan(slope))
    elif family == "vertical-stop":
        slope = _vertical_stop_slope_values(
            parameters["ny"] * pitch,
            parameters["height"],
        )
        rotation_x = 180 - degrees(atan(slope))
    else:
        rotation_x = (
            None
            if family == "vertical-tile-bracket" and panel_height_cells is not None
            else BAMBU_PRINT_ROTATIONS.get(family)
        )
    rotation_y = (
        90.0
        if family == "rod"
        else -90.0
        if family == "vertical-tile-bracket" and panel_height_cells is not None
        else None
    )
    if family == "ramp" and parameters.get("ramp_join") == "male":
        object_settings = {}
    elif family == "vertical-tile-bracket" and panel_height_cells is not None:
        object_settings = {"enable_support": "1", "support_type": "normal(auto)"}
    else:
        object_settings = dict(BAMBU_OBJECT_SETTINGS.get(family, {}))
    return BambuPrintPolicy(rotation_x, rotation_y, object_settings)


def vertical_stop_print_rotation(spec: Accessory) -> float:
    if spec.family != "vertical-stop":
        raise ValueError("vertical-stop print rotation requires a vertical-stop specification")
    rotation = bambu_print_policy(_typed_print_parameters(spec)).rotation_x
    assert rotation is not None
    return rotation


def bambu_print_rotation(spec: Accessory | Rod | RodBrace) -> float | None:
    return bambu_print_policy(_typed_print_parameters(spec)).rotation_x


def bambu_print_rotation_y(spec: Accessory | Rod | RodBrace) -> float | None:
    return bambu_print_policy(_typed_print_parameters(spec)).rotation_y


def required_bambu_print_rotation(parameters: dict) -> float | None:
    return bambu_print_policy(parameters).rotation_x


def required_bambu_print_rotation_y(parameters: dict) -> float | None:
    return bambu_print_policy(parameters).rotation_y


def required_bambu_object_settings(parameters: dict) -> dict[str, str]:
    return bambu_print_policy(parameters).object_settings


def _vertical_stop(spec: Accessory) -> Part:
    width = spec.nx * spec.interface.pitch
    depth = spec.ny * spec.interface.pitch
    slope = _vertical_stop_slope(spec)
    top = BASE_HEIGHT_MM + slope * depth
    base = _mounted_base(spec, root_radius=1, round_top=False)
    profile = Face(
        Wire.make_polygon(
            [
                (0, 0, BASE_HEIGHT_MM - 1),
                (0, depth, BASE_HEIGHT_MM - 1),
                (0, depth, top),
                (0, 0, BASE_HEIGHT_MM),
            ],
            close=True,
        )
    )
    raw = Part(base.fuse(Solid.extrude(profile, (width, 0, 0))).clean().solids())
    selected = []
    categories = {"cargo-cap": 0, "diagonal": 0, "front": 0, "underside": 0}
    for edge in raw.edges():
        bounds = edge.bounding_box()
        if edge.geom_type != GeomType.LINE:
            continue
        category = None
        if (
            abs(bounds.min.Y - depth) < 1e-5
            and abs(bounds.max.Y - depth) < 1e-5
            and (
                (bounds.size.X < 1e-5 and bounds.size.Z > 10)
                or (abs(bounds.min.Z - top) < 1e-5 and abs(bounds.max.Z - top) < 1e-5)
            )
        ):
            category = "cargo-cap"
        elif (
            bounds.size.X < 1e-5
            and bounds.size.Y > 10
            and bounds.size.Z > 10
            and (abs(bounds.min.X) < 1e-5 or abs(bounds.min.X - width) < 1e-5)
        ):
            category = "diagonal"
        elif (
            abs(bounds.min.Y) < 1e-5
            and abs(bounds.max.Y) < 1e-5
            and (
                bounds.size.Z > 4
                or (
                    bounds.size.X > 10
                    and abs(bounds.min.Z - BASE_HEIGHT_MM) < 1e-5
                    and abs(bounds.max.Z - BASE_HEIGHT_MM) < 1e-5
                )
            )
        ):
            category = "front"
        elif (
            abs(bounds.min.Z) < 1e-5
            and abs(bounds.max.Z) < 1e-5
            and (
                (
                    bounds.size.X > 10
                    and (
                        (abs(bounds.min.Y) < 1e-5 and abs(bounds.max.Y) < 1e-5)
                        or (abs(bounds.min.Y - depth) < 1e-5 and abs(bounds.max.Y - depth) < 1e-5)
                    )
                )
                or (
                    bounds.size.Y > 10
                    and (
                        (abs(bounds.min.X) < 1e-5 and abs(bounds.max.X) < 1e-5)
                        or (abs(bounds.min.X - width) < 1e-5 and abs(bounds.max.X - width) < 1e-5)
                    )
                )
            )
        ):
            category = "underside"
        if category is not None:
            selected.append(edge)
            categories[category] += 1
    expected = {"cargo-cap": 3, "diagonal": 2, "front": 3, "underside": 4}
    if categories != expected:
        raise ValueError(f"vertical-stop free-edge classification changed: {categories}")
    operation = BRepFilletAPI_MakeFillet(raw.wrapped)
    for edge in selected:
        operation.Add(VERTICAL_STOP_RADIUS_MM, edge.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("vertical-stop coupled R2 free-edge fillet failed")
    return Part(Solid(operation.Shape()).wrapped)


@lru_cache(maxsize=16)
def _ramp_profile_tip_y(tile_thickness: float) -> float:
    if tile_thickness == 13:
        return RAMP_PROFILE_TIP_Y_MM

    def finished_run(raw_tip: float) -> float:
        profile = Face(
            Wire.make_polygon(
                [
                    (0, 0, 0),
                    (0, raw_tip, 0),
                    (0, RAMP_CARRIER_RUN_MM, tile_thickness),
                    (0, 0, tile_thickness),
                ],
                close=True,
            )
        )
        tip = max(profile.vertices(), key=lambda vertex: vertex.Y)
        return (
            profile.fillet_2d(
                RAMP_FREE_EDGE_RADIUS_MM,
                [tip],
            )
            .bounding_box()
            .max.Y
        )

    low = RAMP_RUN_MM
    high = RAMP_RUN_MM + 4 * tile_thickness
    while finished_run(high) < RAMP_RUN_MM:
        high += max(10, tile_thickness)
    for _ in range(48):
        middle = (low + high) / 2
        if finished_run(middle) < RAMP_RUN_MM:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _ramp_shelf_radius(tile_thickness: float) -> float:
    angle = atan(tile_thickness / (_ramp_profile_tip_y(tile_thickness) - RAMP_CARRIER_RUN_MM))
    return min(
        RAMP_SHELF_RADIUS_MM,
        (RAMP_CARRIER_RUN_MM - RAMP_MINIMUM_FLAT_SHELF_MM) / tan(angle / 2),
    )


def _ramp(spec: Accessory, *, cut_joins: bool = True) -> Part:
    width = spec.nx * spec.interface.pitch
    raw_tip = _ramp_profile_tip_y(spec.interface.height)
    profile = Face(
        Wire.make_polygon(
            [
                (0, 0, 0),
                (0, raw_tip, 0),
                (0, RAMP_CARRIER_RUN_MM, spec.interface.height),
                (0, 0, spec.interface.height),
            ],
            close=True,
        )
    )
    # Resolve these arcs in section: the 3D fillet builder can treat a
    # shallow shelf/slope angle as tangent and silently leave the crease.
    profile = profile.fillet_2d(
        _ramp_shelf_radius(spec.interface.height),
        [
            vertex
            for vertex in profile.vertices()
            if abs(vertex.Y - RAMP_CARRIER_RUN_MM) < 1e-7
            and abs(vertex.Z - spec.interface.height) < 1e-7
        ],
    )
    profile = profile.fillet_2d(
        RAMP_FREE_EDGE_RADIUS_MM,
        [max(profile.vertices(), key=lambda vertex: vertex.Y)],
    )
    blank = Part(Solid.extrude(profile, (width, 0, 0)).wrapped)
    operation = BRepFilletAPI_MakeFillet(blank.wrapped)
    free_edges = 0
    protected_rounds = 0
    for edge in blank.edges():
        bounds = edge.bounding_box()
        back = abs(bounds.min.Y) < 1e-6 and abs(bounds.max.Y) < 1e-6
        if back and abs(bounds.min.Z) < 1e-6 and abs(bounds.max.Z) < 1e-6 and bounds.size.X > 10:
            operation.Add(1.0, edge.wrapped)
            protected_rounds += 1
        elif not back and bounds.size.X < 1e-6:
            operation.Add(RAMP_FREE_EDGE_RADIUS_MM, edge.wrapped)
            free_edges += 1
    operation.Build()
    if not operation.IsDone() or free_edges != 10 or protected_rounds != 1:
        raise ValueError(
            f"ramp edge rounding changed: {free_edges} free / {protected_rounds} protected"
        )
    part = Part(Solid(operation.Shape()).wrapped)
    if not cut_joins:
        return part
    joins = [
        tile_join_tool(
            spec.interface,
            male=spec.ramp_join == "male",
        )
        .rotate(Axis.Z, 180 if spec.ramp_join == "male" else 0)
        .moved(Location(((cell + 0.5) * spec.interface.pitch, 0, 0)))
        for cell in range(spec.nx)
    ]
    if spec.ramp_join == "male":
        # Only the outward half of the stock tool is a tab. Its construction
        # wall must not flatten the fixed ramp slope at large unit sizes.
        depth = spec.interface.male_join_depth
        outside = _box(0, -depth, width, depth, spec.interface.height)
        tabs = [Part(join.intersect(outside).solids()) for join in joins]
        result = part.fuse(*tabs).clean()
    else:
        result = part.cut(*joins).clean()
    if abs(result.bounding_box().max.Y - RAMP_RUN_MM) > 1e-5:
        raise ValueError("ramp finished run changed")
    return result


def _free_top_rims(spec: Accessory) -> list[tuple[str, float]]:
    p = spec.interface.pitch
    outward = spec.edge_outward
    if spec.family == "edge-x":
        return [("Y", -outward)]
    if spec.family == "edge-y":
        return [("Y", outward)]
    if spec.family == "corner-in":
        return [
            ("X", p - outward if spec.variant in (2, 3) else outward),
            ("Y", p - outward if spec.variant in (1, 2) else outward),
        ]
    if spec.family == "corner-out":
        return {
            1: [("X", -outward)],
            2: [("Y", outward)],
            3: [("X", p + outward), ("Y", p + outward)],
            4: [("X", outward)],
            5: [("Y", -outward)],
            6: [("X", -outward), ("Y", -outward)],
        }[spec.variant]
    return [("X", -22.5), ("X", 22.5)]


def _round_free_top(part: Part, spec: Accessory) -> Part:
    support = spec.family.startswith("support")
    top = 0 if support else spec.interface.height
    rims = _free_top_rims(spec)
    edges = [
        edge
        for edge in horizontal_edges(part, top)
        if edge.geom_type == GeomType.LINE
        and any(
            abs(getattr(edge.bounding_box().min, axis) - coordinate) < 1e-5
            and abs(getattr(edge.bounding_box().max, axis) - coordinate) < 1e-5
            for axis, coordinate in rims
        )
    ]
    if not edges:
        raise ValueError(f"{spec.family}: no non-mating top rim edges found")
    return part.fillet(SUPPORT_TOP_RADIUS_MM if support else EDGE_TOP_RADIUS_MM, edges)


def _rounded_original_corner_out_half_body(spec: Accessory, half_face: Face) -> Part:
    p = spec.interface.pitch
    partner_variant, partner_offset = {
        1: (2, (0, p, 0)),
        2: (1, (0, -p, 0)),
        4: (5, (-p, 0, 0)),
        5: (4, (p, 0, 0)),
    }[spec.variant]
    partner_face, _ = _edge_plan(
        Accessory(
            "corner-out",
            variant=partner_variant,
            interface=spec.interface,
            edge_outward=spec.edge_outward,
            complete_edge_holes=spec.complete_edge_holes,
            edge_hole_diameter=spec.edge_hole_diameter,
        )
    )
    partner_face = partner_face.moved(Location(partner_offset))
    pair_faces = half_face.fuse(partner_face).faces()
    if len(pair_faces) != 1:
        raise ValueError("corner-out: half-pair plan did not form one face")
    pair_body = prism(Face(pair_faces[0].wrapped), spec.interface.height)
    operation = BRepFilletAPI_MakeFillet(pair_body.wrapped)
    for edge in pair_body.edges():
        operation.Add(EDGE_BODY_RADIUS_MM, edge.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("corner-out: coupled half-pair R3 body fillet failed")
    rounded_pair = Part(Solid(operation.Shape()).wrapped)
    trim = prism(half_face, spec.interface.height + 2).moved(Location((0, 0, -1)))
    body = Part(rounded_pair.intersect(trim).solids()).clean()
    if not body.is_valid or len(body.solids()) != 1 or body.volume <= 0:
        raise ValueError("corner-out: half-pair split produced invalid body")
    return body


def _support_plan(spec: Accessory) -> tuple[float, list[dict]]:
    if spec.family == "support-end":
        male = spec.variant in (3, 4)
        return 2 * spec.interface.pitch, [
            _join(
                0,
                0 if male else 2 * spec.interface.pitch,
                180,
                male,
                support=True,
            )
        ]
    length = spec.length if spec.family == "support-bit" else spec.nx * spec.interface.pitch
    return length, [
        _join(0, 0, 180, True, support=True),
        _join(0, length, 180, False, support=True),
    ]


def _support_window_spans(spec: Accessory, length: float) -> list[tuple[float, float]]:
    scale = 1.0 if spec.family == "support-bit" else spec.interface.unit_scale
    period = 60 * scale
    start = 0.0
    result = []
    while start < length - 35 * scale - 1e-9:
        span = min(32 * scale, length - start - 24 * scale)
        if span >= 12 * scale:
            result.append((start + 12 * scale, span))
        start += period
    return result


def _support(spec: Accessory, *, round_top: bool = True) -> Part:
    length, joins = _support_plan(spec)
    if spec.family == "support-end":
        ramp = (55 if spec.variant in (2, 4) else 75) * spec.interface.unit_scale
        if spec.variant in (1, 2):
            points = [(0, -12), (ramp, -25), (length, -25), (length, 0), (0, 0)]
        else:
            points = [(0, -25), (length - ramp, -25), (length, -12), (length, 0), (0, 0)]
        if round_top and spec.variant == 2:
            profile = Face(Wire.make_polygon([(-22.5, y, z) for y, z in points], close=True))
            profile_radius = SUPPORT_END_PROFILE_RADII_MM[spec.variant]
            profile = profile.fillet_2d(profile_radius, profile.vertices())
            part = Part(Solid.extrude(profile, (45, 0, 0)).wrapped)
            cap_edges = [
                edge
                for edge in part.edges()
                if edge.bounding_box().size.X < 1e-6
                and (
                    abs(edge.bounding_box().min.X + 22.5) < 1e-6
                    or abs(edge.bounding_box().min.X - 22.5) < 1e-6
                )
            ]
            part = part.fillet(SUPPORT_END_CAP_RADIUS_MM, cap_edges)
        else:
            part = _cross_prism(points, 45, -22.5)
    else:
        part = _box(-22.5, 0, 45, length, 25, -25)
    if not round_top:
        for start, span in _support_window_spans(spec, length):
            part = part.cut(_box(-10, start, 20, span, 27, -26))
        return _apply_joins(part, joins)
    if spec.family == "support-end" and spec.variant in (1, 3, 4):
        operation = BRepFilletAPI_MakeFillet(part.wrapped)
        for edge in part.edges():
            operation.Add(SUPPORT_END_BODY_RADII_MM[spec.variant], edge.wrapped)
        operation.Build()
        if not operation.IsDone():
            raise ValueError(f"support-end variant {spec.variant} body fillet failed")
        part = Part(Solid(operation.Shape()).wrapped)
    elif spec.family != "support-end":
        outer_edges = [edge for edge in part.edges() if edge.bounding_box().size.Y > length - 1]
        part = part.fillet(SUPPORT_BODY_RADIUS_MM, outer_edges)
    for start, span in _support_window_spans(spec, length):
        window = rectangle(-10, start, 20, span)
        window_radius = (
            SUPPORT_END_WINDOW_RADII_MM[spec.variant]
            if spec.family == "support-end"
            else SUPPORT_BODY_RADIUS_MM
        )
        window = window.fillet_2d(window_radius, window.vertices())
        part = part.cut(prism(window, 27).moved(Location((0, 0, -26))))
    rim_margin = 10 * (1.0 if spec.family == "support-bit" else spec.interface.unit_scale)
    window_rims = [
        edge
        for edge in part.edges()
        if edge.bounding_box().min.X >= -10.01
        and edge.bounding_box().max.X <= 10.01
        and edge.center().Y >= rim_margin
        and edge.center().Y <= length - rim_margin
        and (abs(edge.bounding_box().min.Z) < 1e-5 or abs(edge.bounding_box().max.Z + 25) < 1e-5)
    ]
    if window_rims:
        if spec.family == "support-end":
            rim_radius = SUPPORT_END_HORIZONTAL_WINDOW_RIM_RADII_MM[spec.variant] * min(
                1.0, spec.interface.unit_scale
            )
        else:
            rim_radius = SUPPORT_WINDOW_RADIUS_MM
        part = part.fillet(rim_radius, window_rims)
    if spec.family == "support-end":
        sloped_window_rims = [
            edge
            for edge in part.edges()
            if edge.bounding_box().min.X >= -10.01
            and edge.bounding_box().max.X <= 10.01
            and edge.center().Y >= rim_margin
            and edge.center().Y <= length - rim_margin
            and abs(edge.bounding_box().min.Z) > 1e-5
            and abs(edge.bounding_box().max.Z + 25) > 1e-5
            and not (edge.bounding_box().size.X < 1e-5 and edge.bounding_box().size.Y < 1e-5)
        ]
        part = part.fillet(
            SUPPORT_END_SLOPED_WINDOW_RIM_RADII_MM[spec.variant]
            * min(1.0, spec.interface.unit_scale),
            sloped_window_rims,
        )
    return _apply_joins(part, joins)


def accessory_datums(spec: Accessory | Rod | RodBrace) -> dict:
    """Machine-readable nominal mating datums; no physical-fit assertions."""
    if isinstance(spec, Rod):
        return rod_datums(spec)
    if isinstance(spec, RodBrace):
        return brace_datums(spec)
    if spec.family in ("plate", "vertical-tile-bracket", "vertical-stop", "lock-45"):
        if spec.family == "vertical-tile-bracket":
            p = spec.interface.pitch
            seat = spec.ny * p - spec.interface.height
            panel_rows = spec.panel_height_cells or spec.ny
            return {
                "shoulder_z": 0,
                "plug_tip_z": -spec.interface.plug_depth,
                "mount_centers": _mount_centers(spec),
                "joins": [],
                "base_cells_x_y": (spec.nx, spec.ny),
                "panel_cells_x_z": (spec.nx, panel_rows),
                "panel_seat_y": seat,
                "panel_plug_tip_y": seat + spec.interface.plug_depth,
                "panel_plug_centers": [
                    ((column + 0.5) * p, seat, PANEL_BOTTOM_MM + (row + 0.5) * p)
                    for row in range(panel_rows)
                    for column in range(spec.nx)
                ],
                "panel_bottom_z": PANEL_BOTTOM_MM,
                "bearing_z": PANEL_BOTTOM_MM,
                "nominal_bearing_gap": 0,
                "panel_inset": spec.interface.height,
                "outward_tile_face": "underside",
                "supported_outward_tile_faces": ("underside", "top"),
                "top_outward_tile_rotation_degrees": {"z": 180, "x": -90},
                "panel_stem_profile_inset": PANEL_STEM_PROFILE_INSET_MM,
                "panel_tip_transition": (
                    spec.interface.plug_depth - PANEL_TIP_RADIUS_MM - PANEL_TIP_TRANSITION_MM,
                    spec.interface.plug_depth - PANEL_TIP_RADIUS_MM,
                ),
                "bidirectional_physical_fit_verified": False,
                "backing": "solid; backed interior round holes are blind",
            }
        result = {
            "shoulder_z": 0,
            "plug_tip_z": -spec.interface.plug_depth,
            "mount_centers": _mount_centers(spec),
            "joins": [],
        }
        if spec.family == "vertical-stop":
            result.update(
                cargo_face_y=spec.ny * spec.interface.pitch,
                cargo_height_z=spec.height,
                body="full-width filled wedge; no wall holes",
                free_edge_radius=VERTICAL_STOP_RADIUS_MM,
            )
        return result
    if spec.family == "ramp":
        male = spec.ramp_join == "male"
        return {
            "underside_z": 0,
            "top_z": spec.interface.height,
            "finished_run": RAMP_RUN_MM,
            "shelf_radius": _ramp_shelf_radius(spec.interface.height),
            "minimum_flat_shelf": RAMP_MINIMUM_FLAT_SHELF_MM,
            "ramp_join": spec.ramp_join,
            "tab_projection": spec.interface.male_join_depth if male else 0,
            "overall_depth": RAMP_RUN_MM + (spec.interface.male_join_depth if male else 0),
            "width_cells": spec.nx,
            "ramp_direction": "positive Y away from the tile",
            "mating_tile_edge": (
                "south female edge after Z=180 rotation; west female edge after Z=90 rotation"
                if male
                else "north male edge at Y=0"
            ),
            "mount_centers": [],
            "joins": [
                {
                    **_join(
                        (cell + 0.5) * spec.interface.pitch,
                        0,
                        180 if male else 0,
                        male,
                        interface=spec.interface,
                    ),
                    "joint_style": spec.interface.joint_style,
                    "height": (
                        spec.interface.male_height if male else spec.interface.female_opening_height
                    ),
                    "open_through_top": spec.interface.joint_style == "full-height",
                }
                for cell in range(spec.nx)
            ],
        }
    if spec.family.startswith("support"):
        length, joins = _support_plan(spec)
        result = {
            "bearing_z": 0,
            "underside_z": -25,
            "body_length": length,
            "mount_centers": [],
            "joins": joins,
        }
        if spec.family == "support-end":
            result.update(
                end=SUPPORT_END_NAMES[spec.variant - 1],
                ramp_length=(55 if spec.variant in (2, 4) else 75) * spec.interface.unit_scale,
                ramp_rise=13,
            )
        return result
    _, joins = _edge_plan(spec)
    for join in joins:
        join["joint_style"] = spec.interface.joint_style
        join["height"] = (
            spec.interface.male_height
            if join["sex"] == "male"
            else spec.interface.female_opening_height
        )
        join["open_through_top"] = (
            join["sex"] == "female" and spec.interface.joint_style == "full-height"
        )
    return {
        "underside_z": 0,
        "top_z": spec.interface.height,
        "mount_centers": [],
        "joins": joins,
        "edge_outward": spec.edge_outward,
        "complete_edge_holes": spec.complete_edge_holes,
        "edge_hole_diameter": spec.edge_hole_diameter,
        "edge_hole_centers": _edge_hole_centers(spec) if spec.complete_edge_holes else [],
    }


def make_accessory(spec: Accessory | Rod | RodBrace) -> Part:
    """Build one connected, labeled accessory without changing print orientation."""
    if isinstance(spec, Rod):
        return make_rod(spec)
    if isinstance(spec, RodBrace):
        return make_rod_brace(spec)
    if not isinstance(spec, Accessory):
        raise ValueError("spec must be an Accessory, Rod or RodBrace")
    if spec.family == "vertical-tile-bracket":
        part = _vertical_bracket(spec)
    elif spec.family == "vertical-stop":
        part = _vertical_stop(spec)
    elif spec.family == "ramp":
        part = _ramp(spec)
    elif spec.family == "plate":
        part = _plate(spec)
    elif spec.family == "lock-45":
        part = _filled_angled_stop(spec)
    elif spec.family.startswith("support"):
        part = _support(spec)
    else:
        face, joins = _edge_plan(spec)
        if spec.interface.joint_style == "full-height":
            males, females = [], []
            for join in joins:
                profile = dovetail_face(spec.interface, depth=join["depth"]).rotate(
                    Axis.Z, join["angle"]
                )
                profile = profile.moved(Location(join["position"]))
                (males if join["sex"] == "male" else females).append(profile)
            part = full_height_part(
                face,
                males,
                females,
                spec.interface.height,
                round_body_corners=False,
                free_top_rims=_free_top_rims(spec),
                free_top_radius=EDGE_TOP_RADIUS_MM,
                interface_blend_radius=spec.interface.tile_join_blend_radius,
            )
        else:
            if spec.family == "corner-out" and spec.variant in (1, 2, 4, 5):
                body = _rounded_original_corner_out_half_body(spec, face)
            else:
                body = prism(face, spec.interface.height)
                operation = BRepFilletAPI_MakeFillet(body.wrapped)
                for edge in body.edges():
                    operation.Add(EDGE_BODY_RADIUS_MM, edge.wrapped)
                operation.Build()
                if not operation.IsDone():
                    raise ValueError(f"{spec.family}: coupled R3 body fillet failed")
                body = Part(Solid(operation.Shape()).wrapped)
            part = _apply_joins(body, joins, interface=spec.interface)
        if spec.interface.joint_style == "full-height":
            part = part.fillet(1, horizontal_edges(part, 0))
        part = _cut_completed_edge_holes(part, spec)
    suffix = (
        (
            f"base{spec.nx}x{spec.ny}_wall{spec.nx}x{spec.panel_height_cells}"
            if spec.family == "vertical-tile-bracket" and spec.panel_height_cells is not None
            else f"{spec.nx}x{spec.ny}"
        )
        if spec.family in ("plate", "vertical-tile-bracket", "vertical-stop", "lock-45")
        else f"v{spec.variant}"
        if spec.family in ("corner-in", "corner-out", "support-end")
        else f"{spec.length:g}mm"
        if spec.family == "support-bit"
        else str(spec.nx)
    )
    part.label = f"{spec.family}_{suffix}"
    if spec.family == "ramp" and spec.ramp_join == "male":
        part.label += "_male"
    if spec.family.startswith("lock-") or spec.family == "vertical-stop":
        part.label += f"_h{spec.height:g}"
    if spec.family in EDGE_FAMILIES:
        if spec.edge_outward != 10:
            part.label += f"_out{spec.edge_outward:g}mm"
        if spec.complete_edge_holes:
            part.label += (
                "_complete-holes"
                if spec.edge_hole_diameter == DEFAULT_HOLE_DIAMETER_MM
                else f"_complete-{spec.edge_hole_diameter:g}mm-holes"
            )
    part.label += f"_{spec.interface.joint_style}"
    if not part.is_valid or len(part.solids()) != 1 or part.volume <= 0:
        raise ValueError(f"{part.label}: invalid or disconnected geometry")
    return part
