"""Keep slow files together while balancing runner allocations by measured cost."""

COSTS = {
    "test_x2d_catalogue.py": 292,
    "test_footprint_packing.py": 114,
    "test_auto_roof_support.py": 250,
    "test_h2d_catalogue.py": 220,
    "test_zeekr_extras.py": 180,
    "test_zeekr_contours.py": 226,
    "test_ramp.py": 49,
    "test_adjustable_stop.py": 54,
    "test_edge_seams.py": 54,
    "test_edge_variants.py": 38,
    "test_vertical_tile_bracket.py": 17,
    "test_solid_bottom.py": 13,
    "test_print_orientation.py": 8,
    "test_pull_handles.py": 7,
}


def partition_files(paths: set[str], count: int) -> list[set[str]]:
    if count < 1:
        raise ValueError("partition count must be positive")
    allocations = [set() for _ in range(count)]
    totals = [0] * count

    def cost(path):
        return COSTS.get(path.rsplit("/", 1)[-1], 200)

    for path in sorted(paths, key=lambda path: (-cost(path), path)):
        index = min(range(count), key=lambda index: (totals[index], index))
        allocations[index].add(path)
        totals[index] += cost(path)
    return allocations
