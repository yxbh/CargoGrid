"""Feature-aware bounds and reach checks for Bambu's generated extrusion."""

import re
from math import atan2, cos, hypot, pi, sin

from cargo_grid.printers import DualNozzlePrinter

SUPPORT_REACH_ALLOWANCE_MM = 0.5
SUPPORT_FEATURES = frozenset(("Support", "Support interface", "Support transition"))


def _extend_bounds(bounds: tuple[float, ...] | None, box: tuple[float, ...]) -> tuple[float, ...]:
    if bounds is None:
        return box
    return tuple(min(a, b) for a, b in zip(bounds[:3], box[:3])) + tuple(
        max(a, b) for a, b in zip(bounds[3:], box[3:])
    )


def feature_bounds(gcode: str) -> dict[str, tuple[float, ...]]:
    """Include line width and XY arc extrema, excluding startup/custom macros."""
    x = y = z = e = width = 0.0
    absolute_e, absolute_xyz = False, True
    role = "unknown"
    bounds: dict[str, tuple[float, ...]] = {}
    for line in gcode.splitlines():
        if line.startswith("; FEATURE:"):
            role = line.split(":", 1)[1].strip()
        elif line.startswith("; LINE_WIDTH:"):
            width = float(line.split(":", 1)[1])
        command = line.split(";", 1)[0].strip()
        operation = command.split(maxsplit=1)[0] if command else ""
        if operation in ("M82", "M83"):
            absolute_e = operation == "M82"
        elif operation in ("G90", "G91"):
            absolute_xyz = operation == "G90"
        fields = {
            key: float(value)
            for key, value in re.findall(r"([XYZEIJ])([+-]?(?:\d+(?:\.\d*)?|\.\d+))", command)
        }
        if operation == "G92":
            x, y, z, e = (fields.get(key, value) for key, value in zip("XYZE", (x, y, z, e)))
        elif operation in ("G0", "G1", "G2", "G3"):
            nx, ny, nz = (
                fields.get(key, value) if absolute_xyz else value + fields.get(key, 0)
                for key, value in zip("XYZ", (x, y, z))
            )
            extrusion = fields.get("E", e if absolute_e else 0)
            delta = extrusion - e if absolute_e else extrusion
            if (
                delta > 0
                and role not in ("unknown", "Custom")
                and (nx != x or ny != y or "I" in fields or "J" in fields)
            ):
                if width <= 0:
                    raise ValueError("generated extrusion has no positive line width")
                points = [(x, y), (nx, ny)]
                if operation in ("G2", "G3"):
                    cx, cy = x + fields.get("I", 0), y + fields.get("J", 0)
                    radius = hypot(x - cx, y - cy)
                    start, end = atan2(y - cy, x - cx), atan2(ny - cy, nx - cx)
                    direction = -1 if operation == "G2" else 1
                    sweep = (direction * (end - start)) % (2 * pi)
                    if (x, y) == (nx, ny):
                        sweep = 2 * pi
                    for angle in (0, pi / 2, pi, 3 * pi / 2):
                        if (direction * (angle - start)) % (2 * pi) <= sweep + 1e-9:
                            points.append((cx + radius * cos(angle), cy + radius * sin(angle)))
                box = (
                    min(p[0] for p in points) - width / 2,
                    min(p[1] for p in points) - width / 2,
                    min(z, nz),
                    max(p[0] for p in points) + width / 2,
                    max(p[1] for p in points) + width / 2,
                    max(z, nz),
                )
                bounds[role] = _extend_bounds(bounds.get(role), box)
            x, y, z = nx, ny, nz
            if absolute_e:
                e = extrusion
    return bounds


def reach_problem(
    gcode: str,
    printer: DualNozzlePrinter,
    reserved_tower: tuple[float, ...] | None = None,
) -> str | None:
    features = feature_bounds(gcode)
    if not features:
        return "sliced plate has no generated extrusion"
    for feature, bounds in features.items():
        x0, y0, z0, x1, y1, z1 = bounds
        allowance = SUPPORT_REACH_ALLOWANCE_MM if feature in SUPPORT_FEATURES else 0
        if (
            x0 < 0
            or x1 > printer.build.x
            or y0 < 0
            or y1 > printer.build.y
            or z0 < 0
            or z1 > printer.build.z
            or z1 > printer.shared_height_mm
            or x0 < printer.shared_x_mm[0] - allowance
            or x1 > printer.shared_x_mm[1] + allowance
        ):
            return (
                f"{feature} all-layer extrusion bounds {bounds} "
                f"exceed {printer.name} common reach/bed"
            )
    tower = features.get("Prime tower")
    if reserved_tower is not None and tower is not None:
        if (
            tower[0] < reserved_tower[0]
            or tower[1] < reserved_tower[1]
            or tower[3] > reserved_tower[2]
            or tower[4] > reserved_tower[3]
        ):
            return (
                f"prime-tower extrusion bounds {tower} exceed reserved footprint {reserved_tower}"
            )
    return None
