"""Plate planning shared by catalogues and collections: named groups, prime towers and packing.

Callers decide what goes together (ordered, titled groups with their own model gap); this module
decides which plates need a prime tower for auto roof support, reserves the tower column and the
support-foot allowance, packs each group and returns the plates, names and tower positions.
"""

from dataclasses import dataclass, field

from cargo_grid.footprints import (
    ProjectedFootprint,
    pack_projected_footprints,
    projected_mesh_footprint,
)
from cargo_grid.jobs import Design
from cargo_grid.meshes import checked_mesh
from cargo_grid.packing import PrimeTower, PrintPlacement, h2d_common_build, pack_sizes
from cargo_grid.parameters import BuildVolume, Exclusion, positive
from cargo_grid.roof_support import OBJECT_AUTO_SUPPORT, female_roofs

# Auto roof support prints a PLA interface, so every plate with supported parts needs a prime
# tower. Bambu's default tower is used as it is; each plate only gets a tower position and a
# model-free corner around the reserved tower bounds. On the H2D the bounds (X 283..325,
# Y 1..39.5) stay inside the reach of both nozzles (X 25..325), and models can still reach
# X 276.5 beside them, which the 246 mm-wide tiles with a 5-cell side need.
DEFAULT_PRIME_TOWER = PrimeTower()
H2D_AUTO_SUPPORT_TOWER_ORIGIN = (290.5, 8.0)
# Models keep this far from the reserved tower bounds: Bambu's automatic support foot grows up
# to about 5 mm past a supported part.
AUTO_SUPPORT_TOWER_CLEARANCE_MM = 6.5
# Bambu's automatic support-foot expansion grows the first support layer up to about 5 mm past
# the part outline, so plates with supported parts add this to the model clearance and keep
# parts a further 1 mm from the front and back plate edges.
AUTO_SUPPORT_FOOT_ALLOWANCE_MM = 4.0
AUTO_SUPPORT_PLATE_MARGIN_MM = 6.0
H2D_FOOTPRINT_SEARCH_ALLOWANCE_MM = 0.75


def needs_auto_support(design: Design) -> bool:
    """Whether auto roof support switches object support on, so the plate prints PLA."""
    return bool(female_roofs(design)) or design.bambu_object_settings == OBJECT_AUTO_SUPPORT


def h2d_auto_support_build() -> BuildVolume:
    """H2D common reach for plates with a prime tower in the front-right corner."""
    common = h2d_common_build()
    return BuildVolume(
        common.x,
        common.y,
        common.z,
        margin=AUTO_SUPPORT_PLATE_MARGIN_MM,
        exclusions=(
            *common.exclusions,
            _keep_out(DEFAULT_PRIME_TOWER.footprint(*H2D_AUTO_SUPPORT_TOWER_ORIGIN), common),
        ),
    )


def _keep_out(bounds: tuple[float, float, float, float], build: BuildVolume) -> Exclusion:
    """Model-free rectangle: tower bounds grown by the tower clearance, clipped to the plate."""
    gap = AUTO_SUPPORT_TOWER_CLEARANCE_MM
    x0, y0, x1, y1 = bounds
    left, front = max(0.0, x0 - gap), max(0.0, y0 - gap)
    right, back = min(build.x, x1 + gap), min(build.y, y1 + gap)
    return Exclusion(left, front, right - left, back - front)


@dataclass(frozen=True)
class PlateGroup:
    """Designs that share plates; ``projected`` tries outline nesting when rectangles overflow."""

    title: str | None
    designs: list[Design]
    gap: float
    projected: bool = False


@dataclass
class PlatePlan:
    designs: list[Design]
    placements: list[PrintPlacement]
    plate_names: dict[int, str]
    plate_builds: dict[int, BuildVolume]
    group_gaps: dict[str | None, float]
    unfit: list[Design] = field(default_factory=list)
    projected_footprints: list[ProjectedFootprint | None] = field(default_factory=list)
    projected_clearances: dict[int, float] = field(default_factory=dict)
    projected_packing: dict[str | None, dict] = field(default_factory=dict)
    prime_tower: PrimeTower | None = None
    prime_tower_positions: dict[int, tuple[float, float]] = field(default_factory=dict)
    prime_tower_clearances: dict[int, float] = field(default_factory=dict)
    auto_roof_support: dict | None = None

    @property
    def plate_count(self) -> int:
        return max((placement.plate for placement in self.placements), default=-1) + 1


class _TowerCorner:
    """Bambu's default tower in one front corner, with models kept clear of its bounds."""

    tower = DEFAULT_PRIME_TOWER

    def __init__(self, build: BuildVolume, origin: tuple[float, float], reason: str) -> None:
        self.build, self.origin, self.reason = build, origin, reason
        self.bounds = self.tower.footprint(*origin)

    def supported_build(self) -> BuildVolume:
        build = self.build
        return BuildVolume(
            build.x,
            build.y,
            build.z,
            margin=build.margin,
            reserve_x=build.reserve_x,
            reserve_y=build.reserve_y,
            reserve_z=build.reserve_z,
            exclusions=(*build.exclusions, _keep_out(self.bounds, build)),
        )

    def record(self, plates: list[int], groups: list[str | None], gaps: list[float]) -> dict:
        titled = [group for group in groups if group is not None]
        return {
            "prime_tower_plates": [plate + 1 for plate in plates],
            **({"prime_tower_groups": titled} if titled else {}),
            "prime_tower_origin_mm": self.origin,
            "reserved_tower_bounds_mm": self.bounds,
            "model_clearance_to_tower_bounds_mm": AUTO_SUPPORT_TOWER_CLEARANCE_MM,
            "model_gap_on_tower_plates_mm": min(gaps),
            "reason": self.reason,
        }


class _H2DTowerCorner(_TowerCorner):
    def __init__(self) -> None:
        common = h2d_common_build()
        super().__init__(
            BuildVolume(
                common.x,
                common.y,
                common.z,
                margin=AUTO_SUPPORT_PLATE_MARGIN_MM,
                exclusions=common.exclusions,
            ),
            H2D_AUTO_SUPPORT_TOWER_ORIGIN,
            "PLA interface plates need a prime tower both nozzles reach; Bambu's default tower "
            "goes in the front-right corner and models keep clear of its reserved bounds",
        )

    def supported_build(self) -> BuildVolume:
        return h2d_auto_support_build()


class _FrontLeftTowerCorner(_TowerCorner):
    """Generic builds: the tower sits in the front-left corner of the usable area."""

    def __init__(self, build: BuildVolume) -> None:
        super().__init__(
            build,
            (build.margin + self.tower.left, build.margin + self.tower.front),
            "PLA interface plates need a prime tower; Bambu's default tower goes in the "
            "front-left corner of the usable area, so check that every nozzle reaches it",
        )
        x0, y0, x1, y1 = self.bounds
        usable_x = build.x - build.margin - build.reserve_x
        usable_y = build.y - build.margin - build.reserve_y
        if (
            x1 > usable_x
            or y1 > usable_y
            or any(
                x0 < area.x + area.width
                and area.x < x1
                and y0 < area.y + area.depth
                and area.y < y1
                for area in build.exclusions
            )
        ):
            raise ValueError(
                "auto roof support needs room for a prime tower at the front left of the build"
            )


def _tower_layout(build: BuildVolume) -> _TowerCorner:
    return _H2DTowerCorner() if build == h2d_common_build() else _FrontLeftTowerCorner(build)


def _projected_bounds(
    build: BuildVolume,
) -> tuple[tuple[float, float, float, float], tuple[tuple[float, float, float, float], ...]]:
    """Model area for outline nesting plus any partial-depth exclusions as obstacles."""
    x0, y0 = build.margin, build.margin
    x1 = build.x - build.margin - build.reserve_x
    y1 = build.y - build.margin - build.reserve_y
    obstacles = []
    for area in build.exclusions:
        full_depth = area.y <= 0 and area.y + area.depth >= build.y
        if full_depth and area.x <= 0:
            x0 = max(x0, area.x + area.width)
        elif full_depth and area.x + area.width >= build.x:
            x1 = min(x1, area.x)
        else:
            obstacles.append((area.x, area.y, area.x + area.width, area.y + area.depth))
    return (x0, y0, x1, y1), tuple(obstacles)


def plan_plates(
    groups: list[PlateGroup],
    build: BuildVolume,
    *,
    size,
    auto_roof_support: bool = False,
) -> PlatePlan:
    """Pack ordered groups onto consecutive plates, reserving prime towers for auto support.

    ``build`` is the base placement area (the H2D common reach or a generic usable area) and
    ``size`` returns each design's placed bounds. With ``auto_roof_support``, a group with any
    design that gets object support prints PLA, so its plates use the tower layout for that
    base area and add the support-foot allowance to the group gap. Designs that do not fit
    their group's area are returned in ``unfit`` for the caller to omit or reject.
    """
    tower = _tower_layout(build) if auto_roof_support else None
    plan = PlatePlan([], [], {}, {}, {}, prime_tower=tower.tower if tower else None)
    towered_plates, towered_groups, towered_gaps = set(), [], []
    for group in groups:
        positive("plate group gap", group.gap, zero=True)
        towered = tower is not None and any(needs_auto_support(d) for d in group.designs)
        gap = group.gap + AUTO_SUPPORT_FOOT_ALLOWANCE_MM if towered else group.gap
        group_build = tower.supported_build() if towered else build
        plan.group_gaps[group.title] = gap
        members = []
        for design in group.designs:
            if group_build.placement(size(design)) is None:
                plan.unfit.append(design)
            else:
                members.append(design)
        if not members:
            continue
        if towered:
            towered_groups.append(group.title)
            towered_gaps.append(gap)
        rectangular = pack_sizes(
            [size(design) for design in members], group_build, gap=gap, pack=True
        )
        packed, footprints, nested = rectangular, [None] * len(members), False
        if group.projected and max(p.plate for p in rectangular) > 0:
            candidate_footprints = []
            for design in members:
                vertices, faces, _ = checked_mesh(design.bambu_shape)
                candidate_footprints.append(projected_mesh_footprint(vertices, faces))
            search_gap = max(gap - H2D_FOOTPRINT_SEARCH_ALLOWANCE_MM, gap / 2)
            bounds, obstacles = _projected_bounds(group_build)
            try:
                packed = pack_projected_footprints(
                    candidate_footprints,
                    bounds,
                    gap=gap,
                    search_gap=search_gap,
                    obstacles=obstacles,
                )
            except ValueError as error:
                plan.projected_packing[group.title] = {
                    "status": "rectangle fallback",
                    "reason": str(error),
                }
            else:
                footprints, nested = candidate_footprints, True
                plan.projected_packing[group.title] = {
                    "status": "applied",
                    "minimum_projected_gap_mm": gap,
                    "search_gap_mm": search_gap,
                    "grid_mm": 1,
                    "maximum_candidate_positions": 4_000_000,
                    "maximum_order_attempts": 8,
                }
        elif group.projected:
            plan.projected_packing[group.title] = {
                "status": "rectangle retained",
                "reason": "the group already fits one plate at the requested bounds gap",
            }
        offset = plan.plate_count
        count = max(placement.plate for placement in packed) + 1
        for local in range(count):
            if group.title is not None:
                plan.plate_names[offset + local] = (
                    group.title if count == 1 else f"{group.title} {local + 1}"
                )
            plan.plate_builds[offset + local] = group_build
        if nested:
            plan.projected_clearances[offset] = gap
        for design, placement in zip(members, packed):
            plate = placement.plate + offset
            if towered and needs_auto_support(design):
                plan.prime_tower_positions[plate] = tower.origin
                plan.prime_tower_clearances[plate] = AUTO_SUPPORT_TOWER_CLEARANCE_MM
                towered_plates.add(plate)
            plan.designs.append(design)
            plan.placements.append(
                PrintPlacement(plate, placement.x, placement.y, placement.rotation)
            )
        plan.projected_footprints.extend(footprints)
    if tower is not None:
        plan.auto_roof_support = tower.record(
            sorted(towered_plates),
            towered_groups,
            towered_gaps or [next(iter(plan.group_gaps.values()), 0.0)],
        )
    return plan
