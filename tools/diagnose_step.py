"""Temporary cross-platform STEP investigation; not a production acceptance check."""

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
from build123d import Part, PrecisionMode, export_step, import_step
from OCP.BinTools import BinTools, BinTools_FormatVersion
from OCP.BRep import BRep_Tool
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GProp import GProp_GProps
from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
from OCP.TopLoc import TopLoc_Location
from test_selection import ROOT, environment, write_json

from cargo_grid.adjustable_stop import make_adjustable_stop_parts
from cargo_grid.export import _StepSource
from cargo_grid.meshes import checked_mesh


def binary(shape, path):
    if not BinTools.Write_s(
        shape.wrapped,
        str(path),
        False,
        False,
        BinTools_FormatVersion.BinTools_FormatVersion_CURRENT,
    ):
        raise ValueError(f"binary BREP write failed: {path}")
    return hashlib.file_digest(path.open("rb"), "sha256").hexdigest()


def integrals(shape):
    result = {}
    for epsilon in (1e-12, 1e-14):
        properties = GProp_GProps()
        error = BRepGProp.VolumeProperties_s(shape.wrapped, properties, epsilon, True, False)
        result[f"adaptive-{epsilon}"] = {"mass": properties.Mass(), "error": error}
        for inertia in (False, True):
            for spans in (False, True):
                properties = GProp_GProps()
                error = BRepGProp.VolumePropertiesGK_s(
                    shape.wrapped,
                    properties,
                    Eps=epsilon,
                    OnlyClosed=True,
                    IsUseSpan=spans,
                    CGFlag=True,
                    IFlag=inertia,
                    SkipShared=False,
                )
                result[f"GK-{epsilon}-inertia-{inertia}-spans-{spans}"] = {
                    "mass": properties.Mass(),
                    "error": error,
                }
    return result


def mesh(shape, path, tolerance):
    copied = Part(BRepBuilderAPI_Copy(shape.wrapped, True, False).Shape())
    mesher = BRepMesh_IncrementalMesh(copied.wrapped, tolerance, False, 0.1, True)
    mesher.Perform()
    if not mesher.IsDone():
        raise ValueError("diagnostic tessellation did not finish")
    vertices, faces, validation = checked_mesh(copied)
    origin = np.array([30.0, 60.0, 8.0])
    triangles = vertices[faces] - origin
    contributions = (
        np.einsum("ij,ij->i", triangles[:, 0], np.cross(triangles[:, 1], triangles[:, 2])) / 6
    )
    volume = math.fsum(map(float, contributions))
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(vertices, dtype="<f8").tobytes())
    digest.update(np.ascontiguousarray(faces, dtype="<i8").tobytes())
    if path is not None:
        np.savez_compressed(path, vertices=vertices, triangles=faces)
    return {
        "volume": volume,
        "vertices": len(vertices),
        "triangles": len(faces),
        "mesh_sha256": digest.hexdigest(),
        "requested_chord_mm": tolerance,
        "maximum_reported_deflection_mm": max(
            BRep_Tool.Triangulation_s(face.wrapped, TopLoc_Location()).Deflection()
            for face in copied.faces()
        ),
        "validation": validation,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result = {
        "environment": environment(ROOT),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "src_tree": subprocess.check_output(["git", "rev-parse", "HEAD:src"], text=True).strip(),
        "shapes": {},
    }
    base = make_adjustable_stop_parts()[0]
    source = _StepSource.measure(base)
    result["budget"] = source.volume_budget
    shapes = {"source": base}

    def save():
        write_json(args.output / "result.json", result)

    result["shapes"]["source"] = {
        "binary_brep_sha256": binary(base, args.output / "source.binbrep"),
        "maximum_tolerance_mm": ShapeAnalysis_ShapeTolerance().Tolerance(base.wrapped, 1),
        "adaptive_source_mass": source.adaptive_volume,
    }
    save()
    if not args.source_only:
        for mode in PrecisionMode:
            name = mode.name.lower()
            path = args.output / f"fixed-base-{name}.step"
            if not export_step(base, path, precision_mode=mode, timestamp="2000-01-01T00:00:00"):
                raise ValueError(f"STEP write failed: {name}")
            shapes[name] = import_step(path)
            result["shapes"][name] = {
                "binary_brep_sha256": binary(shapes[name], args.output / f"{name}.binbrep"),
                "step_sha256": hashlib.file_digest(path.open("rb"), "sha256").hexdigest(),
                "valid": shapes[name].is_valid,
                "solid_count": len(shapes[name].solids()),
            }
            save()
    for name, shape in shapes.items():
        measurements = result["shapes"][name]
        measurements["integrals"] = integrals(shape)
        measurements["meshes"] = {}
        tolerances = (
            (0.02,)
            if args.source_only or name not in {"source", "average", "greatest"}
            else (
                0.02,
                0.002,
                0.0005,
            )
        )
        for tolerance in tolerances:
            try:
                measurements["meshes"][str(tolerance)] = mesh(
                    shape,
                    args.output / f"{name}-mesh.npz" if tolerance == 0.02 else None,
                    tolerance,
                )
            except ValueError as error:
                measurements["meshes"][str(tolerance)] = {"error": str(error)}
                print(name, tolerance, "MESH REJECTED:", error, flush=True)
            save()
        print(name, json.dumps(measurements, sort_keys=True), flush=True)
    if not args.source_only:
        for replica in (1, 2):
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--source-only",
                    "--output",
                    str(args.output / f"replica-{replica}"),
                ],
                check=True,
            )


if __name__ == "__main__":
    main()
