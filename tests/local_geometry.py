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
