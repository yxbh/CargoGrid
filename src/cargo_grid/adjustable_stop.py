"""Three-part adjustable stop geometry.

All dimensions are millimeters. The fixed base underside is Z=0. The moving
wall is authored locally with its front at Y=0 and its fingers pointing in +Y,
then positioned at the requested extension. Its front posts reuse Cargo-Grid's
shared bidirectional native-BREP panel connector.
"""

from dataclasses import dataclass
from functools import lru_cache
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
    RectangleRounded,
    Spline,
    ThreePointArc,
    Wire,
    extrude,
    fillet,
)
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.GeomAbs import GeomAbs_C0, GeomAbs_G1
from OCP.OCP.collections import (
    IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher,
)
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS

from cargo_grid.accessories import make_bidirectional_panel_connector


@dataclass(frozen=True)
class AdjustableStopDimensions:
    width: float = 60.0
    base_start: float = 10.0
    base_length: float = 120.0
    floor: float = 5.0
    floor_gap: float = 0.35
    wall_height: float = 120.0
    wall_thickness: float = 8.0
    finger_reference_length: float = 124.0
    prong_shortening: float = 6.5
    outer_width: float = 9.0
    outer_centre: float = 15.8
    centre_width: float = 10.0
    finger_bottom: float = -0.25
    finger_top: float = 11.4
    reinforcement_length: float = 24.0
    reinforcement_height: float = 34.0
    reinforcement_finger_join_top: float = 11.35
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
    moving_tooth_reference_stations: tuple[float, float, float] = (104.0, 112.0, 120.0)
    ratchet_ramp_radial_per_axial: float = 4.0 / 3.0
    rack_lock_offset: float = 0.7
    moving_lock_offset: float = 0.5
    tooth_tip_land: float = 2.0
    moving_carrier_reference_start: float = 98.0
    moving_carrier_reference_end: float = 123.0
    release_stroke: float = 3.7
    release_transition_reference_end: float = 96.0
    keeper_reference_start: float = 28.0
    keeper_back_shift: float = 3.0
    keeper_length: float = 18.0
    keeper_gap: float = 0.5
    keeper_roof_bottom: float = 22.0
    keeper_roof_thickness: float = 4.0
    keeper_centre_half_width: float = 5.7
    keeper_outer_inner: float = 17.2
    pad_inner: float = 11.3
    pad_outer: float = 20.3
    pad_reference_start: float = 113.0
    pad_length: float = 11.0
    pad_height: float = 24.0
    guide_inner: float = 5.1
    guide_outer: float = 7.45
    guide_start: float = 13.5
    guide_reference_end: float = 45.0
    outer_guide_start: float = 12.0
    guide_top: float = 16.85
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
    def base_end(self) -> float:
        return self.base_start + self.base_length

    @property
    def finger_length(self) -> float:
        return self.finger_reference_length - self.prong_shortening

    @property
    def moving_tooth_stations(self) -> tuple[float, float, float]:
        return tuple(
            station - self.prong_shortening for station in self.moving_tooth_reference_stations
        )

    @property
    def rack_tooth_stations(self) -> tuple[float, ...]:
        return tuple(
            self.moving_tooth_reference_stations[0] - self.extension + index * self.pitch
            for index in range(self.positions + 2)
        )

    @property
    def moving_carrier_start(self) -> float:
        return self.moving_carrier_reference_start - self.prong_shortening

    @property
    def moving_carrier_end(self) -> float:
        return self.moving_carrier_reference_end - self.prong_shortening

    @property
    def release_transition_end(self) -> float:
        return self.release_transition_reference_end - self.prong_shortening

    @property
    def keeper_start(self) -> float:
        return self.keeper_reference_start + self.keeper_back_shift

    @property
    def keeper_end(self) -> float:
        return self.keeper_start + self.keeper_length

    @property
    def pad_start(self) -> float:
        return self.pad_reference_start - self.prong_shortening

    @property
    def guide_end(self) -> float:
        return self.guide_reference_end + self.keeper_back_shift

    @property
    def finger_guide_clearance(self) -> float:
        return self.guide_inner - self.centre_width / 2

    @property
    def outer_guide_inner(self) -> float:
        return self.outer_centre + self.outer_width / 2 + self.finger_guide_clearance

    @property
    def outer_guide_end(self) -> float:
        return self.guide_end

    @property
    def outer_guide_tool_end(self) -> float:
        return self.outer_guide_end + DETAIL_EDGE_RADIUS_MM

    @property
    def outer_guide_tool_outer(self) -> float:
        return self.rack_wall_inner + 2 * DETAIL_EDGE_RADIUS_MM

    @property
    def screw_axes(self) -> tuple[tuple[float, float], ...]:
        return tuple(
            (x, y + self.keeper_back_shift)
            for x, y in ((-26.0, 32.5), (26.0, 32.5), (-26.0, 41.5), (26.0, 41.5))
        )

    @property
    def pusher_y_offset(self) -> float:
        return self.prong_shortening

    @property
    def flexible_beam_length(self) -> float:
        return self.release_transition_end - self.reinforcement_length

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
    def finger_height(self) -> float:
        return self.finger_top - self.finger_bottom

    @property
    def finger_bottom_z(self) -> float:
        return self.pusher_z + self.finger_bottom

    @property
    def pusher_bed_bottom(self) -> float:
        return self.finger_bottom

    @property
    def finger_top_z(self) -> float:
        return self.pusher_z + self.finger_top

    @property
    def pad_above_finger(self) -> float:
        return self.pad_height - self.finger_top

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


DIMENSIONS = AdjustableStopDimensions()
BASE_ANCHOR_CENTRES_Y_MM = DIMENSIONS.base_anchor_centres_y
MOVING_TOOTH_STATIONS_MM = DIMENSIONS.moving_tooth_stations
RACK_TOOTH_STATIONS_MM = DIMENSIONS.rack_tooth_stations
SCREW_AXES_MM = DIMENSIONS.screw_axes
CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM = (30.0, 90.0)
FREE_EDGE_RADIUS_MM = 2.0
DETAIL_EDGE_RADIUS_MM = 1.0
EDGE_SELECTION_TOLERANCE_MM = 1e-5


@dataclass(frozen=True)
class AdjustableStopSpec:
    """Approved adjustable-stop geometry with one configurable static pose."""

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
                f"adjustable-stop extension must be within 0..{DIMENSIONS.extension:g} mm"
            )
        if not isinstance(self.released_illustration, bool):
            raise ValueError("released illustration must be a boolean")


@dataclass(frozen=True)
class AdjustableStopProngLockClipDimensions:
    side_clearance: float = 0.05
    leg_lateral_clearance: float = 0.0
    top_clearance: float = 0.05
    floor_clearance: float = 0.6
    bar_start: float = 101.0
    bar_thickness: float = 3.0
    lift_wing_overhang: float = 2.0
    round_radius: float = 1.0
    collar_y_clearance: float = 0.1
    collar_rear_y_clearance: float = 0.8
    collar_rear_bar_thickness: float = 3.0
    leg_root_relief_height: float = 2.5
    loop_depth: float = 8.0
    loop_outer_width: float = 36.0
    loop_outer_height: float = 31.0
    loop_inner_width: float = 24.0
    loop_inner_height: float = 19.0
    loop_outer_corner_radius: float = 4.0
    loop_inner_corner_radius: float = 5.0
    loop_inner_edge_radius: float = 3.0
    loop_bar_overlap: float = 1.9
    loop_bottom_band: float = 7.0


PRONG_LOCK_CLIP_DIMENSIONS = AdjustableStopProngLockClipDimensions()


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


def _clip_locating_wall(
    side: int,
    *,
    y0: float,
    y1: float,
    z0: float,
    transition_start: float,
    transition_end: float,
    centre_outer: float,
    gap_outer: float,
    lateral_clearance: float,
    root_clearance: float,
) -> Part:
    inner_wide = centre_outer + lateral_clearance
    outer_wide = gap_outer - lateral_clearance
    inner_narrow = centre_outer + root_clearance
    outer_narrow = gap_outer - root_clearance
    points = {
        "inner_bottom": (inner_wide, y0, z0),
        "outer_bottom": (outer_wide, y0, z0),
        "outer_transition_start": (outer_wide, y0, transition_start),
        "outer_transition_end": (outer_narrow, y0, transition_end),
        "inner_transition_end": (inner_narrow, y0, transition_end),
        "inner_transition_start": (inner_wide, y0, transition_start),
    }
    edges = []
    for edge in (
        Line(points["inner_bottom"], points["outer_bottom"]),
        Line(points["outer_bottom"], points["outer_transition_start"]),
        Spline(
            points["outer_transition_start"],
            points["outer_transition_end"],
            tangents=((0, 0, 1), (0, 0, 1)),
        ),
        Line(points["outer_transition_end"], points["inner_transition_end"]),
        Spline(
            points["inner_transition_end"],
            points["inner_transition_start"],
            tangents=((0, 0, -1), (0, 0, -1)),
        ),
        Line(points["inner_transition_start"], points["inner_bottom"]),
    ):
        edges.extend(edge.edges())
    wall = extrude(Face(Wire(edges)), amount=y1 - y0, dir=(0, 1, 0))
    return wall if side == 1 else wall.mirror(Plane.YZ)


def _clip_rear_crossbar(
    *,
    y0: float,
    y1: float,
    z0: float,
    z1: float,
    transition_start: float,
    transition_end: float,
    gap_outer: float,
    lateral_clearance: float,
    root_clearance: float,
) -> Part:
    outer_wide = gap_outer - lateral_clearance
    outer_narrow = gap_outer - root_clearance
    points = {
        "left_bottom": (-outer_wide, y0, z0),
        "right_bottom": (outer_wide, y0, z0),
        "right_transition_start": (outer_wide, y0, transition_start),
        "right_transition_end": (outer_narrow, y0, transition_end),
        "right_top": (outer_narrow, y0, z1),
        "left_top": (-outer_narrow, y0, z1),
        "left_transition_end": (-outer_narrow, y0, transition_end),
        "left_transition_start": (-outer_wide, y0, transition_start),
    }
    edges = []
    for edge in (
        Line(points["left_bottom"], points["right_bottom"]),
        Line(points["right_bottom"], points["right_transition_start"]),
        Spline(
            points["right_transition_start"],
            points["right_transition_end"],
            tangents=((0, 0, 1), (0, 0, 1)),
        ),
        Line(points["right_transition_end"], points["right_top"]),
        Line(points["right_top"], points["left_top"]),
        Line(points["left_top"], points["left_transition_end"]),
        Spline(
            points["left_transition_end"],
            points["left_transition_start"],
            tangents=((0, 0, -1), (0, 0, -1)),
        ),
        Line(points["left_transition_start"], points["left_bottom"]),
    ):
        edges.extend(edge.edges())
    return extrude(Face(Wire(edges)), amount=y1 - y0, dir=(0, 1, 0))


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


def _c0_edges(part: Part) -> list[Edge]:
    edge_faces = IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapesAndAncestors_s(
        part.wrapped,
        TopAbs_EDGE,
        TopAbs_FACE,
        edge_faces,
    )
    result = []
    for index in range(1, edge_faces.Extent() + 1):
        faces = edge_faces.FindFromIndex(index)
        if faces.Extent() != 2:
            continue
        edge = TopoDS.Edge(edge_faces.FindKey(index))
        if (
            BRep_Tool.Continuity_s(
                edge,
                TopoDS.Face(faces.First()),
                TopoDS.Face(faces.Last()),
            )
            == GeomAbs_C0
        ):
            result.append(Edge(edge))
    return result


def _mark_geometrically_tangent_edges(part: Part) -> None:
    edge_faces = IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapesAndAncestors_s(
        part.wrapped,
        TopAbs_EDGE,
        TopAbs_FACE,
        edge_faces,
    )
    builder = BRep_Builder()
    for edge in _c0_edges(part):
        faces = edge_faces.FindFromKey(edge.wrapped)
        if faces.Extent() != 2:
            continue
        first = Face(TopoDS.Face(faces.First()))
        second = Face(TopoDS.Face(faces.Last()))
        point = edge.center()
        if abs(abs(first.normal_at(point).dot(second.normal_at(point))) - 1) < 1e-6:
            builder.Continuity(
                edge.wrapped,
                first.wrapped,
                second.wrapped,
                GeomAbs_G1,
            )


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


def _anchor_profile(d: AdjustableStopDimensions) -> Face:
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


def _analytic_anchor(d: AdjustableStopDimensions, centre_y: float | None = None) -> Part:
    centre_y = d.anchor_y if centre_y is None else centre_y
    anchor = extrude(_anchor_profile(d), amount=d.anchor_depth + 0.5, dir=(0, 0, 1))
    anchor = anchor.moved(Location((0, centre_y, -d.anchor_depth)))
    bottom = [edge for edge in anchor.edges() if abs(edge.center().Z + d.anchor_depth) < 1e-5]
    return fillet(bottom, radius=d.anchor_end_radius)


def _rack_tooth(
    side: int,
    station: float,
    d: AdjustableStopDimensions = DIMENSIONS,
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
    d: AdjustableStopDimensions = DIMENSIONS,
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
        d.pusher_bed_bottom,
        d.tooth_height - d.pusher_bed_bottom,
    )


def _guide_wall(
    side: int,
    d: AdjustableStopDimensions = DIMENSIONS,
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


@lru_cache(maxsize=4)
def _outer_guide_pad(
    side: int,
    d: AdjustableStopDimensions = DIMENSIONS,
) -> Part:
    x0, x1 = sorted((side * d.outer_guide_inner, side * d.outer_guide_tool_outer))
    pad = _block(
        x0,
        x1,
        d.base_start,
        d.outer_guide_tool_end,
        d.floor,
        d.guide_top,
    )
    inner_overlap_x = side * (d.outer_guide_inner + DETAIL_EDGE_RADIUS_MM)
    overlap_x0, overlap_x1 = sorted((inner_overlap_x, side * d.outer_guide_tool_outer))
    pad += _block(
        overlap_x0,
        overlap_x1,
        d.outer_guide_start + DETAIL_EDGE_RADIUS_MM,
        d.outer_guide_end - DETAIL_EDGE_RADIUS_MM,
        d.floor - d.finger_guide_clearance,
        d.floor + d.finger_guide_clearance,
    )
    return pad


def _outer_guide_floor_relief(
    side: int,
    d: AdjustableStopDimensions = DIMENSIONS,
) -> Part:
    radius = DETAIL_EDGE_RADIUS_MM
    inner = side * d.outer_guide_inner
    solid_centre_x = side * (d.outer_guide_inner + radius)
    passage_x = side * (d.outer_guide_inner - radius)
    x0, x1 = sorted((inner, solid_centre_x))
    relief_end = d.outer_guide_tool_end + radius
    length = relief_end - d.base_start
    corner = _block(
        x0,
        x1,
        d.base_start,
        relief_end,
        d.floor,
        d.floor + radius,
    )
    tangent_quarter = (
        Cylinder(
            radius,
            length,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
        )
        .rotate(Axis.X, -90)
        .moved(Location((solid_centre_x, d.base_start, d.floor + radius)))
    )
    intrusion_x0, intrusion_x1 = sorted((passage_x, inner))
    intrusion = _block(
        intrusion_x0,
        intrusion_x1,
        d.base_start,
        relief_end,
        d.floor,
        d.floor + radius,
    )
    return intrusion + corner.cut(tangent_quarter)


def _unify_same_domain(part: Part) -> Part:
    unifier = ShapeUpgrade_UnifySameDomain(part.wrapped, True, True, False)
    unifier.Build()
    unified_shape = Part(unifier.Shape())
    unified = Part(unified_shape.solids())
    if not unified.is_valid or len(unified.solids()) != 1:
        raise ValueError("same-domain base unification failed")
    return unified


def _mark_outer_guide_tangency(
    part: Part,
    d: AdjustableStopDimensions = DIMENSIONS,
) -> None:
    edge_faces = IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapesAndAncestors_s(
        part.wrapped,
        TopAbs_EDGE,
        TopAbs_FACE,
        edge_faces,
    )
    builder = BRep_Builder()
    marked = 0
    for edge in part.edges():
        bounds = edge.bounding_box()
        if not any(
            _lies_at(edge, "X", side * d.outer_guide_inner) for side in (-1, 1)
        ) or not _lies_at(edge, "Z", d.floor + DETAIL_EDGE_RADIUS_MM):
            continue
        if (
            bounds.min.Y < d.outer_guide_start - EDGE_SELECTION_TOLERANCE_MM
            or bounds.max.Y > d.outer_guide_end + EDGE_SELECTION_TOLERANCE_MM
        ):
            continue
        for index in range(1, edge_faces.Extent() + 1):
            if not edge_faces.FindKey(index).IsSame(edge.wrapped):
                continue
            faces = edge_faces.FindFromIndex(index)
            if faces.Size() != 2:
                raise ValueError("outer-guide tangent edge must have two adjacent faces")
            builder.Continuity(
                TopoDS.Edge(edge_faces.FindKey(index)),
                TopoDS.Face(faces.First()),
                TopoDS.Face(faces.Last()),
                GeomAbs_G1,
            )
            marked += 1
            break
    if marked != 2:
        raise ValueError(f"expected two outer-guide floor tangencies, found {marked}")


def _make_base(
    d: AdjustableStopDimensions = DIMENSIONS,
    *,
    round_edges: bool = True,
    unify_same_domain: bool = True,
) -> Part:
    base = _block(-d.width / 2, d.width / 2, d.base_start, d.base_end, 0, d.floor)
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
        base += _block(x0, x1, d.base_start, d.base_end, d.floor - 0.1, d.rack_height)
        base += _outer_guide_pad(side, d)
    if round_edges:
        perimeter = [
            edge
            for edge in base.edges()
            if edge.geom_type == GeomType.LINE
            and edge.bounding_box().min.Z >= -EDGE_SELECTION_TOLERANCE_MM
            and (
                _lies_at(edge, "X", -d.width / 2)
                or _lies_at(edge, "X", d.width / 2)
                or _lies_at(edge, "Y", d.base_start)
                or _lies_at(edge, "Y", d.base_end)
            )
        ]
        base = _fillet_exact(
            base,
            perimeter,
            radius=FREE_EDGE_RADIUS_MM,
            expected=20,
            feature="fixed-base R2 outer perimeter",
        )
    for side in (-1, 1):
        x0, x1 = sorted((side * d.rack_carrier_inner, side * d.rack_carrier_outer))
        base += _block(
            x0,
            x1,
            d.rack_carrier_start,
            d.rack_carrier_end,
            d.floor - 0.1,
            d.pusher_z + d.tooth_height,
        )
        for station in d.rack_tooth_stations:
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
        outer_guide_edges = []
        for edge in base.edges():
            if edge.geom_type != GeomType.LINE:
                continue
            bounds = edge.bounding_box()
            for side in (-1, 1):
                inner = side * d.outer_guide_inner
                root = side * d.rack_wall_inner
                if (
                    _lies_at(edge, "X", inner)
                    and (_lies_at(edge, "Z", d.floor) or _lies_at(edge, "Z", d.guide_top))
                    and bounds.min.Y >= d.outer_guide_start - EDGE_SELECTION_TOLERANCE_MM
                    and bounds.max.Y <= d.outer_guide_tool_end + EDGE_SELECTION_TOLERANCE_MM
                    or _lies_at(edge, "Y", d.outer_guide_tool_end)
                    and _lies_at(edge, "X", inner)
                    or _lies_at(edge, "Y", d.outer_guide_tool_end)
                    and (_lies_at(edge, "Z", d.floor) or _lies_at(edge, "Z", d.guide_top))
                    and bounds.min.X >= min(inner, root) - EDGE_SELECTION_TOLERANCE_MM
                    and bounds.max.X <= max(inner, root) + EDGE_SELECTION_TOLERANCE_MM
                    or _lies_at(edge, "Y", d.outer_guide_tool_end)
                    and _lies_at(edge, "X", root)
                ):
                    outer_guide_edges.append(edge)
                    break
        base = _fillet_exact(
            base,
            outer_guide_edges,
            radius=DETAIL_EDGE_RADIUS_MM,
            expected=12,
            feature="merged outer-guide R1 exposed edge",
        )
        for side in (-1, 1):
            base = base.cut(_outer_guide_floor_relief(side, d))
    if unify_same_domain:
        base = _unify_same_domain(base)
    if round_edges:
        _mark_outer_guide_tangency(base, d)
    base.label = "fixed_base_analytic_anchor_candidate"
    base.color = Color(0.19, 0.39, 0.50)
    return base


def _finger_displacement(y: float, d: AdjustableStopDimensions) -> float:
    t = max(
        0.0,
        min(
            1.0,
            (y - d.reinforcement_length) / (d.release_transition_end - d.reinforcement_length),
        ),
    )
    return d.release_stroke * t * t * (3 - 2 * t)


def _outer_finger(side: int, released: bool, d: AdjustableStopDimensions) -> Part:
    centre = side * d.outer_centre
    if not released:
        return _block(
            centre - d.outer_width / 2,
            centre + d.outer_width / 2,
            -0.5,
            d.finger_length,
            d.finger_bottom,
            d.finger_top,
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
    return Pos(0, 0, d.finger_bottom) * extrude(
        Face(Wire(edges)),
        amount=d.finger_height,
        dir=(0, 0, 1),
    )


def _squeeze_pad(
    centre_x: float,
    d: AdjustableStopDimensions = DIMENSIONS,
    *,
    width: float | None = None,
    round_edges: bool = True,
) -> Part:
    width = d.pad_outer - d.pad_inner if width is None else width
    pad = _block(
        centre_x - width / 2,
        centre_x + width / 2,
        d.pad_start,
        d.pad_start + d.pad_length,
        0,
        d.pad_height,
    )
    return (
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


def _make_pusher_body(
    released: bool,
    d: AdjustableStopDimensions = DIMENSIONS,
    *,
    round_edges: bool = True,
) -> Part:
    pusher = _block(
        -d.width / 2,
        d.width / 2,
        -d.wall_thickness,
        0,
        d.pusher_bed_bottom,
        d.wall_height,
    )
    centre_finger = _block(
        -d.centre_width / 2,
        d.centre_width / 2,
        -0.5,
        d.finger_length,
        d.finger_bottom,
        d.finger_top,
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
            d.pusher_bed_bottom,
            d.tooth_height,
        )
        if round_edges:
            carrier_inner_x = side * (d.outer_centre - d.outer_width / 2) - shift
            carrier = _fillet_exact(
                carrier,
                [
                    edge
                    for edge in carrier.edges()
                    if edge.geom_type == GeomType.LINE
                    and _lies_at(edge, "X", carrier_inner_x)
                    and _lies_at(edge, "Z", d.tooth_height)
                    and _span(edge, "Y") > 1
                ],
                radius=DETAIL_EDGE_RADIUS_MM,
                expected=1,
                feature="moving-carrier inner-top R1 clip fit",
            )
        pusher += carrier
        for station in MOVING_TOOTH_STATIONS_MM:
            pusher += _moving_tooth(side, station, shift, d)
        pusher += _squeeze_pad(
            side * d.outer_centre - shift,
            d,
            round_edges=round_edges,
        )
    pusher += _squeeze_pad(
        0,
        d,
        width=d.centre_width,
        round_edges=round_edges,
    )
    for x, width in (
        (-d.outer_centre, d.outer_width),
        (0, d.centre_width),
        (d.outer_centre, d.outer_width),
    ):
        profile = Plane.YZ.offset(x - width / 2) * Polygon(
            (-0.5, d.pusher_bed_bottom),
            (d.reinforcement_length, d.pusher_bed_bottom),
            (d.reinforcement_length, d.reinforcement_finger_join_top),
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
                or _lies_at(edge, "Z", d.finger_top)
                and d.reinforcement_length - 1 < edge.center().Y < d.reinforcement_length
                and _span(edge, "X") > 1
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
    spec: AdjustableStopSpec,
    d: AdjustableStopDimensions = DIMENSIONS,
    *,
    round_edges: bool = True,
) -> Part:
    pusher = _make_pusher_body(
        spec.released_illustration,
        d,
        round_edges=round_edges,
    ).moved(Location((0, d.pusher_y_offset - spec.extension_mm, d.pusher_z)))
    node = make_bidirectional_panel_connector()
    front_y = -d.wall_thickness + d.pusher_y_offset - spec.extension_mm
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
    d: AdjustableStopDimensions = DIMENSIONS,
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


def make_adjustable_stop_parts(
    spec: AdjustableStopSpec = AdjustableStopSpec(),
) -> tuple[Part, Part, Part]:
    """Build the three complete native-BREP manufactured parts."""

    if not isinstance(spec, AdjustableStopSpec):
        raise ValueError("spec must be an AdjustableStopSpec")
    base = _make_base()
    pusher = _make_pusher(spec)
    keeper = _make_keeper()
    for part in (base, pusher, keeper):
        if not part.is_valid or len(part.solids()) != 1 or part.volume <= 0:
            raise ValueError(f"expected one valid connected solid: {part.label}")
    return base, pusher, keeper


def make_adjustable_stop_prong_lock_clip(
    spec: AdjustableStopSpec = AdjustableStopSpec(),
    clip_dimensions: AdjustableStopProngLockClipDimensions = PRONG_LOCK_CLIP_DIMENSIONS,
) -> Part:
    """Build the removable three-tab lock clip; defaults are undimpled v10b #1."""

    if not isinstance(spec, AdjustableStopSpec):
        raise ValueError("spec must be an AdjustableStopSpec")
    if spec.released_illustration:
        raise ValueError("the prong lock clip seats only on an unsqueezed moving wall")
    if not isinstance(clip_dimensions, AdjustableStopProngLockClipDimensions):
        raise ValueError("clip_dimensions must be an AdjustableStopProngLockClipDimensions")

    d = DIMENSIONS
    c = clip_dimensions
    bar_start = c.bar_start - d.prong_shortening
    tab_start = d.pad_start
    tab_end = d.pad_start + d.pad_length
    bar_end = tab_start - c.collar_y_clearance
    collar_rear_start = tab_end + c.collar_rear_y_clearance
    collar_rear_end = collar_rear_start + c.collar_rear_bar_thickness
    centre_prong_outer = d.centre_width / 2
    nominal_gap = d.pad_inner - centre_prong_outer
    tine_width = nominal_gap - 2 * c.leg_lateral_clearance
    tine_length = bar_end - bar_start
    tine_bottom = d.floor + c.floor_clearance - d.pusher_z
    tine_depth = d.tooth_height + c.top_clearance - tine_bottom
    if min(tine_width, tine_length, tine_depth, c.bar_thickness) <= 0:
        raise ValueError("prong lock clip dimensions must leave positive material")
    if not 0 < c.round_radius < min(tine_width, c.bar_thickness) / 2:
        raise ValueError("prong lock clip radius does not fit its smallest section")
    if c.lift_wing_overhang <= 0:
        raise ValueError("prong lock clip lift-wing overhang must be positive")
    if not (
        d.moving_carrier_start < bar_start < bar_end < tab_start and collar_rear_start > tab_end
    ):
        raise ValueError("prong lock clip collar must bracket the centre squeeze tab")
    if not (
        -0.5 < c.leg_lateral_clearance <= c.side_clearance
        and 0 < c.side_clearance < nominal_gap / 2
    ):
        raise ValueError("prong lock clip lateral and root clearances do not fit the tab gaps")
    if not (
        0 < c.collar_y_clearance < c.round_radius
        and 0 < c.collar_rear_y_clearance < c.round_radius
        and c.collar_rear_bar_thickness > 2 * c.round_radius
        and 0 < c.leg_root_relief_height < d.finger_top - tine_bottom
    ):
        raise ValueError("prong lock clip collar dimensions do not fit the squeeze tabs")
    if not 0 < c.loop_depth <= bar_end - bar_start:
        raise ValueError("prong lock clip loop must stay within the carrier band")
    if (
        c.loop_outer_width <= c.loop_inner_width
        or c.loop_outer_height <= c.loop_inner_height
        or not 0 < c.loop_bar_overlap < c.bar_thickness
        or c.loop_bottom_band <= 0
        or c.loop_bottom_band + c.loop_inner_height >= c.loop_outer_height
    ):
        raise ValueError("prong lock clip loop must leave positive band sections")
    if not (
        c.loop_inner_edge_radius < c.loop_depth / 2
        and c.loop_outer_corner_radius < min(c.loop_outer_width, c.loop_outer_height) / 2
        and c.loop_inner_corner_radius < min(c.loop_inner_width, c.loop_inner_height) / 2
    ):
        raise ValueError("prong lock clip loop radii do not fit")

    bar_bottom = d.tooth_height + c.top_clearance
    bar_outer = d.pad_outer + c.lift_wing_overhang
    if bar_outer > d.moving_tip - c.side_clearance:
        raise ValueError("prong lock clip lift wings must leave the tooth tips exposed")
    clip = _block(
        -bar_outer,
        bar_outer,
        bar_start,
        bar_end,
        bar_bottom,
        bar_bottom + c.bar_thickness,
    )
    clip += _block(
        -d.pad_inner + c.side_clearance,
        d.pad_inner - c.side_clearance,
        bar_start,
        bar_end,
        tine_bottom,
        bar_bottom + c.bar_thickness,
    )
    clip -= _block(
        -centre_prong_outer - c.side_clearance,
        centre_prong_outer + c.side_clearance,
        bar_start - 0.1,
        bar_end + 0.1,
        tine_bottom - 0.1,
        d.finger_top + c.top_clearance,
    )
    locating_transition_end = d.finger_top - c.leg_root_relief_height
    locating_transition_start = locating_transition_end - 2 * c.round_radius
    for side in (-1, 1):
        root_x0, root_x1 = sorted(
            (
                side * (centre_prong_outer + c.side_clearance),
                side * (d.pad_inner - c.side_clearance),
            )
        )
        clip += _block(
            root_x0,
            root_x1,
            bar_end - 0.1,
            collar_rear_start + 0.1,
            tine_bottom,
            bar_bottom + c.bar_thickness,
        )
        clip += _clip_locating_wall(
            side,
            y0=bar_start,
            y1=collar_rear_start + 0.1,
            z0=tine_bottom,
            transition_start=locating_transition_start,
            transition_end=locating_transition_end,
            centre_outer=centre_prong_outer,
            gap_outer=d.pad_inner,
            lateral_clearance=c.leg_lateral_clearance,
            root_clearance=c.side_clearance,
        )
    clip += _clip_rear_crossbar(
        y0=collar_rear_start,
        y1=collar_rear_end,
        z0=tine_bottom,
        z1=bar_bottom + c.bar_thickness,
        transition_start=locating_transition_start,
        transition_end=locating_transition_end,
        gap_outer=d.pad_inner,
        lateral_clearance=c.leg_lateral_clearance,
        root_clearance=c.side_clearance,
    )
    clip = clip.clean()
    loop_outer_bottom = bar_bottom + c.bar_thickness - c.loop_bar_overlap
    loop_inner_bottom = loop_outer_bottom + c.loop_bottom_band
    loop_back = bar_start + c.loop_depth
    loop_outer = Pos(0, 0, loop_outer_bottom + c.loop_outer_height / 2) * (
        Plane.XZ.offset(-loop_back)
        * RectangleRounded(
            c.loop_outer_width,
            c.loop_outer_height,
            c.loop_outer_corner_radius,
        )
    )
    loop_inner = Pos(0, 0, loop_inner_bottom + c.loop_inner_height / 2) * (
        Plane.XZ.offset(-loop_back)
        * RectangleRounded(
            c.loop_inner_width,
            c.loop_inner_height,
            c.loop_inner_corner_radius,
        )
    )
    clip += extrude(loop_outer - loop_inner, amount=c.loop_depth)
    clip = clip.clean()
    inner_half_width = c.loop_inner_width / 2
    inner_top = loop_inner_bottom + c.loop_inner_height
    inner_edges = [
        edge
        for edge in _c0_edges(clip)
        if edge.bounding_box().min.X >= inner_half_width * -1 - EDGE_SELECTION_TOLERANCE_MM
        and edge.bounding_box().max.X <= inner_half_width + EDGE_SELECTION_TOLERANCE_MM
        and edge.bounding_box().min.Z >= loop_inner_bottom - EDGE_SELECTION_TOLERANCE_MM
        and edge.bounding_box().max.Z <= inner_top + EDGE_SELECTION_TOLERANCE_MM
        and (_lies_at(edge, "Y", bar_start) or _lies_at(edge, "Y", loop_back))
    ]
    clip = _fillet_exact(
        clip,
        inner_edges,
        radius=c.loop_inner_edge_radius,
        expected=16,
        feature="prong-lock clip R3 finger opening",
    )
    exterior_edges = _c0_edges(clip)
    clip = _fillet_exact(
        clip,
        exterior_edges,
        radius=c.round_radius,
        expected=94,
        feature="prong-lock clip final-body R1 exterior",
    )
    _mark_geometrically_tangent_edges(clip)
    if _c0_edges(clip):
        raise ValueError("prong lock clip retains an unrounded exterior edge")
    if not clip.is_valid or len(clip.solids()) != 1 or clip.volume <= 0:
        raise ValueError("prong lock clip must be one valid positive-volume solid")

    clip = clip.moved(Location((0, d.pusher_y_offset - spec.extension_mm, d.pusher_z)))
    clip.label = "PRONG_LOCK_CLIP"
    clip.color = Color(0.58, 0.24, 0.72)
    return clip
