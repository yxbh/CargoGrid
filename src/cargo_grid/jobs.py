"""Part identity, quantities and assembly frames shared by all CLI commands."""

import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from math import isfinite

from build123d import Axis, Part

from cargo_grid.footprints import ProjectedFootprint
from cargo_grid.layout import Layout
from cargo_grid.packing import PrintPlacement
from cargo_grid.parameters import BuildVolume, Tile, count, positive
from cargo_grid.tiles import hole_placements, make_tile


@dataclass
class Design:
    name: str
    shape: Part
    parameters: dict
    display_name: str | None = None
    quantity: int = 1
    assembly_frames: list[tuple[float, float, float]] = field(default_factory=list)
    holes: list[dict] = field(default_factory=list)
    recommended_print_rotation_x: float | None = None
    recommended_print_rotation_y: float | None = None
    apply_orientation_to_bambu: bool = False
    bambu_object_settings: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        count("design quantity", self.quantity)
        if self.display_name is not None and not self.display_name.strip():
            raise ValueError("design display name must not be blank")
        if self.recommended_print_rotation_x is not None and not isfinite(
            self.recommended_print_rotation_x
        ):
            raise ValueError("recommended print rotation must be finite degrees")
        if self.recommended_print_rotation_y is not None and not isfinite(
            self.recommended_print_rotation_y
        ):
            raise ValueError("recommended print rotation must be finite degrees")
        if not isinstance(self.apply_orientation_to_bambu, bool):
            raise ValueError("apply_orientation_to_bambu must be a boolean")
        if (
            self.apply_orientation_to_bambu
            and self.recommended_print_rotation_x is None
            and self.recommended_print_rotation_y is None
        ):
            raise ValueError("Bambu orientation requires an explicit recommended rotation")
        if self.bambu_object_settings not in (
            {},
            {"enable_support": "1", "support_type": "normal(auto)"},
        ):
            raise ValueError("unsupported Bambu per-object settings")

    @property
    def size(self) -> tuple[float, float, float]:
        box = self.shape.bounding_box()
        return tuple(box.size)

    @property
    def bambu_shape(self) -> Part:
        if self.apply_orientation_to_bambu:
            shape = self.shape
            if self.recommended_print_rotation_x is not None:
                shape = shape.rotate(Axis.X, self.recommended_print_rotation_x)
            if self.recommended_print_rotation_y is not None:
                shape = shape.rotate(Axis.Y, self.recommended_print_rotation_y)
            return shape
        return self.shape

    @property
    def bambu_size(self) -> tuple[float, float, float]:
        return tuple(self.bambu_shape.bounding_box().size)


@dataclass
class Job:
    designs: list[Design]
    build: BuildVolume
    kind: str
    omitted: list[dict] = field(default_factory=list)
    footprint: tuple[float, float] | None = None
    part_gap: float = 2
    print_placements: list[PrintPlacement] | None = None
    plate_names: dict[int, str] = field(default_factory=dict)
    plate_settings: dict[int, dict[str, str]] = field(default_factory=dict)
    placement_policy: dict = field(default_factory=dict)
    projected_footprints: list[ProjectedFootprint | None] | None = None
    projected_footprint_clearances: dict[int, float] = field(default_factory=dict)
    plate_builds: dict[int, BuildVolume] = field(default_factory=dict)

    def __post_init__(self):
        if not self.designs:
            raise ValueError("a job needs at least one design")
        positive("part gap", self.part_gap, zero=True)
        if self.print_placements is not None and len(self.print_placements) != len(self.designs):
            raise ValueError("explicit print placements must match the design count")
        self.validate_plate_builds()
        if self.projected_footprints is not None:
            if self.print_placements is None:
                raise ValueError("projected footprints require explicit print placements")
            if len(self.projected_footprints) != len(self.designs):
                raise ValueError("projected footprints must match the design count")
            if any(
                footprint is not None and not isinstance(footprint, ProjectedFootprint)
                for footprint in self.projected_footprints
            ):
                raise ValueError("projected footprints must be ProjectedFootprint instances")
        if self.projected_footprint_clearances and self.projected_footprints is None:
            raise ValueError("projected footprint clearances require projected footprints")
        placement_plates = (
            {placement.plate for placement in self.print_placements}
            if self.print_placements is not None
            else set()
        )
        for plate, clearance in self.projected_footprint_clearances.items():
            if not isinstance(plate, int) or plate < 0:
                raise ValueError(
                    "projected footprint clearance plates must be nonnegative integers"
                )
            if plate not in placement_plates:
                raise ValueError("projected footprint clearance plate has no placed designs")
            positive("projected footprint clearance", clearance)
        if self.projected_footprints is not None:
            for footprint, placement in zip(self.projected_footprints, self.print_placements):
                if placement.plate in self.projected_footprint_clearances and footprint is None:
                    raise ValueError(
                        "projected footprint clearance plates require every design footprint"
                    )

    def validate_plate_builds(self) -> None:
        if not isinstance(self.plate_builds, dict):
            raise ValueError("plate_builds must be a mapping")
        if self.plate_builds and self.print_placements is None:
            raise ValueError("plate_builds require explicit print placements")
        if self.print_placements is not None and any(
            not isinstance(placement, PrintPlacement) for placement in self.print_placements
        ):
            raise ValueError("explicit placements must be PrintPlacement instances")
        placement_plates = (
            {placement.plate for placement in self.print_placements}
            if self.print_placements is not None
            else set()
        )
        for plate, build in self.plate_builds.items():
            if isinstance(plate, bool) or not isinstance(plate, int) or plate < 0:
                raise ValueError("plate_builds keys must be nonnegative integer plate indices")
            if not isinstance(build, BuildVolume):
                raise ValueError("plate_builds values must be BuildVolume instances")
            if plate not in placement_plates:
                raise ValueError("plate_builds entry has no placed design")
            if build.x > self.build.x or build.y > self.build.y or build.z > self.build.z:
                raise ValueError("plate build dimensions cannot exceed the physical build")


def tile_identity(tile: Tile) -> tuple[str, dict]:
    parameters = asdict(tile)
    token = sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:10]
    scope = f"_{tile.hole_scope}-holes" if tile.hole_diameter is not None else ""
    name = f"tile_{tile.nx}x{tile.ny}_{tile.interface.joint_style}{scope}_{token}"
    return name, parameters


def tile_design(tile: Tile) -> Design:
    name, parameters = tile_identity(tile)
    shape = make_tile(tile)
    shape.label = name
    return Design(name, shape, parameters, holes=[asdict(h) for h in hole_placements(tile)])


def layout_job(layout: Layout, build: BuildVolume) -> Job:
    designs: dict[Tile, Design] = {}
    for placed in layout.pieces:
        if placed.tile not in designs:
            designs[placed.tile] = tile_design(placed.tile)
            designs[placed.tile].quantity = 0
        design = designs[placed.tile]
        if build.placement(design.size) is None:
            raise ValueError(f"actual exported candidate {design.name} does not fit")
        design.quantity += 1
        design.assembly_frames.append((placed.x, placed.y, 0))
    return Job(list(designs.values()), build, "layout", footprint=(layout.width, layout.depth))
