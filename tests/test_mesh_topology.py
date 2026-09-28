"""Independent edge-count and winding oracles, including deliberately broken meshes."""

from collections import Counter

import numpy as np
import pytest

from cargo_grid.meshes import _edge_defects, _mesh_edges


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


@pytest.mark.parametrize(
    "faces",
    [
        [],
        [[0, 1, 2]],
        [[0, 1, 2], [0, 1, 3]],
        [[0, 1, 2], [2, 1, 0]],
        [[0, 1, 2], [0, 1, 2], [2, 1, 0]],
        [[0, 0, 1]],
        [[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]],
        [[0, 1, 2], [0, 1, 3], [1, 2, 3], [2, 0, 3]],
        [[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3], [0, 2, 1], [0, 1, 2]],
    ],
)
def test_integer_edge_keys_answer_the_counter_defect_questions(faces):
    array = np.array(faces, dtype=np.int64).reshape(-1, 3)
    counts, winding = _mesh_edges(array)
    expected = (
        any(n == 1 for n in counts.values()),
        any(n != 2 for n in counts.values()) or any(winding.values()),
    )
    assert _edge_defects(array, int(array.max(initial=-1)) + 1) == expected


def test_integer_edge_keys_match_counters_on_random_meshes():
    rng = np.random.default_rng(1)
    closed = np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]])
    for _ in range(200):
        faces = np.concatenate([closed + 4 * k for k in range(rng.integers(1, 5))])
        if rng.random() < 0.7:
            faces = faces[rng.permutation(len(faces))[: rng.integers(1, len(faces) + 1)]]
        if rng.random() < 0.3:
            faces[rng.integers(len(faces))] = faces[rng.integers(len(faces))][::-1]
        counts, winding = _mesh_edges(faces)
        expected = (
            any(n == 1 for n in counts.values()),
            any(n != 2 for n in counts.values()) or any(winding.values()),
        )
        assert _edge_defects(faces, int(faces.max()) + 1) == expected


def test_integer_edge_keys_refuse_indices_outside_the_vertex_range():
    with pytest.raises(ValueError, match="vertex indices"):
        _edge_defects(np.array([[0, 1, 5]]), 5)
