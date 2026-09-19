"""Source-derived normal-stop checks; physical fit, strength and removal remain unverified."""

import json
from dataclasses import replace
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from build123d import Axis, GeomType, Location, Part, Solid, Vector, export_step, import_step
from OCP.BRep import BRep_Tool
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

from cargo_grid import BuildVolume
from cargo_grid.accessories import (
    VERTICAL_STOP_CELLS,
    VERTICAL_STOP_HEIGHTS_MM,
    Accessory,
    _downward_plug,
    _mounted_base,
    _vertical_stop_slope,
    accessory_datums,
    make_accessory,
    vertical_stop_print_rotation,
)
from cargo_grid.catalogue import accessory_design, accessory_variants
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, export_job, write_3mf
from cargo_grid.jobs import Job
from cargo_grid.meshes import checked_mesh

CASES = [
    (1, 1, 60, 138018.07378332317, 135.65095768561332, 4385.433372204407),
    (1, 1, 120, 252394.55403519978, 116.08947705488677, 7130.9316949135355),
    (1, 2, 60, 271020.20151973784, 154.56668309343257, 7193.115631075868),
    (1, 2, 120, 492565.0782894738, 135.31415520897613, 9136.771585413158),
    (2, 1, 60, 276381.79212997947, 135.65095768561332, 9084.111985280555),
    (2, 1, 120, 505322.58099155524, 116.08947705488677, 14771.215653749465),
    (2, 2, 60, 542574.4086457045, 154.56668309343257, 14900.025235800013),
    (2, 2, 120, 985827.4944714617, 135.31415520897613, 18926.169712641542),
]


def volume(shape) -> float:
    return sum(solid.volume for solid in shape.solids()) if shape else 0


def symmetric_difference(a, b) -> float:
    return volume(a.cut(b)) + volume(b.cut(a))


def edge_continuities(shape) -> list[str]:
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, ancestors)
    result = []
    for edge in shape.edges():
        faces = list(ancestors.FindFromKey(edge.wrapped))
        assert len(faces) == 2
        result.append(
            str(
                BRep_Tool.Continuity_s(
                    edge.wrapped,
                    TopoDS.Face_s(faces[0]),
                    TopoDS.Face_s(faces[1]),
                )
            ).rsplit(".", 1)[-1]
        )
    return result


@pytest.mark.parametrize("nx,ny,height,expected_volume,angle,bed_area", CASES)
def test_filled_wedge_rounding_bounds_and_step_roundtrip(
    nx, ny, height, expected_volume, angle, bed_area, tmp_path
):
    spec = Accessory("vertical-stop", nx=nx, ny=ny, height=height)
    shape = make_accessory(spec)
    budget = max(1e-6, shape.area * Precision.Confusion_s())
    assert shape.is_valid and len(shape.solids()) == 1
    assert tuple(shape.bounding_box().min) == pytest.approx((0, 0, -12.8), abs=1e-5)
    assert tuple(shape.bounding_box().max) == pytest.approx((60 * nx, 60 * ny, height), abs=1e-5)
    assert abs(shape.volume - expected_volume) <= budget
    assert set(edge_continuities(shape)) == {"GeomAbs_G1"}
    slope = _vertical_stop_slope(spec)
    for y in (15, 30, 60 * ny - 1):
        roof = 4.1 + slope * y
        assert shape.is_inside(Vector(30 * nx, y, (3.1 + roof) / 2))
    assert shape.is_inside(Vector(30 * nx, 60 * ny - 0.1, height / 2))
    assert vertical_stop_print_rotation(spec) == pytest.approx(angle, abs=1e-12)
    posed = shape.rotate(Axis.X, angle)
    posed = Part(posed.moved(Location(-posed.bounding_box().min)).solids())
    contact = sum(
        face.area
        for face in posed.faces()
        if face.geom_type == GeomType.PLANE
        and face.normal_at().Z < -0.99
        and abs(face.bounding_box().min.Z) < 1e-5
        and abs(face.bounding_box().max.Z) < 1e-5
    )
    assert contact == pytest.approx(bed_area, abs=1e-5)
    path = tmp_path / "vertical-stop.step"
    assert export_step(shape, path)
    restored = import_step(path)
    assert restored.is_valid and len(restored.solids()) == 1
    assert abs(restored.volume - shape.volume) <= budget
    assert tuple(restored.bounding_box().size) == pytest.approx(tuple(shape.bounding_box().size))
    _, _, mesh = checked_mesh(shape)
    assert mesh["closed_oriented_manifold"]
    assert mesh["mesh_volume_mm3"] > 0
    assert mesh["maximum_weld_displacement_mm"] <= mesh["seam_weld_mm"]


@pytest.mark.parametrize("nx,ny", VERTICAL_STOP_CELLS)
@pytest.mark.parametrize("height", VERTICAL_STOP_HEIGHTS_MM)
def test_x_plugs_and_roots_are_exactly_protected(nx, ny, height):
    spec = Accessory("vertical-stop", nx=nx, ny=ny, height=height)
    shape = make_accessory(spec)
    base = _mounted_base(spec, root_radius=1, round_top=False)
    datums = accessory_datums(spec)
    assert datums["body"] == "full-width filled wedge; no wall holes"
    assert datums["cargo_face_y"] == 60 * ny
    assert datums["cargo_height_z"] == height
    assert datums["free_edge_radius"] == 2
    for center in datums["mount_centers"]:
        plug = _downward_plug().moved(Location(center))
        assert volume(plug.cut(shape)) < 1e-8
        region = Solid.make_box(48, 48, 13.1).moved(Location((center[0] - 24, center[1] - 24, -13)))
        before = Part(base.intersect(region)[0].wrapped)
        after = Part(shape.intersect(region)[0].wrapped)
        assert symmetric_difference(before, after) < 1e-8


def test_variants_are_finite_distinct_and_use_scoped_normal_auto():
    variants = accessory_variants(BuildVolume(350, 320, 325))
    stops = [spec for spec in variants if spec.family == "vertical-stop"]
    assert len(variants) == 65
    assert {(spec.nx, spec.ny, spec.height) for spec in stops} == {
        (nx, ny, height) for nx, ny in VERTICAL_STOP_CELLS for height in VERTICAL_STOP_HEIGHTS_MM
    }
    assert len({accessory_design(spec).name for spec in stops}) == 8
    for spec in stops:
        design = accessory_design(spec)
        assert design.apply_orientation_to_bambu
        assert design.bambu_object_settings == {
            "enable_support": "1",
            "support_type": "normal(auto)",
        }


def test_bambu_project_records_dynamic_pose_and_object_support(tmp_path):
    spec = Accessory("vertical-stop", nx=2, ny=1, height=120)
    design = accessory_design(spec)
    settings = BambuSettings((Material("PETG", "PETG", "#637b70"),), 0.4, 0.2)
    path = tmp_path / "vertical-stop.3mf"
    report = write_3mf(Job([design], BuildVolume(350, 320, 325), "part"), path, bambu=settings)
    item = report["plates"][0]["items"][0]
    assert item["source_to_project_transform"]["rotation_x_degrees"] == pytest.approx(
        116.08947705488677
    )
    assert item["object_settings"] == {
        "enable_support": "1",
        "support_type": "normal(auto)",
    }
    with ZipFile(path) as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
    metadata = {entry.get("key"): entry.get("value") for entry in config.find("object")}
    assert metadata["enable_support"] == "1"
    assert metadata["support_type"] == "normal(auto)"
    with pytest.raises(ValueError, match="object settings"):
        write_3mf(
            Job(
                [replace(design, bambu_object_settings={})],
                BuildVolume(350, 320, 325),
                "part",
            ),
            tmp_path / "missing-settings.3mf",
            bambu=settings,
        )


def test_mixed_catalogue_scopes_auto_support_to_vertical_stop_only(tmp_path):
    stop = accessory_design(Accessory("vertical-stop", nx=2, ny=1, height=120))
    plate = accessory_design(Accessory("plate"))
    settings = BambuSettings((Material("PETG", "PETG", "#637b70"),), 0.4, 0.2)
    path = tmp_path / "mixed.3mf"
    report = write_3mf(
        Job([stop, plate], BuildVolume(350, 320, 325), "catalogue"),
        path,
        bambu=settings,
    )
    items = [item for plate_record in report["plates"] for item in plate_record["items"]]
    stop_item = next(item for item in items if item["design"] == stop.name)
    plate_item = next(item for item in items if item["design"] == plate.name)
    assert stop_item["object_settings"]["enable_support"] == "1"
    assert "object_settings" not in plate_item
    assert all(
        volume_record["role"] == "model" and volume_record["filament_slot"] == 1
        for plate_record in report["plates"]
        for volume_record in plate_record["volumes"]
    )
    with ZipFile(path) as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        project = json.loads(archive.read("Metadata/project_settings.config"))
    by_name = {}
    for obj in config.findall("object"):
        values = {entry.get("key"): entry.get("value") for entry in obj.findall("metadata")}
        by_name[values["name"]] = values
    assert by_name[f"{stop.name}_batch_1"]["enable_support"] == "1"
    assert "enable_support" not in by_name[f"{plate.name}_batch_2"]
    assert "enable_support" not in project


def test_source_exports_keep_model_datum_and_apply_pose_only_to_bambu(tmp_path):
    design = accessory_design(Accessory("vertical-stop", nx=2, ny=1, height=120))
    settings = BambuSettings((Material("PETG", "PETG", "#637b70"),), 0.4, 0.2)
    for backend in (None, settings):
        output = tmp_path / ("core" if backend is None else "bambu")
        manifest = json.loads(
            export_job(
                Job([design], BuildVolume(350, 320, 325), "part"),
                output,
                stl=False,
                bambu=backend,
            ).read_text()
        )
        source = import_step(output / f"{design.name}.step")
        assert tuple(source.bounding_box().size) == pytest.approx(design.size)
        assert source.bounding_box().min.Z == pytest.approx(-12.8)
        recommendation = manifest["designs"][0]["recommended_print_orientation"]
        assert recommendation["applied_to_exports"] == {
            "step": False,
            "stl": False,
            "core_3mf": False,
            "bambu_3mf": backend is not None,
        }
        object_settings = manifest["designs"][0]["recommended_bambu_object_settings"]
        assert object_settings["applied_to_bambu_3mf"] == (backend is not None)
        item = manifest["export"]["plates"][0]["items"][0]
        assert ("source_to_project_transform" in item) == (backend is not None)
        assert ("object_settings" in item) == (backend is not None)


def test_cli_defaults_to_2x1_h60_and_preserves_explicit_height(tmp_path):
    for name, extra, expected in (
        ("default", [], (2, 1, 60)),
        (
            "explicit",
            [
                "--width-cells",
                "1",
                "--depth-cells",
                "2",
                "--stop-height-mm",
                "120",
            ],
            (1, 2, 120),
        ),
    ):
        output = tmp_path / name
        assert (
            main(
                [
                    "part",
                    "--family",
                    "vertical-stop",
                    "--build-width-mm",
                    "350",
                    "--build-depth-mm",
                    "320",
                    "--build-height-mm",
                    "325",
                    "--no-stl",
                    "--output",
                    str(output),
                    *extra,
                ]
            )
            == 0
        )
        parameters = json.loads((output / "manifest.json").read_text())["designs"][0]["parameters"]
        assert (parameters["nx"], parameters["ny"], parameters["height"]) == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"family": "vertical-stop"},
        {"family": "vertical-stop", "nx": 2, "ny": 1, "height": 50},
        {"family": "vertical-stop", "nx": 2, "ny": 1, "height": 100},
    ],
)
def test_invalid_vertical_stop_dimensions_are_rejected(kwargs):
    with pytest.raises(ValueError, match="vertical-stop|unsupported mounting grid"):
        Accessory(**kwargs)
