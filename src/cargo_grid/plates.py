"""Plate planning shared by catalogues and collections: named groups, prime towers and packing.

Callers decide what goes together (ordered, titled groups with their own model gap); this module
decides which plates need a prime tower for auto roof support, reserves the tower column and the
support-foot allowance, packs each group and returns the plates, names and tower positions.
"""

from dataclasses import asdict, dataclass, field, replace
from math import floor, pi, sqrt

from cargo_grid.footprints import (
    PACKING_SEARCH_ATTEMPTS,
    PACKING_SEARCH_SEED,
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
    pack_sizes,
    packing_score,
)
from cargo_grid.parameters import BuildVolume, Exclusion, positive
from cargo_grid.printers import DualNozzlePrinter, printer_for_common_build

# Auto roof support prints a PLA interface, so every supported plate reserves a prime tower.
DEFAULT_PRIME_TOWER = PrimeTower()
# Models keep this far from the reserved tower bounds: Bambu's automatic support foot grows up
# to about 5 mm past a supported part.
AUTO_SUPPORT_TOWER_CLEARANCE_MM = 6.5
# Bambu's automatic support-foot expansion grows the first support layer up to about 5 mm past
# the part outline, so plates with supported parts add this to the model clearance and keep
# parts a further 1 mm from the front and back plate edges.
AUTO_SUPPORT_FOOT_ALLOWANCE_MM = 4.0
AUTO_SUPPORT_PLATE_MARGIN_MM = 6.0
FOOTPRINT_SEARCH_ALLOWANCE_MM = 0.75
# When Bambu Studio 02.08.02.61 opens a project it moves any tower whose estimated square is
# not at least this far inside the area every nozzle reaches, so towers are written in range.
BAMBU_TOWER_MARGIN_MM = 15.0
# Local slices put the real tower up to 0.6 mm behind its estimated square and brim.
TOWER_BACK_ALLOWANCE_MM = 1.0
# Models beside the H2D tower keep this much less than the support-foot clearance so the 246 mm
# wide tiles with a 5-cell side still fit on tower plates. Every published plate passed Bambu's
# path-conflict check with it at 0.32 and 0.24 mm layers; at 0.2 mm the larger tower conflicted
# with a rotated tile and an edge strip whose supported sides faced it.
TOWER_LEFT_CLEARANCE_MM = 1.5
# Perimeter plates can put first-layer tower paths 4.914 mm ahead of its stored origin.
RECTANGULAR_TOWER_FRONT_ALLOWANCE_MM = 5.0
# At 0.32 mm, native line-rounded rectangular depth needs another millimetre behind the brim.
RECTANGULAR_TOWER_BACK_ALLOWANCE_MM = {0.24: 1.0, 0.32: 2.0}
RECTANGULAR_TOWER_WIPE_REACH_MM = 9.75
SIDES = ("left", "front", "right", "back")


@dataclass(frozen=True)
class BambuTowerEstimate:
    """Bambu Studio 02.08.02.61's pre-slice tower estimate for one PETG and one PLA.

    It matches the shape Bambu uses to decide whether a stored tower position is in range.
    Defaults reproduce the H2D positions: 30 mm³ purge per filament, 10 mm of 1.75 mm filament
    per nozzle change, a 150% infill gap and 8 mm ribs. X2D uses 45 mm³, the larger of its
    PETG/PLA prime volumes, for both filaments just as Bambu's estimate does. A specified
    width selects a rib-free rectangle whose depth retains that volume and height minimum.
    """

    layer_height: float
    prime_volume_mm3: float = 30.0
    nozzle_change_mm3: float = 10 * pi * 1.75**2 / 4
    infill_gap: float = 1.5
    rib_width_mm: float = 8.0
    width_mm: float | None = None

    def __post_init__(self) -> None:
        positive("layer height", self.layer_height)
        if self.width_mm is not None:
            positive("tower width", self.width_mm)
            if self.width_mm < 2:
                raise ValueError("Bambu tower width must be at least 2 mm")
            if self.layer_height not in RECTANGULAR_TOWER_BACK_ALLOWANCE_MM:
                raise ValueError("rectangular towers require 0.24 or 0.32 mm layers")

    def size_mm(self, height: float) -> tuple[float, float]:
        """GUI load-range estimate; a specified width selects the rib-free rectangle."""
        if self.width_mm is None:
            side = self.side_mm(height)
            return side, side
        volume = 2 * self.prime_volume_mm3 + self.nozzle_change_mm3
        depth = volume / (self.layer_height * self.width_mm) * self.infill_gap
        return self.width_mm, max(_bambu_min_tower_depth(height), depth)

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
    """A family whose rectangle and projected layouts are compared without mixing groups."""

    title: str | None
    designs: list[Design]
    gap: float
    projected: bool = False
    build: BuildVolume | None = None
    tower_clearance: TowerClearance | None = None
    front_left_tower: bool = False
    max_layout_attempts: int = PACKING_SEARCH_ATTEMPTS
    # Rectangle-packed designs that need another plate area or tower. They join the designs on
    # the group's last plate, under the tail's layout, when all of them fit on that one plate;
    # otherwise they follow on plates of their own. The plates keep the group's title.
    tail: "PlateGroup | None" = None


@dataclass
class _GroupPack:
    """A group's designs on local plates, before the plan numbers them."""

    sources: list[Design]
    designs: list[Design]
    placements: list[PrintPlacement]
    footprints: list[ProjectedFootprint | None]
    build: BuildVolume
    reservation: TowerReservation | None
    gap: float
    unfit: list[Design]
    nested: bool = False
    record: dict | None = None

    @property
    def plate_count(self) -> int:
        return max((placement.plate for placement in self.placements), default=-1) + 1

    def split(self, plate: int) -> tuple["_GroupPack", list[Design]]:
        """The plates before ``plate``, and the source designs on ``plate``."""
        keep = [i for i, placement in enumerate(self.placements) if placement.plate < plate]
        carried = [s for s, p in zip(self.sources, self.placements) if p.plate == plate]
        earlier = replace(
            self,
            **{
                name: [getattr(self, name)[i] for i in keep]
                for name in ("sources", "designs", "placements", "footprints")
            },
        )
        return earlier, carried


def diagonal_fit(design: Design, build: BuildVolume) -> Design | None:
    """Keep the existing print pose and try a half-quarter-turn only when necessary."""
    candidate = replace(
        design,
        recommended_print_rotation_z=(design.recommended_print_rotation_z or 0) + 45,
        apply_orientation_to_bambu=True,
    )
    return candidate if build.placement(candidate.bambu_size) is not None else None


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

    def reserve(self, height: float, *, front_left: bool = False) -> TowerReservation:
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


class _DualNozzleTowerLayout(_TowerLayout):
    """Front-right by default, with a front-left rectangle for measured catalogue groups."""

    def __init__(self, printer: DualNozzlePrinter, estimate: BambuTowerEstimate) -> None:
        common = printer.common_build()
        super().__init__(
            BuildVolume(
                common.x,
                common.y,
                common.z,
                margin=AUTO_SUPPORT_PLATE_MARGIN_MM,
                exclusions=common.exclusions,
            )
        )
        self.printer = printer
        self.estimate = estimate
        left, right = printer.shared_x_mm
        self.reason = (
            "PLA interface plates need a prime tower both nozzles reach; Bambu's default tower goes "
            "in the front-right corner where Bambu Studio keeps it (its estimated square 15 mm inside "
            f"X {left:g}..{right:g} and Y >= 15), and models keep clear of the estimated tower and brim"
        )
        if estimate.width_mm is not None:
            self.reason = (
                "X2D catalogue plates use a rectangular tower on the right for ordinary groups "
                "and the left for plates with measured four-cell parts, preserving prime volumes "
                "and automatic support feet; reserve its estimated depth, automatic brim and "
                "nozzle-change wipe reach; plates with four-cell parts use 1.5 mm clearance on "
                "its right, and other plates keep 6.5 mm on every side"
            )

    def reserve(self, height: float, *, front_left: bool = False) -> TowerReservation:
        width, depth = self.estimate.size_mm(height)
        brim = self.estimate.brim_mm(height)
        if self.estimate.width_mm is not None:
            x = (
                self.printer.shared_x_mm[0] + BAMBU_TOWER_MARGIN_MM
                if front_left
                else floor((self.printer.shared_x_mm[1] - BAMBU_TOWER_MARGIN_MM - width) * 100)
                / 100
            )
            y = BAMBU_TOWER_MARGIN_MM
            reach = PrimeTower(
                max(brim + 1.0, RECTANGULAR_TOWER_WIPE_REACH_MM),
                brim + RECTANGULAR_TOWER_FRONT_ALLOWANCE_MM,
                width + max(brim + 1.0, RECTANGULAR_TOWER_WIPE_REACH_MM),
                depth + brim + RECTANGULAR_TOWER_BACK_ALLOWANCE_MM[self.estimate.layer_height],
            )
            clearance = TowerClearance.uniform(AUTO_SUPPORT_TOWER_CLEARANCE_MM)
            return self._with_keep_out((x, y), reach, clearance)
        if front_left:
            raise ValueError("front-left dual-nozzle towers require a rectangular catalogue")
        limit = self.printer.shared_x_mm[1]
        # Round down so Bambu's in-range test (x + margin + side <= limit) keeps the position.
        x = floor((limit - BAMBU_TOWER_MARGIN_MM - width) * 100) / 100
        y = BAMBU_TOWER_MARGIN_MM
        reach = PrimeTower(brim, brim, limit - x, depth + brim + TOWER_BACK_ALLOWANCE_MM)
        clearance = TowerClearance(
            TOWER_LEFT_CLEARANCE_MM,
            AUTO_SUPPORT_TOWER_CLEARANCE_MM,
            AUTO_SUPPORT_TOWER_CLEARANCE_MM,
            AUTO_SUPPORT_TOWER_CLEARANCE_MM,
        )
        return self._with_keep_out((x, y), reach, clearance)


def dual_nozzle_tower_reservation(
    printer: DualNozzlePrinter, layer_height_mm: float, height_mm: float
) -> TowerReservation:
    """The same tower estimate and keep-out for catalogue, extras and adjustable-stop jobs."""
    return _DualNozzleTowerLayout(
        printer,
        BambuTowerEstimate(layer_height_mm, prime_volume_mm3=printer.tower_prime_volume_mm3),
    ).reserve(height_mm)


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

    def reserve(self, height: float, *, front_left: bool = False) -> TowerReservation:
        return self.reservation


def _tower_layout(
    build: BuildVolume, layer_height_mm: float | None, width_mm: float | None = None
) -> _TowerLayout:
    printer = printer_for_common_build(build)
    if printer is not None:
        if layer_height_mm is None:
            raise ValueError(
                f"{printer.name} auto roof support needs the layer height for the tower position"
            )
        return _DualNozzleTowerLayout(
            printer,
            BambuTowerEstimate(
                layer_height_mm,
                prime_volume_mm3=printer.tower_prime_volume_mm3,
                width_mm=width_mm,
            ),
        )
    return _FrontLeftTowerLayout(build)


def _projected_bounds(
    build: BuildVolume,
) -> tuple[tuple[float, float, float, float], tuple[tuple[float, float, float, float], ...]]:
    """Model area plus every exclusion, including strict folded full-depth boundaries."""
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
        obstacles.append((area.x, area.y, area.x + area.width, area.y + area.depth))
    return (x0, y0, x1, y1), tuple(obstacles)


def plan_plates(
    groups: list[PlateGroup],
    build: BuildVolume,
    *,
    size,
    auto_roof_support: bool = False,
    layer_height_mm: float | None = None,
    diagonal_fallback: bool = False,
    tower_width_mm: float | None = None,
) -> PlatePlan:
    """Pack ordered groups onto consecutive plates, reserving prime towers for auto support.

    ``build`` is the base placement area (a printer's common reach or a generic usable area) and
    ``size`` returns each design's placed bounds. With ``auto_roof_support``, every nonempty
    group inherits global support and may print PLA, so its plates use the tower layout for that
    base area and add the support-foot allowance to the group gap. Designs that do not fit
    their group's area are returned in ``unfit`` for the caller to omit or reject. On named printers
    the tower position follows Bambu's estimate for ``layer_height_mm`` and the group's
    tallest design, so Bambu Studio keeps it when it opens the project. A group's ``tail`` is
    packed with the designs on its last plate as one group, so that plate uses the tail's layout.
    """
    printer = printer_for_common_build(build)
    if tower_width_mm is not None and (
        not auto_roof_support or printer is None or printer.catalogue_support is None
    ):
        raise ValueError("rectangular prime towers require X2D Auto-support planning")
    tower = _tower_layout(build, layer_height_mm, tower_width_mm) if auto_roof_support else None
    plan = PlatePlan([], [], {}, {}, {})
    towered_plates: dict[int, TowerReservation] = {}
    towered_groups, towered_gaps = [], []
    measured_sizes: dict[int, tuple[float, float, float]] = {}

    def pack(group: PlateGroup) -> _GroupPack:
        positive("plate group gap", group.gap, zero=True)
        for design in group.designs:
            if id(design) not in measured_sizes:
                measured_sizes[id(design)] = size(design)
        towered = tower is not None and bool(group.designs)
        gap = group.gap + AUTO_SUPPORT_FOOT_ALLOWANCE_MM if towered else group.gap
        reservation = (
            tower.reserve(
                max(measured_sizes[id(design)][2] for design in group.designs),
                front_left=group.front_left_tower,
            )
            if towered
            else None
        )
        if reservation and group.tower_clearance is not None:
            reservation = tower._with_keep_out(
                reservation.origin, reservation.reach, group.tower_clearance
            )
        group_build = reservation.build if reservation else build
        if group.build is not None:
            group_build = group.build
            if reservation:
                group_build = replace(
                    group_build,
                    exclusions=(
                        *group_build.exclusions,
                        _keep_out(reservation.bounds, reservation.clearance, group_build),
                    ),
                )
                reservation = replace(reservation, build=group_build)
        result = _GroupPack([], [], [], [], group_build, reservation, gap, [])
        for design in group.designs:
            if group_build.placement(measured_sizes[id(design)]) is None:
                rotated = diagonal_fit(design, group_build) if diagonal_fallback else None
                if rotated is None:
                    result.unfit.append(design)
                    continue
                result.designs.append(rotated)
            else:
                result.designs.append(design)
            result.sources.append(design)
        members = result.designs
        if not members:
            return result

        sizes = [
            design.bambu_size
            if design.recommended_print_rotation_z is not None
            else measured_sizes[id(design)]
            for design in members
        ]
        rectangular = pack_sizes(sizes, group_build, gap=gap, pack=True)
        packed, footprints, nested = rectangular, [None] * len(members), False
        rectangle_score = packing_score(rectangular, sizes)
        if group.projected and rectangle_score[0] > 1:
            candidate_footprints = []
            for design in members:
                vertices, faces, _ = checked_mesh(design.bambu_shape)
                candidate_footprints.append(projected_mesh_footprint(vertices, faces))
            search_gap = max(gap - FOOTPRINT_SEARCH_ALLOWANCE_MM, gap / 2)
            bounds, obstacles = _projected_bounds(group_build)
            attempts = (
                group.max_layout_attempts
                if rectangle_score[0] > 2
                else min(group.max_layout_attempts, PACKING_SEARCH_ATTEMPTS)
            )
            try:
                candidate = pack_projected_footprints(
                    candidate_footprints,
                    bounds,
                    bounding_sizes=[size[:2] for size in sizes],
                    max_layout_attempts=attempts,
                    gap=gap,
                    search_gap=search_gap,
                    obstacles=obstacles,
                )
            except ValueError as error:
                result.record = {
                    "status": "rectangle fallback",
                    "reason": str(error),
                }
            else:
                if packing_score(candidate, sizes) < rectangle_score:
                    packed, footprints, nested = candidate, candidate_footprints, True
                result.record = {
                    "status": "applied" if nested else "rectangle preferred",
                    "rectangle_plates": max(p.plate for p in rectangular) + 1,
                    "projected_plates": max(p.plate for p in candidate) + 1,
                    "minimum_projected_gap_mm": gap,
                    "search_gap_mm": search_gap,
                    "grid_mm": 1,
                    "maximum_candidate_positions": 4_000_000,
                    "maximum_layout_attempts": attempts,
                    "random_seed": PACKING_SEARCH_SEED,
                }
        result.placements, result.footprints, result.nested = packed, footprints, nested
        return result

    for group in groups:
        if group.tail is not None and group.tail.projected:
            raise ValueError("plate group tails use rectangle packing")
        packed = pack(group)
        plan.group_gaps[group.title] = packed.gap
        plan.unfit.extend(packed.unfit)
        if packed.record is not None:
            plan.projected_packing[group.title] = packed.record
        segments = [packed]
        if group.tail is not None and group.tail.designs:
            earlier, carried = packed.split(packed.plate_count - 1)
            merged = (
                pack(replace(group.tail, designs=[*carried, *group.tail.designs]))
                if carried
                else None
            )
            if merged is not None and merged.plate_count == 1 and not merged.unfit:
                segments = [earlier, merged]
            else:
                tail = pack(group.tail)
                plan.unfit.extend(tail.unfit)
                segments.append(tail)
        segments = [segment for segment in segments if segment.placements]
        if any(segment.reservation is not None for segment in segments):
            towered_groups.append(group.title)
            towered_gaps.extend(s.gap for s in segments if s.reservation is not None)
        start = offset = plan.plate_count
        count = sum(segment.plate_count for segment in segments)
        for segment in segments:
            for local in range(segment.plate_count):
                if group.title is not None:
                    plan.plate_names[offset + local] = (
                        group.title if count == 1 else f"{group.title} {offset + local - start + 1}"
                    )
                plan.plate_builds[offset + local] = segment.build
            if segment.nested:
                plan.projected_clearances.update(
                    {plate + offset: segment.gap for plate in range(segment.plate_count)}
                )
            reservation = segment.reservation
            for design, placement in zip(segment.designs, segment.placements):
                plate = placement.plate + offset
                if reservation is not None:
                    plan.prime_tower_positions[plate] = reservation.origin
                    plan.prime_tower_reaches[plate] = reservation.reach
                    plan.prime_tower_clearances[plate] = reservation.clearance
                    towered_plates[plate] = reservation
                plan.designs.append(design)
                plan.placements.append(
                    PrintPlacement(plate, placement.x, placement.y, placement.rotation)
                )
            plan.projected_footprints.extend(segment.footprints)
            offset += segment.plate_count
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
