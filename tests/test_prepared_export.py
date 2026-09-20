"""Owned export state, one checked mesh, and independent serialized-artifact checks."""

import json
from collections import Counter
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
import pytest
from build123d import Axis, Box, Location, Shape, Vector, import_step
from OCP.BRep import BRep_Tool
from OCP.TopLoc import TopLoc_Location

from cargo_grid import BuildVolume, export, prepared
from cargo_grid.export import BambuSettings, Material, export_job, write_3mf
from cargo_grid.jobs import Design, Job

BAMBU = BambuSettings((Material("PETG", "PETG", "#637b70"),), 0.4, 0.2)


def stl_triangles(path):
    data = path.read_bytes()
    count = int.from_bytes(data[80:84], "little")
    assert len(data) == 84 + count * 50
    records = np.frombuffer(
        data,
        dtype=np.dtype([("normal", "<f4", (3,)), ("points", "<f4", (3, 3)), ("attr", "<u2")]),
        offset=84,
    )
    assert len(records) == count
    return records["points"].astype(float)


def archive_triangles(path):
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
    result = []
    for mesh in root.findall(".//{*}mesh"):
        points = np.array(
            [[float(v.get(k)) for k in ("x", "y", "z")] for v in mesh.find("{*}vertices")]
        )
        faces = np.array(
            [[int(t.get(k)) for k in ("v1", "v2", "v3")] for t in mesh.find("{*}triangles")]
        )
        result.append(points[faces])
    return result, root


def assert_serialized_closed(triangles, expected_volume):
    counts, directions = Counter(), Counter()
    for triangle in triangles:
        points = [tuple(point) for point in triangle]
        assert len(set(points)) == 3
        for a, b in zip(points, points[1:] + points[:1]):
            key = tuple(sorted((a, b)))
            counts[key] += 1
            directions[key] += 1 if a < b else -1
    assert set(counts.values()) == {2}
    assert not any(directions.values())
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    assert np.all(np.linalg.norm(normals, axis=1) > 0)
    volume = np.sum(triangles[:, 0] * np.cross(triangles[:, 1], triangles[:, 2])) / 6
    assert volume == pytest.approx(expected_volume, rel=1e-7)


@pytest.mark.parametrize("bambu", [None, BAMBU], ids=["core", "bambu"])
@pytest.mark.parametrize("stl", [False, True], ids=["no-stl", "stl"])
def test_one_owned_checked_mesh_supplies_all_copies_and_output_formats(
    tmp_path, monkeypatch, bambu, stl
):
    source = Box(20, 30, 4).moved(Location((7, -5, 3)))
    design = Design(
        "posed",
        source,
        {},
        quantity=3,
        recommended_print_rotation_x=37,
        recommended_print_rotation_y=-19,
        apply_orientation_to_bambu=True,
    )
    expected = source.rotate(Axis.X, 37).rotate(Axis.Y, -19) if bambu else source
    prepared.checked_mesh(source)
    before_nodes = [
        BRep_Tool.Triangulation_s(face.wrapped, TopLoc_Location()).NbNodes()
        for face in source.faces()
    ]
    meshed = []
    original_mesh, original_bounds = prepared.checked_mesh, Shape.bounding_box

    def mesh(shape):
        assert not shape.wrapped.IsPartner(source.wrapped)
        meshed.append(shape)
        return original_mesh(shape)

    def bounds(shape, *args, **kwargs):
        assert all(not shape.wrapped.IsPartner(item.wrapped) for item in meshed)
        return original_bounds(shape, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(prepared, "checked_mesh", mesh)
        patch.setattr(Shape, "bounding_box", bounds)
        path = export_job(
            Job([design], BuildVolume(100, 100, 100), "part"),
            tmp_path / "job",
            stl=stl,
            bambu=bambu,
        )
    assert len(meshed) == 1
    assert all(
        BRep_Tool.Triangulation_s(face.wrapped, TopLoc_Location()) is None
        for face in meshed[0].faces()
    )
    assert [
        BRep_Tool.Triangulation_s(face.wrapped, TopLoc_Location()).NbNodes()
        for face in source.faces()
    ] == before_nodes
    manifest = json.loads(path.read_text())
    assert manifest["designs"][0]["quantity"] == 3
    assert len(manifest["export"]["plates"]) == 3
    if stl:
        triangles = stl_triangles(path.parent / "posed.stl")
        assert_serialized_closed(triangles, 2400)
        assert all(source.distance_to(Vector(*p)) < 1e-5 for p in triangles.reshape(-1, 3))
    else:
        assert not (path.parent / "posed.stl").exists()
    meshes, root = archive_triangles(path.parent / "job.3mf")
    assert len(meshes) == 1
    assert len(root.findall("./{*}build/{*}item")) == 3
    assert_serialized_closed(meshes[0], 2400)
    assert all(expected.distance_to(Vector(*p)) < 1e-5 for p in meshes[0].reshape(-1, 3))
    assert tuple(import_step(path.parent / "posed.step").bounding_box().min) == pytest.approx(
        tuple(source.bounding_box().min), abs=1e-5
    )


def test_standalone_archive_reuses_shared_source_but_not_its_distinct_poses(tmp_path, monkeypatch):
    shape = Box(20, 30, 4)
    designs = [
        Design("source", shape, {}),
        Design(
            "turned", shape, {}, recommended_print_rotation_x=90, apply_orientation_to_bambu=True
        ),
    ]
    calls = []
    original = prepared.checked_mesh

    def checked(source):
        calls.append(source)
        return original(source)

    monkeypatch.setattr(prepared, "checked_mesh", checked)
    path = tmp_path / "poses.3mf"
    write_3mf(Job(designs, BuildVolume(100, 100, 100), "part"), path, bambu=BAMBU)
    assert len(calls) == 1
    meshes, _ = archive_triangles(path)
    assert len(meshes) == 2
    sizes = [np.ptp(triangles.reshape(-1, 3), axis=0) for triangles in meshes]
    assert sizes[0] == pytest.approx((20, 30, 4))
    assert sizes[1] == pytest.approx((20, 4, 30))
    for triangles in meshes:
        assert_serialized_closed(triangles, 2400)


def test_unique_design_meshes_are_released_after_their_last_batch(tmp_path, monkeypatch):
    live = set()
    calls = []
    original_mesh = prepared.checked_mesh
    original_release = prepared.PreparedShape.release_mesh

    def mesh(shape):
        assert not live, "the preceding design's arrays must not accumulate"
        live.add(id(shape))
        calls.append(shape)
        return original_mesh(shape)

    def release(geometry):
        original_release(geometry)
        live.discard(id(geometry.shape))

    monkeypatch.setattr(prepared, "checked_mesh", mesh)
    monkeypatch.setattr(prepared.PreparedShape, "release_mesh", release)
    designs = [Design(f"block_{n}", Box(20 + n, 30, 4), {}, quantity=2) for n in range(8)]
    report = json.loads(
        export_job(
            Job(designs, BuildVolume(100, 100, 100), "part"), tmp_path / "stream"
        ).read_text()
    )
    assert len(calls) == len(designs)
    assert not live
    assert len(report["designs"]) == 8
    assert len(report["export"]["plates"]) == 16
    assert len(list((tmp_path / "stream").glob("*.stl"))) == 8


def test_export_snapshot_is_independent_and_next_request_observes_mutation(tmp_path, monkeypatch):
    design = Design("block", Box(20, 30, 4), {"note": "initial"})
    job = Job([design], BuildVolume(100, 100, 100), "part")
    original = export.export_step

    def write(shape, *args, **kwargs):
        assert not shape.wrapped.IsPartner(design.shape.wrapped)
        design.shape = Box(40, 10, 6).moved(Location((7, 11, 13)))
        design.quantity = 2
        design.parameters["note"] = "changed"
        return original(shape, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(export, "export_step", write)
        first = json.loads(export_job(job, tmp_path / "first").read_text())
    second = json.loads(export_job(job, tmp_path / "second").read_text())
    for report, size, quantity, note in (
        (first, (20, 30, 4), 1, "initial"),
        (second, (40, 10, 6), 2, "changed"),
    ):
        entry = report["designs"][0]
        assert entry["size_mm"] == pytest.approx(size)
        assert entry["quantity"] == quantity and entry["parameters"]["note"] == note
        assert len(report["export"]["plates"]) == quantity
    second_step = import_step(tmp_path / "second/block.step")
    assert tuple(second_step.bounding_box().min) == pytest.approx((-13, 6, 10), abs=1e-5)
    assert_serialized_closed(stl_triangles(tmp_path / "second/block.stl"), 2400)


@pytest.mark.parametrize("rotation", [0, 90])
def test_prepared_packing_bounds_match_independent_cad_with_displaced_origin(rotation):
    shape = Box(20, 30, 4).moved(Location((7, 11, -13)))
    measured = prepared.Bounds.measure(shape).rotated_z(rotation)
    actual = shape.rotate(Axis.Z, rotation).bounding_box()
    assert measured.minimum == pytest.approx(tuple(actual.min), abs=1e-7)
    assert measured.maximum == pytest.approx(tuple(actual.max), abs=1e-7)


def test_mesh_failure_is_not_replaced_by_a_successful_export(tmp_path, monkeypatch):
    def broken(shape):
        raise ValueError("mesh witness")

    monkeypatch.setattr(prepared, "checked_mesh", broken)
    with pytest.raises(ValueError, match="mesh witness"):
        export_job(
            Job([Design("block", Box(2, 3, 4), {})], BuildVolume(100, 100, 100), "part"),
            tmp_path / "bad",
        )
    assert not (tmp_path / "bad/manifest.json").exists()
    assert not (tmp_path / "bad/job.3mf").exists()
