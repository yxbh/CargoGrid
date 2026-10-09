"""Pull-handle extras: the two-unit X-plug handle with and without its front strap bar."""

import json
from math import cos, radians, sin
from zipfile import ZipFile

import pytest
from build123d import Box, CenterOf, Plane, section

from cargo_grid import pull_handle_set
from cargo_grid.accessories import bambu_print_policy
from cargo_grid.cli import main
from cargo_grid.interfaces import x_profile
from cargo_grid.jobs import Design, Job
from cargo_grid.packing import h2d_common_build
from cargo_grid.parameters import BuildVolume, Interface
from cargo_grid.pull_handle_set import extras_job
from cargo_grid.pull_handles import (
    BASE_MM,
    GRIP_HEIGHT_MM,
    HAND_CLEARANCE_MM,
    POST_WIDTH_MM,
    STRAP_BAR_DEPTH_MM,
    STRAP_SLOT_HEIGHT_MM,
    PullHandle,
    depth_cells,
    front_rest_margin_mm,
    grip_top_mm,
    make_pull_handle,
    plug_centers,
    print_rotation_x,
    strap_bar_front_y_mm,
    strap_bar_top_mm,
    width_cells,
)
from cargo_grid.roof_support import OBJECT_AUTO_SUPPORT, RoofSupportSettings, validate_roof_job

H2D = BuildVolume(350, 320, 325)
H2D_OPTIONS = [
    "--h2d-dual-safe",
    "--build-width-mm",
    "350",
    "--build-depth-mm",
    "320",
    "--build-height-mm",
    "325",
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
NAMES = {
    True: "pull-handle_2x1_strap-bar_f4cf379d11",
    False: "pull-handle_2x1_no-strap-bar_32b0cb868b",
}


@pytest.fixture(scope="module")
def h2d_job():
    return extras_job(H2D, placement_build=h2d_common_build(), gap=4, orient_for_bambu=True)


@pytest.fixture(scope="module")
def designs(h2d_job):
    return {design.parameters["strap_bar"]: design for design in h2d_job.designs}


def _flat(pairs):
    return [value for pair in pairs for value in pair]


def _spans(shape, plane):
    return sorted(
        tuple(round(value, 3) for value in (*face.bounding_box().min, *face.bounding_box().max))
        for face in section(shape, section_by=plane).faces()
    )


@pytest.mark.parametrize("strap_bar", [True, False])
def test_standard_handles_are_valid_2x1_solids_on_the_seating_plane(designs, strap_bar):
    shape = designs[strap_bar].shape
    assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
    bounds = shape.bounding_box()
    assert (bounds.min.X, bounds.max.X) == pytest.approx((0, 120), abs=1e-5)
    assert 0 < bounds.min.Y < bounds.max.Y < 60
    assert bounds.min.Z == pytest.approx(-Interface().plug_depth, abs=1e-5)
    assert bounds.max.Z == pytest.approx(grip_top_mm(), abs=1e-5)


@pytest.mark.parametrize("strap_bar", [True, False])
def test_hand_opening_and_grip_bar_have_their_documented_size(designs, strap_bar):
    shape = designs[strap_bar].shape
    middle = _spans(shape, Plane.XY.offset(BASE_MM + HAND_CLEARANCE_MM / 2))
    assert _flat((span[0], span[3]) for span in middle) == pytest.approx(
        [0, POST_WIDTH_MM, 120 - POST_WIDTH_MM, 120], abs=1e-3
    )
    centre = _spans(shape, Plane.YZ.offset(60))
    grip = [span for span in centre if span[2] > BASE_MM + 1]
    expected = [(BASE_MM + HAND_CLEARANCE_MM, grip_top_mm())]
    if strap_bar:
        expected.insert(0, (BASE_MM + STRAP_SLOT_HEIGHT_MM, strap_bar_top_mm()))
        bar = grip[0]
        front = strap_bar_front_y_mm()
        assert [bar[1], bar[4]] == pytest.approx([front, front + STRAP_BAR_DEPTH_MM], abs=1e-3)
    assert _flat((span[2], span[5]) for span in grip) == pytest.approx(_flat(expected), abs=1e-3)
    assert grip[-1][5] - grip[-1][2] == pytest.approx(GRIP_HEIGHT_MM, abs=1e-3)


@pytest.mark.parametrize("strap_bar", [True, False])
def test_two_x_plugs_sit_on_neighbouring_socket_centres(designs, strap_bar):
    faces = section(designs[strap_bar].shape, section_by=Plane.XY.offset(-6)).faces()
    plug_area = x_profile(Interface(), plug=True).area
    assert _flat(sorted((face.center().X, face.center().Y) for face in faces)) == pytest.approx(
        [30, 30, 90, 30], abs=1e-3
    )
    assert [face.area for face in faces] == pytest.approx([plug_area, plug_area], rel=1e-6)


@pytest.mark.parametrize("strap_bar", [True, False])
def test_designs_carry_the_validated_bambu_policy(designs, strap_bar):
    design = designs[strap_bar]
    policy = bambu_print_policy(design.parameters)
    assert design.name == NAMES[strap_bar]
    assert design.apply_orientation_to_bambu
    assert design.recommended_print_rotation_x == policy.rotation_x == print_rotation_x(strap_bar)
    assert design.recommended_print_rotation_y is policy.rotation_y is None
    assert design.bambu_object_settings == policy.object_settings == OBJECT_AUTO_SUPPORT


def _posed_points(shape, rotation):
    points, _ = shape.tessellate(0.01, 0.1)
    c, s = cos(radians(rotation)), sin(radians(rotation))
    return [((p.X, p.Y, p.Z), p.Y * c - p.Z * s, p.Y * s + p.Z * c) for p in points]


def _front_rest_contacts(design, interface=Interface()):
    rotation = design.recommended_print_rotation_x
    posed = _posed_points(design.shape, rotation)
    bed = min(z for _, _, z in posed)
    contacts = {"grip": [], "bar": [], "seat": []}
    bar_front = strap_bar_front_y_mm(interface)
    for (x, sy, sz), y, z in posed:
        if z > bed + 0.05:
            continue
        if sz > grip_top_mm() - 6.5:
            contacts["grip"].append(y)
        elif bar_front - 0.1 < sy < bar_front + 3.1 and 15 < sz < 21:
            contacts["bar"].append(y)
        elif sz < 3 and sy < 6:
            contacts["seat"].append(y)
        else:
            raise AssertionError(f"unexpected bed contact at {(x, sy, sz)}")
    centre = design.shape.center(CenterOf.MASS)
    c, s = cos(radians(rotation)), sin(radians(rotation))
    resting = [y for ys in contacts.values() for y in ys]
    centre_y = centre.Y * c - centre.Z * s
    assert min(resting) < centre_y < max(resting)
    # Points near a rounded contact spread either side of its tangent line; use each band's middle.
    lines = [sum(ys) / len(ys) for ys in contacts.values() if ys]
    measured = min(centre_y - min(lines), max(lines) - centre_y)
    strap_bar = design.parameters["strap_bar"]
    expected = front_rest_margin_mm(design.shape, interface, strap_bar)
    assert measured == pytest.approx(expected, abs=0.1)
    assert measured >= 5
    return {name for name, ys in contacts.items() if ys}


def test_strap_bar_handle_rests_on_its_grip_and_bar_edges(designs):
    assert designs[True].recommended_print_rotation_x == pytest.approx(108.225, abs=1e-3)
    assert _front_rest_contacts(designs[True]) == {"grip", "bar"}


def test_no_strap_bar_handle_rests_on_its_grip_and_seat_edges(designs):
    assert designs[False].recommended_print_rotation_x == pytest.approx(106.693, abs=1e-3)
    assert _front_rest_contacts(designs[False]) == {"grip", "seat"}


def test_h2d_set_puts_each_version_on_its_own_named_plate(h2d_job):
    assert h2d_job.kind == "extras"
    assert h2d_job.part_gap == 4
    assert h2d_job.plate_names == {0: "Pull handle - strap bar", 1: "Pull handle - no strap bar"}
    assert [design.name for design in h2d_job.designs] == [NAMES[True], NAMES[False]]
    assert [placement.plate for placement in h2d_job.print_placements] == [0, 1]
    common = h2d_common_build()
    for design, placement in zip(h2d_job.designs, h2d_job.print_placements):
        width, depth, height = design.bambu_size
        if placement.rotation == 90:
            width, depth = depth, width
        assert common.contains_box(placement.x, placement.y, width, depth, height)
    assert h2d_job.placement_policy["collection"] == "pull-handle"
    poses = h2d_job.manifest_metadata["print_poses"]
    for design, key in zip(h2d_job.designs, ("strap_bar", "no_strap_bar")):
        expected = front_rest_margin_mm(design.shape, strap_bar=design.parameters["strap_bar"])
        assert poses[f"{key}_centre_of_mass_inside_rest_edges_mm"] == pytest.approx(
            expected, abs=0.01
        )
    assert h2d_job.manifest_metadata["inventory"] == {
        "pull_handle_with_strap_bar": 1,
        "pull_handle_without_strap_bar": 1,
    }


def _stand_in(spec):
    name, parameters = pull_handle_set.pull_handle_identity(spec)
    return Design(
        name,
        Box(120, 60, 70),
        parameters,
        recommended_print_rotation_x=print_rotation_x(spec.strap_bar),
        apply_orientation_to_bambu=True,
        bambu_object_settings=bambu_print_policy(parameters).object_settings,
    )


@pytest.mark.parametrize("orient", [True, False])
def test_packing_uses_print_poses_only_for_bambu_projects(monkeypatch, orient):
    monkeypatch.setattr(pull_handle_set, "pull_handle_design", _stand_in)
    job = extras_job(BuildVolume(200, 200, 200), orient_for_bambu=orient)
    assert job.placement_policy["print_poses_packed"] is orient
    assert job.placement_policy["minimum_model_gap_mm"] == 2
    tall = job.designs[0].bambu_size[2] if orient else 70
    build = BuildVolume(200, 200, tall - 1)
    with pytest.raises(ValueError, match="exceed the configured build envelope"):
        extras_job(build, orient_for_bambu=orient)


@pytest.mark.parametrize(
    "spec,message",
    [
        (dict(interface=Interface(solid_bottom_mm=0.32)), "pull handles are unchanged"),
        (dict(interface={"pitch": 60}), "must be an Interface"),
        (dict(strap_bar=1), "true or false"),
    ],
)
def test_handles_reject_settings_that_do_not_apply(spec, message):
    with pytest.raises(ValueError, match=message):
        PullHandle(**spec)


@pytest.mark.parametrize(
    "pitch,cells",
    [(60, (2, 1)), (30, (4, 2)), (45, (3, 2)), (40, (3, 2)), (80, (2, 1)), (130, (1, 1))],
)
def test_footprint_is_the_fewest_whole_cells_that_fit_a_hand(pitch, cells):
    interface = Interface(pitch)
    assert (width_cells(interface), depth_cells(interface)) == cells
    assert cells[0] * pitch >= 120 and cells[1] * pitch >= 60
    assert (cells[0] - 1) * pitch < 120 and (cells[1] - 1) * pitch < 60
    centers = plug_centers(interface)
    assert len(centers) == cells[0] * cells[1]
    assert {(x, y) for x, y, _ in centers} == {
        ((i + 0.5) * pitch, (j + 0.5) * pitch) for i in range(cells[0]) for j in range(cells[1])
    }


@pytest.mark.parametrize(
    "pitch",
    [46, 130, pytest.param(59, marks=pytest.mark.slow), pytest.param(200, marks=pytest.mark.slow)],
)
def test_deep_and_single_cell_footprints_build_both_versions(pitch):
    interface = Interface(pitch)
    for strap_bar in (True, False):
        shape = make_pull_handle(PullHandle(strap_bar, interface))
        assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
        bounds = shape.bounding_box()
        assert bounds.max.X == pytest.approx(width_cells(interface) * pitch, abs=1e-5)
        assert bounds.max.Z == pytest.approx(grip_top_mm(), abs=1e-5)


def test_strap_bar_handle_refuses_tiles_too_thick_to_rest_on_its_front():
    with pytest.raises(ValueError, match="wouldn't rest steadily on its front"):
        make_pull_handle(PullHandle(True, Interface(height=40)))
    assert make_pull_handle(PullHandle(False, Interface(height=40))).is_valid


@pytest.mark.parametrize(
    "interface",
    [Interface(height=16), Interface(fit_offset=0.1), Interface(joint_style="full-height")],
)
def test_other_interface_settings_get_their_own_identity(interface):
    for strap_bar in (True, False):
        name, parameters = pull_handle_set.pull_handle_identity(PullHandle(strap_bar, interface))
        assert name.startswith(f"pull-handle_2x1_{'strap-bar' if strap_bar else 'no-strap-bar'}_")
        assert name != NAMES[strap_bar]
        assert Interface(**parameters["interface"]) == interface


@pytest.fixture(scope="module")
def custom_job():
    return extras_job(
        BuildVolume(256, 256, 256), interface=Interface(45, 16), orient_for_bambu=True
    )


def test_custom_grid_keeps_the_hand_sizes_and_follows_unit_and_thickness(custom_job):
    interface = Interface(45, 16)
    assert custom_job.manifest_metadata["cells"] == (3, 2)
    assert custom_job.manifest_metadata["footprint_mm"] == (135, 90)
    assert custom_job.manifest_metadata["hand_opening_mm"] == {"width": 103, "height": 40}
    for design in custom_job.designs:
        shape = design.shape
        assert shape.is_valid and len(shape.solids()) == 1
        bounds = shape.bounding_box()
        assert (bounds.min.X, bounds.max.X) == pytest.approx((0, 135), abs=1e-5)
        assert bounds.min.Z == pytest.approx(-interface.plug_depth, abs=1e-5)
        assert bounds.max.Z == pytest.approx(grip_top_mm(), abs=1e-5)
        faces = section(shape, section_by=Plane.XY.offset(-6)).faces()
        assert len(faces) == 6
        plug_area = x_profile(interface, plug=True).area
        assert [face.area for face in faces] == pytest.approx([plug_area] * 6, rel=1e-6)
        policy = bambu_print_policy(design.parameters)
        assert design.recommended_print_rotation_x == policy.rotation_x
        assert design.bambu_object_settings == policy.object_settings
    assert [design.display_name for design in custom_job.designs] == [
        "Pull handle - 3x2 - strap bar",
        "Pull handle - 3x2 - no strap bar",
    ]


def test_deeper_strap_bar_handle_rests_on_its_grip_and_seat_edges(custom_job):
    design = custom_job.designs[0]
    assert design.parameters["strap_bar"]
    assert design.recommended_print_rotation_x == pytest.approx(118.32, abs=0.01)
    assert _front_rest_contacts(design, Interface(45, 16)) == {"grip", "seat"}


@pytest.mark.parametrize("strap_bar", [True, False])
def test_identity_does_not_depend_on_int_or_float_interface_values(strap_bar):
    spelled = Interface(60, 13, 0, "original", 0)
    name, parameters = pull_handle_set.pull_handle_identity(PullHandle(strap_bar, spelled))
    assert name == NAMES[strap_bar]
    assert parameters["interface"] == {
        "pitch": 60.0,
        "height": 13.0,
        "fit_offset": 0.0,
        "joint_style": "original",
    }


def test_set_rejects_a_negative_gap():
    with pytest.raises(ValueError, match="packing gap"):
        extras_job(H2D, gap=-1)


def test_cli_h2d_pull_handle_extras_export_one_project(tmp_path):
    output = tmp_path / "handles"
    assert main(["extras", "pull-handle", *H2D_OPTIONS, "--no-stl", "--output", str(output)]) == 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["kind"] == "extras"
    assert manifest["placement_policy"]["collection"] == "pull-handle"
    assert manifest["placement_policy"]["name"] == "H2D dual-nozzle safe"
    entries = {entry["name"]: entry for entry in manifest["designs"]}
    assert set(entries) == set(NAMES.values())
    for strap_bar, name in NAMES.items():
        entry = entries[name]
        assert (output / f"{name}.step").exists()
        assert entry["step_roundtrip"] == "passed"
        assert entry["mesh"]["closed_oriented_manifold"]
        assert entry["compatibility"]["x_attachment_interface_present"] is True
        assert entry["compatibility"]["tile_edge_interface_present"] is False
        orientation = entry["recommended_print_orientation"]
        assert orientation["rotations"] == [{"axis": "X", "degrees": print_rotation_x(strap_bar)}]
        assert orientation["applied_to_exports"]["bambu_3mf"] is True
        assert "recommended_bambu_object_settings" in entry
    with ZipFile(output / "job.3mf") as archive:
        settings = archive.read("Metadata/model_settings.config").decode()
    assert "Pull handle - strap bar" in settings and "Pull handle - no strap bar" in settings
    assert settings.count('key="enable_support" value="1"') == 2
    assert settings.count("Auto For Match") == 2


@pytest.mark.parametrize(
    "options,message",
    [
        (["--no-holes"], "round-hole options apply to tiles"),
        (["--hole-diameter-mm", "8"], "round-hole options apply to tiles"),
        (["--hole-scope", "interior"], "round-hole options apply to tiles"),
        (["--solid-bottom-thickness-mm", "0.32"], "pull handles are unchanged"),
        (["--roof-support"], "catalogues and extras are not supported"),
        (["--packing-gap-mm", "0"], "H2D packing gap"),
    ],
)
def test_cli_pull_handle_extras_reject_options_that_do_not_apply(
    options, message, tmp_path, capsys
):
    with pytest.raises(SystemExit) as caught:
        main(["extras", "pull-handle", *H2D_OPTIONS, "--output", str(tmp_path / "set"), *options])
    assert caught.value.code == 2
    assert message in capsys.readouterr().err


H2D_PLA = ["--material", "Bambu PLA Basic @BBL H2D 0.8 nozzle", "PLA", "#0A2989"]
AUTO_SUPPORT = ["--roof-support", "--roof-support-mode", "auto"]


def test_cli_h2d_pull_handle_extras_inherit_global_support_with_a_pla_interface(tmp_path):
    output = tmp_path / "handles"
    arguments = ["extras", "pull-handle", *H2D_OPTIONS, *H2D_PLA, *AUTO_SUPPORT, "--no-stl"]
    assert main([*arguments, "--output", str(output)]) == 0
    manifest = json.loads((output / "manifest.json").read_text())
    record = manifest["placement_policy"]["auto_roof_support"]
    assert record["prime_tower_plates"] == [1, 2]
    assert record["prime_tower_groups"] == ["Pull handle - strap bar", "Pull handle - no strap bar"]
    supported = manifest["export"]["roof_support"]["supported_objects"]
    assert [(entry["design"], entry["female_roofs"]) for entry in supported] == [
        (NAMES[True], []),
        (NAMES[False], []),
    ]
    assert manifest["export"]["roof_support"]["unsupported_designs"] == []
    with ZipFile(output / "job.3mf") as archive:
        project = json.loads(archive.read("Metadata/project_settings.config"))
        objects = archive.read("Metadata/model_settings.config").decode()
    assert project["filament_type"] == ["PETG", "PLA"]
    assert (project["enable_support"], project["support_type"]) == ("1", "normal(auto)")
    # The support base inherits the profile Default (the part's PETG); only the interface is PLA.
    assert "support_filament" not in project and project["support_interface_filament"] == "2"
    assert "support_filament" not in project["different_settings_to_system"][0].split(";")
    assert project["support_top_z_distance"] == "0"
    # Both handles inherit global support, so neither carries a per-object override.
    assert 'key="enable_support"' not in objects and 'key="support_type"' not in objects
    assert [entry["object_settings"] for entry in supported] == [{}, {}]


def test_auto_support_reserves_a_tower_on_both_plates(monkeypatch):
    monkeypatch.setattr(pull_handle_set, "pull_handle_design", _stand_in)
    job = extras_job(
        H2D,
        placement_build=h2d_common_build(),
        gap=4,
        orient_for_bambu=True,
        auto_roof_support=True,
        layer_height_mm=0.24,
    )
    assert list(job.prime_tower_positions) == [0, 1]
    assert job.placement_policy["auto_roof_support"]["prime_tower_plates"] == [1, 2]
    assert job.placement_policy["plate_group_minimum_model_gap_mm"] == {
        "Pull handle - strap bar": 8.0,
        "Pull handle - no strap bar": 8.0,
    }
    plain = extras_job(H2D, placement_build=h2d_common_build(), gap=4, orient_for_bambu=True)
    assert plain.prime_tower is None and not plain.prime_tower_positions
    assert "auto_roof_support" not in plain.placement_policy


def test_auto_support_accepts_accessory_support_but_not_a_job_with_nothing_to_support():
    strap_bar = _stand_in(PullHandle(True))
    no_bar = _stand_in(PullHandle(False))
    unsupported = Design(
        "unsupported_handle", Box(120, 60, 70), dict(no_bar.parameters), bambu_object_settings={}
    )
    auto = RoofSupportSettings(mode="auto")
    validate_roof_job(Job([strap_bar, no_bar], H2D, "extras"), auto, 0.32)
    with pytest.raises(ValueError, match="no retained original female pocket roofs"):
        validate_roof_job(Job([unsupported], H2D, "extras"), auto, 0.32)
    with pytest.raises(ValueError, match="no retained original female pocket roofs"):
        validate_roof_job(Job([strap_bar, no_bar], H2D, "part"), RoofSupportSettings(), 0.32)


def test_cli_h2d_pull_handle_extras_keep_the_standard_grid(tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "extras",
                "pull-handle",
                *H2D_OPTIONS,
                "--unit-size-mm",
                "30",
                "--output",
                str(tmp_path / "set"),
            ]
        )
    assert caught.value.code == 2
    assert "--h2d-dual-safe requires original joints, --unit-size-mm 60" in capsys.readouterr().err
