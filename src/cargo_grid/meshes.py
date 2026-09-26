"""Checked tessellation with explicit handling of kernel-scale degenerate faces."""

import struct
from collections import Counter
from pathlib import Path

import numpy as np
from build123d import Compound, Shape, Vector
from OCP.BRep import BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_REVERSED
from OCP.TopLoc import TopLoc_Location
from scipy.spatial import cKDTree


def _mesh_edges(faces):
    if not len(faces):
        return Counter(), Counter()
    faces = np.asarray(faces)
    edges = np.concatenate((faces[:, (0, 1)], faces[:, (1, 2)], faces[:, (2, 0)]))
    direction = np.where(edges[:, 0] < edges[:, 1], 1, -1)
    edges.sort(axis=1)
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    edges, direction = edges[order], direction[order]
    starts = np.r_[0, np.flatnonzero(np.any(edges[1:] != edges[:-1], axis=1)) + 1]
    counts = np.diff(np.r_[starts, len(edges)])
    orientation = np.add.reduceat(direction, starts)
    keys = [tuple(edge) for edge in edges[starts].tolist()]
    return (
        Counter(dict(zip(keys, counts.tolist()))),
        Counter(dict(zip(keys, orientation.tolist()))),
    )


def _edge_defects(faces, vertex_count: int) -> tuple[bool, bool]:
    """Return whether any edge is open, and whether any edge breaks the closed-manifold rule.

    These are the same questions ``checked_mesh`` asks of the ``_mesh_edges`` counters.
    Each undirected edge of indices below ``vertex_count`` gets one integer key instead of
    a dictionary entry.
    """
    if not len(faces):
        return False, False
    faces = np.asarray(faces, dtype=np.int64)
    if faces.min() < 0 or faces.max() >= vertex_count or vertex_count > 2**31:
        raise ValueError("edge check needs vertex indices in 0..vertex_count-1 below 2**31")
    start = np.concatenate((faces[:, 0], faces[:, 1], faces[:, 2]))
    end = np.concatenate((faces[:, 1], faces[:, 2], faces[:, 0]))
    direction = np.where(start < end, 1, -1)
    key = np.minimum(start, end) * vertex_count + np.maximum(start, end)
    order = np.argsort(key, kind="stable")
    key, direction = key[order], direction[order]
    starts = np.r_[0, np.flatnonzero(key[1:] != key[:-1]) + 1]
    counts = np.diff(np.r_[starts, len(key)])
    orientation = np.add.reduceat(direction, starts)
    return bool(np.any(counts == 1)), bool(np.any(counts != 2) or np.any(orientation))


def _close_cad_microtriangles(shape, vertices, faces, counts, orientation):
    """Close only three-edge mesher cracks supported by the unchanged CAD surface."""
    if any(n > 2 for n in counts.values()):
        return faces, []
    boundary = {edge for edge, n in counts.items() if n == 1}
    repairs = []
    added = []
    surface = None
    tolerance = Precision.Confusion_s()
    while boundary:
        a, b = next(iter(boundary))
        neighbors_a = {y if x == a else x for x, y in boundary if a in (x, y)}
        neighbors_b = {y if x == b else x for x, y in boundary if b in (x, y)}
        common = neighbors_a & neighbors_b
        if len(neighbors_a) != 2 or len(neighbors_b) != 2 or len(common) != 1:
            return faces, []
        c = common.pop()
        neighbors_c = {y if x == c else x for x, y in boundary if c in (x, y)}
        if neighbors_c != {a, b}:
            return faces, []
        triangle = [a, b, c] if orientation[(a, b)] < 0 else [b, a, c]
        if any(
            orientation[(min(x, y), max(x, y))] == (1 if x < y else -1)
            for x, y in zip(triangle, triangle[1:] + triangle[:1])
        ):
            return faces, []
        if np.any(np.all(np.sort(faces, axis=1) == np.sort(triangle), axis=1)):
            return faces, []
        p = vertices[triangle]
        lengths = np.linalg.norm(p - np.roll(p, -1, axis=0), axis=1)
        area = float(np.linalg.norm(np.cross(p[1] - p[0], p[2] - p[0])) / 2)
        perimeter = float(lengths.sum())
        if lengths.max() > 0.1 or not 1e-14 < area <= min(1e-7, perimeter * tolerance):
            return faces, []
        if surface is None:
            surface = Compound(shape.faces())
        samples = [
            (i * p[0] + j * p[1] + (4 - i - j) * p[2]) / 4 for i in range(5) for j in range(5 - i)
        ]
        deviation = max(surface.distance_to(Vector(*point)) for point in samples)
        if deviation > tolerance:
            return faces, []
        added.append(triangle)
        repairs.append(
            {
                "area_mm2": area,
                "perimeter_mm": perimeter,
                "maximum_edge_mm": float(lengths.max()),
                "max_sampled_cad_deviation_mm": deviation,
                "cad_tolerance_mm": tolerance,
                "surface_sample_count": len(samples),
                "vertices_moved": False,
            }
        )
        boundary.difference_update({(min(x, y), max(x, y)) for x, y in ((a, b), (b, c), (c, a))})
    return np.concatenate([faces, np.array(added)], axis=0) if added else faces, repairs


def checked_mesh(shape: Shape) -> tuple[np.ndarray, np.ndarray, dict]:
    mesher = BRepMesh_IncrementalMesh(shape.wrapped, 0.02, False, 0.1, True)
    mesher.Perform()
    if not mesher.IsDone():
        raise ValueError("OCCT tessellation did not finish")
    points, triangles, skipped = [], [], []
    point_count = 0
    for face in shape.faces():
        topods_face = face.wrapped
        location = TopLoc_Location()
        poly = BRep_Tool.Triangulation_s(topods_face, location)
        if poly is None:
            if face.area > 1e-10:
                raise ValueError(f"unmeshed nondegenerate face: area {face.area:g} mm2")
            skipped.append(face.area)
            continue
        node = poly.Node
        nodes = range(1, poly.NbNodes() + 1)
        # An identity location leaves gp_Pnt coordinates unchanged, so skip the no-op transform.
        if location.IsIdentity():
            face_points = [node(i).Coord() for i in nodes]
        else:
            transformation = location.Transformation()
            face_points = [node(i).Transformed(transformation).Coord() for i in nodes]
        if face_points:
            points.append(np.array(face_points))
        triangle = poly.Triangle
        face_triangles = [triangle(i).Get() for i in range(1, poly.NbTriangles() + 1)]
        if face_triangles:
            face_triangles = np.array(face_triangles, dtype=np.int64)
            if topods_face.Orientation() == TopAbs_REVERSED:
                face_triangles = face_triangles[:, (0, 2, 1)]
            triangles.append(face_triangles + (point_count - 1))
        point_count += len(face_points)
    vertices = np.concatenate(points) if points else np.array(points)
    # Join duplicate surface seams within the model kernel's vertex tolerance,
    # not the much larger mesh chord tolerance.
    quantized = np.round(vertices / 1e-6).astype(np.int64)
    _, indices, inverse = np.unique(quantized, axis=0, return_index=True, return_inverse=True)
    vertices = vertices[indices]
    faces = inverse[np.concatenate(triangles) if triangles else np.array(triangles)]
    kernel_tolerance = max(BRep_Tool.Tolerance_s(v.wrapped) for v in shape.vertices())
    # A Boolean may assign conservative topological tolerances without an
    # actual geometric gap. Never move mesh vertices by that entire allowance.
    weld_tolerance = min(1e-5, max(1e-6, 2 * kernel_tolerance))
    parents = list(range(len(vertices)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    pairs = cKDTree(vertices).query_pairs(weld_tolerance)
    for a, b in pairs:
        parents[root(b)] = root(a)
    # Only vertices named by a welded pair can have another root.
    remap = np.arange(len(vertices))
    for i in {index for pair in pairs for index in pair}:
        remap[i] = root(i)
    displacement = float(np.linalg.norm(vertices - vertices[remap], axis=1).max())
    if displacement > weld_tolerance + 1e-9:
        raise ValueError("transitive seam welding exceeds the geometric displacement budget")
    faces = remap[faces]
    faces = faces[
        (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 2] != faces[:, 0])
    ]
    has_boundary, defective = _edge_defects(faces, len(vertices))
    repairs = []
    if has_boundary:
        undirected, oriented = _mesh_edges(faces)
        faces, repairs = _close_cad_microtriangles(shape, vertices, faces, undirected, oriented)
        _, defective = _edge_defects(faces, len(vertices))
    if defective:
        undirected, _ = _mesh_edges(faces)
        invalid = [
            (vertices[a].tolist(), vertices[b].tolist(), n)
            for (a, b), n in undirected.items()
            if n != 2
        ]
        raise ValueError(
            f"export mesh is not a closed consistently oriented two-manifold: "
            f"{dict(Counter(undirected.values()))}; first boundaries {invalid[:3]}"
        )
    v = vertices[faces]
    area = np.linalg.norm(np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]), axis=1) / 2
    if np.any(area < 1e-14):
        raise ValueError("export mesh has zero-area triangles")
    volume = float(np.einsum("ij,ij->i", v[:, 0], np.cross(v[:, 1], v[:, 2])).sum() / 6)
    if volume <= 0:
        raise ValueError("export mesh has nonpositive signed volume")
    return (
        vertices,
        faces,
        {
            "closed_oriented_manifold": True,
            "triangles": len(faces),
            "chord_tolerance_mm": 0.02,
            "seam_weld_mm": weld_tolerance,
            "kernel_vertex_tolerance_mm": kernel_tolerance,
            "maximum_weld_displacement_mm": displacement,
            "unmeshed_kernel_sliver_areas_mm2": skipped,
            "cad_supported_microtriangle_repairs": repairs,
            "mesh_volume_mm3": volume,
        },
    )


def write_stl(path: Path, vertices: np.ndarray, faces: np.ndarray) -> None:
    with path.open("wb") as stream:
        stream.write(b"Cargo-Grid checked mesh".ljust(80, b"\0"))
        stream.write(struct.pack("<I", len(faces)))
        dtype = np.dtype(
            [("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attributes", "<u2")]
        )
        for start in range(0, len(faces), 65536):
            p = vertices[faces[start : start + 65536]]
            normals = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
            normals /= np.linalg.norm(normals, axis=1)[:, None]
            records = np.zeros(len(p), dtype=dtype)
            records["normal"] = normals
            records["vertices"] = p
            stream.write(records.tobytes())
