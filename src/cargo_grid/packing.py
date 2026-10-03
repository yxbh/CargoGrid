"""Deterministic first-fit rectangle packing, not an optimal nesting solver."""

from dataclasses import dataclass

from cargo_grid.parameters import BuildVolume, Exclusion, positive


@dataclass(frozen=True)
class PrimeTower:
    """Rectangular Bambu prime tower (rib wall off, no rotation) and its reserved envelope.

    ``width`` is a project-wide Bambu setting; each plate only chooses the tower origin, the
    lower-left corner of the tower body. The brim is set explicitly because Bambu's automatic
    tower brim grows with tower height. Local Bambu Studio slices of PETG/PLA H2D plates show a
    PLA purge line starting 9 mm left of the origin and a depth that Bambu sizes from the
    plate's height and purge volume, so ``depth`` reserves room for it to grow toward
    positive Y. A rotated tower is not offered: Bambu's conflict check treats the
    tower as unrotated and rejects models beside the actual rotated column.
    """

    width: float
    depth: float
    brim: float = 3.0
    purge_lead: float = 9.0

    def __post_init__(self) -> None:
        positive("prime tower width", self.width)
        positive("prime tower depth", self.depth)
        positive("prime tower brim", self.brim, zero=True)
        positive("prime tower purge lead", self.purge_lead, zero=True)

    def footprint(self, x: float, y: float) -> tuple[float, float, float, float]:
        """Reserved extrusion bounds (x0, y0, x1, y1) for a tower whose origin is (x, y).

        Sliced towers also reached 1 mm past the brim on the right and twice the brim plus
        0.5 mm below the origin, so the envelope includes those wipe and brim paths.
        """
        return (
            x - self.purge_lead,
            y - 2 * self.brim - 0.5,
            x + self.width + self.brim + 1,
            y + self.depth,
        )

    def bambu_settings(self) -> dict[str, str]:
        return {
            "enable_prime_tower": "1",
            "prime_tower_rib_wall": "0",
            "prime_tower_width": f"{self.width:g}",
            "prime_tower_brim_width": f"{self.brim:g}",
            "wipe_tower_rotation_angle": "0",
        }


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
