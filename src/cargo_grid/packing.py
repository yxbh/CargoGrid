"""Deterministic first-fit rectangle packing, not an optimal nesting solver."""

from dataclasses import dataclass

from cargo_grid.parameters import BuildVolume, Exclusion, positive


@dataclass(frozen=True)
class PrimeTower:
    """Space kept free for Bambu's default prime tower; no tower settings are written.

    Each plate only chooses the tower origin (``wipe_tower_x``/``wipe_tower_y``). The fields
    are how far tower extrusion may reach from that origin. The defaults are used for generic
    builds: Bambu Studio 02.08.02.61 sliced the default H2D rib-wall tower on 0.32 mm PETG/PLA
    plates as a 25.5 to 28.3 mm square whose walls, wipe lines and automatic first-layer brim
    reached up to 7.05 mm left, 6.39 mm in front, 34.05 mm right and 30.83 mm behind the
    origin; the defaults add a little to each. H2D plans size each plate's reach from Bambu's
    own tower estimate instead (``plates.BambuTowerEstimate``).
    """

    left: float = 7.5
    front: float = 7.0
    right: float = 34.5
    back: float = 31.5

    def __post_init__(self) -> None:
        positive("prime tower left reach", self.left, zero=True)
        positive("prime tower front reach", self.front, zero=True)
        positive("prime tower right reach", self.right)
        positive("prime tower back reach", self.back)

    def footprint(self, x: float, y: float) -> tuple[float, float, float, float]:
        """Reserved extrusion bounds (x0, y0, x1, y1) for a tower whose origin is (x, y)."""
        return (x - self.left, y - self.front, x + self.right, y + self.back)


@dataclass(frozen=True)
class TowerClearance:
    """Minimum distance from each side of a reserved tower area to any model's bounds."""

    left: float
    front: float
    right: float
    back: float

    def __post_init__(self) -> None:
        for side in ("left", "front", "right", "back"):
            positive(f"prime tower {side} clearance", getattr(self, side), zero=True)

    @classmethod
    def uniform(cls, value: float) -> "TowerClearance":
        return cls(value, value, value, value)

    @property
    def minimum(self) -> float:
        return min(self.left, self.front, self.right, self.back)

    def grow(self, bounds: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = bounds
        return (x0 - self.left, y0 - self.front, x1 + self.right, y1 + self.back)


def h2d_common_build() -> BuildVolume:
    """H2D shared nozzle reach with a further 5 mm model inset."""
    return BuildVolume(
        350,
        320,
        320,
        margin=5,
        exclusions=(Exclusion(0, 0, 30, 320), Exclusion(320, 0, 30, 320)),
    )


@dataclass(frozen=True)
class PrintPlacement:
    plate: int
    x: float
    y: float
    rotation: int


def pack_sizes(
    sizes: list[tuple[float, float, float]],
    build: BuildVolume,
    *,
    gap: float = 2,
    pack: bool = True,
) -> list[PrintPlacement]:
    positive("part gap", gap, zero=True)
    occupied: list[list[tuple[float, float, float, float]]] = []
    results: dict[int, PrintPlacement] = {}
    order = (
        sorted(range(len(sizes)), key=lambda i: (-sizes[i][0] * sizes[i][1], i))
        if pack
        else range(len(sizes))
    )
    for index in order:
        size = sizes[index]
        if build.placement(size) is None:
            raise ValueError(f"part {index} bounds {size} do not fit the usable envelope")
        chosen = None
        candidates = range(len(occupied) + 1) if pack else (len(occupied),)
        for plate in candidates:
            rectangles = occupied[plate] if plate < len(occupied) else []
            xs = {
                build.margin,
                *(x + w + gap for x, y, w, d in rectangles),
                *(a.x + a.width for a in build.exclusions),
            }
            ys = {
                build.margin,
                *(y + d + gap for x, y, w, d in rectangles),
                *(a.y + a.depth for a in build.exclusions),
            }
            for angle in (0, 90):
                w, d = size[:2] if angle == 0 else size[1::-1]
                for y in sorted(ys):
                    for x in sorted(xs):
                        if not build.contains_box(x, y, w, d, size[2]):
                            continue
                        if any(
                            x < rx + rw + gap - 1e-6
                            and x + w + gap > rx + 1e-6
                            and y < ry + rd + gap - 1e-6
                            and y + d + gap > ry + 1e-6
                            for rx, ry, rw, rd in rectangles
                        ):
                            continue
                        chosen = PrintPlacement(plate, x, y, angle)
                        if plate == len(occupied):
                            occupied.append([])
                        occupied[plate].append((x, y, w, d))
                        break
                    if chosen:
                        break
                if chosen:
                    break
            if chosen:
                break
        if chosen is None:
            raise ValueError(
                f"could not pack part {index}; exclusions or reservations block placement"
            )
        results[index] = chosen
    first_fit = [results[i] for i in range(len(sizes))]
    if not pack or not sizes:
        return first_fit
    compact = _compact_sizes(sizes, build, gap)
    if max(placement.plate for placement in compact) < max(
        placement.plate for placement in first_fit
    ):
        return compact
    return first_fit


def _compact_sizes(
    sizes: list[tuple[float, float, float]],
    build: BuildVolume,
    gap: float,
) -> list[PrintPlacement]:
    occupied: list[list[tuple[float, float, float, float]]] = []
    results: dict[int, PrintPlacement] = {}
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][0] * sizes[i][1], i))
    for index in order:
        size = sizes[index]
        options = []
        for plate in range(len(occupied) + 1):
            rectangles = occupied[plate] if plate < len(occupied) else []
            xs = {
                build.margin,
                *(x + width + gap for x, y, width, depth in rectangles),
                *(area.x + area.width for area in build.exclusions),
            }
            ys = {
                build.margin,
                *(y + depth + gap for x, y, width, depth in rectangles),
                *(area.y + area.depth for area in build.exclusions),
            }
            for angle in (0, 90):
                width, depth = size[:2] if angle == 0 else size[1::-1]
                for y in sorted(ys):
                    for x in sorted(xs):
                        if not build.contains_box(x, y, width, depth, size[2]):
                            continue
                        if any(
                            x < other_x + other_width + gap - 1e-6
                            and x + width + gap > other_x + 1e-6
                            and y < other_y + other_depth + gap - 1e-6
                            and y + depth + gap > other_y + 1e-6
                            for other_x, other_y, other_width, other_depth in rectangles
                        ):
                            continue
                        extent_x = (
                            max(
                                (
                                    x + width,
                                    *(
                                        other_x + other_width
                                        for other_x, _, other_width, _ in rectangles
                                    ),
                                )
                            )
                            - build.margin
                        )
                        extent_y = (
                            max(
                                (
                                    y + depth,
                                    *(
                                        other_y + other_depth
                                        for _, other_y, _, other_depth in rectangles
                                    ),
                                )
                            )
                            - build.margin
                        )
                        score = (
                            plate,
                            max(extent_x, extent_y),
                            extent_x * extent_y,
                            extent_y,
                            extent_x,
                            y,
                            x,
                            angle,
                        )
                        options.append((score, plate, x, y, width, depth, angle))
        if not options:
            raise ValueError(
                f"could not pack part {index}; exclusions or reservations block placement"
            )
        _, plate, x, y, width, depth, angle = min(options)
        if plate == len(occupied):
            occupied.append([])
        occupied[plate].append((x, y, width, depth))
        results[index] = PrintPlacement(plate, x, y, angle)
    return [results[i] for i in range(len(sizes))]
