"""Explicit sacrificial sandwich volumes; no hidden slicer-generated supports."""

from dataclasses import dataclass
from math import floor
from typing import Literal

from build123d import Compound, Location, Part, Shape, ShapeList

from cargo_grid.interfaces import prism, rectangle, section_face
from cargo_grid.jobs import Design
from cargo_grid.parameters import BuildVolume, count, positive


@dataclass(frozen=True)
class StackSettings:
    count: int
    gap: float
    interface_thickness: float
    model_slot: int
    support_slot: int
    interface_slot: int

    def __post_init__(self) -> None:
        count("stack count", self.count)
        positive("stack gap", self.gap)
        positive("interface thickness", self.interface_thickness)
        for name in ("model_slot", "support_slot", "interface_slot"):
            count(name, getattr(self, name))
        if self.gap <= 2 * self.interface_thickness:
            raise ValueError("gap must exceed both release interfaces, leaving support-base volume")
        if self.interface_slot in (self.model_slot, self.support_slot):
            raise ValueError(
                "release-interface material slot must differ from model and support base"
            )

    def maximum_count(self, height: float, build: BuildVolume) -> int:
        return max(0, floor((build.usable[2] + self.gap + 1e-6) / (height + self.gap)))


@dataclass
class Volume:
    name: str
    shape: Part
    role: str
    slot: int
    subtype: Literal["normal_part", "support_enforcer"] = "normal_part"
    roof_side: Literal["west", "south", "north", "east"] | None = None
    roof_index: int | None = None


def _part(shape: Shape | ShapeList | None) -> Part:
    if shape is None:
        raise ValueError("empty sacrificial support region")
    if isinstance(shape, ShapeList):
        shape = Compound(list(shape))
    return Part(shape.solids())


def stack_volumes(design: Design, settings: StackSettings, build: BuildVolume) -> list[Volume]:
    if not design.name.startswith("tile_"):
        raise ValueError("v1 stacking only accepts repeated identical tiles")
    size = design.size
    if settings.count > settings.maximum_count(size[2], build):
        raise ValueError(
            f"stack count {settings.count} exceeds maximum "
            f"{settings.maximum_count(size[2], build)} including sandwich gaps"
        )
    total = settings.count * size[2] + (settings.count - 1) * settings.gap
    if build.placement((size[0], size[1], total)) is None:
        raise ValueError("stack and print reservations do not fit the usable build region")
    tile = design.shape.moved(Location(-design.shape.bounding_box().min))
    height = size[2]
    bottom_profile = section_face(tile, 1.5)
    # The full-height section keeps all X openings open. The envelope is trimmed
    # against both real tile surfaces, including entry flares and underside rounds.
    bottom_profile = bottom_profile.moved(Location((0, 0, -1.5)))
    volumes = []
    for level in range(settings.count):
        z = level * (height + settings.gap)
        lower = tile.moved(Location((0, 0, z)))
        volumes.append(
            Volume(f"{design.name}_layer_{level + 1}", lower, "model", settings.model_slot)
        )
        if level == settings.count - 1:
            continue
        upper = tile.moved(Location((0, 0, z + height + settings.gap)))
        domain = prism(bottom_profile, height + settings.gap + 1 - 8)
        domain = _part(domain.moved(Location((0, 0, z + 8))).cut(lower, upper))
        t = settings.interface_thickness
        # Flat division planes keep support-base material away from vertical
        # step faces too. The lower release collar is thicker around low tabs
        # and entry flares, rather than a Z-shift skin that could fuse sideways.
        xy = rectangle(-1, -1, size[0] + 2, size[1] + 2)
        lower_band = prism(xy, height + t - 8).moved(Location((0, 0, z + 8)))
        upper_band = prism(xy, 1 + t).moved(Location((0, 0, z + height + settings.gap - t)))
        release_lower = _part(domain.intersect(lower_band))
        release_upper = _part(domain.intersect(upper_band))
        middle_band = prism(xy, settings.gap - 2 * t)
        middle_band = middle_band.moved(Location((0, 0, z + height + t)))
        support = _part(domain.intersect(middle_band))
        for role, shape, slot in (
            ("release-lower", release_lower, settings.interface_slot),
            ("support-base", support, settings.support_slot),
            ("release-upper", release_upper, settings.interface_slot),
        ):
            if shape is None or not shape.is_valid or shape.volume <= 1e-6:
                raise ValueError(f"could not construct valid {role} for sandwich {level + 1}")
            # Individual islands are separate normal volumes, not disconnected
            # mesh shells masquerading as one printable solid.
            for index, solid in enumerate(shape.solids()):
                volumes.append(
                    Volume(
                        f"{design.name}_sandwich_{level + 1}_{role}_{index + 1}", solid, role, slot
                    )
                )
    return volumes
