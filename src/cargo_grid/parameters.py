"""Resolved millimeter dimensions, independent of printer and slicer profiles."""

from dataclasses import dataclass
from math import isfinite
from typing import Literal

JointStyle = Literal["full-height", "original"]
DEFAULT_HOLE_DIAMETER_MM = 10.0
REFERENCE_UNIT_SIZE_MM = 60.0
REFERENCE_TILE_THICKNESS_MM = 13.0


def positive(name: str, value: float, *, zero: bool = False) -> None:
    if not isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")


def count(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class Interface:
    pitch: float = REFERENCE_UNIT_SIZE_MM
    height: float = REFERENCE_TILE_THICKNESS_MM
    fit_offset: float = 0.0
    joint_style: JointStyle = "original"

    def __post_init__(self) -> None:
        if self.joint_style not in ("full-height", "original"):
            raise ValueError("joint_style must be full-height or original")
        positive("pitch", self.pitch)
        positive("height", self.height)
        if self.pitch < 30 or self.height < 6:
            raise ValueError(
                "unit size >= 30 mm and tile thickness >= 6 mm required by interface envelopes"
            )
        if not isfinite(self.fit_offset) or abs(self.fit_offset) > 0.5:
            raise ValueError("fit_offset must be between -0.5 and 0.5 mm")

    @property
    def reference_defaults(self) -> bool:
        return self == Interface(joint_style="original")

    @property
    def unit_scale(self) -> float:
        return self.pitch / REFERENCE_UNIT_SIZE_MM

    @property
    def reference_socket_dimensions(self) -> bool:
        return (
            self.pitch == REFERENCE_UNIT_SIZE_MM
            and self.height == REFERENCE_TILE_THICKNESS_MM
            and self.fit_offset == 0
        )

    @property
    def male_height(self) -> float:
        return self.height if self.joint_style == "full-height" else self.height - 3.0

    @property
    def female_opening_height(self) -> float:
        return self.height if self.joint_style == "full-height" else self.height - 2.8

    @property
    def plug_depth(self) -> float:
        return self.height - 0.2

    @property
    def male_join_depth(self) -> float:
        return 6.0 * self.unit_scale

    @property
    def female_join_depth(self) -> float:
        return self.male_join_depth + 0.1

    @property
    def tile_join_blend_radius(self) -> float:
        return min(1.0, self.unit_scale)

    @property
    def socket_entry_radius(self) -> float:
        return 3.0

    def compatibility(self) -> dict:
        return {
            "joint_style": self.joint_style,
            "experimental": self.joint_style == "full-height",
            "unit_size_mm": self.pitch,
            "tile_thickness_mm": self.height,
            "nominal_local_plane_scale": self.unit_scale,
            "standard_60x13_dimensions": self.reference_socket_dimensions,
            "original_tile_edge_dimensions": self.reference_defaults,
            "original_x_attachment_dimensions": self.reference_socket_dimensions,
            "absolute_allowances": {
                "fit_offset_mm": self.fit_offset,
                "plug_seating_gap_mm": 0.2,
                "panel_stem_inset_mm": 0.08,
            },
            "support_rail_interface": "separate fixed physical dovetail; not scaled with tile unit size",
            "attachment_seating_note": (
                "Socket/plug dimensions and seating datum are retained, but open-through edge pockets remove roof-bearing land near edge sockets."
                if self.joint_style == "full-height"
                else "Original socket entry and roof-bearing lands; printed friction and load capacity remain unverified."
            ),
            "geometry_warning": (
                "At the standard 60/13 mm dimensions the negative-X pocket/socket web thins to about 0.146 mm at Z=12.5 and opens to the exterior near Z=12.75. Round holes are not the cause; structural integrity is not validated."
                if self.joint_style == "full-height" and self.reference_socket_dimensions
                else "Custom unit size/thickness match internally but printed fit and compatibility with standard 60/13 mm parts require independent checks."
                if not self.reference_defaults
                else None
            ),
            "edge_note": (
                "Experimental full-height tabs require open-through matching pockets; not compatible with original roofed female edges."
                if self.joint_style == "full-height"
                else "Original partial-height edge geometry; custom dimensions require separate compatibility checks."
            ),
            "physical_fit_verified": False,
        }


@dataclass(frozen=True)
class Tile:
    nx: int = 1
    ny: int = 1
    interface: Interface = Interface()
    hole_diameter: float | None = DEFAULT_HOLE_DIAMETER_MM
    minimum_web: float = 1.5
    west: bool = True
    east: bool = True
    south: bool = True
    north: bool = True
    filler_west: float = 0
    filler_east: float = 0
    filler_south: float = 0
    filler_north: float = 0
    hole_scope: Literal["interior", "full"] = "full"

    def __post_init__(self) -> None:
        count("nx", self.nx)
        count("ny", self.ny)
        positive("minimum_web", self.minimum_web)
        if self.hole_diameter is not None:
            positive("hole_diameter", self.hole_diameter)
        if self.hole_scope not in ("interior", "full"):
            raise ValueError("hole_scope must be interior or full")
        for side in ("west", "east", "south", "north"):
            positive(f"filler_{side}", getattr(self, f"filler_{side}"), zero=True)
            if getattr(self, side) and getattr(self, f"filler_{side}"):
                raise ValueError(f"filler_{side} requires a terminated {side} boundary")

    @property
    def body_size(self) -> tuple[float, float]:
        return (self.nx * self.interface.pitch, self.ny * self.interface.pitch)


@dataclass(frozen=True)
class Exclusion:
    x: float
    y: float
    width: float
    depth: float

    def __post_init__(self) -> None:
        for name in ("x", "y"):
            positive(name, getattr(self, name), zero=True)
        positive("exclusion width", self.width)
        positive("exclusion depth", self.depth)


@dataclass(frozen=True)
class BuildVolume:
    x: float
    y: float
    z: float
    margin: float = 0
    reserve_x: float = 0
    reserve_y: float = 0
    reserve_z: float = 0
    exclusions: tuple[Exclusion, ...] = ()

    def __post_init__(self) -> None:
        for name in ("x", "y", "z"):
            positive(name, getattr(self, name))
        for name in ("margin", "reserve_x", "reserve_y", "reserve_z"):
            positive(name, getattr(self, name), zero=True)
        if min(self.usable) <= 0:
            raise ValueError("reservations leave no usable build volume")
        for area in self.exclusions:
            if area.x + area.width > self.x or area.y + area.depth > self.y:
                raise ValueError("exclusion lies outside build volume")

    @property
    def usable(self) -> tuple[float, float, float]:
        return (
            self.x - 2 * self.margin - self.reserve_x,
            self.y - 2 * self.margin - self.reserve_y,
            self.z - self.reserve_z,
        )

    def contains_box(
        self,
        x: float,
        y: float,
        width: float,
        depth: float,
        height: float,
    ) -> bool:
        """Return whether normalized positive bounds fit the usable envelope."""
        return (
            width > 0
            and depth > 0
            and height > 0
            and x >= self.margin
            and y >= self.margin
            and x + width <= self.x - self.margin - self.reserve_x + 1e-6
            and y + depth <= self.y - self.margin - self.reserve_y + 1e-6
            and height <= self.z - self.reserve_z + 1e-6
            and not any(
                x < area.x + area.width
                and x + width > area.x
                and y < area.y + area.depth
                and y + depth > area.y
                for area in self.exclusions
            )
        )

    def placement(self, size: tuple[float, float, float]) -> tuple[float, float, int] | None:
        """Find a single-object placement, trying 0 and 90 degree print rotations."""
        for dimension in size:
            positive("part dimension", dimension)
        for angle in (0, 90):
            w, d = size[:2] if angle == 0 else size[1::-1]
            xs = sorted({self.margin, *(a.x + a.width for a in self.exclusions)})
            ys = sorted({self.margin, *(a.y + a.depth for a in self.exclusions)})
            for x in xs:
                for y in ys:
                    if self.contains_box(x, y, w, d, size[2]):
                        return x, y, angle
        return None
