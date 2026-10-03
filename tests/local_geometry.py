"""Local geometry queries shared by tests that check contacts between large solids."""

from math import inf

from build123d import Box, Location, Part


def overlap_region(first, second, margin=1.0):
    """Return a box around where the two bounding boxes overlap, grown by ``margin``."""
    a, b = first.bounding_box(), second.bounding_box()
    lower = [max(low_a, low_b) - margin for low_a, low_b in zip(a.min, b.min)]
    upper = [min(high_a, high_b) + margin for high_a, high_b in zip(a.max, b.max)]
    if any(low >= high for low, high in zip(lower, upper)):
        return None
    center = tuple((low + high) / 2 for low, high in zip(lower, upper))
    return Box(*(high - low for low, high in zip(lower, upper))).moved(Location(center))


def bounded_distance(first, second, margin=1.0):
    """Return the exact distance when it is below ``margin``, otherwise at least ``margin``.

    Closest points closer than ``margin`` lie inside both bounding boxes grown by ``margin``,
    so clipping both shapes to that region keeps them while dropping distant faces.
    """
    region = overlap_region(first, second, margin)
    if region is None:
        return inf
    pieces = []
    for shape in (first, second):
        common = shape.intersect(region)
        solids = common.solids() if common else []
        if not solids:
            return inf
        pieces.append(Part(solids))
    return pieces[0].distance_to(pieces[1])


def through_open_area(shapes, x0, x1, y0, y1, step=0.1):
    """Area of the XY band ``[x0, x1] x [y0, y1]`` left open through the full height.

    The band minus the union of horizontal sections taken every ``step`` mm through the shapes
    clipped to the band. Sampling can only miss material between levels, so the result is an
    upper bound on the true opening; compare configurations sampled the same way.
    """
    from build123d import Face, Plane, Wire, section

    top = max(shape.bounding_box().max.Z for shape in shapes)
    center = ((x0 + x1) / 2, (y0 + y1) / 2, top / 2)
    clip = Box(x1 - x0, y1 - y0, top + 2).moved(Location(center))
    pieces = []
    for shape in shapes:
        bounds = shape.bounding_box()
        if bounds.max.X < x0 or bounds.min.X > x1 or bounds.max.Y < y0 or bounds.min.Y > y1:
            continue
        common = shape.intersect(clip)
        pieces.extend(common.solids() if common else [])
    band = Face(Wire.make_polygon([(x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0)], close=True))
    count = max(2, round(top / step))
    # Flat walls give many identical sections; one cut per distinct face keeps this fast.
    faces = {}
    for piece in pieces:
        for index in range(count):
            z = top * (index + 0.5) / count
            for face in section(Part([piece]), section_by=Plane.XY.offset(z)).faces():
                bounds = face.bounding_box()
                key = (
                    round(face.area, 5),
                    len(face.edges()),
                    *(
                        round(v, 4)
                        for v in (bounds.min.X, bounds.min.Y, bounds.max.X, bounds.max.Y)
                    ),
                )
                faces.setdefault(key, face.moved(Location((0, 0, -z))))
    remaining = band
    for face in sorted(faces.values(), key=lambda face: -face.area):
        remaining = remaining.cut(face)
        if not remaining.faces():
            return 0.0
    return sum(face.area for face in remaining.faces())
