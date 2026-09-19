"""Round interfaces, physical dimensions and evidence scopes for rods and braces."""

import json
from dataclasses import replace
from math import cos, pi, sin
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from build123d import GeomType, Location, Vector, import_step
from OCP.BRepAdaptor import BRepAdaptor_Surface

from cargo_grid import Rod, RodBrace, make_rod
from cargo_grid.accessories import accessory_datums, make_accessory
from cargo_grid.catalogue import accessory_design, accessory_variants, catalogue_job
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, export_job, write_3mf
from cargo_grid.jobs import Design, Job
from cargo_grid.parameters import BuildVolume, Interface, Tile
from cargo_grid.rods import BRACE_BORES_MM, BRACE_SPACINGS_MM, ROD_HEIGHTS_MM, fit_evidence
from cargo_grid.tiles import hole_placements, make_tile

BAMBU = BambuSettings((Material("PETG", "PETG", "#637B70"),), nozzle=0.8, layer_height=0.32)
FOUR = [*(Rod(h) for h in ROD_HEIGHTS_MM), *(RodBrace(s) for s in BRACE_SPACINGS_MM)]


def common_volume(a, b):
    shape = a.intersect(b)
    return 0 if shape is None else sum(s.volume for s in shape.solids())


@pytest.mark.parametrize("height,volume", [(120, 10879.175954559389), (240, 20303.95391532877)])
def test_rods_preserve_trial_dimensions_rounds_and_seating(height, volume):
    spec = Rod(height)
    shape = make_accessory(spec)
    assert shape.is_valid and len(shape.solids()) == 1
    assert shape.volume == pytest.approx(volume, abs=0.001)
    assert tuple(shape.bounding_box().size) == pytest.approx((18, 18, height + 12), abs=1e-5)
    datums = accessory_datums(spec)
    assert datums["shoulder_z"] == 12 and datums["underside_clearance_mm"] == 1
    assert datums["above_mat_height_mm"] == height
    cylinders = [f for f in shape.faces() if f.geom_type == GeomType.CYLINDER]
    peg = next(f for f in cylinders if abs(f.bounding_box().min.Z - 1) < 1e-5)
    assert peg.radius == pytest.approx(5)
    assert peg.bounding_box().size.Z == pytest.approx(11)
    shaft = next(f for f in cylinders if abs(f.bounding_box().min.Z - 15.2) < 1e-5)
    assert shaft.radius == pytest.approx(5)
    assert shaft.bounding_box().max.Z == pytest.approx(height + 10)
    shoulder = [
        f
        for f in shape.faces()
        if f.geom_type == GeomType.PLANE
        and abs(f.center().Z - 12) < 1e-5
        and f.bounding_box().size.Z < 1e-5
    ]
    assert len(shoulder) == 1
    assert shoulder[0].area == pytest.approx(pi * (9**2 - 5**2))
    radii = {
        round(BRepAdaptor_Surface(f.wrapped).Torus().MinorRadius(), 6)
        for f in shape.faces()
        if f.geom_type == GeomType.TORUS
    }
    assert radii == {1, 2}
    for z in (0.25, 0.75, 1.01, 6, 11.99):
        r = 4 + z if z < 1 else 5
        for angle in range(0, 360, 15):
            a = angle * pi / 180
            assert shape.is_inside(Vector((r - 0.01) * cos(a), (r - 0.01) * sin(a), z))
            assert not shape.is_inside(Vector((r + 0.01) * cos(a), (r + 0.01) * sin(a), z))


@pytest.mark.parametrize("spacing", BRACE_SPACINGS_MM)
@pytest.mark.parametrize("diameter", BRACE_BORES_MM)
def test_brace_bores_labels_and_passage_over_actual_rod(spacing, diameter):
    spec = RodBrace(spacing, diameter)
    shape = make_accessory(spec)
    assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
    assert tuple(shape.bounding_box().size) == pytest.approx((spacing + 18, 18, 6.4), abs=1e-5)
    datums = accessory_datums(spec)
    assert datums["straight_bore_length_mm"] == pytest.approx(5.6)
    assert datums["nominal_diametral_clearance_mm"] == pytest.approx(diameter - 10)
    assert datums["nominal_radial_clearance_mm"] == pytest.approx((diameter - 10) / 2)
    bores = [
        f
        for f in shape.faces()
        if f.geom_type == GeomType.CYLINDER
        and abs(BRepAdaptor_Surface(f.wrapped).Cylinder().Radius() - diameter / 2) < 1e-5
    ]
    assert len(bores) == 2
    centres = sorted(f.bounding_box().center().X for f in bores)
    assert centres == pytest.approx([0, spacing], abs=1e-5)
    for f in bores:
        assert f.bounding_box().min.Z == pytest.approx(0.4)
        assert f.bounding_box().max.Z == pytest.approx(6)
    for x in (0, spacing):
        for z in (0.05, 0.2, 0.41, 1, 3.2, 5.43, 5.99, 6.2, 6.35):
            r = diameter / 2 + max(0, 0.4 - z, z - 6)
            for angle in range(0, 360, 15):
                a = angle * pi / 180
                assert not shape.is_inside(Vector(x + (r - 0.01) * cos(a), (r - 0.01) * sin(a), z))
                assert shape.is_inside(Vector(x + (r + 0.01) * cos(a), (r + 0.01) * sin(a), z))
    floors = [
        f
        for f in shape.faces()
        if f.geom_type == GeomType.PLANE
        and abs(f.center().Z - 5.44) < 1e-5
        and f.bounding_box().size.Z < 1e-5
    ]
    assert len(floors) == len(f"{spacing:g}") + len(f"{diameter:.1f}")
    for rod, x in ((make_rod(), 0), (make_rod(Rod(240)), spacing)):
        located = rod.moved(Location((x, 0, 1)))
        for z in (254, 251, 134, 131, 80):
            assert common_volume(shape.moved(Location((0, 0, z))), located) < 1e-6
        assert common_volume(shape.moved(Location((0, 0, 14))), located) > 1


def test_actual_tile_hole_pairs_and_custom_thickness():
    for tile_spec, centers, second_shift in (
        (Tile(2, 2), ((30, 60), (90, 60)), 0),
        (Tile(2, 1), ((60, 30), (180, 30)), 120),
    ):
        tile = make_tile(tile_spec)
        accepted = {(p.x, p.y) for p in hole_placements(tile_spec) if p.accepted}
        assert centers[0] in accepted and (centers[1][0] - second_shift, centers[1][1]) in accepted
        neighbor = tile.moved(Location((second_shift, 0, 0)))
        if second_shift:
            assert common_volume(tile, neighbor) < 1e-6
        for (x, y), shape in zip(centers, (tile, neighbor)):
            for z in (0.5, 6.5, 12.5):
                for angle in range(0, 360, 30):
                    a = angle * pi / 180
                    assert not shape.is_inside(Vector(x + 4.99 * cos(a), y + 4.99 * sin(a), z))
                    assert shape.is_inside(Vector(x + 5.01 * cos(a), y + 5.01 * sin(a), z))
            assert common_volume(make_rod().moved(Location((x, y, 1))), shape) < 1e-6
    custom = Rod(120, tile_thickness_mm=10)
    shape = make_rod(custom)
    assert shape.bounding_box().size.Z == pytest.approx(129)
    assert accessory_datums(custom)["straight_peg_z_mm"] == (1, 9)
    assert fit_evidence(custom)["status"] == "not-tested"


@pytest.mark.parametrize("spec", FOUR)
def test_round_family_exports_strict_step_mesh_and_evidence(spec, tmp_path):
    design = accessory_design(spec)
    report = json.loads(
        export_job(
            Job([design], BuildVolume(350, 320, 320), "part"),
            tmp_path / "bambu",
            bambu=BAMBU,
        ).read_text()
    )
    entry = report["designs"][0]
    assert entry["step_roundtrip"] == "passed"
    assert entry["step_volume_delta_mm3"] <= entry["step_volume_budget_mm3"]
    assert entry["step_bounds_delta_mm"] <= 1e-5
    assert entry["mesh"]["closed_oriented_manifold"]
    assert entry["mating_datums"] == json.loads(json.dumps(accessory_datums(spec)))
    restored = import_step(tmp_path / "bambu" / f"{design.name}.step")
    assert tuple(restored.bounding_box().size) == pytest.approx(design.size, abs=1e-5)
    if isinstance(spec, Rod):
        assert entry["fit_evidence"]["status"] == "user-reported-fit"
        assert entry["fit_evidence"]["reported_process"]["rod_print_rotation_y_degrees"] == 90
        assert entry["fit_evidence"]["measured_diameter_or_force"] is None
        assert entry["recommended_print_orientation"]["rotation_axis"] == "Y"
        assert entry["recommended_print_orientation"]["rotation_degrees"] == 90
        assert entry["recommended_print_orientation"]["applied_to_exports"] == {
            "step": False,
            "stl": False,
            "core_3mf": False,
            "bambu_3mf": True,
        }
        assert design.bambu_size == pytest.approx((spec.overall_length_mm, 18, 18))
        assert (
            entry["recommended_bambu_object_settings"]["settings"]["support_type"] == "normal(auto)"
        )
    else:
        assert entry["fit_evidence"]["status"] == "provisional"
        assert entry["fit_evidence"]["physical_test_pending"]
        assert "recommended_bambu_object_settings" not in entry
    assert report["physical_fit_verified"] is False


def test_core_stays_upright_and_bambu_requires_the_tested_pose(tmp_path):
    rod = accessory_design(Rod(240))
    core = write_3mf(Job([rod], BuildVolume(350, 320, 320), "part"), tmp_path / "core.3mf")
    assert core["plates"][0]["items"][0]["size_mm"] == pytest.approx((18, 18, 252))
    with ZipFile(tmp_path / "core.3mf") as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
        assert max(float(v.get("z")) for v in root.findall(".//{*}vertex")) == pytest.approx(252)
    for invalid in (
        Design(rod.name, rod.shape, rod.parameters),
        replace(rod, apply_orientation_to_bambu=False),
        replace(rod, recommended_print_rotation_y=-90),
        replace(rod, bambu_object_settings={}),
    ):
        with pytest.raises(ValueError, match="orientation|settings"):
            write_3mf(
                Job([invalid], BuildVolume(350, 320, 320), "part"),
                tmp_path / "bad.3mf",
                bambu=BAMBU,
            )
    assert not (tmp_path / "bad.3mf").exists()


def test_finite_catalogue_round_parameters_are_absolute():
    variants = accessory_variants(BuildVolume(350, 320, 325), Interface(pitch=30, height=10))
    rods = [v for v in variants if isinstance(v, Rod)]
    braces = [v for v in variants if isinstance(v, RodBrace)]
    assert rods == [Rod(120, tile_thickness_mm=10), Rod(240, tile_thickness_mm=10)]
    assert braces == [RodBrace(60), RodBrace(120)]
    assert accessory_design(Rod()).name == accessory_design(Rod(120, 10, 13)).name
    assert accessory_design(RodBrace()).name == accessory_design(RodBrace(60, 10)).name
    assert accessory_design(Rod(240)).name != accessory_design(Rod()).name
    assert accessory_design(RodBrace(60, 10.2)).name != accessory_design(RodBrace()).name
    assert fit_evidence(Rod(120, 9.9))["status"] == "not-tested"


@pytest.mark.parametrize(
    "factory,kwargs",
    [
        (Rod, {"above_mat_height_mm": 5}),
        (Rod, {"above_mat_height_mm": float("nan")}),
        (Rod, {"tile_thickness_mm": 5}),
        (Rod, {"peg_diameter_mm": 10.6}),
        (Rod, {"peg_diameter_mm": float("inf")}),
        (RodBrace, {"center_spacing_mm": 30}),
        (RodBrace, {"bore_diameter_mm": 10.1}),
        (RodBrace, {"bore_diameter_mm": float("nan")}),
    ],
)
def test_invalid_round_parameters(factory, kwargs):
    with pytest.raises(ValueError):
        factory(**kwargs)


def test_cli_rod_fit_checked_after_horizontal_orientation(tmp_path):
    common = [
        "part",
        "--build-width-mm",
        "260",
        "--build-depth-mm",
        "40",
        "--build-height-mm",
        "20",
        "--family",
        "rod",
        "--rod-height-mm",
        "240",
        "--no-stl",
    ]
    with pytest.raises(SystemExit):
        main([*common, "--output", str(tmp_path / "upright")])
    assert not (tmp_path / "upright").exists()
    assert (
        main(
            [
                *common,
                "--copy-count",
                "2",
                "--bambu",
                "--material",
                "PETG",
                "PETG",
                "#637B70",
                "--nozzle-diameter-mm",
                ".8",
                "--layer-height-mm",
                ".32",
                "--output",
                str(tmp_path / "horizontal"),
            ]
        )
        == 0
    )
    report = json.loads((tmp_path / "horizontal/manifest.json").read_text())
    assert report["designs"][0]["quantity"] == 2
    assert report["designs"][0]["parameters"]["above_mat_height_mm"] == 240
    assert all(
        item["size_mm"] == pytest.approx([252, 18, 18])
        for p in report["export"]["plates"]
        for item in p["items"]
    )


@pytest.mark.parametrize(
    "family,extra",
    [
        ("tile", ["--rod-height-mm", "120"]),
        ("rod", ["--width-cells", "2"]),
        ("rod", ["--stop-height-mm", "120"]),
        ("rod-brace", ["--length-cells", "1"]),
        ("rod", ["--bore-diameter-mm", "10.2"]),
        ("rod-brace", ["--peg-diameter-mm", "10"]),
        ("rod", ["--fit-offset-mm", ".1"]),
        ("rod-brace", ["--fit-offset-mm", ".1"]),
        ("rod", ["--hole-diameter-mm", "10"]),
        ("rod", ["--ramp-join", "male"]),
    ],
)
def test_cli_rejects_unrelated_options(family, extra, tmp_path):
    with pytest.raises(SystemExit) as error:
        main(
            [
                "part",
                "--family",
                family,
                "--build-width-mm",
                "350",
                "--build-depth-mm",
                "320",
                "--build-height-mm",
                "320",
                "--output",
                str(tmp_path / "bad"),
                *extra,
            ]
        )
    assert error.value.code == 2
    assert not (tmp_path / "bad").exists()


def test_cli_brace_defaults_and_adjustment(tmp_path):
    assert (
        main(
            [
                "part",
                "--family",
                "rod-brace",
                "--brace-spacing-mm",
                "120",
                "--bore-diameter-mm",
                "10.2",
                "--build-width-mm",
                "150",
                "--build-depth-mm",
                "30",
                "--build-height-mm",
                "10",
                "--no-stl",
                "--output",
                str(tmp_path / "brace"),
            ]
        )
        == 0
    )
    report = json.loads((tmp_path / "brace/manifest.json").read_text())["designs"][0]
    assert report["parameters"] == {
        "family": "rod-brace",
        "center_spacing_mm": 120,
        "bore_diameter_mm": 10.2,
    }
    assert report["mating_datums"]["nominal_diametral_clearance_mm"] == 0.2
    assert report["fit_evidence"]["status"] == "not-tested"


def test_catalogue_uses_pose_for_fit_and_reports_omissions(monkeypatch):
    import cargo_grid.catalogue as catalogue

    monkeypatch.setattr(catalogue, "tile_sizes", lambda *args: [])
    monkeypatch.setattr(catalogue, "accessory_variants", lambda *args: FOUR)
    build = BuildVolume(260, 45, 20)
    upright = catalogue_job(build)
    assert {d.parameters["family"] for d in upright.designs} == {"rod-brace"}
    assert len(upright.omitted) == 2
    horizontal = catalogue_job(build, orient_for_bambu=True)
    assert len(horizontal.designs) == 4 and not horizontal.omitted
