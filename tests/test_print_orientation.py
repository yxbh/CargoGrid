"""Actual project meshes, bed contact, packing and per-artifact transforms."""

import json
import xml.etree.ElementTree as ET
from math import ceil, sqrt
from zipfile import ZipFile

import numpy as np
import pytest
from build123d import import_step
from test_export import _project_facts

from cargo_grid import BuildVolume
from cargo_grid import catalogue as catalogue_module
from cargo_grid.accessories import (
    VERTICAL_BRACKET_CELLS,
    VERTICAL_BRACKET_CONFIGS,
    VERTICAL_STOP_CELLS,
    VERTICAL_STOP_HEIGHTS_MM,
    Accessory,
    bambu_print_rotation,
    vertical_stop_print_rotation,
)
from cargo_grid.catalogue import accessory_design, catalogue_job
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, export_job, write_3mf
from cargo_grid.jobs import Job
from cargo_grid.meshes import checked_mesh
from cargo_grid.parameters import Exclusion

BAMBU = BambuSettings((Material("Diagnostic PETG", "PETG", "#789784"),), 0.4, 0.2)


@pytest.mark.parametrize(
    "family,nx,ny,height,angle",
    [
        *(
            (
                "vertical-tile-bracket",
                x,
                y,
                50,
                bambu_print_rotation(Accessory("vertical-tile-bracket", nx=x, ny=y)),
            )
            for x, y in VERTICAL_BRACKET_CELLS
        ),
        *(
            (
                "vertical-stop",
                x,
                y,
                height,
                vertical_stop_print_rotation(Accessory("vertical-stop", nx=x, ny=y, height=height)),
            )
            for x, y in VERTICAL_STOP_CELLS
            for height in VERTICAL_STOP_HEIGHTS_MM
        ),
        ("lock-45", 1, 1, 50, -135),
        ("lock-45", 2, 2, 50, -135),
        ("plate", 1, 1, 50, 180),
    ],
)
def test_bambu_mesh_has_broad_bed_contact_and_recorded_transform(
    family, nx, ny, height, angle, tmp_path
):
    from build123d import GeomType, Location

    design = accessory_design(Accessory(family, nx=nx, ny=ny, height=height))
    path = tmp_path / "job.3mf"
    result = write_3mf(Job([design], BuildVolume(350, 320, 325), "part"), path, bambu=BAMBU)
    item = result["plates"][0]["items"][0]
    assert item["size_mm"] == pytest.approx(design.bambu_size)
    assert ("object_settings" in item) == (family == "vertical-stop")
    transform = item["source_to_project_transform"]
    assert transform["rotation_x_degrees"] == angle and transform["applied_to_mesh"]
    assert len(transform["matrix_3mf"]) == 12
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
        mesh = root.find(".//{*}mesh")
        vertices = np.array(
            [[float(v.get(k)) for k in ("x", "y", "z")] for v in mesh.find("{*}vertices")]
        )
        triangles = np.array(
            [[int(t.get(k)) for k in ("v1", "v2", "v3")] for t in mesh.find("{*}triangles")]
        )
        build = root.find("./{*}build/{*}item")
        placement = np.array(list(map(float, build.get("transform").split())))
        vertices = vertices @ placement[:9].reshape(3, 3) + placement[9:]
    assert vertices[:, 2].min() >= -1e-5
    assert vertices[:, 2].min() == pytest.approx(0, abs=1e-5)
    mesh_points, _, mesh_report = checked_mesh(design.bambu_shape)
    expected_top = mesh_points[:, 2].max() + transform["translation_mm"][2]
    assert vertices[:, 2].max() == pytest.approx(expected_top, abs=1e-5)
    assert abs(vertices[:, 2].max() - design.bambu_size[2]) <= mesh_report["chord_tolerance_mm"]
    bed = vertices[triangles]
    bed = bed[np.max(np.abs(bed[:, :, 2]), axis=1) < 1e-5]
    area = np.linalg.norm(np.cross(bed[:, 1] - bed[:, 0], bed[:, 2] - bed[:, 0]), axis=1).sum() / 2
    posed = design.bambu_shape
    posed = posed.moved(Location(-posed.bounding_box().min))
    expected_area = sum(
        f.area
        for f in posed.faces()
        if f.geom_type == GeomType.PLANE
        and f.normal_at().Z < -0.99
        and abs(f.bounding_box().min.Z) < 1e-5
        and abs(f.bounding_box().max.Z) < 1e-5
    )
    assert area == pytest.approx(expected_area, abs=0.002)


@pytest.mark.parametrize("nx", [1, 2])
def test_shallow_brackets_apply_side_down_y_rotation_and_record_it(nx, tmp_path):
    from build123d import GeomType, Location

    design = accessory_design(
        Accessory(
            "vertical-tile-bracket",
            nx=nx,
            ny=1,
            panel_height_cells=2,
        )
    )
    result = write_3mf(
        Job([design], BuildVolume(350, 320, 325), "part"),
        tmp_path / f"shallow-{nx}.3mf",
        bambu=BAMBU,
    )
    item = result["plates"][0]["items"][0]
    transform = item["source_to_project_transform"]
    assert "rotation_x_degrees" not in transform
    assert transform["rotation_y_degrees"] == -90
    assert len(transform["matrix_3mf"]) == 12
    assert item["object_settings"] == {
        "enable_support": "1",
        "support_type": "normal(auto)",
    }
    posed = design.bambu_shape.moved(Location(-design.bambu_shape.bounding_box().min))
    bed_area = sum(
        face.area
        for face in posed.faces()
        if face.geom_type == GeomType.PLANE
        and face.normal_at().Z < -0.99
        and abs(face.bounding_box().min.Z) < 1e-5
        and abs(face.bounding_box().max.Z) < 1e-5
    )
    assert bed_area == pytest.approx(3136.052586000847, abs=0.002)


def test_transformed_catalogue_packing_respects_exclusions_and_quantity(tmp_path):
    designs = [
        accessory_design(Accessory("vertical-tile-bracket", nx=x, ny=y))
        for x, y in VERTICAL_BRACKET_CELLS
    ]
    designs[0].quantity = 2
    designs.append(accessory_design(Accessory("plate")))
    designs.append(accessory_design(Accessory("lock-45")))
    designs.append(accessory_design(Accessory("vertical-stop", nx=2, ny=1, height=60)))
    build = BuildVolume(250, 210, 115, margin=5, exclusions=(Exclusion(5, 5, 30, 30),))
    result = write_3mf(Job(designs, build, "catalogue"), tmp_path / "packed.3mf", bambu=BAMBU)
    _, plates, volumes = _project_facts(tmp_path / "packed.3mf")
    assert sum(len(p[2]) for p in plates) == 7
    assert sum(p["quantity"] for p in result["plates"]) == 7
    cols = ceil(sqrt(len(plates)))
    for record in result["plates"]:
        rectangles = []
        for item in record["items"]:
            w, d, h = item["size_mm"]
            if item["rotation"] == 90:
                w, d = d, w
            x, y = item["x"], item["y"]
            assert all(
                x + w <= e.x or e.x + e.width <= x or y + d <= e.y or e.y + e.depth <= y
                for e in build.exclusions
            )
            assert h <= build.z
            assert all(
                x + w <= a or a + aw <= x or y + d <= b or b + ad <= y
                for a, b, aw, ad in rectangles
            )
            rectangles.append((x, y, w, d))
            design = next(d for d in designs if d.name == item["design"])
            assert ("source_to_project_transform" in item) == design.apply_orientation_to_bambu
            if design.apply_orientation_to_bambu:
                pose = item["source_to_project_transform"]
                assert pose["packed_size_mm"] == pytest.approx((w, d, h))
                assert pose["plate_local_lower_corner_mm"] == pytest.approx((x, y, 0))
    for number, _, members in plates:
        index = number - 1
        ox, oy = index % cols * build.x * 1.2, -(index // cols) * build.y * 1.2
        for name, _ in members:
            bounds = next(v[-1] for v in volumes if v[0] == name)
            xmin, xmax, ymin, ymax, zmin, zmax = bounds
            assert xmin - ox >= 5 - 1e-5 and ymin - oy >= 5 - 1e-5
            assert xmax - ox <= build.x - 5 + 1e-5 and ymax - oy <= build.y - 5 + 1e-5
            assert zmin == pytest.approx(0, abs=1e-5) and zmax <= 115 + 1e-5


def test_source_exports_and_project_orientation_contracts_are_unchanged(tmp_path):
    design = accessory_design(Accessory("vertical-tile-bracket", nx=2))
    for backend in (None, BAMBU):
        folder = tmp_path / ("core" if backend is None else "bambu")
        manifest = json.loads(
            export_job(
                Job([design], BuildVolume(350, 320, 325), "part"), folder, stl=False, bambu=backend
            ).read_text()
        )
        source = import_step(folder / f"{design.name}.step")
        assert source.bounding_box().min.Z == pytest.approx(-12.8)
        assert tuple(source.bounding_box().size) == pytest.approx(design.size)
        applied = manifest["designs"][0]["recommended_print_orientation"]["applied_to_exports"]
        assert applied == {
            "step": False,
            "stl": False,
            "core_3mf": False,
            "bambu_3mf": backend is not None,
        }
        packed = manifest["export"]["plates"][0]["items"][0]
        assert packed["size_mm"] == pytest.approx(design.bambu_size if backend else design.size)
    angled = accessory_design(Accessory("lock-45"))
    assert angled.recommended_print_rotation_x == -135
    assert angled.apply_orientation_to_bambu
    plate = accessory_design(Accessory("plate"))
    assert plate.recommended_print_rotation_x == 180
    assert plate.apply_orientation_to_bambu
    assert plate.bambu_size == pytest.approx(plate.size)
    ramp = accessory_design(Accessory("ramp", nx=3))
    assert not ramp.apply_orientation_to_bambu and ramp.bambu_size == ramp.size
    assert ramp.bambu_object_settings["support_type"] == "normal(auto)"
    male_ramp = accessory_design(Accessory("ramp", nx=3, ramp_join="male"))
    assert not male_ramp.apply_orientation_to_bambu
    assert male_ramp.bambu_size == male_ramp.size
    assert male_ramp.bambu_size == pytest.approx((180, 56, 13), abs=1e-5)
    assert not male_ramp.bambu_object_settings


@pytest.mark.parametrize("join", ["female", "male"])
def test_ramp_project_keeps_source_underside_and_join_specific_support(join, tmp_path):
    design = accessory_design(Accessory("ramp", nx=3, ramp_join=join))
    path = tmp_path / "ramp.3mf"
    result = write_3mf(Job([design], BuildVolume(350, 320, 325), "part"), path, bambu=BAMBU)
    item = result["plates"][0]["items"][0]
    assert item["size_mm"] == pytest.approx((180, 56 if join == "male" else 50, 13), abs=1e-5)
    assert ("object_settings" in item) == (join == "female")
    assert design.shape.bounding_box().min.Z == pytest.approx(0, abs=1e-5)
    with ZipFile(path) as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
    metadata = {
        entry.get("key"): entry.get("value") for entry in config.find("object").findall("metadata")
    }
    if join == "female":
        assert metadata["enable_support"] == "1"
        assert metadata["support_type"] == "normal(auto)"
    else:
        assert "enable_support" not in metadata and "support_type" not in metadata
    _, _, meshes = _project_facts(path)
    bounds = meshes[0][-1]
    assert bounds[4] == pytest.approx(0, abs=1e-5)
    assert bounds[5] == pytest.approx(13, abs=1e-5)
    assert bounds[3] - bounds[2] == pytest.approx(56 if join == "male" else 50, abs=0.02)


@pytest.mark.parametrize("n", [1, 2])
def test_angled_project_uses_back_face_before_packing(n, tmp_path):
    from build123d import GeomType, Location

    design = accessory_design(Accessory("lock-45", nx=n, ny=n))
    path = tmp_path / "angled.3mf"
    project = write_3mf(Job([design], BuildVolume(350, 320, 325), "part"), path, bambu=BAMBU)
    item = project["plates"][0]["items"][0]
    assert item["source_to_project_transform"]["rotation_x_degrees"] == -135
    assert item["size_mm"] == pytest.approx(design.bambu_size)
    _, _, volumes = _project_facts(path)
    assert volumes[0][-1][4] == pytest.approx(0, abs=1e-5)
    assert abs(volumes[0][-1][5] - design.bambu_size[2]) <= 0.02
    shape = design.bambu_shape
    shape = shape.moved(Location(-shape.bounding_box().min))
    bed = [
        f
        for f in shape.faces()
        if f.geom_type == GeomType.PLANE
        and f.normal_at().Z < -0.99
        and abs(f.bounding_box().min.Z) < 1e-5
        and abs(f.bounding_box().max.Z) < 1e-5
    ]
    assert sum(f.area for f in bed) > 1000 * n


def test_cli_default_bracket_cells_and_orientation_aware_height(tmp_path):
    assert (
        main(
            [
                "part",
                "--family",
                "vertical-tile-bracket",
                "--build-width-mm",
                "130",
                "--build-depth-mm",
                "100",
                "--build-height-mm",
                "65",
                "--bambu",
                "--material",
                "PETG",
                "PETG",
                "#789784",
                "--nozzle-diameter-mm",
                ".4",
                "--layer-height-mm",
                ".2",
                "--no-stl",
                "--output",
                str(tmp_path / "bracket"),
            ]
        )
        == 0
    )
    manifest = json.loads((tmp_path / "bracket/manifest.json").read_text())
    assert (
        manifest["designs"][0]["parameters"]["nx"],
        manifest["designs"][0]["parameters"]["ny"],
    ) == (2, 1)
    assert manifest["designs"][0]["size_mm"][2] > 65
    assert manifest["export"]["plates"][0]["items"][0]["size_mm"][2] < 65
    with pytest.raises(SystemExit):
        main(
            [
                "part",
                "--family",
                "lock-90",
                "--build-width-mm",
                "130",
                "--build-depth-mm",
                "100",
                "--build-height-mm",
                "65",
                "--output",
                str(tmp_path / "removed"),
            ]
        )


def test_catalogue_fit_uses_project_pose_only_when_requested(monkeypatch):
    enumerate_accessories = catalogue_module.accessory_variants
    requests = []

    def brackets(build, interface, **hole_pattern):
        specs = [
            spec
            for spec in enumerate_accessories(build, interface, **hole_pattern)
            if spec.family == "vertical-tile-bracket"
        ]
        requests.append(specs)
        return specs

    monkeypatch.setattr(catalogue_module, "accessory_variants", brackets)
    monkeypatch.setattr(catalogue_module, "tile_sizes", lambda build, interface: [])
    build = BuildVolume(180, 130, 110)
    source = catalogue_job(build)
    project = catalogue_job(build, orient_for_bambu=True)
    expected_specs = {
        Accessory("vertical-tile-bracket", nx=1, ny=2),
        Accessory("vertical-tile-bracket", nx=2, ny=1),
        Accessory("vertical-tile-bracket", nx=2, ny=2),
        Accessory("vertical-tile-bracket", nx=1, ny=1, panel_height_cells=2),
        Accessory("vertical-tile-bracket", nx=2, ny=1, panel_height_cells=2),
    }
    assert len(requests) == 2
    assert all(len(specs) == len(set(specs)) and set(specs) == expected_specs for specs in requests)

    def cells(parameters):
        return (
            parameters["nx"],
            parameters["ny"],
            parameters.get("panel_height_cells", parameters["ny"]),
        )

    all_cells = {(1, 2, 2), (2, 1, 1), (2, 2, 2), (1, 1, 2), (2, 1, 2)}
    for job, expected, oriented in (
        (source, {(2, 1, 1)}, False),
        (project, {(1, 2, 2), (2, 1, 1), (2, 2, 2), (1, 1, 2)}, True),
    ):
        assert {cells(design.parameters) for design in job.designs} == expected
        assert len(job.designs) == len(expected)
        assert {cells(item["parameters"]) for item in job.omitted} == all_cells - expected
        assert len(job.omitted) == len(all_cells - expected)
        for design in job.designs:
            assert design.shape.is_valid and len(design.shape.solids()) == 1
            assert design.shape.volume > 0
            size = design.bambu_size if oriented else design.size
            assert build.placement(size) is not None
        for item in job.omitted:
            assert build.placement(item["size_mm"]) is None
            assert item["reason"] == "actual bounds exceed usable envelope"
    assert not {id(design) for design in source.designs} & {
        id(design) for design in project.designs
    }


def test_bambu_rejects_claimed_accessory_without_its_validated_pose(tmp_path):
    from dataclasses import replace

    design = accessory_design(Accessory("vertical-tile-bracket", nx=2))
    raw = replace(design, apply_orientation_to_bambu=False)
    with pytest.raises(ValueError, match="accessory_design"):
        write_3mf(
            Job([raw], BuildVolume(350, 320, 325), "part"),
            tmp_path / "bad.3mf",
            bambu=BAMBU,
        )
    assert not (tmp_path / "bad.3mf").exists()


@pytest.mark.native
def test_native_roundtrip_keeps_all_changed_accessories_in_their_project_pose(tmp_path):
    import os
    import subprocess
    from pathlib import Path

    executable = os.environ.get("CARGO_GRID_BAMBU")
    if not executable:
        pytest.skip("Set CARGO_GRID_BAMBU for native oriented-accessory roundtrip checks")
    binary = Path(executable).resolve(strict=True)
    designs = [
        *(
            accessory_design(
                Accessory(
                    "vertical-tile-bracket",
                    nx=x,
                    ny=base_y,
                    panel_height_cells=panel_z if panel_z != base_y else None,
                )
            )
            for x, base_y, panel_z in VERTICAL_BRACKET_CONFIGS
        ),
        *(accessory_design(Accessory("lock-45", nx=n, ny=n)) for n in (1, 2)),
        accessory_design(Accessory("ramp")),
        accessory_design(Accessory("ramp", nx=5)),
        *(
            accessory_design(Accessory("vertical-stop", nx=x, ny=y, height=height))
            for x, y in VERTICAL_STOP_CELLS
            for height in VERTICAL_STOP_HEIGHTS_MM
        ),
    ]
    source, target = tmp_path / "input.3mf", tmp_path / "native.3mf"
    write_3mf(Job(designs, BuildVolume(350, 320, 325), "catalogue"), source, bambu=BAMBU)
    _, before_plates, before = _project_facts(source)
    with (tmp_path / "native.log").open("w") as log:
        result = subprocess.run(
            [
                str(binary),
                "--datadir",
                str(tmp_path / "settings"),
                "--debug",
                "2",
                "--arrange",
                "0",
                "--orient",
                "0",
                "--info",
                "--export-3mf",
                str(target),
                str(source),
            ],
            cwd=tmp_path,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=180,
            check=False,
        )
    assert result.returncode == 0, (tmp_path / "native.log").read_text()
    _, after_plates, after = _project_facts(target)
    assert before_plates == after_plates
    assert len(before) == len(after) == 17
    for a, b in zip(before, after):
        assert a[:-1] == b[:-1]
        assert b[-1] == pytest.approx(a[-1], abs=0.001)
