"""Rear-panel contour construction from the selected outline."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import InitVar, dataclass, field, replace
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from math import atan, ceil, isfinite, sqrt, tan

import numpy as np
from build123d import (
    Axis,
    Edge,
    Face,
    Location,
    Part,
    Plane,
    Solid,
    Vector,
    Wire,
    section,
)
from OCP.BRepTools import BRepTools
from scipy.interpolate import CubicHermiteSpline, PchipInterpolator

from cargo_grid.accessories import (
    RAMP_CARRIER_RUN_MM,
    RAMP_FREE_EDGE_RADIUS_MM,
    RAMP_MINIMUM_FLAT_SHELF_MM,
    RAMP_SHELF_RADIUS_MM,
)
from cargo_grid.interfaces import socket_entry_tool, tile_join_tool, x_profile
from cargo_grid.jobs import Design
from cargo_grid.parameters import Interface, Tile, interface_parameters, positive
from cargo_grid.tiles import hole_placements

DEFAULT_SE_ALONG_EDGE_SHIFT_MM = 0.0


DEFAULT_CENTRE_DEPTH_MM = 365.0


DEFAULT_PEN_OFFSET_MM = 5.0


DEFAULT_EAST_EDGE_X_MM = 602.5


TRACED_NORTH_CORNER_RADIUS_MM = 11.4


RAW_SE_TAPER_STATIONS_U_S_MM = (
    (0.0, 224.0),
    (1.0, 228.4),
    (2.0, 236.8),
    (5.0, 249.4),
    (10.0, 262.6),
    (20.0, 286.3),
    (30.0, 300.4),
    (40.0, 311.7),
    (60.0, 327.5),
    (80.0, 337.6),
    (100.0, 342.8),
    (120.0, 346.7),
    (140.0, 349.6),
    (160.0, 352.1),
)


RAW_NORTH_EDGE_BEND_STATIONS_U_S_MM = (
    (100.0, 0.0),
    (80.0, 1.4),
    (60.0, 2.9),
    (40.0, 4.7),
    (20.0, 6.2),
)


NORTH_EDGE_CORNER_SLOPE_DS_DU = -0.075


CORRECTED_SE_SAMPLE_U_MM = (
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    30.0,
    40.0,
    60.0,
    80.0,
    100.0,
    120.0,
    140.0,
    155.571,
)


CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU = 0.115


TILE_MODULES = (4, 4, 2, 4, 4)


FIELD_WIDTH_MM = 1080.0


FIELD_DEPTH_MM = 240.0


FIELD_X_MIN_MM = -540.0


FIELD_Y_MIN_MM = 650.0


NORTH_LIMIT_MM = 620.0


REAR_LIMIT_MM = 950.0


SIDE_SPLIT_Y_MM = 770.0


CORNER_TIP_REGULARIZATION_MM = 0.1


SIDE_SOCKET_MINIMUM_WEB_MM = 1.5


PERIMETER_HOLE_MINIMUM_WEB_MM = 1.5


def _merged_taper_stations(
    pen_offset_mm: float,
    se_along_edge_shift_mm: float,
) -> tuple[tuple[float, float], ...]:
    curve = _corrected_taper_curve(pen_offset_mm, se_along_edge_shift_mm)
    return (
        (0.0, RAW_SE_TAPER_STATIONS_U_S_MM[0][1] - pen_offset_mm),
        *((u, float(curve(u))) for u in CORRECTED_SE_SAMPLE_U_MM),
    )


@lru_cache(maxsize=16)
def _corrected_taper_curve(
    pen_offset_mm: float,
    se_along_edge_shift_mm: float,
) -> PchipInterpolator:
    raw = np.asarray(RAW_SE_TAPER_STATIONS_U_S_MM)
    curve = PchipInterpolator(raw[:, 0], raw[:, 1] + se_along_edge_shift_mm)
    u = np.linspace(raw[0, 0], raw[-1, 0], 16001)
    s = curve(u)
    derivative = curve(u, 1)
    length = np.sqrt(1 + derivative**2)
    corrected_u = u + pen_offset_mm * derivative / length - pen_offset_mm
    corrected_s = s - pen_offset_mm / length - pen_offset_mm
    order = np.argsort(corrected_u)
    return PchipInterpolator(corrected_u[order], corrected_s[order])


def _crown_coefficients(
    centre_depth_mm: float,
    se_along_edge_shift_mm: float,
    outline_inset_mm: float = 0.0,
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM,
    east_edge_x_mm: float = DEFAULT_EAST_EDGE_X_MM,
) -> tuple[float, float, float, float]:
    junction_u, junction_s = _merged_taper_stations(
        pen_offset_mm,
        se_along_edge_shift_mm,
    )[-1]
    tangent = CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU
    distance = east_edge_x_mm - outline_inset_mm - junction_u
    depth = centre_depth_mm - junction_s
    minimum_depth = tangent * distance / 4
    maximum_depth = tangent * distance / 2
    if not minimum_depth - 1e-6 <= depth <= maximum_depth + 1e-6:
        raise ValueError(
            "centre depth must keep crown coefficients nonnegative: "
            f"{junction_s + minimum_depth:.6f} <= centre_depth_mm <= "
            f"{junction_s + maximum_depth:.6f}"
        )
    depth = min(max(depth, minimum_depth), maximum_depth)
    scaled_quadratic = 2 * depth - tangent * distance / 2
    scaled_quartic = tangent * distance / 2 - depth
    a = scaled_quadratic / distance**2
    b = scaled_quartic / distance**4
    return a, b, junction_s, tangent


def _south_crown_y_slope(
    x_mm: float,
    centre_depth_mm: float,
    se_along_edge_shift_mm: float,
    outline_inset_mm: float = 0.0,
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM,
    east_edge_x_mm: float = DEFAULT_EAST_EDGE_X_MM,
) -> tuple[float, float]:
    junction_u = _merged_taper_stations(
        pen_offset_mm,
        se_along_edge_shift_mm,
    )[-1][0]
    distance = east_edge_x_mm - outline_inset_mm - junction_u
    t = abs(x_mm)
    if t > distance + 1e-7:
        raise ValueError("south crown is defined only inside the merged taper junction")
    a, b, _, _ = _crown_coefficients(
        centre_depth_mm,
        se_along_edge_shift_mm,
        outline_inset_mm,
        pen_offset_mm,
        east_edge_x_mm,
    )
    s = centre_depth_mm - a * t**2 - b * t**4
    ds_dt = -2 * a * t - 4 * b * t**3
    slope_dy_dx = ds_dt if x_mm >= 0 else -ds_dt
    return 620 + s, slope_dy_dx


DEFAULT_CORRECTED_JUNCTION_U_MM = _merged_taper_stations(
    DEFAULT_PEN_OFFSET_MM,
    DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
)[-1][0]


DEFAULT_CORRECTED_JUNCTION_X_MM = DEFAULT_EAST_EDGE_X_MM - DEFAULT_CORRECTED_JUNCTION_U_MM


SOUTH_CONTOUR_CAD_SAMPLES = tuple(
    (
        float(x),
        *_south_crown_y_slope(
            float(x),
            DEFAULT_CENTRE_DEPTH_MM,
            DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
            0.0,
            DEFAULT_PEN_OFFSET_MM,
            DEFAULT_EAST_EDGE_X_MM,
        ),
    )
    for x in sorted(
        {
            *range(
                -int(DEFAULT_CORRECTED_JUNCTION_X_MM),
                int(DEFAULT_CORRECTED_JUNCTION_X_MM) + 1,
                5,
            ),
            -DEFAULT_CORRECTED_JUNCTION_X_MM,
            DEFAULT_CORRECTED_JUNCTION_X_MM,
        }
    )
)


@dataclass(frozen=True)
class ContourOutline:
    """A closed XY boundary in vehicle millimetres: X right, Y toward the tailgate."""

    curve: InitVar[Edge | Wire]
    digest: str = field(init=False)
    bounds_mm: tuple[float, float, float, float] = field(init=False)
    _wire: Wire = field(init=False, repr=False, compare=False)
    _face: Face = field(init=False, repr=False, compare=False)

    def __post_init__(self, curve: Edge | Wire) -> None:
        if not isinstance(curve, (Edge, Wire)):
            raise ValueError("contour outline must be a closed Edge or Wire")
        wire = deepcopy(curve if isinstance(curve, Wire) else Wire([curve]))
        if not wire.is_valid or not wire.is_closed:
            raise ValueError("contour outline must be one valid closed wire")
        face = Face(wire)
        normal = face.normal_at()
        if (
            not face.is_valid
            or face.area <= 0
            or abs(normal.X) > 1e-10
            or abs(normal.Y) > 1e-10
            or abs(face.center().Z) > 1e-7
        ):
            raise ValueError("contour outline must enclose a positive area in the Z=0 XY plane")
        if face.center().Z != 0:
            wire = wire.moved(Location((0, 0, -face.center().Z)))
            face = Face(wire)
        stream = BytesIO()
        BRepTools.Write_s(wire.wrapped, stream)
        bounds = wire.bounding_box()
        object.__setattr__(self, "_wire", wire)
        object.__setattr__(self, "_face", face)
        object.__setattr__(self, "digest", sha256(stream.getvalue()).hexdigest())
        object.__setattr__(
            self, "bounds_mm", (bounds.min.X, bounds.min.Y, bounds.max.X, bounds.max.Y)
        )

    @classmethod
    def from_stations(cls, stations_xy_mm: Sequence[tuple[float, float]]) -> "ContourOutline":
        """Interpolate an ordered loop of stations with one smooth periodic curve."""
        points = tuple((float(x), float(y), 0.0) for x, y in stations_xy_mm)
        if points and points[0] == points[-1]:
            points = points[:-1]
        if (
            len(points) < 4
            or len(set(points)) != len(points)
            or not all(isfinite(value) for point in points for value in point)
        ):
            raise ValueError("outline stations need at least four distinct finite XY positions")
        return cls(Edge.make_spline(points, periodic=True))

    def _limits(self, coordinate: float, *, vertical: bool) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bounds_mm
        ends = (
            ((coordinate, y0 - 1, 0), (coordinate, y1 + 1, 0))
            if vertical
            else ((x0 - 1, coordinate, 0), (x1 + 1, coordinate, 0))
        )
        common = self._face.intersect(Edge.make_line(*ends))
        vertices = common.vertices() if common is not None else []
        if len(vertices) != 2:
            raise ValueError("outline must cross each construction ray in one uninterrupted span")
        values = [vertex.Y if vertical else vertex.X for vertex in vertices]
        return min(values), max(values)

    def north_y(self, x_mm: float) -> float:
        return self._limits(x_mm, vertical=True)[0]

    def south_y(self, x_mm: float) -> float:
        return self._limits(x_mm, vertical=True)[1]

    def side_x(self, side: str, y_mm: float) -> float:
        if side not in ("west", "east"):
            raise ValueError("outline side must be west or east")
        return self._limits(y_mm, vertical=False)[0 if side == "west" else 1]

    def south_slope(self, x_mm: float) -> float:
        point = Vector(x_mm, self.south_y(x_mm), 0)
        edge = min(self._wire.edges(), key=lambda candidate: candidate.distance_to(point))
        tangent = edge.tangent_at(point)
        if abs(tangent.X) < 1e-12:
            raise ValueError(
                "south outline must have a finite Y-by-X slope at construction stations"
            )
        return tangent.Y / tangent.X


@dataclass(frozen=True)
class ContourParameters:
    """Panel dimensions in the measured vehicle coordinate frame."""

    outline_inset_mm: float = 0.0
    field_x_min_mm: float = FIELD_X_MIN_MM
    field_y_min_mm: float = FIELD_Y_MIN_MM
    north_limit_y_mm: float = NORTH_LIMIT_MM
    rear_limit_y_mm: float = REAR_LIMIT_MM
    side_split_y_mm: float = SIDE_SPLIT_Y_MM
    traced_north_corner_radius_mm: float = TRACED_NORTH_CORNER_RADIUS_MM
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM
    east_edge_x_mm: float = DEFAULT_EAST_EDGE_X_MM
    se_along_edge_shift_mm: float = DEFAULT_SE_ALONG_EDGE_SHIFT_MM
    centre_depth_mm: float = DEFAULT_CENTRE_DEPTH_MM
    interface: Interface = Interface()
    outline: ContourOutline | None = None

    def __post_init__(self) -> None:
        positive("outline inset", self.outline_inset_mm, zero=True)
        positive("traced north corner radius", self.traced_north_corner_radius_mm)
        positive("pen offset", self.pen_offset_mm, zero=True)
        positive("east edge x", self.east_edge_x_mm)
        if self.traced_north_corner_radius_mm <= self.pen_offset_mm:
            raise ValueError("pen offset must be smaller than traced north corner radius")
        if replace(self.interface, solid_bottom_mm=0.0) != Interface():
            raise ValueError(
                "Zeekr contour geometry requires the standard 60x13 original interface"
            )
        if self.field_x_min_mm != -FIELD_WIDTH_MM / 2:
            raise ValueError("Zeekr contour geometry field must remain centred at x=0")
        if self.field_y_min_mm - self.north_limit_y_mm != 30:
            raise ValueError(
                "Zeekr contour geometry requires the selected 30 mm north finishing band"
            )
        if not (self.field_y_min_mm < self.side_split_y_mm < self.field_y_min_mm + FIELD_DEPTH_MM):
            raise ValueError("side split must lie inside the four-row tile mating run")
        if self.rear_limit_y_mm <= self.field_y_min_mm + FIELD_DEPTH_MM:
            raise ValueError("rear limit must lie beyond the tile field")
        if self.outline is None:
            _crown_coefficients(
                self.centre_depth_mm,
                self.se_along_edge_shift_mm,
                self.outline_inset_mm,
                self.pen_offset_mm,
                self.east_edge_x_mm,
            )
        elif not isinstance(self.outline, ContourOutline):
            raise ValueError("outline must be a ContourOutline")
        elif (
            self.outline_inset_mm,
            self.traced_north_corner_radius_mm,
            self.pen_offset_mm,
            self.east_edge_x_mm,
            self.se_along_edge_shift_mm,
            self.centre_depth_mm,
        ) != (
            0.0,
            TRACED_NORTH_CORNER_RADIUS_MM,
            DEFAULT_PEN_OFFSET_MM,
            DEFAULT_EAST_EDGE_X_MM,
            DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
            DEFAULT_CENTRE_DEPTH_MM,
        ):
            raise ValueError("a supplied outline cannot also use preset outline adjustments")

    @property
    def north_corner_radius_mm(self) -> float:
        return self.traced_north_corner_radius_mm - self.pen_offset_mm

    @property
    def field_y_max_mm(self) -> float:
        return self.field_y_min_mm + FIELD_DEPTH_MM

    def assembly_y_mm(self, measured_vehicle_y_mm: float) -> float:
        """North-positive assembly coordinate from raw tailgate-positive measurements."""
        return self.rear_limit_y_mm - measured_vehicle_y_mm

    @property
    def field_assembly_y_min_mm(self) -> float:
        return self.assembly_y_mm(self.field_y_max_mm)

    @property
    def field_assembly_y_max_mm(self) -> float:
        return self.assembly_y_mm(self.field_y_min_mm)


@lru_cache(maxsize=16)
def _taper_interpolators(
    pen_offset_mm: float,
    se_along_edge_shift_mm: float,
) -> tuple[PchipInterpolator, CubicHermiteSpline]:
    stations = _merged_taper_stations(pen_offset_mm, se_along_edge_shift_mm)
    u = np.asarray([station[0] for station in stations])
    s = np.asarray([station[1] for station in stations])
    inverse_seed = PchipInterpolator(s, u)
    inverse_slopes = inverse_seed(s, 1)
    inverse_slopes[0] = 0
    inverse_slopes[-1] = 1 / CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU
    inverse = CubicHermiteSpline(s, u, inverse_slopes)
    dense_s = np.linspace(s[0], s[-1], 8001)
    dense_u = inverse(dense_s)
    if np.any(np.diff(dense_u) <= 0):
        raise ValueError("regularized corrected taper is not strictly monotone")
    return PchipInterpolator(dense_u, dense_s), inverse


@lru_cache(maxsize=16)
def _corrected_north_edge_curve(pen_offset_mm: float) -> CubicHermiteSpline:
    corrected_u, corrected_s = _corrected_north_edge_station_arrays(pen_offset_mm)
    slopes = PchipInterpolator(corrected_u, corrected_s)(corrected_u, 1)
    slopes[0] = NORTH_EDGE_CORNER_SLOPE_DS_DU
    slopes[-1] = 0
    return CubicHermiteSpline(corrected_u, corrected_s, slopes)


def _corrected_north_edge_station_arrays(
    pen_offset_mm: float,
) -> tuple[np.ndarray, np.ndarray]:
    raw = np.asarray(sorted(RAW_NORTH_EDGE_BEND_STATIONS_U_S_MM), dtype=float)
    u = raw[:, 0]
    s = raw[:, 1]
    derivative = np.gradient(s, u)
    derivative[0] = NORTH_EDGE_CORNER_SLOPE_DS_DU
    length = np.sqrt(1 + derivative**2)
    corrected_u = u - pen_offset_mm * derivative / length - pen_offset_mm
    corrected_s = s + pen_offset_mm / length - pen_offset_mm
    corrected_s[-1] = 0
    return corrected_u, corrected_s


def _north_edge_s_slope(
    u_mm: float,
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM,
) -> tuple[float, float]:
    curve = _corrected_north_edge_curve(pen_offset_mm)
    if u_mm <= curve.x[0]:
        return float(curve(curve.x[0])), float(curve(curve.x[0], 1))
    if u_mm >= curve.x[-1]:
        return 0.0, 0.0
    return float(curve(u_mm)), float(curve(u_mm, 1))


def _north_corner_geometry(
    parameters: ContourParameters,
) -> dict[str, tuple[float, float] | float]:
    radius = parameters.north_corner_radius_mm
    straight_x = parameters.east_edge_x_mm - parameters.outline_inset_mm
    center_x = straight_x - radius
    corrected = _corrected_north_edge_curve(parameters.pen_offset_mm)
    line_u = float(corrected.x[0])
    line_s = float(corrected(line_u))
    line_slope_ds_du = float(corrected(line_u, 1))
    line_slope = -line_slope_ds_du
    line_intercept = 620 + line_s + line_slope_ds_du * (straight_x - line_u)
    center_y = radius * sqrt(1 + line_slope**2) + line_slope * center_x + line_intercept
    signed = -line_slope * center_x + center_y - line_intercept
    tangent_x = center_x + line_slope * signed / (1 + line_slope**2)
    tangent_y = center_y - signed / (1 + line_slope**2)
    tangent_u = straight_x - tangent_x
    return {
        "center": (center_x, center_y),
        "north_tangent": (tangent_x, tangent_y),
        "north_tangent_u": tangent_u,
        "east_tangent": (straight_x, center_y),
    }


def right_wall_x_mm(
    y_mm: float,
    *,
    outline_inset_mm: float = 0.0,
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM,
    east_edge_x_mm: float = DEFAULT_EAST_EDGE_X_MM,
    se_along_edge_shift_mm: float = DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
) -> float:
    """Merged straight wall and inverse monotone taper PCHIP."""
    positive("outline inset", outline_inset_mm, zero=True)
    s = y_mm - 620
    stations = _merged_taper_stations(pen_offset_mm, se_along_edge_shift_mm)
    if s < -1e-7 or s > stations[-1][1] + 1e-7:
        raise ValueError("wall sample y lies outside the merged station range")
    s = min(max(s, 0.0), stations[-1][1])
    u = (
        0.0
        if s <= stations[0][1]
        else float(_taper_interpolators(pen_offset_mm, se_along_edge_shift_mm)[1](s))
    )
    result = east_edge_x_mm - outline_inset_mm - u
    if result <= 0:
        raise ValueError("outline inset removes the measured half-width")
    return result


def right_outline_x_mm(
    y_mm: float,
    *,
    outline_inset_mm: float = 0.0,
    traced_north_corner_radius_mm: float = TRACED_NORTH_CORNER_RADIUS_MM,
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM,
    east_edge_x_mm: float = DEFAULT_EAST_EDGE_X_MM,
    se_along_edge_shift_mm: float = DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
) -> float:
    """Merged right outline including the tangent north plan corner."""
    positive("outline inset", outline_inset_mm, zero=True)
    parameters = ContourParameters(
        outline_inset_mm=outline_inset_mm,
        traced_north_corner_radius_mm=traced_north_corner_radius_mm,
        pen_offset_mm=pen_offset_mm,
        east_edge_x_mm=east_edge_x_mm,
        se_along_edge_shift_mm=se_along_edge_shift_mm,
    )
    geometry = _north_corner_geometry(parameters)
    center_x, center_y = geometry["center"]
    _, north_y = geometry["north_tangent"]
    if north_y <= y_mm <= center_y:
        return center_x + sqrt(
            max(0.0, parameters.north_corner_radius_mm**2 - (y_mm - center_y) ** 2)
        )
    if y_mm < north_y:
        return float(geometry["north_tangent"][0])
    return right_wall_x_mm(
        y_mm,
        outline_inset_mm=outline_inset_mm,
        pen_offset_mm=pen_offset_mm,
        east_edge_x_mm=east_edge_x_mm,
        se_along_edge_shift_mm=se_along_edge_shift_mm,
    )


def tail_y_mm(x_mm: float, parameters: ContourParameters = ContourParameters()) -> float:
    """Merged taper outside the junction and concave quartic crown inside."""
    if parameters.outline is not None:
        return parameters.outline.south_y(x_mm)
    t = abs(x_mm)
    straight_x = parameters.east_edge_x_mm - parameters.outline_inset_mm
    junction_u = _merged_taper_stations(
        parameters.pen_offset_mm,
        parameters.se_along_edge_shift_mm,
    )[-1][0]
    junction_x = straight_x - junction_u
    if t <= junction_x + 1e-7:
        return _south_crown_y_slope(
            x_mm,
            parameters.centre_depth_mm,
            parameters.se_along_edge_shift_mm,
            parameters.outline_inset_mm,
            parameters.pen_offset_mm,
            parameters.east_edge_x_mm,
        )[0]
    if t > straight_x + 1e-7:
        raise ValueError("x lies outside the merged rear outline")
    u = straight_x - t
    s = float(
        _taper_interpolators(
            parameters.pen_offset_mm,
            parameters.se_along_edge_shift_mm,
        )[0](u)
    )
    return 620 + s


def _tail_slope_dy_dx(
    x_mm: float,
    parameters: ContourParameters,
) -> float:
    if parameters.outline is not None:
        return parameters.outline.south_slope(x_mm)
    t = abs(x_mm)
    straight_x = parameters.east_edge_x_mm - parameters.outline_inset_mm
    corrected = _taper_interpolators(
        parameters.pen_offset_mm,
        parameters.se_along_edge_shift_mm,
    )[0]
    junction_u = _merged_taper_stations(
        parameters.pen_offset_mm,
        parameters.se_along_edge_shift_mm,
    )[-1][0]
    junction_x = straight_x - junction_u
    if t <= junction_x + 1e-7:
        return _south_crown_y_slope(
            x_mm,
            parameters.centre_depth_mm,
            parameters.se_along_edge_shift_mm,
            parameters.outline_inset_mm,
            parameters.pen_offset_mm,
            parameters.east_edge_x_mm,
        )[1]
    u = straight_x - t
    ds_du = float(corrected(u, 1))
    return -ds_du if x_mm >= 0 else ds_du


def _profile_face(
    x: float,
    raw_tip: float,
    carrier: float,
    height: float,
    y_offset: float,
) -> Face:
    return Face(
        Wire.make_polygon(
            [
                (x, y_offset, 0),
                (x, y_offset + raw_tip, 0),
                (x, y_offset + carrier, height),
                (x, y_offset, height),
            ],
            close=True,
        )
    )


@lru_cache(maxsize=4096)
def _ramp_raw_tip(
    x: float,
    run: float,
    carrier: float,
    height: float,
    y_offset: float,
    nose_radius: float,
) -> float:
    def nose_extent(raw_tip: float) -> float:
        face = _profile_face(x, raw_tip, carrier, height, y_offset)
        nose = max(face.vertices(), key=lambda vertex: vertex.Y)
        return face.fillet_2d(nose_radius, [nose]).bounding_box().max.Y - y_offset

    low, high = run, run + 4 * height
    while nose_extent(high) < run:
        high += max(10, height)
    for _ in range(48):
        middle = (low + high) / 2
        if nose_extent(middle) < run:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _ramp_profile_wire(
    x: float,
    run: float,
    interface: Interface,
    *,
    y_offset: float = 0,
) -> Wire:
    """Rounded filled-ramp section ending at an exact finished run."""
    positive("ramp run", run)
    height = interface.body_height
    carrier = _ramp_carrier_run_mm(run)
    nose_radius = min(RAMP_FREE_EDGE_RADIUS_MM, max(0.02, run * 0.15))
    flat_shelf = min(RAMP_MINIMUM_FLAT_SHELF_MM, max(0.02, carrier * 0.2))
    # Keep coordinates in the scalar cache key; construct a fresh wire on every call.
    raw_tip = _ramp_raw_tip(x, run, carrier, height, y_offset, nose_radius)
    face = _profile_face(x, raw_tip, carrier, height, y_offset)
    angle = atan(height / (raw_tip - carrier))
    shelf_radius = min(
        RAMP_SHELF_RADIUS_MM,
        (carrier - flat_shelf) / tan(angle / 2),
    )
    shelf = [
        vertex
        for vertex in face.vertices()
        if abs(vertex.Y - (y_offset + carrier)) < 1e-7 and abs(vertex.Z - height) < 1e-7
    ]
    face = face.fillet_2d(shelf_radius, shelf)
    face = face.fillet_2d(
        nose_radius,
        [max(face.vertices(), key=lambda vertex: vertex.Y)],
    )
    return face.outer_wire()


def _variable_ramp(
    x_runs: list[tuple[float, float]],
    interface: Interface,
    *,
    y_offset: float = 0,
) -> Part:
    if len(x_runs) < 2 or any(b[0] <= a[0] for a, b in zip(x_runs, x_runs[1:])):
        raise ValueError("variable ramp sections require increasing x positions")
    wires = [_ramp_profile_wire(x, run, interface, y_offset=y_offset) for x, run in x_runs]
    return Part(Solid.make_loft(wires, ruled=True).wrapped)


def _ramp_carrier_run_mm(run: float) -> float:
    return min(RAMP_CARRIER_RUN_MM, max(0.05, run * 0.25))


def _hole_cutter(
    x: float,
    y: float,
    interface: Interface,
) -> Solid:
    # A solid bottom leaves completed openings blind from the top, as on tiles.
    start = interface.solid_bottom_mm or -1.0
    return Solid.make_cylinder(5, interface.body_height + 1 - start).moved(Location((x, y, start)))


def _south_boundary_hole_centers(
    cells: int,
    parameters: ContourParameters,
) -> tuple[tuple[float, float], ...]:
    tile = Tile(
        cells,
        4,
        parameters.interface,
        hole_diameter=10,
        hole_scope="full",
    )
    centers = tuple(
        (placement.x, placement.y)
        for placement in hole_placements(tile)
        if placement.accepted and abs(placement.y) < 1e-8
    )
    expected = tuple(
        (index * parameters.interface.pitch / 2, 0.0) for index in range(2 * cells + 1)
    )
    if centers != expected:
        raise ValueError("accepted tile south-boundary hole pattern changed")
    return centers


@lru_cache(maxsize=16)
def _male_tab_root_side_web_mm(interface: Interface) -> float:
    root = section(tile_join_tool(interface, male=True), section_by=Plane.XZ)
    half_width = min(abs(vertex.X) for vertex in root.vertices() if abs(vertex.X) > 5 + 1e-7)
    return half_width - 5


def _south_hole_feasibility(
    x0: float,
    cells: int,
    parameters: ContourParameters,
) -> list[dict]:
    centers = _south_boundary_hole_centers(cells, parameters)
    reports = []
    for local_x, local_y in centers:
        run = tail_y_mm(x0 + local_x, parameters) - parameters.field_y_max_mm
        carrier = _ramp_carrier_run_mm(run)
        nearest = min(
            sqrt((local_x - other_x) ** 2 + (local_y - other_y) ** 2)
            for other_x, other_y in centers
            if (other_x, other_y) != (local_x, local_y)
        )
        tab_center = abs((local_x / parameters.interface.pitch) % 1 - 0.5) < 1e-7
        reports.append(
            {
                "center": (local_x, local_y),
                "assembly_center": (
                    x0 + local_x,
                    parameters.field_assembly_y_min_mm,
                    0.0,
                ),
                "cut_depth_into_high_shelf_mm": 5.0,
                "high_shelf_carrier_run_mm": carrier,
                "minimum_high_shelf_material_behind_cut_mm": carrier - 5,
                "minimum_adjacent_hole_web_mm": nearest - 10,
                "cuts_male_tab": tab_center,
                "minimum_male_tab_root_side_web_mm": (
                    _male_tab_root_side_web_mm(parameters.interface) if tab_center else None
                ),
                "ownership": (
                    "outer side-corner quarter"
                    if abs(x0 + local_x) == FIELD_WIDTH_MM / 2
                    else (
                        "module-seam quarter"
                        if local_x in (0.0, cells * parameters.interface.pitch)
                        else "south-boundary half"
                    )
                ),
            }
        )
    return reports


def _tail_samples(
    x0: float,
    x1: float,
    parameters: ContourParameters,
    *,
    maximum_step_mm: float = 2.5,
) -> list[tuple[float, float]]:
    count = max(1, ceil((x1 - x0) / maximum_step_mm))
    samples = set(np.linspace(x0, x1, count + 1).tolist())
    samples.update(sample_x for sample_x, _, _ in SOUTH_CONTOUR_CAD_SAMPLES if x0 < sample_x < x1)
    values = {x: tail_y_mm(x, parameters) for x in sorted(samples)}
    ordered = sorted(values)
    # The ruled loft is piecewise linear in plan. Make the first chord on both
    # sides of every module seam equal to the analytic plan tangent.
    for boundary in (x0, x1):
        if not -FIELD_WIDTH_MM / 2 <= boundary <= FIELD_WIDTH_MM / 2:
            continue
        slope = _tail_slope_dy_dx(boundary, parameters)
        neighbor = ordered[1] if boundary == x0 else ordered[-2]
        values[neighbor] = values[boundary] + slope * (neighbor - boundary)
    return [(x - x0, values[x] - parameters.field_y_max_mm) for x in ordered]


def _south_ramp_shape(
    x0: float,
    cells: int,
    parameters: ContourParameters,
) -> tuple[Part, list[tuple[float, float]]]:
    width = cells * parameters.interface.pitch
    x_runs = _tail_samples(x0, x0 + width, parameters)
    source_runs = sorted((width - x, run) for x, run in x_runs)
    part = (
        _variable_ramp(source_runs, parameters.interface)
        .rotate(Axis.Z, 180)
        .moved(Location((width, 0, 0)))
    )
    joins = []
    for cell in range(cells):
        x = (cell + 0.5) * parameters.interface.pitch
        tool = tile_join_tool(parameters.interface, male=True).moved(Location((x, 0, 0)))
        outside = Solid.make_box(
            width,
            parameters.interface.male_join_depth,
            parameters.interface.body_height,
        )
        tab = Part(tool.intersect(outside).solids())
        part = part.fuse(tab)
        joins.append((x, 0.0))
    hole_centers = _south_boundary_hole_centers(cells, parameters)
    part = part.cut(*(_hole_cutter(x, y, parameters.interface) for x, y in hole_centers))
    result = part.clean()
    if not result.is_valid or len(result.solids()) != 1 or result.volume <= 0:
        raise ValueError("Zeekr south ramp segment is invalid or disconnected")
    return result, x_runs


def _side_outline_x(
    side: str,
    vehicle_y: float,
    parameters: ContourParameters,
) -> float:
    if parameters.outline is not None:
        frame_x = parameters.field_x_min_mm if side == "west" else -parameters.field_x_min_mm
        return parameters.outline.side_x(side, vehicle_y) - frame_x
    width = (
        right_outline_x_mm(
            vehicle_y,
            outline_inset_mm=parameters.outline_inset_mm,
            traced_north_corner_radius_mm=parameters.traced_north_corner_radius_mm,
            pen_offset_mm=parameters.pen_offset_mm,
            east_edge_x_mm=parameters.east_edge_x_mm,
            se_along_edge_shift_mm=parameters.se_along_edge_shift_mm,
        )
        - FIELD_WIDTH_MM / 2
    )
    return -width if side == "west" else width


def _candidate_side_socket_centers(
    side: str,
    segment: str,
    parameters: ContourParameters,
) -> tuple[tuple[float, float, float], ...]:
    x = -parameters.interface.pitch / 2 if side == "west" else parameters.interface.pitch / 2
    all_centers = tuple(
        (x, parameters.field_assembly_y_min_mm + (row + 0.5) * parameters.interface.pitch, 0)
        for row in range(4)
    )
    split = parameters.assembly_y_mm(parameters.side_split_y_mm)
    return tuple(
        center
        for center in all_centers
        if (center[1] < split if segment == "south" else center[1] > split)
    )


def _side_boundary_hole_centers(
    side: str,
    segment: str,
    parameters: ContourParameters,
) -> tuple[tuple[float, float], ...]:
    tile = Tile(
        4,
        4,
        parameters.interface,
        hole_diameter=10,
        hole_scope="full",
    )
    boundary_x = 0 if side == "west" else tile.body_size[0]
    local_y = tuple(
        placement.y
        for placement in hole_placements(tile)
        if placement.accepted and abs(placement.x - boundary_x) < 1e-8
    )
    if local_y != tuple(float(y) for y in range(0, 241, 30)):
        raise ValueError("accepted 4x4 side-boundary hole pattern changed")
    centers = tuple((0.0, parameters.field_assembly_y_min_mm + y) for y in local_y)
    split = parameters.assembly_y_mm(parameters.side_split_y_mm)
    return tuple(
        center
        for center in centers
        if (center[1] <= split if segment == "south" else center[1] >= split)
    )


def _side_interior_hole_candidates(
    side: str,
    segment: str,
    parameters: ContourParameters,
) -> tuple[dict, ...]:
    virtual_tile = Tile(
        1,
        4,
        parameters.interface,
        hole_diameter=10,
        hole_scope="full",
    )
    sign = -1 if side == "west" else 1
    split = parameters.assembly_y_mm(parameters.side_split_y_mm)
    candidates = []
    for placement in hole_placements(virtual_tile):
        if not placement.accepted or placement.x not in (
            parameters.interface.pitch / 2,
            parameters.interface.pitch,
        ):
            continue
        assembly_y = parameters.field_assembly_y_min_mm + placement.y
        if not (assembly_y <= split if segment == "south" else assembly_y >= split):
            continue
        candidates.append(
            {
                "center": (sign * placement.x, assembly_y),
                "column": (
                    "socket-column"
                    if placement.x == parameters.interface.pitch / 2
                    else "outer-edge-column"
                ),
            }
        )
    return tuple(candidates)


def _north_boundary_vehicle_y_mm(
    x_mm: float,
    parameters: ContourParameters,
) -> float:
    if parameters.outline is not None:
        return parameters.outline.north_y(x_mm)
    t = abs(x_mm)
    straight_x = parameters.east_edge_x_mm - parameters.outline_inset_mm
    if t <= 507.5:
        return parameters.north_limit_y_mm
    geometry = _north_corner_geometry(parameters)
    tangent_x = geometry["north_tangent"][0]
    if t <= tangent_x:
        u = straight_x - t
        return (
            parameters.north_limit_y_mm
            + _north_edge_s_slope(
                u,
                parameters.pen_offset_mm,
            )[0]
        )
    if t <= straight_x:
        center_x, center_y = geometry["center"]
        return center_y - sqrt(max(0.0, parameters.north_corner_radius_mm**2 - (t - center_x) ** 2))
    raise ValueError("north boundary sample lies outside the selected outline")


def _side_hole_transition_webs(
    side: str,
    center: tuple[float, float],
    parameters: ContourParameters,
) -> tuple[float, float, float]:
    local_x, assembly_y = center
    sign = -1 if side == "west" else 1
    frame_x = parameters.field_x_min_mm if side == "west" else -parameters.field_x_min_mm
    contour_web = float("inf")
    south_web = float("inf")
    north_web = float("inf")
    for offset in np.linspace(-5, 5, 101):
        chord = sqrt(max(0.0, 25 - offset**2))
        sample_y = assembly_y + offset
        vehicle_y = parameters.rear_limit_y_mm - sample_y
        contour_x = _side_outline_x(side, vehicle_y, parameters)
        contour_web = min(
            contour_web,
            sign * (contour_x - local_x) - chord,
        )
        global_x = frame_x + local_x + offset
        if abs(global_x) > parameters.east_edge_x_mm - parameters.outline_inset_mm:
            outside = parameters.east_edge_x_mm - parameters.outline_inset_mm - abs(global_x)
            south_web = min(south_web, outside)
            north_web = min(north_web, outside)
            continue
        south_web = min(
            south_web,
            assembly_y - chord - parameters.assembly_y_mm(tail_y_mm(global_x, parameters)),
        )
        north_web = min(
            north_web,
            parameters.assembly_y_mm(_north_boundary_vehicle_y_mm(global_x, parameters))
            - assembly_y
            - chord,
        )
    return contour_web, south_web, north_web


def _side_interior_hole_feasibility(
    side: str,
    segment: str,
    parameters: ContourParameters,
) -> list[dict]:
    interface = parameters.interface
    tile = Tile(1, 4, interface, hole_diameter=10, hole_scope="full")
    protected_socket = x_profile(
        interface,
        offset=3 + tile.minimum_web + interface.fit_offset,
    )
    conceptual_sockets = tuple(
        (
            -interface.pitch / 2 if side == "west" else interface.pitch / 2,
            parameters.field_assembly_y_min_mm + (row + 0.5) * interface.pitch,
        )
        for row in range(4)
    )
    boundary_holes = tuple(
        (0.0, parameters.field_assembly_y_min_mm + offset)
        for offset in range(0, int(FIELD_DEPTH_MM) + 1, 30)
    )
    join_tools = []
    for center_y in (
        parameters.field_assembly_y_min_mm + interface.pitch / 2,
        parameters.field_assembly_y_min_mm + 3 * interface.pitch / 2,
        parameters.field_assembly_y_min_mm + 5 * interface.pitch / 2,
        parameters.field_assembly_y_min_mm + 7 * interface.pitch / 2,
    ):
        join_tools.append(
            tile_join_tool(interface, male=side == "west")
            .rotate(Axis.Z, -90)
            .moved(Location((0, center_y, 0)))
        )
    reports = []
    for candidate in _side_interior_hole_candidates(side, segment, parameters):
        center = candidate["center"]
        cutter = _hole_cutter(*center, interface)
        socket_keepout = min(
            protected_socket.distance_to(Vector(center[0] - socket_x, center[1] - socket_y, 0)) - 5
            for socket_x, socket_y in conceptual_sockets
        )
        adjacent_boundary = min(
            sqrt((center[0] - hole_x) ** 2 + (center[1] - hole_y) ** 2) - 10
            for hole_x, hole_y in boundary_holes
        )
        join_web = min(cutter.distance_to(join) for join in join_tools)
        contour_web, south_web, north_web = _side_hole_transition_webs(
            side,
            center,
            parameters,
        )
        tile_boundary_web = abs(center[0]) - 5
        report = {
            **candidate,
            "assembly_center": (
                (parameters.field_x_min_mm if side == "west" else -parameters.field_x_min_mm)
                + center[0],
                center[1],
                0.0,
            ),
            "minimum_socket_keepout_web_mm": socket_keepout,
            "minimum_contour_web_mm": contour_web,
            "minimum_join_web_mm": join_web,
            "minimum_adjacent_boundary_hole_web_mm": adjacent_boundary,
            "minimum_tile_boundary_web_mm": tile_boundary_web,
            "minimum_south_transition_web_mm": south_web,
            "minimum_north_transition_web_mm": north_web,
            "ownership": (
                "split-seam half"
                if abs(center[1] - parameters.assembly_y_mm(parameters.side_split_y_mm)) < 1e-7
                else "full hole in one cap segment"
            ),
        }
        limiting = min(
            socket_keepout,
            contour_web,
            join_web,
            adjacent_boundary,
            tile_boundary_web,
            south_web,
            north_web,
        )
        report["minimum_reported_material_mm"] = limiting
        report["meets_minimum_web"] = (
            candidate["column"] == "socket-column"
            and limiting >= PERIMETER_HOLE_MINIMUM_WEB_MM - 1e-7
        )
        report["rejection_reason"] = (
            None
            if report["meets_minimum_web"]
            else (
                "outer column breaks through corrected contour"
                if contour_web < PERIMETER_HOLE_MINIMUM_WEB_MM
                else "perimeter-hole clearance below minimum"
            )
        )
        reports.append(report)
    return reports


def _side_outboard_edge(side: str, parameters: ContourParameters) -> Edge | Wire:
    if parameters.outline is not None:
        frame_x = parameters.field_x_min_mm if side == "west" else -parameters.field_x_min_mm
        return parameters.outline._wire.rotate(Axis.X, 180).moved(
            Location((-frame_x, parameters.rear_limit_y_mm, parameters.interface.body_height))
        )
    sign = -1 if side == "west" else 1
    points = [
        Vector(
            sign
            * (
                right_outline_x_mm(
                    parameters.rear_limit_y_mm - assembly_y,
                    outline_inset_mm=parameters.outline_inset_mm,
                    traced_north_corner_radius_mm=parameters.traced_north_corner_radius_mm,
                    pen_offset_mm=parameters.pen_offset_mm,
                    east_edge_x_mm=parameters.east_edge_x_mm,
                    se_along_edge_shift_mm=parameters.se_along_edge_shift_mm,
                )
                - FIELD_WIDTH_MM / 2
            ),
            assembly_y,
            parameters.interface.body_height,
        )
        for assembly_y in np.linspace(
            parameters.field_assembly_y_min_mm,
            parameters.assembly_y_mm(parameters.north_limit_y_mm),
            271,
        )
    ]
    return Edge.make_spline(points)


def _side_socket_feasibility(
    side: str,
    segment: str,
    parameters: ContourParameters,
) -> list[dict]:
    interface = parameters.interface
    outboard = _side_outboard_edge(side, parameters)
    split = parameters.assembly_y_mm(parameters.side_split_y_mm)
    seam = Edge.make_line(
        (-100, split, interface.body_height),
        (100, split, interface.body_height),
    )
    hole_centers = (
        *_side_boundary_hole_centers(side, segment, parameters),
        *(
            report["center"]
            for report in _side_interior_hole_feasibility(
                side,
                segment,
                parameters,
            )
            if report["meets_minimum_web"]
        ),
    )
    reports = []
    for center in _candidate_side_socket_centers(side, segment, parameters):
        cutter = socket_entry_tool(interface).moved(Location(center))
        top = section(cutter, section_by=Plane.XY.offset(interface.body_height))
        hole_clearance = min(
            cutter.distance_to(_hole_cutter(hole_x, hole_y, interface))
            for hole_x, hole_y in hole_centers
        )
        if side == "east":
            join = (
                tile_join_tool(interface, male=False)
                .rotate(Axis.Z, -90)
                .moved(Location((0, center[1], 0)))
            )
        else:
            join_intersection = (
                tile_join_tool(interface, male=True)
                .rotate(Axis.Z, -90)
                .moved(Location((0, center[1], 0)))
                .intersect(
                    Solid.make_box(
                        interface.male_join_depth,
                        interface.pitch,
                        interface.body_height,
                    ).moved(
                        Location(
                            (
                                0,
                                center[1] - interface.pitch / 2,
                                0,
                            )
                        )
                    )
                )
            )
            join = Part(join_intersection.solids())
        entry_half = socket_entry_tool(interface).bounding_box().max.X
        global_center_x = (
            parameters.field_x_min_mm + center[0]
            if side == "west"
            else -parameters.field_x_min_mm + center[0]
        )
        sample_x = np.linspace(
            global_center_x - entry_half,
            global_center_x + entry_half,
            201,
        )
        south_edge_y = max(
            parameters.assembly_y_mm(tail_y_mm(float(x), parameters)) for x in sample_x
        )
        south_clearance = center[1] - entry_half - south_edge_y
        north_clearance = (
            parameters.assembly_y_mm(parameters.north_limit_y_mm) - center[1] - entry_half
        )
        if parameters.outline is not None:
            north_clearance = (
                min(
                    parameters.assembly_y_mm(parameters.outline.north_y(float(x))) for x in sample_x
                )
                - center[1]
                - entry_half
            )
        report = {
            "center": center,
            "minimum_outboard_contour_mm": top.distance_to(outboard),
            "minimum_tile_edge_join_mm": cutter.distance_to(join),
            "minimum_10mm_boundary_hole_mm": hole_clearance,
            "minimum_cap_split_seam_mm": top.distance_to(seam),
            "minimum_south_corner_transition_mm": south_clearance,
            "minimum_north_corner_transition_mm": north_clearance,
            "socket_entry_roundover_mm": interface.socket_entry_radius,
            "full_depth_mm": interface.height,
            "additional_cap_top_bottom_fillet_mm": 0,
        }
        limiting = min(
            report["minimum_outboard_contour_mm"],
            report["minimum_tile_edge_join_mm"],
            report["minimum_10mm_boundary_hole_mm"],
            report["minimum_cap_split_seam_mm"],
            report["minimum_south_corner_transition_mm"],
            report["minimum_north_corner_transition_mm"],
        )
        report["minimum_reported_material_mm"] = limiting
        report["meets_minimum_web"] = limiting >= SIDE_SOCKET_MINIMUM_WEB_MM - 1e-7
        reports.append(report)
    return reports


def _side_socket_centers(
    side: str,
    segment: str,
    parameters: ContourParameters,
) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        report["center"]
        for report in _side_socket_feasibility(side, segment, parameters)
        if report["meets_minimum_web"]
    )


def _side_body_face(
    side: str,
    local_y0: float,
    local_y1: float,
    parameters: ContourParameters,
) -> Face:
    if parameters.outline is not None:
        outline = parameters.outline
        frame_x = parameters.field_x_min_mm if side == "west" else -parameters.field_x_min_mm
        x0, y0, x1, _ = outline.bounds_mm
        x0, x1 = (x0, frame_x) if side == "west" else (frame_x, x1)
        north = abs(local_y0 - (parameters.north_limit_y_mm - parameters.field_y_min_mm)) < 1e-7
        if not north:
            y0 = parameters.field_y_min_mm + local_y0
        y1 = parameters.field_y_min_mm + local_y1
        if x0 >= x1 or y0 >= y1:
            raise ValueError("outline leaves no material for a side cap")
        region = Face(
            Wire.make_polygon([(x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0)], close=True)
        )
        common = outline._face.intersect(region)
        faces = common.faces() if common is not None else []
        if len(faces) != 1:
            raise ValueError("outline must leave one connected face for each side cap")
        return faces[0].moved(Location((-frame_x, -parameters.field_y_min_mm, 0)))
    count = max(2, ceil((local_y1 - local_y0) / 10))
    north_corner = abs(local_y0 - (parameters.north_limit_y_mm - parameters.field_y_min_mm)) < 1e-7
    corner_geometry = _north_corner_geometry(parameters) if north_corner else None
    outer_end_y = (
        corner_geometry["east_tangent"][1] - parameters.field_y_min_mm if north_corner else local_y0
    )
    inner_u = parameters.east_edge_x_mm - parameters.outline_inset_mm - FIELD_WIDTH_MM / 2
    inner_north_y = (
        parameters.north_limit_y_mm
        + _north_edge_s_slope(inner_u, parameters.pen_offset_mm)[0]
        - parameters.field_y_min_mm
        if north_corner
        else local_y0
    )
    ys = np.linspace(local_y1, outer_end_y, count + 1)
    outer = [
        Vector(
            _side_outline_x(side, parameters.field_y_min_mm + y, parameters),
            y,
            0,
        )
        for y in ys
    ]
    edges = [
        Edge.make_line((0, inner_north_y, 0), (0, local_y1, 0)),
        Edge.make_line((0, local_y1, 0), outer[0]),
        Edge.make_spline(outer),
    ]
    if north_corner:
        sign = -1 if side == "west" else 1
        center_global_x, center_global_y = corner_geometry["center"]
        tangent_global_x, tangent_global_y = corner_geometry["north_tangent"]
        center_x = sign * (center_global_x - FIELD_WIDTH_MM / 2)
        center_y = center_global_y - parameters.field_y_min_mm
        north_tangent = Vector(
            sign * (tangent_global_x - FIELD_WIDTH_MM / 2),
            tangent_global_y - parameters.field_y_min_mm,
            0,
        )
        radial_start = outer[-1] - Vector(center_x, center_y, 0)
        radial_end = north_tangent - Vector(center_x, center_y, 0)
        radial_sum = radial_start + radial_end
        radial_length = sqrt(radial_sum.X**2 + radial_sum.Y**2)
        middle = Vector(
            center_x + parameters.north_corner_radius_mm * radial_sum.X / radial_length,
            center_y + parameters.north_corner_radius_mm * radial_sum.Y / radial_length,
            0,
        )
        line_end_u = 20.0
        line_end = Vector(
            sign
            * (
                parameters.east_edge_x_mm
                - parameters.outline_inset_mm
                - line_end_u
                - FIELD_WIDTH_MM / 2
            ),
            parameters.north_limit_y_mm
            + _north_edge_s_slope(line_end_u, parameters.pen_offset_mm)[0]
            - parameters.field_y_min_mm,
            0,
        )
        north_u = np.linspace(line_end_u, inner_u, 20)
        north_points = [
            Vector(
                sign
                * (
                    parameters.east_edge_x_mm - parameters.outline_inset_mm - u - FIELD_WIDTH_MM / 2
                ),
                parameters.north_limit_y_mm
                + _north_edge_s_slope(float(u), parameters.pen_offset_mm)[0]
                - parameters.field_y_min_mm,
                0,
            )
            for u in north_u
        ]
        start_tangent = Vector(
            -sign,
            _north_edge_s_slope(line_end_u, parameters.pen_offset_mm)[1],
            0,
        )
        end_tangent = Vector(
            -sign,
            _north_edge_s_slope(inner_u, parameters.pen_offset_mm)[1],
            0,
        )
        edges.extend(
            (
                Edge.make_three_point_arc(outer[-1], middle, north_tangent),
                Edge.make_line(north_tangent, line_end),
                Edge.make_spline(
                    north_points,
                    tangents=[start_tangent, end_tangent],
                ),
            )
        )
    else:
        edges.append(Edge.make_line(outer[-1], (0, local_y0, 0)))
    return Face(Wire(edges))


def _side_corner_ramp(
    side: str,
    parameters: ContourParameters,
) -> tuple[Part, list[tuple[float, float]]]:
    y_offset = FIELD_DEPTH_MM
    outer = _side_outline_x(side, parameters.field_y_max_mm, parameters)
    low, high = sorted((outer, 0.0))
    count = max(2, ceil((high - low) / 5))
    xs = np.linspace(low, high, count + 1)
    x_runs = []
    for local_x in xs:
        vehicle_x = (
            parameters.field_x_min_mm + local_x
            if side == "west"
            else -parameters.field_x_min_mm + local_x
        )
        run = tail_y_mm(vehicle_x, parameters) - parameters.field_y_max_mm
        x_runs.append((float(local_x), max(CORNER_TIP_REGULARIZATION_MM, run)))
    return (
        _variable_ramp(x_runs, parameters.interface, y_offset=y_offset),
        x_runs,
    )


def _side_shape(
    side: str,
    segment: str,
    parameters: ContourParameters,
) -> tuple[
    Part,
    list[dict],
    list[tuple[float, float]],
    tuple[tuple[float, float, float], ...],
    tuple[tuple[float, float], ...],
    list[dict],
]:
    split = parameters.side_split_y_mm - parameters.field_y_min_mm
    if segment == "north":
        local_y0 = parameters.north_limit_y_mm - parameters.field_y_min_mm
        local_y1 = split
        centers = (30.0, 90.0)
    elif segment == "south":
        local_y0 = split
        local_y1 = FIELD_DEPTH_MM
        centers = (150.0, 210.0)
    else:
        raise ValueError("side segment must be north or south")

    body = Part(
        Solid.extrude(
            _side_body_face(side, local_y0, local_y1, parameters),
            (0, 0, parameters.interface.body_height),
        ).wrapped
    )
    corner_samples: list[tuple[float, float]] = []
    if segment == "south":
        corner, corner_samples = _side_corner_ramp(side, parameters)
        body = body.fuse(corner).clean()

    male = side == "west"
    joins = []
    for y in centers:
        tool = tile_join_tool(parameters.interface, male=male).rotate(Axis.Z, -90)
        tool = tool.moved(Location((0, y, 0)))
        if male:
            outside = Solid.make_box(
                parameters.interface.male_join_depth,
                parameters.interface.pitch,
                parameters.interface.body_height,
            ).moved(Location((0, y - parameters.interface.pitch / 2, 0)))
            tab = Part(tool.intersect(outside).solids())
            body = body.fuse(tab)
        else:
            body = body.cut(tool)
        joins.append(
            {
                "position": (0.0, y, 0.0),
                "angle": -90,
                "sex": "male" if male else "female",
                "depth": (
                    parameters.interface.male_join_depth
                    if male
                    else parameters.interface.female_join_depth
                ),
                "height": (
                    parameters.interface.male_height
                    if male
                    else parameters.interface.female_opening_height
                ),
                "interface": "tile-dovetail",
                "joint_style": parameters.interface.joint_style,
                "open_through_top": False,
            }
        )
    result = body.clean()
    result = result.mirror(Plane.XZ).moved(Location((0, parameters.field_assembly_y_max_mm, 0)))
    joins = [
        {
            **join,
            "position": (
                join["position"][0],
                parameters.field_assembly_y_max_mm - join["position"][1],
                join["position"][2],
            ),
        }
        for join in joins
    ]
    socket_centers = _side_socket_centers(side, segment, parameters)
    hole_centers = _side_boundary_hole_centers(side, segment, parameters)
    interior_hole_reports = _side_interior_hole_feasibility(
        side,
        segment,
        parameters,
    )
    interior_hole_centers = tuple(
        report["center"] for report in interior_hole_reports if report["meets_minimum_web"]
    )
    result = result.cut(
        *(
            socket_entry_tool(parameters.interface).moved(Location(center))
            for center in socket_centers
        )
    )
    result = result.cut(*(_hole_cutter(x, y, parameters.interface) for x, y in hole_centers))
    result = result.cut(
        *(_hole_cutter(x, y, parameters.interface) for x, y in interior_hole_centers)
    ).clean()
    if not result.is_valid or len(result.solids()) != 1 or result.volume <= 0:
        raise ValueError(f"Zeekr {side} {segment} side segment is invalid or disconnected")
    return (
        result,
        joins,
        corner_samples,
        socket_centers,
        hole_centers,
        interior_hole_reports,
    )


def _solid_bottom_suffix(interface: Interface) -> str:
    return f"_solid-bottom-{interface.solid_bottom_mm:g}mm" if interface.solid_bottom_mm else ""


def _outline_design(design: Design, parameters: ContourParameters) -> Design:
    if parameters.outline is None:
        return design
    outline = parameters.outline
    design.name += f"_outline-{outline.digest[:16]}"
    design.parameters["outline"] = {"sha256": outline.digest, "bounds_mm": outline.bounds_mm}
    design.parameters.pop("south_contour_source", None)
    design.parameters.pop("south_contour_bow_mm", None)
    if "global_contour_samples_x_y_slope" in design.mating_datums:
        frame_x = design.assembly_frames[0][0]
        design.mating_datums["global_contour_samples_x_y_slope"] = [
            (
                frame_x + x,
                tail_y_mm(frame_x + x, parameters),
                _tail_slope_dy_dx(frame_x + x, parameters),
            )
            for x, _ in design.mating_datums["nose_samples_local_x_run_mm"]
        ]
    return design


def _south_design(
    cells: int,
    index: int,
    x: float,
    parameters: ContourParameters,
) -> Design:
    shape, samples = _south_ramp_shape(x, cells, parameters)
    interface = parameters.interface
    joins = [
        {
            "position": ((cell + 0.5) * interface.pitch, 0, 0),
            "angle": 0,
            "sex": "male",
            "depth": interface.male_join_depth,
            "height": interface.male_height,
            "interface": "tile-dovetail",
            "joint_style": interface.joint_style,
            "open_through_top": False,
        }
        for cell in range(cells)
    ]
    runs = [run for _, run in samples]
    hole_reports = _south_hole_feasibility(x, cells, parameters)
    hole_centers = tuple(report["center"] for report in hole_reports)
    design = Design(
        f"zeekr_rear_south_ramp_{index + 1}_{cells}cell{_solid_bottom_suffix(interface)}",
        shape,
        {
            "family": "zeekr-rear-contour-ramp",
            "width_cells": cells,
            "interface": interface_parameters(interface),
            "connector_sex": "male",
            "vehicle_boundary": "SOUTH",
            "outline_inset_mm": parameters.outline_inset_mm,
            "vehicle_x_span_mm": (x, x + cells * interface.pitch),
            "finished_run_range_mm": (min(runs), max(runs)),
            "south_contour_source": "merged three-trace taper plus C1 concave quartic crown",
            "south_contour_bow_mm": (
                parameters.centre_depth_mm
                - _merged_taper_stations(
                    parameters.pen_offset_mm,
                    parameters.se_along_edge_shift_mm,
                )[-1][1]
            ),
            "tile_edge_interface_present": True,
            "x_attachment_interface_present": False,
            "profile": {
                "carrier_run_mm": RAMP_CARRIER_RUN_MM,
                "shelf_radius_mm": RAMP_SHELF_RADIUS_MM,
                "nose_radius_mm": RAMP_FREE_EDGE_RADIUS_MM,
                "segment_caps": "flat butt seams; no seam connectors",
            },
        },
        display_name=f"South male contour ramp {index + 1} - {cells} cells",
        assembly_frames=[(x, parameters.field_assembly_y_min_mm, 0)],
        holes=[
            {
                "x": hole_x,
                "y": hole_y,
                "accepted": True,
                "reason": (
                    "accepted adjacent tile south-boundary site; ramp owns its outward "
                    "half and only its quarter at module seams"
                ),
            }
            for hole_x, hole_y in hole_centers
        ],
        mating_datums={
            "underside_z": 0,
            "top_z": interface.body_height,
            "ramp_direction": "-assembly Y toward tailgate/SOUTH",
            "mating_tile_edge": "canonical south female",
            "joins": joins,
            "completed_10mm_south_boundary_hole_centers": hole_centers,
            "south_boundary_hole_feasibility": hole_reports,
            "nose_samples_local_x_run_mm": samples,
            "global_contour_samples_x_y_slope": [
                (sample_x, sample_y, sample_slope)
                for sample_x, sample_y, sample_slope in SOUTH_CONTOUR_CAD_SAMPLES
                if x - 1e-7 <= sample_x <= x + cells * interface.pitch + 1e-7
            ],
        },
    )
    return _outline_design(design, parameters)


def _side_design(
    side: str,
    segment: str,
    parameters: ContourParameters,
) -> Design:
    (
        shape,
        joins,
        corner_samples,
        socket_centers,
        hole_centers,
        interior_hole_reports,
    ) = _side_shape(side, segment, parameters)
    frame_x = parameters.field_x_min_mm if side == "west" else -parameters.field_x_min_mm
    male = side == "west"
    candidate_feasibility = _side_socket_feasibility(side, segment, parameters)
    feasibility = [report for report in candidate_feasibility if report["meets_minimum_web"]]
    design = Design(
        f"zeekr_rear_{side}_{segment}_cap{_solid_bottom_suffix(parameters.interface)}",
        shape,
        {
            "family": "zeekr-rear-contour-side",
            "side": side,
            "segment": segment,
            "interface": interface_parameters(parameters.interface),
            "outline_inset_mm": parameters.outline_inset_mm,
            "aggregate_mating_run_mm": FIELD_DEPTH_MM,
            "connector_sex": "male" if male else "female",
            "standalone_local_x": (
                "contour body is at negative X; male tabs project +X into the tile"
                if side == "west"
                else "contour body is at positive X; female pockets open from X=0 into the cap"
            ),
            "tile_edge_interface_present": True,
            "x_attachment_interface_present": True,
            "complete_edge_holes": True,
            "edge_hole_diameter": 10,
            "accessory_socket_count": len(socket_centers),
            "socket_minimum_web_mm": SIDE_SOCKET_MINIMUM_WEB_MM,
            "corner_ownership": ("north" if segment == "north" else "south contour/ramp"),
            "segmentation": (
                "two printable cap segments with one flat butt seam; "
                "no separate corner part and no seam connector"
            ),
            "corner_tip_regularization_mm": (
                CORNER_TIP_REGULARIZATION_MM if segment == "south" else None
            ),
        },
        display_name=(
            f"{side.title()} {'male' if male else 'female'} contour cap - {segment} segment"
        ),
        assembly_frames=[(frame_x, 0, 0)],
        holes=[
            {
                "x": x,
                "y": y,
                "accepted": True,
                "reason": (
                    "accepted full-pattern 4x4 side-boundary site; this cap owns only "
                    "the material on its side of the assembly boundaries"
                ),
            }
            for x, y in hole_centers
        ]
        + [
            {
                "x": report["center"][0],
                "y": report["center"][1],
                "accepted": report["meets_minimum_web"],
                "reason": (
                    "accepted virtual-tile socket-column site"
                    if report["meets_minimum_web"]
                    else report["rejection_reason"]
                ),
            }
            for report in interior_hole_reports
        ],
        mating_datums={
            "underside_z": 0,
            "top_z": parameters.interface.body_height,
            "mating_tile_edge": (
                "tile canonical west female" if side == "west" else "tile canonical east male"
            ),
            "joins": joins,
            "accessory_socket_centers": tuple(
                (x, y, parameters.interface.body_height) for x, y, _ in socket_centers
            ),
            "socket_cutter_origins": socket_centers,
            "accessory_socket_interface": (
                "exact ordinary tile X female socket with 3 mm top entry roundover"
            ),
            "completed_10mm_boundary_hole_centers": hole_centers,
            "completed_10mm_interior_hole_centers": tuple(
                report["center"] for report in interior_hole_reports if report["meets_minimum_web"]
            ),
            "interior_hole_feasibility": tuple(
                report for report in interior_hole_reports if report["meets_minimum_web"]
            ),
            "rejected_interior_hole_candidates": tuple(
                report for report in interior_hole_reports if not report["meets_minimum_web"]
            ),
            "socket_feasibility": feasibility,
            "rejected_socket_candidates": [
                report for report in candidate_feasibility if not report["meets_minimum_web"]
            ],
            "corner_ramp_samples_local_x_run_mm": corner_samples,
        },
    )
    return _outline_design(design, parameters)


def rear_panel_designs(
    parameters: ContourParameters = ContourParameters(),
) -> tuple[list[Design], list[Design]]:
    """Build the four measured side-cap segments and five south contour ramps."""
    side_designs = [
        _side_design(side, segment, parameters)
        for side in ("west", "east")
        for segment in ("north", "south")
    ]
    south_designs = []
    x = parameters.field_x_min_mm
    for index, cells in enumerate(TILE_MODULES):
        south_designs.append(_south_design(cells, index, x, parameters))
        x += cells * parameters.interface.pitch
    return side_designs, south_designs
