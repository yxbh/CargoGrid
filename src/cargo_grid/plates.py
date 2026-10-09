"""Plate planning shared by catalogues and collections: named groups, prime towers and packing.

Callers decide what goes together (ordered, titled groups with their own model gap); this module
decides which plates need a prime tower for auto roof support, reserves the tower column and the
support-foot allowance, packs each group and returns the plates, names and tower positions.
"""

from dataclasses import asdict, dataclass, field
from math import floor, pi, sqrt

from cargo_grid.footprints import (
    ProjectedFootprint,
    pack_projected_footprints,
    projected_mesh_footprint,
)
from cargo_grid.jobs import Design
from cargo_grid.meshes import checked_mesh
from cargo_grid.packing import (
    PrimeTower,
    PrintPlacement,
    TowerClearance,
    h2d_common_build,
    pack_sizes,
)
from cargo_grid.parameters import BuildVolume, Exclusion, positive

# Auto roof support prints a PLA interface, so every plate with supported parts needs a prime
# tower. Bambu's default tower is used as it is; each plate only gets a tower position and a
# model-free area around the reserved tower bounds.
DEFAULT_PRIME_TOWER = PrimeTower()
# Models keep this far from the reserved tower bounds: Bambu's automatic support foot grows up
# to about 5 mm past a supported part.
AUTO_SUPPORT_TOWER_CLEARANCE_MM = 6.5
# Bambu's automatic support-foot expansion grows the first support layer up to about 5 mm past
# the part outline, so plates with supported parts add this to the model clearance and keep
# parts a further 1 mm from the front and back plate edges.
AUTO_SUPPORT_FOOT_ALLOWANCE_MM = 4.0
AUTO_SUPPORT_PLATE_MARGIN_MM = 6.0
H2D_FOOTPRINT_SEARCH_ALLOWANCE_MM = 0.75
# When Bambu Studio 02.08.02.61 opens a project it moves any tower whose estimated square is
# not at least this far inside the area every nozzle reaches, so towers are written in range.
BAMBU_TOWER_MARGIN_MM = 15.0
H2D_SHARED_REACH_X_MM = (25.0, 325.0)
# Local slices put the real tower up to 0.6 mm behind its estimated square and brim.
TOWER_BACK_ALLOWANCE_MM = 1.0
# Models beside the H2D tower keep this much less than the support-foot clearance so the 246 mm
# wide tiles with a 5-cell side still fit on tower plates. Every published plate passed Bambu's
# path-conflict check with it at 0.32 and 0.24 mm layers; at 0.2 mm the larger tower conflicted
# with a rotated tile and an edge strip whose supported sides faced it.
H2D_TOWER_LEFT_CLEARANCE_MM = 1.5
SIDES = ("left", "front", "right", "back")


@dataclass(frozen=True)
class BambuTowerEstimate:
    """Bambu Studio 02.08.02.61's pre-slice rib-wall tower estimate for one PETG and one PLA.

    It is the square Bambu uses to decide whether a stored tower position is in range. The
    inputs are the H2D PETG/PLA Basic values that reproduce the positions Bambu Studio wrote
    into saved 0.32 and 0.2 mm projects: 30 mm³ purge per filament, 10 mm of 1.75 mm filament
    per nozzle change, a 150% infill gap and 8 mm ribs.
    """

    layer_height: float
    prime_volume_mm3: float = 30.0
    nozzle_change_mm3: float = 10 * pi * 1.75**2 / 4
    infill_gap: float = 1.5
    rib_width_mm: float = 8.0

    def __post_init__(self) -> None:
        positive("layer height", self.layer_height)

    def side_mm(self, height: float) -> float:
        """Estimated square side for a plate whose tallest object is ``height`` mm."""
        volume = 2 * self.prime_volume_mm3 + self.nozzle_change_mm3
        volume_depth = sqrt(volume / self.layer_height * self.infill_gap)
        depth = max(_bambu_min_tower_depth(height), volume_depth)
        rib = min(self.rib_width_mm, depth / 2)
        return rib / sqrt(2) + depth

    @staticmethod
    def brim_mm(height: float) -> float:
        """Bambu's automatic tower brim: 8 mm at 100 mm tall and above, less below."""
        return 8.0 * min(height, 100.0) / 100.0


def _bambu_min_tower_depth(height: float) -> float:
    """Bambu's minimum tower depth for stability, interpolated by tower height."""
    table = ((5.0, 5.0), (100.0, 20.0), (250.0, 40.0), (350.0, 60.0))
    if height <= table[0][0]:
        return table[0][1]
    for (h0, d0), (h1, d1) in zip(table, table[1:]):
        if height <= h1:
            return d0 + (height - h0) * (d1 - d0) / (h1 - h0)
    return table[-1][1]


def needs_auto_support(design: Design) -> bool:
    """Compatibility helper: every design inherits global Auto support when it is enabled."""
    return True


def _keep_out(
    bounds: tuple[float, float, float, float], clearance: TowerClearance, build: BuildVolume
) -> Exclusion:
    """Model-free rectangle: tower bounds grown by the tower clearance, clipped to the plate."""
    x0, y0, x1, y1 = clearance.grow(bounds)
    left, front = max(0.0, x0), max(0.0, y0)
    right, back = min(build.x, x1), min(build.y, y1)
    return Exclusion(left, front, right - left, back - front)


@dataclass(frozen=True)
class TowerReservation:
    origin: tuple[float, float]
    reach: PrimeTower
    clearance: TowerClearance
    build: BuildVolume

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.reach.footprint(*self.origin)


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
    prime_tower_reaches: dict[int, PrimeTower] = field(default_factory=dict)
    prime_tower_clearances: dict[int, TowerClearance] = field(default_factory=dict)
    auto_roof_support: dict | None = None

    @property
    def plate_count(self) -> int:
        return max((placement.plate for placement in self.placements), default=-1) + 1


class _TowerLayout:
    """Where Bambu's default tower goes on plates with supported parts, and what it reserves."""

    reason = ""

    def __init__(self, build: BuildVolume) -> None:
        self.build = build

    def reserve(self, height: float) -> TowerReservation:
        raise NotImplementedError

    def _with_keep_out(
        self, origin: tuple[float, float], reach: PrimeTower, clearance: TowerClearance
    ) -> TowerReservation:
        build = self.build
        keep_out = _keep_out(reach.footprint(*origin), clearance, build)
        supported = BuildVolume(
            build.x,
            build.y,
            build.z,
            margin=build.margin,
            reserve_x=build.reserve_x,
            reserve_y=build.reserve_y,
            reserve_z=build.reserve_z,
            exclusions=(*build.exclusions, keep_out),
        )
        return TowerReservation(origin, reach, clearance, supported)

    def record(
        self,
        reservations: dict[int, TowerReservation],
        groups: list[str | None],
        gaps: list[float],
    ) -> dict:
        titled = [group for group in groups if group is not None]
        clearances = {item.clearance for item in reservations.values()}
        return {
            "prime_tower_plates": [plate + 1 for plate in sorted(reservations)],
            **({"prime_tower_groups": titled} if titled else {}),
            "prime_tower_origins_mm": {
                plate + 1: item.origin for plate, item in sorted(reservations.items())
            },
            "reserved_tower_bounds_mm": {
                plate + 1: item.bounds for plate, item in sorted(reservations.items())
            },
            "model_clearance_to_tower_bounds_mm": (
                asdict(next(iter(reservations.values())).clearance)
                if reservations and len(clearances) == 1
                else {plate + 1: asdict(item.clearance) for plate, item in reservations.items()}
            ),
            "model_gap_on_tower_plates_mm": min(gaps),
            "reason": self.reason,
        }


class _H2DTowerLayout(_TowerLayout):
    """Front-right H2D tower at the position Bambu Studio keeps when it opens the project."""

    reason = (
        "PLA interface plates need a prime tower both nozzles reach; Bambu's default tower goes "
        "in the front-right corner where Bambu Studio keeps it (its estimated square 15 mm inside "
        "X 25..325 and Y >= 15), and models keep clear of the estimated tower and brim"
    )

    def __init__(self, estimate: BambuTowerEstimate) -> None:
        common = h2d_common_build()
        super().__init__(
            BuildVolume(
                common.x,
                common.y,
                common.z,
                margin=AUTO_SUPPORT_PLATE_MARGIN_MM,
                exclusions=common.exclusions,
            )
        )
        self.estimate = estimate

    def reserve(self, height: float) -> TowerReservation:
        side = self.estimate.side_mm(height)
        brim = self.estimate.brim_mm(height)
        limit = H2D_SHARED_REACH_X_MM[1]
        # Round down so Bambu's in-range test (x + margin + side <= limit) keeps the position.
        x = floor((limit - BAMBU_TOWER_MARGIN_MM - side) * 100) / 100
        y = BAMBU_TOWER_MARGIN_MM
        reach = PrimeTower(brim, brim, limit - x, side + brim + TOWER_BACK_ALLOWANCE_MM)
        clearance = TowerClearance(
            H2D_TOWER_LEFT_CLEARANCE_MM,
            AUTO_SUPPORT_TOWER_CLEARANCE_MM,
            AUTO_SUPPORT_TOWER_CLEARANCE_MM,
            AUTO_SUPPORT_TOWER_CLEARANCE_MM,
        )
        return self._with_keep_out((x, y), reach, clearance)


class _FrontLeftTowerLayout(_TowerLayout):
    """Generic builds: the tower sits in the front-left corner of the usable area."""

    reason = (
        "PLA interface plates need a prime tower; Bambu's default tower goes in the front-left "
        "corner of the usable area, at least 15 mm from the bed edges where Bambu Studio keeps "
        "it, so check that every nozzle reaches it"
    )

    def __init__(self, build: BuildVolume) -> None:
        super().__init__(build)
        tower = DEFAULT_PRIME_TOWER
        origin = (
            max(BAMBU_TOWER_MARGIN_MM, build.margin + tower.left),
            max(BAMBU_TOWER_MARGIN_MM, build.margin + tower.front),
        )
        self.reservation = self._with_keep_out(
            origin, tower, TowerClearance.uniform(AUTO_SUPPORT_TOWER_CLEARANCE_MM)
        )
        x0, y0, x1, y1 = self.reservation.bounds
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

    def reserve(self, height: float) -> TowerReservation:
        return self.reservation


def _tower_layout(build: BuildVolume, layer_height_mm: float | None) -> _TowerLayout:
    if build == h2d_common_build():
        if layer_height_mm is None:
            raise ValueError("H2D auto roof support needs the layer height for the tower position")
        return _H2DTowerLayout(BambuTowerEstimate(layer_height_mm))
    return _FrontLeftTowerLayout(build)


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
    layer_height_mm: float | None = None,
) -> PlatePlan:
    """Pack ordered groups onto consecutive plates, reserving prime towers for auto support.

    ``build`` is the base placement area (the H2D common reach or a generic usable area) and
    ``size`` returns each design's placed bounds. With ``auto_roof_support``, every nonempty
    group inherits global support and may print PLA, so its plates use the tower layout for that
    base area and add the support-foot allowance to the group gap. Designs that do not fit
    their group's area are returned in ``unfit`` for the caller to omit or reject. On the H2D
    the tower position follows Bambu's estimate for ``layer_height_mm`` and the group's
    tallest design, so Bambu Studio keeps it when it opens the project.
    """
    tower = _tower_layout(build, layer_height_mm) if auto_roof_support else None
    plan = PlatePlan([], [], {}, {}, {})
    towered_plates: dict[int, TowerReservation] = {}
    towered_groups, towered_gaps = [], []
    for group in groups:
        positive("plate group gap", group.gap, zero=True)
        towered = tower is not None and bool(group.designs)
        gap = group.gap + AUTO_SUPPORT_FOOT_ALLOWANCE_MM if towered else group.gap
        reservation = (
            tower.reserve(max(size(design)[2] for design in group.designs)) if towered else None
        )
        group_build = reservation.build if reservation else build
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
            if towered:
                plan.prime_tower_positions[plate] = reservation.origin
                plan.prime_tower_reaches[plate] = reservation.reach
                plan.prime_tower_clearances[plate] = reservation.clearance
                towered_plates[plate] = reservation
            plan.designs.append(design)
            plan.placements.append(
                PrintPlacement(plate, placement.x, placement.y, placement.rotation)
            )
        plan.projected_footprints.extend(footprints)
    if tower is not None:
        reaches = list(plan.prime_tower_reaches.values())
        plan.prime_tower = (
            PrimeTower(*(max(getattr(r, side) for r in reaches) for side in SIDES))
            if reaches
            else DEFAULT_PRIME_TOWER
        )
        plan.auto_roof_support = tower.record(
            towered_plates,
            towered_groups,
            towered_gaps or [next(iter(plan.group_gaps.values()), 0.0)],
        )
    return plan
