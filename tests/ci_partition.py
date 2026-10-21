"""Keep slow files together while balancing runner allocations by measured cost."""

COSTS = {
    "test_x2d_catalogue.py": 1527,
    "test_h2d_catalogue.py": 1506,
    "test_auto_roof_support.py": 766,
    "test_zeekr_contours.py": 653,
    "test_zeekr_extras.py": 312,
    "test_ramp.py": 202,
    "test_adjustable_stop.py": 159,
    "test_edge_variants.py": 137,
    "test_edge_seams.py": 136,
    "test_vertical_tile_bracket.py": 74,
    "test_pull_handles.py": 38,
    "test_solid_bottom.py": 33,
    "test_print_orientation.py": 18,
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
