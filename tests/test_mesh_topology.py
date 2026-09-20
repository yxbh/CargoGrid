"""Independent edge-count and winding oracles, including deliberately broken meshes."""

from collections import Counter

import numpy as np
import pytest

from cargo_grid.meshes import _mesh_edges


@pytest.mark.parametrize(
    "faces",
    [
        [],
        [[0, 1, 2]],
        [[0, 1, 2], [0, 1, 3]],
        [[0, 1, 2], [2, 1, 0]],
        [[0, 1, 2], [0, 1, 2], [2, 1, 0]],
        [[0, 0, 1]],
        [[2**40, 2**40 + 1, 2**40 + 2]],
        [[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]],
    ],
)
def test_edge_counts_and_signed_winding_match_independent_oracle(faces):
    array = np.array(faces, dtype=np.int64).reshape(-1, 3)
    before = array.copy()
    expected_counts, expected_winding = Counter(), Counter()
    for triangle in faces:
        for a, b in zip(triangle, triangle[1:] + triangle[:1]):
            key = tuple(sorted((a, b)))
            expected_counts[key] += 1
            expected_winding[key] += 1 if a < b else -1
    counts, winding = _mesh_edges(array)
    assert counts == expected_counts
    assert winding == expected_winding
    assert counts[-1, -2] == winding[-1, -2] == 0
    assert np.array_equal(array, before)


def test_edge_topology_does_not_depend_on_triangle_order():
    faces = np.random.default_rng(0).integers(0, 100, size=(1000, 3))
    counts, winding = _mesh_edges(faces)
    assert _mesh_edges(faces[::-1]) == (counts, winding)
    reversed_counts, reversed_winding = _mesh_edges(faces[:, ::-1])
    assert reversed_counts == counts
    for edge in winding:
        assert reversed_winding[edge] == (-winding[edge] if edge[0] != edge[1] else winding[edge])
