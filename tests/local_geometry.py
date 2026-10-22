"""Local geometry queries shared by tests that check contacts between large solids."""

from math import inf

from build123d import Box, Location, Part, Vector
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.gp import gp_Pnt
from OCP.TopAbs import TopAbs_IN


def prepared_contains(shapes, tolerance=1e-6):
    """Own bounds and exact classifiers after the case's shapes stop changing."""
    queries = []
    for shape in shapes:
        bounds = shape.bounding_box()
        queries.append(
            (tuple(bounds.min), tuple(bounds.max), BRepClass3d_SolidClassifier(shape.wrapped))
        )

    def contains(point):
        coordinates = tuple(Vector(point))
        for lower, upper, classifier in queries:
            if all(
                minimum - tolerance <= value <= maximum + tolerance
                for minimum, maximum, value in zip(lower, upper, coordinates)
            ):
                classifier.Perform(gp_Pnt(*coordinates), tolerance)
                if classifier.State() == TopAbs_IN or classifier.IsOnAFace():
                    return True
        return False

    return contains


def overlap_region(first, second, margin=1.0):
    """Return a box around where the two bounding boxes overlap, grown by ``margin``."""
    a, b = first.bounding_box(), second.bounding_box()
    lower = [max(low_a, low_b) - margin for low_a, low_b in zip(a.min, b.min)]
    upper = [min(high_a, high_b) + margin for high_a, high_b in zip(a.max, b.max)]
    if any(low >= high for low, high in zip(lower, upper)):
        return None
    center = tuple((low + high) / 2 for low, high in zip(lower, upper))
    return Box(*(high - low for low, high in zip(lower, upper))).moved(Location(center))


def clipped_pair(first, second, margin=1.0):
    """Retain the whole intersection and every pair of points closer than ``margin``."""
    region = overlap_region(first, second, margin)
    if region is None:
        return None
    pieces = []
    for shape in (first, second):
        common = shape.intersect(region)
        solids = common.solids() if common else []
        if not solids:
            return None
        pieces.append(Part(solids))
    return tuple(pieces)


def bounded_distance(first, second, margin=1.0):
    """Return the exact distance below ``margin``, otherwise at least ``margin``."""
    pieces = clipped_pair(first, second, margin)
    return inf if pieces is None else pieces[0].distance_to(pieces[1])


def through_open_area(shapes, x0, x1, y0, y1, step=0.1):
    """Area of the XY band ``[x0, x1] x [y0, y1]`` left open through the full height.

    The band minus the union of horizontal sections taken every ``step`` mm through the shapes
    clipped to the band. Sampling can only miss material between levels, so the result is an
    upper bound on the true opening; compare configurations sampled the same way.
    """
    from build123d import Face, Plane, Wire, section

    bounds = [shape.bounding_box() for shape in shapes]
    top = max(box.max.Z for box in bounds)
    center = ((x0 + x1) / 2, (y0 + y1) / 2, top / 2)
    clip = Box(x1 - x0, y1 - y0, top + 2).moved(Location(center))
    pieces = []
    for shape, box in zip(shapes, bounds, strict=True):
        if box.max.X < x0 or box.min.X > x1 or box.max.Y < y0 or box.min.Y > y1:
            continue
        common = shape.intersect(clip)
        pieces.extend(common.solids() if common else [])
    band = Face(Wire.make_polygon([(x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0)], close=True))
    count = max(2, round(top / step))
    # Flat walls give many identical sections; one cut per distinct face keeps this fast.
    faces = {}
    for piece in pieces:
        part = Part([piece])
        for index in range(count):
            z = top * (index + 0.5) / count
            for face in section(part, section_by=Plane.XY.offset(z)).faces():
                bounds = face.bounding_box()
                area = face.area
                key = (
                    round(area, 5),
                    len(face.edges()),
                    *(
                        round(v, 4)
                        for v in (bounds.min.X, bounds.min.Y, bounds.max.X, bounds.max.Y)
                    ),
                )
                if key not in faces:
                    faces[key] = (area, face.moved(Location((0, 0, -z))))
    remaining = band
    for _, face in sorted(faces.values(), key=lambda item: -item[0]):
        remaining = remaining.cut(face)
        if not remaining.faces():
            return 0.0
    return sum(face.area for face in remaining.faces())
