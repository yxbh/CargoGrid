"""Cross-post extras: cross, tee, corner and X box-corner posts with pegs or an X plug."""

import json
from math import pi
from zipfile import ZipFile

import numpy as np
import pytest
from build123d import Axis, Box, Face, GeomType, Kind, Location, Part, Plane, section

from cargo_grid import Tile, cross_post_set, make_tile
from cargo_grid.accessories import bambu_print_policy
from cargo_grid.cli import main
from cargo_grid.cross_post_set import cross_post_identity, extras_job
from cargo_grid.cross_posts import (
    ARM_LAYOUTS,
    LEAD_IN_HEIGHT_MM,
    LEAD_IN_INSET_MM,
    CrossPost,
    cross_post_datums,
    make_cross_post,
    peg_centers,
    peg_sites_accepted,
    plan_face,
    tip_centers,
    tip_peg_diameter_mm,
)
from cargo_grid.interfaces import make_plug, x_profile
from cargo_grid.jobs import Design
from cargo_grid.meshes import checked_mesh
from cargo_grid.parameters import BuildVolume, Interface
from cargo_grid.printers import PRINTERS
from cargo_grid.roof_support import OBJECT_AUTO_SUPPORT
from cargo_grid.tiles import hole_placements

H2D = BuildVolume(350, 320, 325)
H2D_OPTIONS = [
    "--printer",
    "h2d",
    "--bambu",
    "--material",
    "Bambu PETG Basic @BBL H2D 0.8 nozzle",
    "PETG",
    "#637b70",
    "--nozzle-diameter-mm",
    "0.8",
    "--layer-height-mm",
    "0.32",
]
H2D_PLA = ["--material", "Bambu PLA Basic @BBL H2D 0.8 nozzle", "PLA", "#0A2989"]
NAMES = {
    ("cross", "pegs", 60.0): "cross-post_cross_pegs_h60_peg10_451d0c744d",
    ("corner", "x-plug", 120.0): "cross-post_corner_x-plug_h120_a606dd6569",
}
# Plan bounds (min X, min Y, max X, max Y) of the arms at the standard 60 mm unit.
TURNED = 30 / 2**0.5 + 7
PLAN_BOUNDS = {
    ("cross", "pegs"): (-37, -37, 37, 37),
    ("tee", "pegs"): (-37, -7, 37, 37),
    ("corner", "pegs"): (-7, -7, 37, 37),
    ("x", "pegs"): (-37, -37, 37, 37),
    ("x", "x-plug"): (-TURNED, -TURNED, TURNED, TURNED),
}


@pytest.fixture(scope="module")
def h2d_job():
    return extras_job(
        H2D, placement_build=PRINTERS["h2d"].common_build(), gap=4, orient_for_bambu=True
    )


@pytest.fixture(scope="module")
def designs(h2d_job):
    return {
        (d.parameters["arms"], d.parameters["mount"], d.parameters["height_mm"]): d
        for d in h2d_job.designs
    }


@pytest.fixture(scope="module")
def tile_region():
    """A 3x3 full-hole tile, top at Z=0, so every half-unit position has whole holes around it."""
    return make_tile(Tile(3, 3)).moved(Location((0, 0, -13)))


def _overlap(shape, tile):
    """Common volume with the tile, clipped to the shape's own plan bounds."""
    bounds = shape.bounding_box()
    size = bounds.size
    centre = bounds.center()
    region = Box(size.X + 2, size.Y + 2, 16).moved(Location((centre.X, centre.Y, -6)))
    local = Part(tile.intersect(region).solids())
    common = shape.intersect(local)
    return sum(solid.volume for solid in common.solids()) if common else 0.0


KEYS = [
    (arms, mount, height)
    for mount in ("pegs", "x-plug")
    for arms in ARM_LAYOUTS
    for height in (60.0, 120.0)
]


@pytest.mark.parametrize("key", KEYS, ids=["-".join(map(str, key)) for key in KEYS])
def test_standard_posts_are_valid_solids_on_the_seating_plane(designs, key):
    arms, mount, height = key
    shape = designs[key].shape
    assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
    body = section(shape, section_by=Plane.XY.offset(1)).faces()
    assert len(body) == 1
    box = body[0].bounding_box()
    expected = PLAN_BOUNDS.get((arms, mount), PLAN_BOUNDS.get((arms, "pegs")))
    assert (box.min.X, box.min.Y, box.max.X, box.max.Y) == pytest.approx(expected, abs=1e-5)
    bounds = shape.bounding_box()
    assert bounds.max.Z == pytest.approx(height, abs=1e-5)
    depth = 12 if mount == "pegs" else Interface().plug_depth
    assert bounds.min.Z == pytest.approx(-depth, abs=1e-5)


def test_arm_faces_sit_seven_mm_from_the_grid_line_and_lean_in_at_the_top(designs):
    shape = designs["cross", "pegs", 60.0].shape
    (arm,) = section(shape, section_by=Plane.YZ.offset(20)).faces()
    box = arm.bounding_box()
    assert (box.min.Y, box.max.Y, box.min.Z, box.max.Z) == pytest.approx((-7, 7, 0, 60), abs=1e-5)
    corners = sorted((round(v.Y, 5), round(v.Z, 5)) for v in arm.vertices())
    assert corners == [(-7, 0), (-7, 60 - LEAD_IN_HEIGHT_MM), (-4, 60), (4, 60), (7, 0), (7, 48)]


@pytest.mark.parametrize("arms", ARM_LAYOUTS)
def test_pegs_sit_on_the_half_unit_hole_lattice_with_looser_tips(designs, arms):
    spec = CrossPost(arms)
    faces = section(designs[arms, "pegs", 60.0].shape, section_by=Plane.XY.offset(-6)).faces()
    pegs = {(round(f.center().X, 5), round(f.center().Y, 5)): f.area for f in faces}
    assert sorted(pegs) == sorted((x, y) for x, y, _ in peg_centers(spec))
    # The centre peg is the nominal 10 mm hole size; tips leave 0.15 mm a side.
    assert pegs.pop((0, 0)) == pytest.approx(pi * 5**2, rel=1e-6)
    assert list(pegs.values()) == pytest.approx([pi * 4.85**2] * len(pegs), rel=1e-6)
    assert tip_peg_diameter_mm(spec) == pytest.approx(9.7)
    assert tip_peg_diameter_mm(CrossPost(arms, peg_diameter_mm=10.2)) == pytest.approx(9.9)
    assert tip_peg_diameter_mm(CrossPost(arms, "x-plug")) is None


@pytest.mark.parametrize("arms", ARM_LAYOUTS)
def test_x_plug_is_the_shared_plug_and_its_lobe_tops_stay_flush(designs, arms):
    shape = designs[arms, "x-plug", 60.0].shape
    (plug,) = section(shape, section_by=Plane.XY.offset(-6)).faces()
    assert plug.area == pytest.approx(x_profile(Interface(), plug=True).area, rel=1e-6)
    (above,) = section(shape, section_by=Plane.XY.offset(0.01)).faces()
    assert above.area == pytest.approx(plan_face(CrossPost(arms, "x-plug")).area, rel=1e-6)


@pytest.mark.parametrize(
    "centre",
    [(60, 60), (90, 60), (90, 90)],
    ids=["cell-corner", "edge-midpoint", "over-socket"],
)
def test_peg_posts_fit_a_full_hole_tile_at_every_kind_of_half_unit_point(tile_region, centre):
    for arms in ARM_LAYOUTS:
        for turn in (0, 90):
            post = make_cross_post(CrossPost(arms)).rotate(Axis.Z, turn)
            assert _overlap(post.moved(Location((*centre, 0))), tile_region) == 0
    # Between half-unit points the pegs land on solid tile.
    assert _overlap(make_cross_post().moved(Location((75, 60, 0))), tile_region) > 1000


POSITIONS = {"cell-corner": (60, 60), "edge-midpoint": (90, 60), "over-socket": (90, 90)}
# A peg X post's tips reach the diagonal neighbours: sockets around a cell corner.
UNSUPPORTED = {("x", "cell-corner")}


def test_peg_posts_hold_with_two_or_more_holes_exactly_at_their_documented_positions():
    holes = {(hole.x, hole.y) for hole in hole_placements(Tile(3, 3)) if hole.accepted}
    for arms in ARM_LAYOUTS:
        pegs = [(x, y) for x, y, _ in peg_centers(CrossPost(arms))]
        for name, (cx, cy) in POSITIONS.items():
            for _ in range(4):
                engaged = sum((cx + x, cy + y) in holes for x, y in pegs)
                assert (engaged >= 2) is ((arms, name) not in UNSUPPORTED), (arms, name, engaged)
                pegs = [(-y, x) for x, y in pegs]


def test_x_plug_posts_overlap_a_socket_only_as_much_as_the_shared_plug(tile_region):
    plug = _overlap(make_plug().rotate(Axis.X, 180).moved(Location((90, 90, 0))), tile_region)
    assert plug > 0
    for arms in ARM_LAYOUTS:
        post = make_cross_post(CrossPost(arms, "x-plug")).moved(Location((90, 90, 0)))
        assert _overlap(post, tile_region) == pytest.approx(plug, rel=1e-6)


def _downward_faces(shape):
    bed = shape.bounding_box().min.Z
    result = []
    for face in shape.faces():
        normal = face.normal_at(face.center())
        if normal.Z < -0.71 and face.bounding_box().max.Z > bed + 1e-5:
            result.append(face)
    return result


def test_peg_posts_print_upside_down_without_overhangs(designs):
    for arms in ARM_LAYOUTS:
        assert not _downward_faces(designs[arms, "pegs", 60.0].bambu_shape)


def test_x_plug_posts_stand_on_the_plug_and_need_support_only_under_their_arms(designs):
    # Upside down the lobes would need support from the bed to the post height.
    for arms in ARM_LAYOUTS:
        design = designs[arms, "x-plug", 60.0]
        assert design.bambu_shape is design.shape
        overhangs = _downward_faces(design.shape)
        assert overhangs and all(face.bounding_box().max.Z < 1e-5 for face in overhangs)
        flipped = _downward_faces(design.shape.rotate(Axis.X, 180))
        assert any(face.bounding_box().min.Z == pytest.approx(0, abs=1e-5) for face in flipped)


@pytest.mark.parametrize("key", KEYS, ids=["-".join(map(str, key)) for key in KEYS])
def test_designs_carry_the_mount_specific_bambu_policy(designs, key):
    design = designs[key]
    policy = bambu_print_policy(design.parameters)
    plugged = key[1] == "x-plug"
    assert design.apply_orientation_to_bambu is not plugged
    assert design.recommended_print_rotation_x == policy.rotation_x == (None if plugged else 180)
    assert design.recommended_print_rotation_y is policy.rotation_y is None
    expected = OBJECT_AUTO_SUPPORT if plugged else {}
    assert design.bambu_object_settings == policy.object_settings == expected
    if key in NAMES:
        assert design.name == NAMES[key]


@pytest.mark.parametrize("key", [k for k in KEYS if k[2] == 120.0], ids=lambda k: f"{k[0]}-{k[1]}")
def test_bed_contact_is_the_lead_in_top_or_the_plug_tip(designs, key):
    """Geometric bed contact of the tallest posts, not evidence of adhesion or stability."""
    arms, mount, _ = key
    design = designs[key]
    posed = design.bambu_shape
    posed = posed.moved(Location(-posed.bounding_box().min))
    cad = sum(
        face.area
        for face in posed.faces()
        if face.geom_type == GeomType.PLANE
        and face.normal_at().Z < -0.99
        and abs(face.bounding_box().max.Z) < 1e-5
    )
    outline = (
        plan_face(CrossPost(arms)).outer_wire().offset_2d(-LEAD_IN_INSET_MM, kind=Kind.INTERSECTION)
        if mount == "pegs"
        else x_profile(Interface(), plug=True).outer_wire().offset_2d(-2)
    )
    assert cad == pytest.approx(Face(outline).area, rel=1e-6)
    points, triangles, _ = checked_mesh(posed)
    bed = points[triangles]
    bed = bed[np.max(np.abs(bed[:, :, 2]), axis=1) < 1e-5]
    mesh = np.linalg.norm(np.cross(bed[:, 1] - bed[:, 0], bed[:, 2] - bed[:, 0]), axis=1).sum() / 2
    assert mesh == pytest.approx(cad, rel=1e-4)
    width, depth, _ = design.bambu_size
    assert cad >= 0.18 * width * depth


def test_h2d_set_puts_each_mount_on_its_own_named_plate(h2d_job):
    assert h2d_job.kind == "extras"
    assert h2d_job.plate_names == {0: "Cross posts - pegs", 1: "Cross posts - X plug"}
    assert [p.plate for p in h2d_job.print_placements] == [0] * 8 + [1] * 8
    common = PRINTERS["h2d"].common_build()
    for design, placement in zip(h2d_job.designs, h2d_job.print_placements):
        width, depth, height = design.bambu_size
        if placement.rotation == 90:
            width, depth = depth, width
        assert common.contains_box(placement.x, placement.y, width, depth, height)
    assert h2d_job.placement_policy["collection"] == "cross-post"
    assert set(h2d_job.manifest_metadata["inventory"].values()) == {1}
    assert len(h2d_job.manifest_metadata["inventory"]) == 16
    assert h2d_job.omitted == []
    assert [design.display_name for design in h2d_job.designs[:2]] == [
        "Cross post - pegs - 60 mm",
        "Cross post - pegs - 120 mm",
    ]


def _stand_in(spec):
    """Metadata-only post: real identity, policy and datums on a small solid."""
    name, parameters = cross_post_identity(spec)
    policy = bambu_print_policy(parameters)
    return Design(
        name,
        Box(74, 74, spec.height_mm),
        parameters,
        recommended_print_rotation_x=policy.rotation_x,
        apply_orientation_to_bambu=policy.rotation_x is not None,
        bambu_object_settings=policy.object_settings,
        mating_datums=cross_post_datums(spec),
    )


def test_peg_posts_are_left_out_where_tiles_lack_half_unit_holes(monkeypatch):
    monkeypatch.setattr(cross_post_set, "cross_post_design", _stand_in)
    assert not peg_sites_accepted(Interface(40))
    job = extras_job(BuildVolume(256, 256, 256), interface=Interface(40))
    assert job.plate_names == {0: "Cross posts - X plug"}
    assert {d.parameters["mount"] for d in job.designs} == {"x-plug"}
    assert len(job.omitted) == 8
    assert all("don't keep a round hole" in entry["reason"] for entry in job.omitted)
    assert {entry["parameters"]["mount"] for entry in job.omitted} == {"pegs"}
    assert job.manifest_metadata["inventory"]["cross_pegs_h60"] == 0


def test_auto_support_reserves_a_tower_on_both_plates(monkeypatch):
    monkeypatch.setattr(cross_post_set, "cross_post_design", _stand_in)
    job = extras_job(
        H2D,
        placement_build=PRINTERS["h2d"].common_build(),
        gap=4,
        orient_for_bambu=True,
        auto_roof_support=True,
        layer_height_mm=0.24,
    )
    assert list(job.prime_tower_positions) == [0, 1]
    assert job.placement_policy["auto_roof_support"]["prime_tower_groups"] == [
        "Cross posts - pegs",
        "Cross posts - X plug",
    ]


@pytest.mark.parametrize(
    "spec,message",
    [
        (dict(arms="star"), "cross, tee, corner or x"),
        (dict(mount="glue"), "pegs or x-plug"),
        (dict(height_mm=20), "between 30 and 240 mm"),
        (dict(height_mm=241), "between 30 and 240 mm"),
        (dict(peg_diameter_mm=8), "between 9 and 10.5 mm"),
        (dict(mount="x-plug", peg_diameter_mm=10), "have no pegs"),
        (dict(interface=Interface(fit_offset=0.1)), "not the X socket fit offset"),
        (dict(interface=Interface(solid_bottom_mm=0.32)), "cross posts are unchanged"),
        (dict(interface=Interface(40)), "use the x-plug mount"),
        (dict(interface={"pitch": 60}), "must be an Interface"),
    ],
)
def test_posts_reject_settings_that_do_not_apply(spec, message):
    with pytest.raises(ValueError, match=message):
        CrossPost(**spec)


def test_identity_does_not_depend_on_int_or_float_spelling_or_peg_fit_offset(monkeypatch):
    spelled = CrossPost("cross", "pegs", 60, 10, Interface(60, 13, 0, "original", 0))
    assert cross_post_identity(spelled)[0] == NAMES["cross", "pegs", 60.0]
    assert (
        cross_post_identity(CrossPost("cross", "x-plug", 60, interface=Interface(fit_offset=0.1)))[
            0
        ]
        != cross_post_identity(CrossPost("cross", "x-plug", 60))[0]
    )
    monkeypatch.setattr(cross_post_set, "cross_post_design", _stand_in)
    job = extras_job(BuildVolume(256, 256, 256), interface=Interface(fit_offset=0.1))
    names = {d.name for d in job.designs}
    assert NAMES["cross", "pegs", 60.0] in names
    offsets = {d.parameters["mount"]: d.parameters["interface"]["fit_offset"] for d in job.designs}
    assert offsets == {"pegs": 0.0, "x-plug": 0.1}


def test_custom_grid_moves_the_tip_pegs_and_keeps_the_arm_size():
    interface = Interface(45, 16)
    peg = make_cross_post(CrossPost("corner", interface=interface))
    faces = section(peg, section_by=Plane.XY.offset(-6)).faces()
    assert sorted((round(f.center().X, 5), round(f.center().Y, 5)) for f in faces) == [
        (0, 0),
        (0, 22.5),
        (22.5, 0),
    ]
    assert peg.bounding_box().min.Z == pytest.approx(-15, abs=1e-5)
    plugged = make_cross_post(CrossPost("cross", "x-plug", interface=interface))
    bounds = plugged.bounding_box()
    assert (bounds.min.X, bounds.max.X) == pytest.approx((-29.5, 29.5), abs=1e-5)
    assert bounds.min.Z == pytest.approx(-interface.plug_depth, abs=1e-5)


@pytest.mark.parametrize("pitch", [30, 42, 43, 130, 200])
def test_every_layout_builds_at_small_and_large_units_and_thicknesses(pitch):
    for height in (6, 20):
        interface = Interface(pitch, height)
        mounts = ("pegs", "x-plug") if peg_sites_accepted(interface) else ("x-plug",)
        assert ("pegs" in mounts) is (pitch >= 43)
        for mount in mounts:
            for arms in ARM_LAYOUTS:
                spec = CrossPost(arms, mount, interface=interface)
                shape = make_cross_post(spec)
                assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
                reach = max(x for x, _ in tip_centers(spec)) + 7
                assert shape.bounding_box().max.X == pytest.approx(reach, abs=1e-5)


def test_cli_h2d_cross_post_extras_export_one_project(tmp_path, monkeypatch):
    monkeypatch.setattr(cross_post_set, "cross_post_design", _stand_in)
    output = tmp_path / "posts"
    assert main(["extras", "cross-post", *H2D_OPTIONS, "--no-stl", "--output", str(output)]) == 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["placement_policy"]["collection"] == "cross-post"
    assert manifest["placement_policy"]["name"] == "H2D dual-nozzle safe"
    entries = manifest["designs"]
    assert len(entries) == 16
    for entry in entries:
        plugged = entry["parameters"]["mount"] == "x-plug"
        assert entry["step_roundtrip"] == "passed"
        assert entry["mesh"]["closed_oriented_manifold"]
        assert entry["compatibility"]["x_attachment_interface_present"] is plugged
        assert entry["compatibility"]["tile_edge_interface_present"] is False
        assert entry["mating_datums"]["physical_fit_verified"] is False
        if plugged:
            assert "recommended_print_orientation" not in entry
        else:
            orientation = entry["recommended_print_orientation"]
            assert orientation["rotations"] == [{"axis": "X", "degrees": 180.0}]
            assert orientation["applied_to_exports"]["bambu_3mf"] is True
        assert ("recommended_bambu_object_settings" in entry) is plugged
    with ZipFile(output / "job.3mf") as archive:
        settings = archive.read("Metadata/model_settings.config").decode()
    assert "Cross posts - pegs" in settings and "Cross posts - X plug" in settings
    assert settings.count('key="enable_support" value="1"') == 8
    assert settings.count("Auto For Match") == 2


def test_cli_part_makes_copies_of_one_post(tmp_path):
    output = tmp_path / "corner"
    arguments = [
        "part",
        "--family",
        "cross-post",
        "--post-arms",
        "corner",
        "--post-mount",
        "x-plug",
        "--post-height-mm",
        "120",
        "--copy-count",
        "3",
        "--build-width-mm",
        "200",
        "--build-depth-mm",
        "200",
        "--build-height-mm",
        "200",
        "--no-stl",
        "--output",
        str(output),
    ]
    assert main(arguments) == 0
    (entry,) = json.loads((output / "manifest.json").read_text())["designs"]
    assert entry["name"] == NAMES["corner", "x-plug", 120.0]
    assert entry["quantity"] == 3
    assert entry["display_name"] == "Corner post - X plug - 120 mm"


@pytest.mark.parametrize(
    "command,message",
    [
        (["part", "--family", "rod", "--post-arms", "tee"], "--post-arms does not apply to rod"),
        (
            ["part", "--family", "cross-post", "--post-mount", "x-plug", "--peg-diameter-mm", "10"],
            "have no pegs",
        ),
        (["part", "--family", "cross-post", "--fit-offset-mm", "0.1"], "fit offset"),
        (["part", "--family", "cross-post", "--rod-height-mm", "120"], "does not apply"),
        (["part", "--family", "cross-post", "--width-cells", "2"], "does not apply"),
        (["part", "--family", "cross-post", "--holes"], "round-hole options apply to tiles"),
        (
            ["extras", "cross-post", "--no-holes"],
            "round-hole options apply to tiles; the cross-post set has no tiles",
        ),
    ],
)
def test_cli_cross_post_rejects_options_that_do_not_apply(command, message, tmp_path, capsys):
    build = ["--build-width-mm", "200", "--build-depth-mm", "200", "--build-height-mm", "200"]
    with pytest.raises(SystemExit) as caught:
        main([*command, *build, "--output", str(tmp_path / "out")])
    assert caught.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_h2d_cross_post_extras_inherit_global_support_with_a_pla_interface(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(cross_post_set, "cross_post_design", _stand_in)
    output = tmp_path / "posts"
    arguments = ["extras", "cross-post", *H2D_OPTIONS, *H2D_PLA, "--auto-roof-support"]
    assert main([*arguments, "--no-stl", "--output", str(output)]) == 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["placement_policy"]["auto_roof_support"]["prime_tower_plates"] == [1, 2]
    with ZipFile(output / "job.3mf") as archive:
        project = json.loads(archive.read("Metadata/project_settings.config"))
        objects = archive.read("Metadata/model_settings.config").decode()
    assert (project["enable_support"], project["support_type"]) == ("1", "normal(auto)")
    assert project["support_interface_filament"] == "2"
    assert 'key="enable_support"' not in objects
