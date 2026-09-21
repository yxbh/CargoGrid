"""Experimental three-part trunk blocker geometry.

All dimensions are millimeters. The fixed base underside is Z=0. The moving
wall is authored locally with its front at Y=0 and its fingers pointing in +Y,
then positioned at the requested extension. Its front posts reuse Cargo-Grid's
shared bidirectional native-BREP panel connector.
"""

from dataclasses import dataclass
from math import atan, cos, degrees, isfinite, radians, sin, sqrt, tan

from build123d import (
    Align,
    Axis,
    Box,
    Color,
    Cone,
    Cylinder,
    Edge,
    Face,
    GeomType,
    Line,
    Location,
    Part,
    Plane,
    Polygon,
    Pos,
    Spline,
    ThreePointArc,
    Wire,
    extrude,
    fillet,
)

from cargo_grid.accessories import make_bidirectional_panel_connector


@dataclass(frozen=True)
class TrunkBlockerDimensions:
    width: float = 60.0
    base_length: float = 132.0
    floor: float = 5.0
    floor_gap: float = 0.35
    wall_height: float = 120.0
    wall_thickness: float = 8.0
    finger_length: float = 124.0
    outer_width: float = 9.0
    outer_centre: float = 15.8
    centre_width: float = 10.0
    finger_height: float = 11.35
    reinforcement_length: float = 24.0
    reinforcement_height: float = 34.0
    pitch: float = 8.0
    positions: int = 7
    rack_root: float = 25.0
    rack_wall_inner: float = 24.0
    rack_tooth_depth: float = 4.2
    rack_height: float = 16.85
    rack_carrier_inner: float = 24.7
    rack_carrier_outer: float = 27.5
    rack_carrier_start: float = 50.0
    rack_carrier_end: float = 128.0
    moving_tooth_root: float = 17.8
    moving_tooth_depth: float = 6.0
    tooth_height: float = 14.0
    ratchet_ramp_radial_per_axial: float = 4.0 / 3.0
    rack_lock_offset: float = 0.7
    moving_lock_offset: float = 0.5
    tooth_tip_land: float = 2.0
    moving_carrier_start: float = 98.0
    moving_carrier_end: float = 123.0
    release_stroke: float = 3.7
    release_transition_end: float = 96.0
    keeper_start: float = 28.0
    keeper_length: float = 18.0
    keeper_gap: float = 0.5
    keeper_roof_bottom: float = 22.0
    keeper_roof_thickness: float = 4.0
    keeper_centre_half_width: float = 5.7
    keeper_outer_inner: float = 17.2
    pad_inner: float = 11.3
    pad_outer: float = 20.3
    pad_start: float = 113.0
    pad_length: float = 11.0
    pad_height: float = 24.0
    guide_inner: float = 5.15
    guide_outer: float = 7.45
    guide_start: float = 4.0
    guide_end: float = 45.0
    guide_top: float = 16.85
    lower_bearing_top: float = 5.2
    bearing_clearance: float = 0.15
    keeper_seat_z: float = 16.85
    keeper_width: float = 61.6
    screw_clearance: float = 3.4
    screw_pilot: float = 2.7
    screw_pilot_boss_radius: float = 3.35
    screw_pilot_bottom: float = 5.0
    countersink_diameter: float = 7.0
    countersink_angle: float = 90.0
    screw_length: float = 16.0
    screw_head_diameter: float = 6.0
    anchor_y: float = 100.0
    anchor_pitch: float = 60.0
    anchor_depth: float = 12.8
    anchor_end_radius: float = 2.0
    anchor_lobe_centre: float = 14.344
    anchor_lobe_radius: float = 7.856
    anchor_notch_radius: float = 6.215

    @property
    def extension(self) -> float:
        return self.pitch * (self.positions - 1)

    @property
    def rack_tip(self) -> float:
        return self.rack_root - self.rack_tooth_depth

    @property
    def rack_ramp_run(self) -> float:
        return self.rack_tooth_depth / self.ratchet_ramp_radial_per_axial

    @property
    def moving_tip(self) -> float:
        return self.moving_tooth_root + self.moving_tooth_depth

    @property
    def moving_ramp_run(self) -> float:
        return self.moving_tooth_depth / self.ratchet_ramp_radial_per_axial

    @property
    def rack_root_land(self) -> float:
        return self.pitch - self.rack_ramp_run - self.tooth_tip_land

    @property
    def moving_root_land(self) -> float:
        return self.pitch - self.moving_ramp_run - self.tooth_tip_land

    @property
    def ratchet_ramp_angle_degrees(self) -> float:
        return degrees(atan(self.ratchet_ramp_radial_per_axial))

    @property
    def locking_backlash(self) -> float:
        return self.rack_lock_offset - self.moving_lock_offset

    @property
    def locking_engagement(self) -> float:
        return self.moving_tip - self.rack_tip

    @property
    def released_tip_clearance(self) -> float:
        return self.rack_tip - (self.moving_tip - self.release_stroke)

    @property
    def pusher_z(self) -> float:
        return self.floor + self.floor_gap

    @property
    def base_anchor_centres_y(self) -> tuple[float, float]:
        return (self.anchor_y - self.anchor_pitch, self.anchor_y)

    @property
    def keeper_seat(self) -> float:
        return self.keeper_seat_z

    @property
    def pad_above_finger(self) -> float:
        return self.pad_height - self.finger_height

    @property
    def pad_top(self) -> float:
        return self.pusher_z + self.pad_height

    @property
    def countersink_depth(self) -> float:
        return (self.countersink_diameter - self.screw_clearance) / (
            2 * tan(radians(self.countersink_angle / 2))
        )

    @property
    def countersink_side_land(self) -> float:
        outer_axis = max(abs(x) for x, _ in SCREW_AXES_MM)
        return self.keeper_width / 2 - outer_axis - self.countersink_diameter / 2


DIMENSIONS = TrunkBlockerDimensions()
BASE_ANCHOR_CENTRES_Y_MM = DIMENSIONS.base_anchor_centres_y
MOVING_TOOTH_STATIONS_MM = (104.0, 112.0, 120.0)
SCREW_AXES_MM = ((-26.0, 32.5), (26.0, 32.5), (-26.0, 41.5), (26.0, 41.5))
CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM = (30.0, 90.0)
FREE_EDGE_RADIUS_MM = 2.0
DETAIL_EDGE_RADIUS_MM = 1.0
EDGE_SELECTION_TOLERANCE_MM = 1e-5


@dataclass(frozen=True)
class TrunkBlockerSpec:
    """Approved fixed blocker geometry with one configurable static pose."""

    extension_mm: float = 24.0
    released_illustration: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.extension_mm, bool)
            or not isinstance(self.extension_mm, (int, float))
            or not isfinite(self.extension_mm)
            or not 0 <= self.extension_mm <= DIMENSIONS.extension
        ):
            raise ValueError(
                f"trunk-blocker extension must be within 0..{DIMENSIONS.extension:g} mm"
            )
        if not isinstance(self.released_illustration, bool):
            raise ValueError("released illustration must be a boolean")


def _block(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> Part:
    return Pos(x0, y0, z0) * Box(
        x1 - x0,
        y1 - y0,
        z1 - z0,
        align=(Align.MIN, Align.MIN, Align.MIN),
    )


def _prism(points: list[tuple[float, float]], z0: float, height: float) -> Part:
    return extrude(
        Pos(0, 0, z0) * Polygon(*points, align=None),
        amount=height,
        dir=(0, 0, 1),
    )


def _axis_bounds(edge: Edge, axis: str) -> tuple[float, float]:
    bounds = edge.bounding_box()
    return getattr(bounds.min, axis), getattr(bounds.max, axis)


def _lies_at(edge: Edge, axis: str, value: float) -> bool:
    low, high = _axis_bounds(edge, axis)
    return (
        abs(low - value) < EDGE_SELECTION_TOLERANCE_MM
        and abs(high - value) < EDGE_SELECTION_TOLERANCE_MM
    )


def _span(edge: Edge, axis: str) -> float:
    low, high = _axis_bounds(edge, axis)
    return high - low


def _fillet_exact(
    part: Part,
    edges: list[Edge],
    *,
    radius: float,
    expected: int,
    feature: str,
) -> Part:
    if len(edges) != expected:
        raise ValueError(f"{feature} expected {expected} fillet edges, found {len(edges)}")
    rounded = part.fillet(radius, edges)
    if not rounded.is_valid or len(rounded.solids()) != 1:
        raise ValueError(f"{feature} fillet did not produce one valid solid")
    return rounded


def _round_finger(
    finger: Part,
    *,
    tip_y: float,
    expected: int,
) -> Part:
    edges = [
        edge
        for edge in finger.edges()
        if (
            _span(edge, "Y") > 1
            and _span(edge, "Z") < EDGE_SELECTION_TOLERANCE_MM
            or _lies_at(edge, "Y", tip_y)
        )
    ]
    return _fillet_exact(
        finger,
        edges,
        radius=DETAIL_EDGE_RADIUS_MM,
        expected=expected,
        feature="finger R1 free-edge",
    )


def _anchor_profile(d: TrunkBlockerDimensions) -> Face:
    c, r, q = d.anchor_lobe_centre, d.anchor_lobe_radius, d.anchor_notch_radius
    diagonal = (r + q) * sqrt(2)
    arcs = (
        ((c, c), r, 135, 45, -45),
        ((diagonal, 0), q, 135, 180, 225),
        ((c, -c), r, 45, -45, -135),
        ((0, -diagonal), q, 45, 90, 135),
        ((-c, -c), r, -45, -135, -225),
        ((-diagonal, 0), q, -45, 0, 45),
        ((-c, c), r, -135, -225, -315),
        ((0, diagonal), q, -135, -90, -45),
    )
    triplets = [
        [
            (x + radius * cos(radians(angle)), y + radius * sin(radians(angle)), 0)
            for angle in angles
        ]
        for (x, y), radius, *angles in arcs
    ]
    edges = []
    for index, points in enumerate(triplets):
        edges.extend(ThreePointArc(*points).edges())
        edges.extend(Line(points[-1], triplets[(index + 1) % len(triplets)][0]).edges())
    return Face(Wire(edges))


def _analytic_anchor(d: TrunkBlockerDimensions, centre_y: float | None = None) -> Part:
    centre_y = d.anchor_y if centre_y is None else centre_y
    anchor = extrude(_anchor_profile(d), amount=d.anchor_depth + 0.5, dir=(0, 0, 1))
    anchor = anchor.moved(Location((0, centre_y, -d.anchor_depth)))
    bottom = [edge for edge in anchor.edges() if abs(edge.center().Z + d.anchor_depth) < 1e-5]
    return fillet(bottom, radius=d.anchor_end_radius)


def _rack_tooth(
    side: int,
    station: float,
    d: TrunkBlockerDimensions = DIMENSIONS,
) -> Part:
    root = side * d.rack_root
    tip = side * d.rack_tip
    lock_y = station + d.rack_lock_offset
    return _prism(
        [
            (root, lock_y),
            (tip, lock_y),
            (tip, lock_y + d.tooth_tip_land),
            (root, lock_y + d.tooth_tip_land + d.rack_ramp_run),
        ],
        d.floor - 0.1,
        d.pusher_z + d.tooth_height - d.floor + 0.1,
    )


def _moving_tooth(
    side: int,
    station: float,
    shift: float,
    d: TrunkBlockerDimensions = DIMENSIONS,
) -> Part:
    root = side * d.moving_tooth_root - shift
    tip = side * d.moving_tip - shift
    lock_y = station + d.moving_lock_offset
    return _prism(
        [
            (root, lock_y - d.tooth_tip_land - d.moving_ramp_run),
            (tip, lock_y - d.tooth_tip_land),
            (tip, lock_y),
            (root, lock_y),
        ],
        0,
        d.tooth_height,
    )


def _guide_wall(
    side: int,
    d: TrunkBlockerDimensions = DIMENSIONS,
) -> Part:
    x0, x1 = sorted((side * d.guide_inner, side * d.guide_outer))
    return _block(
        x0,
        x1,
        d.guide_start,
        d.guide_end,
        d.floor - 0.1,
        d.guide_top,
    )


def _lower_bearing_lands(
    d: TrunkBlockerDimensions = DIMENSIONS,
) -> tuple[Part, Part, Part]:
    relaxed_outer = d.outer_centre + d.outer_width / 2
    released_inner = d.outer_centre - d.outer_width / 2 - d.release_stroke
    ranges = (
        (-relaxed_outer - d.bearing_clearance, -released_inner + d.bearing_clearance),
        (-d.centre_width / 2 - d.bearing_clearance, d.centre_width / 2 + d.bearing_clearance),
        (released_inner - d.bearing_clearance, relaxed_outer + d.bearing_clearance),
    )
    return tuple(
        _block(
            x0,
            x1,
            d.keeper_start,
            d.keeper_start + d.keeper_length,
            d.floor - 0.1,
            d.lower_bearing_top,
        )
        for x0, x1 in ranges
    )


def _make_base(
    d: TrunkBlockerDimensions = DIMENSIONS,
    *,
    round_edges: bool = True,
) -> Part:
    base = _block(-d.width / 2, d.width / 2, 0.5, 0.5 + d.base_length, 0, d.floor)
    anchor_centres = d.base_anchor_centres_y
    for centre_y in anchor_centres:
        base += _analytic_anchor(d, centre_y)
    root_window = d.anchor_lobe_centre + d.anchor_lobe_radius + d.anchor_end_radius
    root = [
        edge
        for edge in base.edges()
        if abs(edge.center().Z) < 1e-6
        and any(
            centre_y - root_window < edge.bounding_box().min.Y
            and edge.bounding_box().max.Y < centre_y + root_window
            for centre_y in anchor_centres
        )
        and -25 < edge.bounding_box().min.X
        and edge.bounding_box().max.X < 25
    ]
    if len(root) != 16 * len(anchor_centres):
        raise ValueError(
            f"base-anchor R2 roots expected {16 * len(anchor_centres)} edges, found {len(root)}"
        )
    base = fillet(root, radius=d.anchor_end_radius)
    for side in (-1, 1):
        x0, x1 = sorted((side * d.rack_wall_inner, side * d.width / 2))
        base += _block(x0, x1, 0.5, 0.5 + d.base_length, d.floor - 0.1, d.rack_height)
        x0, x1 = sorted((side * d.rack_carrier_inner, side * d.rack_carrier_outer))
        base += _block(
            x0,
            x1,
            d.rack_carrier_start,
            d.rack_carrier_end,
            d.floor - 0.1,
            d.pusher_z + d.tooth_height,
        )
        for index in range(d.positions + 2):
            station = MOVING_TOOTH_STATIONS_MM[0] - d.extension + index * d.pitch
            base += _rack_tooth(side, station, d)
        base += _guide_wall(side, d)
    for x, y in SCREW_AXES_MM:
        base += Pos(x, y, d.floor - 0.1) * Cylinder(
            d.screw_pilot_boss_radius,
            d.rack_height - d.floor + 0.1,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
        )
        base -= Pos(x, y, d.screw_pilot_bottom) * Cylinder(
            d.screw_pilot / 2,
            d.rack_height - d.screw_pilot_bottom + 0.1,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
        )
    if round_edges:
        perimeter = [
            edge
            for edge in base.edges()
            if edge.geom_type == GeomType.LINE
            and edge.bounding_box().min.Z >= -EDGE_SELECTION_TOLERANCE_MM
            and (
                _lies_at(edge, "X", -d.width / 2)
                or _lies_at(edge, "X", d.width / 2)
                or _lies_at(edge, "Y", 0.5)
                or _lies_at(edge, "Y", 0.5 + d.base_length)
            )
        ]
        base = _fillet_exact(
            base,
            perimeter,
            radius=FREE_EDGE_RADIUS_MM,
            expected=20,
            feature="fixed-base R2 outer perimeter",
        )
    for bearing_land in _lower_bearing_lands(d):
        base += bearing_land
    base.label = "fixed_base_analytic_anchor_candidate"
    base.color = Color(0.19, 0.39, 0.50)
    return base


def _finger_displacement(y: float, d: TrunkBlockerDimensions) -> float:
    t = max(
        0.0,
        min(
            1.0,
            (y - d.reinforcement_length) / (d.release_transition_end - d.reinforcement_length),
        ),
    )
    return d.release_stroke * t * t * (3 - 2 * t)


def _outer_finger(side: int, released: bool, d: TrunkBlockerDimensions) -> Part:
    centre = side * d.outer_centre
    if not released:
        return _block(
            centre - d.outer_width / 2,
            centre + d.outer_width / 2,
            -0.5,
            d.finger_length,
            0,
            d.finger_height,
        )
    ys = [
        d.reinforcement_length + index * (d.release_transition_end - d.reinforcement_length) / 12
        for index in range(13)
    ]
    paths = [
        [
            (
                centre + offset - side * _finger_displacement(y, d),
                y,
                0,
            )
            for y in ys
        ]
        for offset in (-d.outer_width / 2, d.outer_width / 2)
    ]
    a, b = paths
    start_a, start_b = (a[0][0], -0.5, 0), (b[0][0], -0.5, 0)
    end_a, end_b = (a[-1][0], d.finger_length, 0), (b[-1][0], d.finger_length, 0)
    edges = []
    for edge in (
        Line(start_a, a[0]),
        Spline(*a, tangents=((0, 1, 0), (0, 1, 0))),
        Line(a[-1], end_a),
        Line(end_a, end_b),
        Line(end_b, b[-1]),
        Spline(*reversed(b), tangents=((0, -1, 0), (0, -1, 0))),
        Line(b[0], start_b),
        Line(start_b, start_a),
    ):
        edges.extend(edge.edges())
    return extrude(Face(Wire(edges)), amount=d.finger_height, dir=(0, 0, 1))


def _make_pusher_body(
    released: bool,
    d: TrunkBlockerDimensions = DIMENSIONS,
    *,
    round_edges: bool = True,
) -> Part:
    pusher = _block(
        -d.width / 2,
        d.width / 2,
        -d.wall_thickness,
        0,
        0,
        d.wall_height,
    )
    centre_finger = _block(
        -d.centre_width / 2,
        d.centre_width / 2,
        -0.5,
        d.finger_length,
        0,
        d.finger_height,
    )
    pusher += (
        _round_finger(centre_finger, tip_y=d.finger_length, expected=8)
        if round_edges
        else centre_finger
    )
    for side in (-1, 1):
        outer_finger = _outer_finger(side, released, d)
        pusher += (
            _round_finger(
                outer_finger,
                tip_y=d.finger_length,
                expected=16 if released else 8,
            )
            if round_edges
            else outer_finger
        )
        shift = side * d.release_stroke if released else 0
        x0, x1 = sorted(
            (
                side * (d.outer_centre - d.outer_width / 2) - shift,
                side * (d.outer_centre + d.outer_width / 2) - shift,
            )
        )
        carrier = _block(
            x0,
            x1,
            d.moving_carrier_start,
            d.moving_carrier_end,
            0,
            d.tooth_height,
        )
        pusher += carrier
        for station in MOVING_TOOTH_STATIONS_MM:
            pusher += _moving_tooth(side, station, shift, d)
        x0, x1 = sorted((side * d.pad_inner - shift, side * d.pad_outer - shift))
        pad = _block(
            x0,
            x1,
            d.pad_start,
            d.pad_start + d.pad_length,
            0,
            d.pad_height,
        )
        pusher += (
            _fillet_exact(
                pad,
                list(pad.edges()),
                radius=DETAIL_EDGE_RADIUS_MM,
                expected=12,
                feature="squeeze-pad R1 free-edge",
            )
            if round_edges
            else pad
        )
    for x, width in (
        (-d.outer_centre, d.outer_width),
        (0, d.centre_width),
        (d.outer_centre, d.outer_width),
    ):
        profile = Plane.YZ.offset(x - width / 2) * Polygon(
            (-0.5, 0),
            (d.reinforcement_length, 0),
            (d.reinforcement_length, d.finger_height),
            (0, d.reinforcement_height),
            (-0.5, d.reinforcement_height),
            align=None,
        )
        reinforcement = extrude(profile, amount=width)
        if round_edges:
            ridge_edges = [
                edge
                for edge in reinforcement.edges()
                if edge.geom_type == GeomType.LINE and _span(edge, "Y") > 1 and _span(edge, "Z") > 1
            ]
            reinforcement = _fillet_exact(
                reinforcement,
                ridge_edges,
                radius=FREE_EDGE_RADIUS_MM,
                expected=2,
                feature="finger-reinforcement R2 ridge",
            )
        pusher += reinforcement
    if round_edges:
        wall_edges = [
            edge
            for edge in pusher.edges()
            if edge.geom_type == GeomType.LINE
            and (
                _lies_at(edge, "X", -d.width / 2)
                or _lies_at(edge, "X", d.width / 2)
                or _lies_at(edge, "Y", -d.wall_thickness)
                or _lies_at(edge, "Z", d.wall_height)
            )
        ]
        pusher = _fillet_exact(
            pusher,
            wall_edges,
            radius=FREE_EDGE_RADIUS_MM,
            expected=11,
            feature="moving-wall R2 outer envelope",
        )
        transition_edges = [
            edge
            for edge in pusher.edges()
            if edge.geom_type == GeomType.LINE
            and (
                _lies_at(edge, "Y", 0)
                and _lies_at(edge, "Z", d.reinforcement_height)
                or _lies_at(edge, "Y", d.reinforcement_length)
                and _lies_at(edge, "Z", d.finger_height)
            )
        ]
        pusher = _fillet_exact(
            pusher,
            transition_edges,
            radius=DETAIL_EDGE_RADIUS_MM,
            expected=6,
            feature="finger-reinforcement R1 transition",
        )
    pusher.label = "moving_wall_connector_backing"
    pusher.color = Color(0.92, 0.51, 0.15)
    return pusher


def _make_pusher(
    spec: TrunkBlockerSpec,
    d: TrunkBlockerDimensions = DIMENSIONS,
    *,
    round_edges: bool = True,
) -> Part:
    pusher = _make_pusher_body(
        spec.released_illustration,
        d,
        round_edges=round_edges,
    ).moved(Location((0, -spec.extension_mm, d.pusher_z)))
    node = make_bidirectional_panel_connector()
    front_y = -d.wall_thickness - spec.extension_mm
    for height in CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM:
        connector = node.rotate(Axis.X, -90).moved(
            Location((-d.width / 2, front_y, d.pusher_z + height + d.width / 2))
        )
        pusher += connector
    pusher.label = (
        "moving_wall_released_static_illustration" if spec.released_illustration else "moving_wall"
    )
    pusher.color = Color(0.92, 0.51, 0.15)
    return pusher


def _make_keeper(
    d: TrunkBlockerDimensions = DIMENSIONS,
    *,
    round_edges: bool = True,
    fill_channels: bool = True,
) -> Part:
    if not 0 < d.countersink_angle < 180:
        raise ValueError("countersink included angle must be between 0 and 180 degrees")
    if d.countersink_diameter <= d.screw_clearance:
        raise ValueError("countersink mouth must be wider than the clearance bore")
    if d.countersink_depth >= d.keeper_roof_thickness:
        raise ValueError("countersink must leave a straight bore through the keeper roof")
    y0, y1 = d.keeper_start, d.keeper_start + d.keeper_length
    top = d.keeper_roof_bottom + d.keeper_roof_thickness
    keeper = _block(
        -d.keeper_width / 2,
        d.keeper_width / 2,
        y0,
        y1,
        d.keeper_seat if fill_channels else d.keeper_roof_bottom,
        top,
    )
    if not fill_channels:
        for x0, x1 in (
            (-d.keeper_width / 2, -d.keeper_outer_inner),
            (-d.keeper_centre_half_width, d.keeper_centre_half_width),
            (d.keeper_outer_inner, d.keeper_width / 2),
        ):
            keeper += _block(
                x0,
                x1,
                y0,
                y1,
                d.keeper_seat,
                d.keeper_roof_bottom + 0.1,
            )
    if round_edges:
        keeper_edges = [
            edge
            for edge in keeper.edges()
            if edge.geom_type == GeomType.LINE
            and (
                _lies_at(edge, "Z", top)
                and (_lies_at(edge, "Y", y0) or _lies_at(edge, "Y", y1))
                or _span(edge, "Z") > 1
                and (
                    _lies_at(edge, "X", -d.keeper_width / 2)
                    or _lies_at(edge, "X", d.keeper_width / 2)
                )
            )
        ]
        keeper = _fillet_exact(
            keeper,
            keeper_edges,
            radius=DETAIL_EDGE_RADIUS_MM,
            expected=6,
            feature="keeper R1 free-edge",
        )
    for x, y in SCREW_AXES_MM:
        keeper -= Pos(x, y, d.keeper_seat - 0.1) * Cylinder(
            d.screw_clearance / 2,
            top - d.keeper_seat + 0.2,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
        )
        keeper -= Pos(x, y, top - d.countersink_depth) * Cone(
            d.screw_clearance / 2,
            d.countersink_diameter / 2 + 0.1 * tan(radians(d.countersink_angle / 2)),
            d.countersink_depth + 0.1,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
        )
    keeper.label = "short_screwed_keeper"
    keeper.color = Color(0.72, 0.78, 0.80)
    return keeper


def make_trunk_blocker_parts(
    spec: TrunkBlockerSpec = TrunkBlockerSpec(),
) -> tuple[Part, Part, Part]:
    """Build the three complete native-BREP manufactured parts."""

    if not isinstance(spec, TrunkBlockerSpec):
        raise ValueError("spec must be a TrunkBlockerSpec")
    base = _make_base()
    pusher = _make_pusher(spec)
    keeper = _make_keeper()
    for part in (base, pusher, keeper):
        if not part.is_valid or len(part.solids()) != 1 or part.volume <= 0:
            raise ValueError(f"expected one valid connected solid: {part.label}")
    return base, pusher, keeper
