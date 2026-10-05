"""Checks shared by tests of planned prime-tower positions and their model-free areas."""

from cargo_grid.plates import BambuTowerEstimate


def placed_bounds(design, placement, size):
    width, depth = size[:2] if placement.rotation == 0 else size[1::-1]
    return placement.x, placement.y, placement.x + width, placement.y + depth


def assert_towers_stay_where_bambu_keeps_them(job, layer_height, size=lambda d: d.bambu_size):
    """Each H2D tower is in Bambu's range, covers its estimate and keeps models clear."""
    estimate = BambuTowerEstimate(layer_height)
    plates = {}
    for design, placement in zip(job.designs, job.print_placements):
        plates.setdefault(placement.plate, []).append((design, placement))
    for plate, (x, y) in job.prime_tower_positions.items():
        tallest = max(size(design)[2] for design, _ in plates[plate])
        side, brim = estimate.side_mm(tallest), estimate.brim_mm(tallest)
        # Bambu Studio keeps a stored position when its estimate is 15 mm inside X 25..325.
        assert y == 15 and 40 <= x and x + 15 + side <= 325 + 1e-9
        x0, y0, x1, y1 = job.tower_bounds(plate)
        assert x0 <= x - brim + 1e-9 and y0 <= y - brim + 1e-9
        assert y1 >= y + side + brim - 1e-9 and x1 == 325
        k0, l0, k1, l1 = job.tower_clearance(plate).grow((x0, y0, x1, y1))
        for design, placement in plates[plate]:
            a0, b0, a1, b1 = placed_bounds(design, placement, size(design))
            assert a1 <= k0 + 1e-6 or b0 >= l1 - 1e-6 or a0 >= k1 - 1e-6, design.name
