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
# tower that both H2D nozzles reach (X 25..325). Bambu makes tower width and style project-wide,
# so one compact 28 mm rectangular tower with a fixed 3 mm brim serves every plate. It starts
# near the front of a reserved right-hand column (X 284..325, the whole plate depth) and Bambu
# grows it toward the back; models on those plates stay at X <= 276.5, so support feet keep clear
# of the purge line. The column fits beside the 246 mm-wide tiles with a 5-cell side, which
# already span the full common depth.
H2D_AUTO_SUPPORT_TOWER = PrimeTower(width=28, depth=300)
H2D_AUTO_SUPPORT_TOWER_ORIGIN = (293.0, 10.0)
H2D_AUTO_SUPPORT_MODEL_MAX_X = 276.5
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
    """H2D common reach for plates that carry the right-hand prime tower column."""
    return BuildVolume(
        350,
        320,
        320,
        margin=AUTO_SUPPORT_PLATE_MARGIN_MM,
        exclusions=(
            Exclusion(0, 0, 30, 320),
            Exclusion(H2D_AUTO_SUPPORT_MODEL_MAX_X, 0, 350 - H2D_AUTO_SUPPORT_MODEL_MAX_X, 320),
        ),
    )


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
    auto_roof_support: dict | None = None

    @property
    def plate_count(self) -> int:
        return max((placement.plate for placement in self.placements), default=-1) + 1


class _TowerLayout:
    tower: PrimeTower
    origin: tuple[float, float]

    def supported_build(self, gap: float) -> BuildVolume:
        raise NotImplementedError

    def record(self, plates: list[int], groups: list[str | None], gaps: list[float]) -> dict:
        raise NotImplementedError


class _H2DTowerColumn(_TowerLayout):
    """Fixed tower in a right-hand column that both H2D nozzles reach."""

    tower = H2D_AUTO_SUPPORT_TOWER
    origin = H2D_AUTO_SUPPORT_TOWER_ORIGIN

    def supported_build(self, gap: float) -> BuildVolume:
        return h2d_auto_support_build()

    def record(self, plates, groups, gaps) -> dict:
        return {
            "prime_tower_plates": [plate + 1 for plate in plates],
            "prime_tower_groups": groups,
            "model_max_x_on_tower_plates_mm": H2D_AUTO_SUPPORT_MODEL_MAX_X,
            "model_gap_on_tower_plates_mm": min(gaps),
            "reason": (
                "PLA interface plates need a prime tower both nozzles reach; the "
                "tower column and its 4 mm clearance are kept free of models"
            ),
        }


class _LeftTowerColumn(_TowerLayout):
    """Tower along the left edge of a generic usable area, as deep as that area allows."""

    def __init__(self, build: BuildVolume) -> None:
        brim = H2D_AUTO_SUPPORT_TOWER.brim
        usable_depth = build.y - 2 * build.margin - build.reserve_y
        if usable_depth <= 2 * brim + 1:
            raise ValueError(
                "auto roof support needs room for a prime tower at the left of the build"
            )
        self.build = build
        self.tower = PrimeTower(
            width=H2D_AUTO_SUPPORT_TOWER.width, depth=usable_depth - 2 * brim - 0.5
        )
        self.origin = (build.margin + self.tower.purge_lead, build.margin + 2 * brim + 0.5)
        x0, y0, self.right, y1 = self.tower.footprint(*self.origin)
        if any(
            x0 < area.x + area.width
            and area.x < self.right
            and y0 < area.y + area.depth
            and area.y < y1
            for area in build.exclusions
        ):
            raise ValueError(
                "auto roof support needs room for a prime tower at the left of the build"
            )

    def strip(self, gap: float) -> float:
        return min(self.build.x, self.right + gap)

    def supported_build(self, gap: float) -> BuildVolume:
        build = self.build
        return BuildVolume(
            build.x,
            build.y,
            build.z,
            margin=build.margin,
            reserve_x=build.reserve_x,
            reserve_y=build.reserve_y,
            reserve_z=build.reserve_z,
            exclusions=(*build.exclusions, Exclusion(0, 0, self.strip(gap), build.y)),
        )

    def record(self, plates, groups, gaps) -> dict:
        titled = [group for group in groups if group is not None]
        return {
            "prime_tower_plates": [plate + 1 for plate in plates],
            **({"prime_tower_groups": titled} if titled else {}),
            "model_min_x_mm": self.strip(min(gaps)),
            "model_gap_mm": min(gaps),
            "reason": (
                "PLA interface plates need a prime tower; it sits at the left of the usable "
                "area, so check that every nozzle of your printer reaches it"
            ),
        }


def _tower_layout(build: BuildVolume) -> _TowerLayout:
    return _H2DTowerColumn() if build == h2d_common_build() else _LeftTowerColumn(build)


def _projected_bounds(build: BuildVolume) -> tuple[float, float, float, float]:
    """Model area for outline nesting; only full-depth side exclusions are supported."""
    x0, y0 = build.margin, build.margin
    x1 = build.x - build.margin - build.reserve_x
    y1 = build.y - build.margin - build.reserve_y
    for area in build.exclusions:
        if area.y > 0 or area.y + area.depth < build.y:
            raise ValueError("projected-footprint packing needs full-depth side exclusions")
        if area.x <= 0:
            x0 = max(x0, area.x + area.width)
        elif area.x + area.width >= build.x:
            x1 = min(x1, area.x)
        else:
            raise ValueError("projected-footprint packing needs full-depth side exclusions")
    return x0, y0, x1, y1


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
        group_build = tower.supported_build(gap) if towered else build
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
            try:
                packed = pack_projected_footprints(
                    candidate_footprints,
                    _projected_bounds(group_build),
                    gap=gap,
                    search_gap=search_gap,
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
