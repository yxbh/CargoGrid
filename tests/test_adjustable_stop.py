"""Adjustable-stop native-CAD geometry and export boundaries."""

import json
from math import floor
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
import pytest
from build123d import Align, Axis, Box, GeomType, Location, Part, Pos, Shape, ShapeList, import_step
from OCP.BRepAdaptor import BRepAdaptor_Surface

from cargo_grid import AdjustableStopSpec, prepared
from cargo_grid.accessories import make_bidirectional_panel_connector
from cargo_grid.adjustable_stop import (
    BASE_ANCHOR_CENTRES_Y_MM,
    CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM,
    DETAIL_EDGE_RADIUS_MM,
    DIMENSIONS,
    FREE_EDGE_RADIUS_MM,
    MOVING_TOOTH_STATIONS_MM,
    PRONG_LOCK_CLIP_DIMENSIONS,
    SCREW_AXES_MM,
    make_adjustable_stop_parts,
    make_adjustable_stop_prong_lock_clip,
)
from cargo_grid.adjustable_stop_export import (
    ADJUSTABLE_STOP_PLATE_NAME,
    _disassembly_proofs,
    _edge_continuity_counts,
    _guide_proofs,
    _pusher_bed_proofs,
    _ratchet_proofs,
    _rounding_proofs,
    adjustable_stop_h2d_settings,
    adjustable_stop_print_job,
    export_adjustable_stop,
    prepare_adjustable_stop_print_job,
    write_adjustable_stop_print_project,
)
from cargo_grid.cli import H2D_PROFILES
from cargo_grid.export import BAMBU_PROCESS_DEFAULTS, write_3mf
from cargo_grid.jobs import Design, Job
from cargo_grid.packing import PrimeTower, PrintPlacement, TowerClearance, h2d_common_build
from cargo_grid.plates import (
    AUTO_SUPPORT_TOWER_CLEARANCE_MM,
    BAMBU_TOWER_MARGIN_MM,
    H2D_SHARED_REACH_X_MM,
    H2D_TOWER_LEFT_CLEARANCE_MM,
    TOWER_BACK_ALLOWANCE_MM,
    BambuTowerEstimate,
)


def _overlap_volume(first, second) -> float:
    common = first.intersect(second)
    if common is None:
        return 0.0
    shapes = common if isinstance(common, ShapeList) else [common]
    return sum(solid.volume for shape in shapes for solid in shape.solids())


def _symmetric_difference_volume(first, second) -> float:
    return sum(solid.volume for solid in first.cut(second).solids()) + sum(
        solid.volume for solid in second.cut(first).solids()
    )


@pytest.mark.parametrize("nozzle_diameter_mm", H2D_PROFILES)
@pytest.mark.parametrize("material_count", [1, 2])
def test_print_job_uses_default_tower_only_for_multi_material_plates(
    tmp_path,
    material_count,
    nozzle_diameter_mm,
):
    tallest_part_mm = 120.2500001
    bambu = adjustable_stop_h2d_settings(
        nozzle_diameter_mm,
        dual_material=material_count > 1,
    )
    profile = H2D_PROFILES[nozzle_diameter_mm]
    assert bambu.nozzle == nozzle_diameter_mm
    assert bambu.layer_height == profile.layer_height_mm
    assert bambu.printer_settings_id == profile.printer
    assert bambu.print_settings_id == profile.process
    assert tuple(material.name for material in bambu.materials) == (
        (profile.petg, profile.pla) if material_count > 1 else (profile.petg,)
    )
    build = h2d_common_build()
    job = prepare_adjustable_stop_print_job(
        Job(
            [Design("adjustable stop test box", Box(20, 20, tallest_part_mm), {})],
            build,
            "adjustable-stop-test",
            part_gap=4,
            print_placements=[PrintPlacement(0, 85, 5, 0)],
            plate_builds={0: build},
        ),
        material_counts_by_plate={0: material_count},
        nozzle_diameter_mm=nozzle_diameter_mm,
    )
    project = tmp_path / f"tower-{nozzle_diameter_mm:g}-{material_count}.3mf"
    write_3mf(
        job,
        project,
        bambu=bambu,
    )
    with ZipFile(project) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    overrides = set(settings["different_settings_to_system"][0].split(";"))
    tower_keys = {"enable_prime_tower", "wipe_tower_x", "wipe_tower_y"}

    if material_count == 1:
        assert job.prime_tower is None
        assert job.prime_tower_positions == {}
        assert tower_keys.isdisjoint(settings)
        assert tower_keys.isdisjoint(overrides)
    else:
        estimate = BambuTowerEstimate(profile.layer_height_mm)
        side = estimate.side_mm(tallest_part_mm)
        brim = estimate.brim_mm(tallest_part_mm)
        limit = H2D_SHARED_REACH_X_MM[1]
        expected_x = floor((limit - BAMBU_TOWER_MARGIN_MM - side) * 100) / 100
        expected_reach = PrimeTower(
            brim,
            brim,
            limit - expected_x,
            side + brim + TOWER_BACK_ALLOWANCE_MM,
        )
        expected_clearance = TowerClearance(
            H2D_TOWER_LEFT_CLEARANCE_MM,
            AUTO_SUPPORT_TOWER_CLEARANCE_MM,
            AUTO_SUPPORT_TOWER_CLEARANCE_MM,
            AUTO_SUPPORT_TOWER_CLEARANCE_MM,
        )
        assert job.prime_tower == expected_reach
        assert set(job.prime_tower_positions) == {0}
        assert job.prime_tower_positions[0] == pytest.approx((expected_x, BAMBU_TOWER_MARGIN_MM))
        assert job.prime_tower_reaches == {0: expected_reach}
        assert job.prime_tower_clearances == {0: expected_clearance}
        x, y = job.prime_tower_positions[0]
        x0, y0, x1, y1 = job.tower_bounds(0)
        assert x0 >= H2D_SHARED_REACH_X_MM[0]
        assert y0 >= 0
        assert y1 <= build.y
        assert x1 == pytest.approx(limit)
        assert y == BAMBU_TOWER_MARGIN_MM
        assert x + BAMBU_TOWER_MARGIN_MM + side <= limit
        assert limit - (x + BAMBU_TOWER_MARGIN_MM + side) < 0.011
        assert x0 - expected_clearance.left >= 85 + 20
        assert settings["wipe_tower_x"] == [f"{x:g}"]
        assert settings["wipe_tower_y"] == [f"{y:g}"]
        assert {"wipe_tower_x", "wipe_tower_y"} <= overrides
        assert "enable_prime_tower" not in settings
        assert "enable_prime_tower" not in overrides
        if nozzle_diameter_mm == 0.4:
            assert (x, y) == pytest.approx((281.42, 15.0))
            assert (x0, y0, x1, y1) == pytest.approx((273.42, 7.0, 325.0, 52.57693534037844))


def test_adjustable_stop_h2d_settings_rejects_unlisted_nozzle():
    with pytest.raises(ValueError, match="must be one of: 0.8, 0.4 mm"):
        adjustable_stop_h2d_settings(0.6, dual_material=True)


def test_adjustable_stop_h2d_settings_defaults_to_tested_0p8_profile():
    settings = adjustable_stop_h2d_settings(dual_material=False)
    profile = H2D_PROFILES[0.8]
    assert settings.nozzle == 0.8
    assert settings.layer_height == profile.layer_height_mm
    assert settings.printer_settings_id == profile.printer
    assert settings.print_settings_id == profile.process


@pytest.mark.parametrize("nozzle_diameter_mm", H2D_PROFILES)
def test_complete_print_project_uses_shared_h2d_support_conventions(
    tmp_path,
    nozzle_diameter_mm,
):
    job = adjustable_stop_print_job(nozzle_diameter_mm)
    assert [design.name for design in job.designs] == [
        "fixed_base",
        "moving_wall",
        "keeper",
        "prong_lock_clip",
    ]
    assert {placement.plate for placement in job.print_placements} == {0}
    assert job.part_gap == 8
    assert set(job.prime_tower_positions) == {0}
    assert job.tower_bounds(0)[2] == pytest.approx(H2D_SHARED_REACH_X_MM[1])

    project = tmp_path / f"adjustable-stop-{nozzle_diameter_mm:g}.3mf"
    write_adjustable_stop_print_project(project, nozzle_diameter_mm)
    with ZipFile(project) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        model = ET.fromstring(archive.read("Metadata/model_settings.config"))
    profile = H2D_PROFILES[nozzle_diameter_mm]
    assert settings["printer_settings_id"] == profile.printer
    assert settings["print_settings_id"] == profile.process
    assert settings["filament_settings_id"] == [profile.petg, profile.pla]
    assert settings["enable_support"] == "1"
    assert settings["support_type"] == "normal(auto)"
    assert {key: settings[key] for key in BAMBU_PROCESS_DEFAULTS} == BAMBU_PROCESS_DEFAULTS
    overrides = set(settings["different_settings_to_system"][0].split(";"))
    assert set(BAMBU_PROCESS_DEFAULTS) <= overrides
    assert {"enable_support", "support_type"} <= overrides
    assert {"wipe_tower_x", "wipe_tower_y"} <= overrides
    inherited = {"support_filament", "support_interface_top_layers", "support_on_build_plate_only"}
    assert inherited.isdisjoint(settings)
    assert inherited.isdisjoint(overrides)
    assert settings["support_interface_filament"] == "2"

    object_settings = {}
    part_names = set()
    for item in model.findall("object"):
        metadata = {
            child.get("key"): child.get("value")
            for child in item.findall("metadata")
            if child.get("key")
        }
        object_settings[metadata["name"]] = metadata
        part_names.update(
            child.get("value")
            for part in item.findall("part")
            for child in part.findall("metadata")
            if child.get("key") == "name"
        )
    assert set(object_settings) == {
        "Fixed base - connectors down_batch_1",
        "Moving wall - fingers down_batch_2",
        "Keeper - countersinks up_batch_3",
        "Prong lock clip - side lying_batch_4",
    }
    assert part_names == {
        "Fixed base - connectors down",
        "Moving wall - fingers down",
        "Keeper - countersinks up",
        "Prong lock clip - side lying",
    }
    plate = model.find("plate")
    assert plate is not None
    plate_metadata = {
        child.get("key"): child.get("value")
        for child in plate.findall("metadata")
        if child.get("key")
    }
    assert plate_metadata["plater_name"] == ADJUSTABLE_STOP_PLATE_NAME
    assert not any(
        term in ET.tostring(model, encoding="unicode").casefold()
        for term in ("accepted", "candidate", "v9c", "v10b")
    )
    assert not any(
        term in json.dumps(job.manifest_metadata).casefold()
        for term in ("accepted", "candidate", "v9c", "v10b")
    )
    supported = [
        values
        for name, values in object_settings.items()
        if name.startswith(("Fixed base", "Moving wall"))
    ]
    unsupported = [
        values
        for name, values in object_settings.items()
        if name.startswith(("Keeper", "Prong lock clip"))
    ]
    assert len(supported) == 2
    assert all(
        "enable_support" not in values and "support_type" not in values for values in supported
    )
    assert len(unsupported) == 2
    assert all(
        "enable_support" not in values and "support_type" not in values for values in unsupported
    )


def test_approved_parts_are_three_valid_native_solids():
    base, pusher, keeper = make_adjustable_stop_parts()
    assert [part.label for part in (base, pusher, keeper)] == [
        "fixed_base",
        "moving_wall",
        "keeper",
    ]
    assert all(
        part.is_valid and len(part.solids()) == 1 and part.volume > 0
        for part in (base, pusher, keeper)
    )
    assert DIMENSIONS.wall_thickness == 8
    assert tuple(pusher.bounding_box().size) == pytest.approx((60, 138.3, 120.25))
    assert pusher.bounding_box().min.Y == pytest.approx(-38.3)
    assert pusher.bounding_box().min.Z == pytest.approx(5.1)
    assert pusher.bounding_box().max.Z == pytest.approx(125.35)
    assert base.bounding_box().min.Z == pytest.approx(-12.8)
    assert tuple(base.bounding_box().size)[:2] == pytest.approx((60, 120))
    assert base.bounding_box().min.Y == pytest.approx(10)
    assert base.bounding_box().max.Y == pytest.approx(130)
    assert keeper.bounding_box().min.Y == pytest.approx(31)
    assert keeper.bounding_box().max.Y == pytest.approx(49)
    assert DIMENSIONS.extension == pytest.approx(48)
    assert DIMENSIONS.prong_shortening == pytest.approx(6.5)
    assert DIMENSIONS.flexible_beam_length == pytest.approx(65.5)
    assert DIMENSIONS.keeper_back_shift == pytest.approx(3)
    assert SCREW_AXES_MM == (
        (-26.0, 35.5),
        (26.0, 35.5),
        (-26.0, 44.5),
        (26.0, 44.5),
    )
    assert _overlap_volume(base, keeper) < 1e-6


def test_base_has_two_identical_60_mm_pitch_underbody_anchors():
    base, _, _ = make_adjustable_stop_parts()
    assert BASE_ANCHOR_CENTRES_Y_MM == (40, 100)
    anchors = []
    for centre_y in BASE_ANCHOR_CENTRES_Y_MM:
        probe = Location((-30, centre_y - 25, -13)) * Box(
            60,
            50,
            13,
            align=(Align.MIN, Align.MIN, Align.MIN),
        )
        anchor = Part(base.intersect(probe).solids())
        bounds = anchor.bounding_box()
        assert anchor.is_valid and len(anchor.solids()) == 1
        assert (bounds.min.Y + bounds.max.Y) / 2 == pytest.approx(centre_y)
        assert bounds.min.Z == pytest.approx(-DIMENSIONS.anchor_depth)
        assert bounds.max.Z == pytest.approx(0)
        anchors.append(anchor)
    assert (
        _symmetric_difference_volume(
            anchors[0].moved(Location((0, DIMENSIONS.anchor_pitch, 0))),
            anchors[1],
        )
        < 1e-6
    )


def test_front_connectors_reuse_shared_native_panel_brep():
    _, pusher, _ = make_adjustable_stop_parts(AdjustableStopSpec(extension_mm=0))
    source = make_bidirectional_panel_connector()
    assert source.bounding_box().min.Z == pytest.approx(-12.8)
    assert source.bounding_box().max.Z == pytest.approx(2)
    front_y = -DIMENSIONS.wall_thickness + DIMENSIONS.pusher_y_offset
    for height in CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM:
        connector = source.rotate(Axis.X, -90).moved(
            Location(
                (
                    -DIMENSIONS.width / 2,
                    front_y,
                    DIMENSIONS.pusher_z + height + DIMENSIONS.width / 2,
                )
            )
        )
        assert front_y - connector.bounding_box().min.Y == pytest.approx(12.8)
        assert connector.bounding_box().max.Y == pytest.approx(front_y + 2)
        assert _overlap_volume(connector, pusher) == pytest.approx(connector.volume)


def test_compact_pusher_offset_preserves_wall_thickness():
    _, pusher, _ = make_adjustable_stop_parts(AdjustableStopSpec(extension_mm=0))
    wall_only = pusher.intersect(
        Location((20, -2, 64))
        * Box(
            5,
            10,
            2,
            align=(Align.MIN, Align.MIN, Align.MIN),
        )
    )
    bounds = Part(wall_only.solids()).bounding_box()
    assert bounds.min.Y == pytest.approx(-1.5)
    assert bounds.max.Y == pytest.approx(6.5)
    assert bounds.size.Y == pytest.approx(DIMENSIONS.wall_thickness)


def test_outer_fingers_have_three_equal_pitch_exposed_teeth():
    _, pusher, _ = make_adjustable_stop_parts(AdjustableStopSpec(extension_mm=0))
    assert MOVING_TOOTH_STATIONS_MM == (97.5, 105.5, 113.5)
    outer_edge = DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2
    for side in (-1, 1):
        x0, x1 = sorted(
            (
                side * (outer_edge + 0.001),
                side * (DIMENSIONS.moving_tip + 0.1),
            )
        )
        cutter = Pos(x0, 101.5, DIMENSIONS.pusher_z) * Box(
            x1 - x0,
            21.5,
            14,
            align=(Align.MIN, Align.MIN, Align.MIN),
        )
        exposed = pusher.intersect(cutter)
        assert len(exposed.solids()) == 3
        lock_planes = sorted(solid.bounding_box().max.Y for solid in exposed.solids())
        assert lock_planes == pytest.approx((104.5, 112.5, 120.5))
        assert np.diff(lock_planes) == pytest.approx((8, 8))
        assert all(solid.bounding_box().size.Z == pytest.approx(14) for solid in exposed.solids())


def test_centre_squeeze_tab_continues_centre_prong_and_clip_has_one_side_bed_face():
    _, pusher, _ = make_adjustable_stop_parts()
    for side in (-1, 1):
        side_x = side * DIMENSIONS.centre_width / 2
        continuous_side_faces = []
        for face in pusher.faces():
            if face.geom_type != GeomType.PLANE:
                continue
            bounds = face.bounding_box()
            if (
                bounds.min.X == pytest.approx(side_x, abs=1e-6)
                and bounds.max.X == pytest.approx(side_x, abs=1e-6)
                and bounds.max.Y > DIMENSIONS.pad_start - 24
            ):
                continuous_side_faces.append(face)
        assert len(continuous_side_faces) == 1
        bounds = continuous_side_faces[0].bounding_box()
        assert bounds.max.X == pytest.approx(side_x, abs=1e-6)
        assert bounds.max.Y == pytest.approx(
            DIMENSIONS.pad_start
            + DIMENSIONS.pad_length
            + DIMENSIONS.pusher_y_offset
            - AdjustableStopSpec().extension_mm
            - DETAIL_EDGE_RADIUS_MM
        )
        assert bounds.max.Z > DIMENSIONS.pad_top

    clip = make_adjustable_stop_prong_lock_clip()
    assert clip.is_valid and len(clip.solids()) == 1
    assert clip.label == "prong_lock_clip"
    bed_y = clip.bounding_box().min.Y
    bed_faces = [
        face
        for face in clip.faces()
        if face.geom_type == GeomType.PLANE
        and face.normal_at().Y < -0.99
        and face.bounding_box().min.Y == pytest.approx(bed_y, abs=1e-6)
        and face.bounding_box().max.Y == pytest.approx(bed_y, abs=1e-6)
    ]
    assert len(bed_faces) == 1
    assert bed_faces[0].area == pytest.approx(486.31699101150593)
    assert bed_faces[0].bounding_box().min.Z == pytest.approx(6.6)
    assert bed_faces[0].bounding_box().max.Z == pytest.approx(50.5)
    assert PRONG_LOCK_CLIP_DIMENSIONS.leg_lateral_clearance == 0
    assert PRONG_LOCK_CLIP_DIMENSIONS.side_clearance == pytest.approx(0.05)
    assert PRONG_LOCK_CLIP_DIMENSIONS.top_clearance == pytest.approx(0.05)
    assert (
        DIMENSIONS.pad_inner
        - DIMENSIONS.centre_width / 2
        - 2 * PRONG_LOCK_CLIP_DIMENSIONS.leg_lateral_clearance
    ) == pytest.approx(6.3)
    assert PRONG_LOCK_CLIP_DIMENSIONS.collar_y_clearance == pytest.approx(0.1)
    assert PRONG_LOCK_CLIP_DIMENSIONS.collar_rear_y_clearance == pytest.approx(0.8)
    assert (
        PRONG_LOCK_CLIP_DIMENSIONS.collar_y_clearance
        + PRONG_LOCK_CLIP_DIMENSIONS.collar_rear_y_clearance
    ) == pytest.approx(0.9)
    assert (DIMENSIONS.pad_start - PRONG_LOCK_CLIP_DIMENSIONS.collar_y_clearance) == pytest.approx(
        106.4
    )
    assert (
        DIMENSIONS.pad_start
        + DIMENSIONS.pad_length
        + PRONG_LOCK_CLIP_DIMENSIONS.collar_rear_y_clearance
    ) == pytest.approx(118.3)
    assert PRONG_LOCK_CLIP_DIMENSIONS.loop_inner_width == 24
    assert PRONG_LOCK_CLIP_DIMENSIONS.loop_inner_height == 19
    assert (
        DIMENSIONS.locking_engagement - PRONG_LOCK_CLIP_DIMENSIONS.side_clearance
    ) == pytest.approx(2.95)
    assert (
        PRONG_LOCK_CLIP_DIMENSIONS.loop_outer_width - PRONG_LOCK_CLIP_DIMENSIONS.loop_inner_width
    ) / 2 == pytest.approx(6)
    assert PRONG_LOCK_CLIP_DIMENSIONS.loop_bottom_band == pytest.approx(7)
    assert (
        PRONG_LOCK_CLIP_DIMENSIONS.loop_outer_height
        - PRONG_LOCK_CLIP_DIMENSIONS.loop_bottom_band
        - PRONG_LOCK_CLIP_DIMENSIONS.loop_inner_height
    ) == pytest.approx(5)
    cylindrical_radii = [
        BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()
        for face in clip.faces()
        if face.geom_type == GeomType.CYLINDER
    ]
    assert sum(radius == pytest.approx(3) for radius in cylindrical_radii) == 8
    assert _edge_continuity_counts(clip).get("C0", 0) == 0


@pytest.mark.slow
def test_matched_ratchet_ramps_contact_and_clear_through_one_pitch():
    parts = make_adjustable_stop_parts()
    proof = _ratchet_proofs(parts, AdjustableStopSpec())
    angle = proof["shared_ramp_angle_degrees"]
    assert angle["nominal"] == pytest.approx(53.13010235415598)
    assert angle["maximum_measured_difference"] < 1e-9
    profile = proof["profile"]
    assert profile["pitch_mm"] == 8
    assert profile["base_tooth_depth_mm"] == pytest.approx(4.2)
    assert profile["base_ramp_run_mm"] == pytest.approx(3.15)
    assert profile["base_tip_land_mm"] == pytest.approx(2)
    assert profile["base_root_land_between_teeth_mm"] == pytest.approx(2.85)
    assert profile["moving_tooth_depth_mm"] == pytest.approx(6)
    assert profile["moving_ramp_run_mm"] == pytest.approx(4.5)
    assert profile["moving_tip_land_mm"] == pytest.approx(2)
    assert profile["moving_root_land_between_teeth_mm"] == pytest.approx(1.5)
    assert profile["outer_finger_to_rack_tip_gap_mm"] == pytest.approx(0.5)
    assert profile["locking_engagement_mm"] == pytest.approx(3.0)
    assert profile["full_release_tip_clearance_mm"] == pytest.approx(0.7)
    assert profile["moving_tooth_count_per_outer_finger"] == 3
    positions = proof["backload_contact_positions"]
    assert [row["extension_mm"] for row in positions] == pytest.approx([0, 8, 16, 24, 32, 40, 48])
    assert all(row["backlash_before_contact_mm"] == pytest.approx(0.2) for row in positions)
    assert all(row["contact_distance_mm"] < 1e-9 for row in positions)
    assert all(row["contact_area_per_tooth_mm2"] == pytest.approx(42.75) for row in positions)
    assert all(row["exact_contact_overlap_volume_mm3"] < 1e-6 for row in positions)
    assert all(row["overlap_after_additional_0p001_mm_backload_mm3"] > 0.1 for row in positions)
    sweep = proof["one_pitch_geometric_sweep"]
    assert sweep["maximum_sampled_required_inward_deflection_mm"] == pytest.approx(
        2.9999996040016415
    )
    assert sweep["minimum_sampled_release_margin_mm"] > 0.69
    end = proof["end_retention_and_release"]
    assert end["last_lock_extension_mm"] == 48
    assert end["carrier_contact_extension_mm"] == 49
    assert end["controlled_overtravel_after_last_lock_mm"] == 1
    assert end["carrier_contact_is_lock_position"] is False
    assert end["maximum_required_inward_cam_displacement_mm"] < 3.01
    assert end["released_overlap_after_0p001_mm_beyond_contact_mm3"] > 0.01
    assert len(end["individual_relaxed_and_released_carrier_contacts"]) == 4
    assert all(
        row["contact_distance_mm"] < 1e-9 and row["overlap_after_0p001_mm_overtravel_mm3"] > 0.01
        for row in end["individual_relaxed_and_released_carrier_contacts"]
    )


def test_approved_outer_assembly_shift_preserves_released_clearances():
    assert DIMENSIONS.outer_centre == 15.8
    assert DIMENSIONS.moving_tooth_root == 17.8
    assert DIMENSIONS.rack_root == 25
    assert DIMENSIONS.rack_wall_inner == 24
    assert DIMENSIONS.rack_carrier_inner == 24.7
    assert DIMENSIONS.rack_carrier_outer == 27.5
    assert DIMENSIONS.release_stroke == 3.7
    assert DIMENSIONS.pad_inner == 11.3
    assert DIMENSIONS.pad_outer == 20.3
    assert DIMENSIONS.pad_reference_start == 113
    assert DIMENSIONS.pad_start == 106.5
    assert DIMENSIONS.pad_length == 11
    released_finger_inner = (
        DIMENSIONS.outer_centre - DIMENSIONS.outer_width / 2 - DIMENSIONS.release_stroke
    )
    released_pad_inner = DIMENSIONS.pad_inner - DIMENSIONS.release_stroke
    assert released_finger_inner - DIMENSIONS.guide_outer == pytest.approx(0.15)
    assert released_pad_inner - DIMENSIONS.guide_outer == pytest.approx(0.15)
    assert DIMENSIONS.rack_carrier_outer - DIMENSIONS.rack_carrier_inner == pytest.approx(2.8)


def test_plain_front_centre_guides_bound_rigid_yaw_without_preloading_fingers():
    parts = make_adjustable_stop_parts()
    _, pusher, _ = parts
    proof = _guide_proofs(parts)
    assert DIMENSIONS.guide_start == 13.5
    assert DIMENSIONS.guide_end == 48
    assert proof["architecture"] == "two plain rectangular side walls"
    assert proof["span_y_mm"] == [13.5, 48]
    assert proof["wall_width_mm"] == pytest.approx(2.35)
    assert proof["wall_length_mm"] == pytest.approx(34.5)
    assert proof["exposed_height_mm"] == pytest.approx(11.85)
    assert proof["uniform_top_z_mm"] == pytest.approx(16.85)
    assert proof["inward_caps_or_overhangs"] is False
    assert proof["centre_channel_width_mm"] == pytest.approx(10.2)
    assert proof["centre_finger_clearance_each_side_mm"] == pytest.approx(0.1)
    assert proof["fully_released_outer_finger_clearance_mm"] == pytest.approx(0.15)
    outer = proof["outer_prong_guides"]
    assert DIMENSIONS.finger_guide_clearance == pytest.approx(0.1)
    assert DIMENSIONS.outer_guide_inner == pytest.approx(20.4)
    assert outer["span_y_mm"] == [12, 48]
    assert outer["inner_faces_x_mm"] == pytest.approx([-20.4, 20.4])
    assert outer["nominal_relaxed_clearance_each_side_mm"] == pytest.approx(0.1)
    assert outer["released_tooth_tip_clearance_mm"] == pytest.approx(0.3)
    assert outer["inward_caps_or_overhangs"] is False
    assert all(row["inner_working_face_count"] == 1 for row in outer["pads"])
    assert all(row["continuous_top_plane_count"] == 1 for row in outer["pads"])
    assert all(row["x24_wall_face_remnant_count"] == 0 for row in outer["pads"])
    assert all(row["wall_junction_cylinder_count"] == 0 for row in outer["pads"])
    assert all(row["inner_working_face_edge_continuities"] == {"G1": 4} for row in outer["pads"])
    assert outer["front_planar_face_count"] == 1
    assert np.array(outer["front_planar_face_bounds_mm"])[:, 0] == pytest.approx([-28, 28])
    assert outer["front_x24_edge_count"] == 0
    assert outer["x24_pad_region_c0_seam_edges"] == []
    assert [row["face_count"] for row in outer["front_top_r2_rounds"]] == [1, 1]
    unification = outer["same_domain_unification"]
    assert unification["before_face_count"] == unification["after_face_count"]
    assert unification["before_edge_count"] == unification["after_edge_count"]
    assert unification["after_face_count"] == unification["reunified_face_count"]
    assert unification["after_edge_count"] == unification["reunified_edge_count"]
    assert unification["reunified_symmetric_brep_difference_mm3"] < 1e-6
    for row in outer["pads"]:
        bounds = np.array(row["inner_working_face_bounds_mm"])
        assert bounds[:, 1] == pytest.approx([12, 48])
        assert bounds[:, 2] == pytest.approx([6, 15.85])
    play = proof["rigid_last_lock_play_comparison"]
    assert play["extension_mm"] == 48
    assert play["tooth_stations_y_mm"] == pytest.approx([56, 64, 72])
    before = play["centre_guide_only"]
    after = play["centre_and_outer_guides"]
    assert before["nominal_peak_to_peak_lateral_translation_mm"] == pytest.approx(0.2)
    assert after["nominal_peak_to_peak_lateral_translation_mm"] == pytest.approx(0.2)
    assert after["maximum_yaw_degrees"] < before["maximum_yaw_degrees"]
    assert [
        row["peak_to_peak_lateral_play_mm"] for row in after["tooth_station_yaw_excursions"]
    ] == pytest.approx([0.2897996179, 0.3789687311, 0.4681378443])
    seat = proof["guide_static_keeper_seat"]
    assert seat["keeper_underside_z_mm"] == pytest.approx(16.85)
    assert seat["surface_contact_expected"] is True
    assert all(row["distance_to_static_keeper_mm"] < 1e-6 for row in seat["checks"])
    assert all(row["overlap_volume_mm3"] < 1e-6 for row in seat["checks"])
    vertical = proof["low_play_vertical_passage"]
    assert vertical["base_floor_top_z_mm"] == pytest.approx(5)
    assert vertical["finger_bottom_z_mm"] == pytest.approx(5.1)
    assert vertical["finger_top_z_mm"] == pytest.approx(16.75)
    assert vertical["keeper_underside_z_mm"] == pytest.approx(16.85)
    assert vertical["clearance_below_mm"] == pytest.approx(0.1)
    assert vertical["clearance_above_mm"] == pytest.approx(0.1)
    assert vertical["total_nominal_clearance_mm"] == pytest.approx(0.2)
    assert all(overlap < 1e-6 for overlap in vertical["former_pad_region_overlap_volumes_mm3"])
    assert np.array(vertical["flat_floor_passage_ranges_x_mm"]) == pytest.approx(
        np.array([[-20.4, -7.45], [-5.1, 5.1], [7.45, 20.4]])
    )
    for x0, x1 in ((-20.3, -11.3), (-5, 5), (11.3, 20.3)):
        probe = Location((x0, 25, 4)) * Box(
            x1 - x0,
            1,
            14,
            align=(Align.MIN, Align.MIN, Align.MIN),
        )
        section = Part(pusher.intersect(probe).solids())
        assert section.bounding_box().min.Z == pytest.approx(5.1)
        assert section.bounding_box().max.Z == pytest.approx(16.75)
        assert section.bounding_box().size.Z == pytest.approx(11.65)
    assert proof["selected_front_edge_to_guide_start_land_mm"] == pytest.approx(1.5)
    assert proof["minimum_continuous_centre_overlap_mm"] == pytest.approx(34.5)
    assert proof["rigid_centre_finger_guide_only_yaw_limit_degrees"] < 0.5
    assert all(row["missing_from_base_mm3"] < 1e-6 for row in proof["walls"])


@pytest.mark.parametrize("released", [False, True])
def test_pusher_bed_facing_underside_is_coplanar_with_fingers(released):
    spec = AdjustableStopSpec(released_illustration=released)
    parts = make_adjustable_stop_parts(spec)
    proof = _pusher_bed_proofs(parts, spec)
    assert DIMENSIONS.pusher_bed_bottom == DIMENSIONS.finger_bottom == pytest.approx(-0.25)
    assert proof["named_local_datum_mm"] == pytest.approx(-0.25)
    assert proof["world_bed_plane_z_mm"] == pytest.approx(5.1)
    assert proof["downward_planar_face_count_within_0p5_mm"] == 1
    face = proof["downward_planar_faces_within_0p5_mm"][0]
    assert face["area_mm2"] > 3200
    assert np.array(face["bounds_mm"])[:, 2] == pytest.approx([5.1, 5.1], abs=1e-6)
    assert proof["minimum_vertical_gap_to_base_floor_mm"] == pytest.approx(0.1)
    assert proof["minimum_vertical_gap_to_mat_z0_mm"] == pytest.approx(5.1)
    assert proof["wall_bottom_below_upright_tile_mm"] == pytest.approx(0.25, abs=1e-6)
    assert proof["upright_tile_overlap_volume_mm3"] < 1e-6
    assert proof["upright_tile_surface_distance_mm"] < 1e-6
    assert set(proof["coplanar_features"]) == {
        "wall backing",
        "finger undersides",
        "reinforcement roots",
        "moving tooth carriers",
        "moving teeth",
    }


@pytest.mark.slow
def test_tall_squeeze_pads_clear_normal_travel_and_require_keeper_off_for_removal():
    parts = make_adjustable_stop_parts()
    proof = _disassembly_proofs(parts)
    revision = proof["tall_pad_revision"]
    assert revision["pad_count"] == 3
    assert DIMENSIONS.pad_height == 24
    assert revision["pad_height_mm"] == 24
    assert revision["outer_pad_width_mm"] == 9
    assert revision["centre_pad_width_mm"] == 10
    assert revision["pad_length_mm"] == 11
    assert revision["finger_height_mm"] == pytest.approx(11.65)
    assert revision["finger_top_above_pusher_floor_mm"] == pytest.approx(11.4)
    assert revision["pad_above_finger_mm"] == pytest.approx(12.6)
    assert revision["pad_top_world_z_mm"] == pytest.approx(29.35)
    assert revision["keeper_roof_bottom_z_mm"] == 22
    assert revision["pad_top_edge_radius_mm"] == 1
    assert revision["base_brep_difference_from_short_pad_revision_mm3"] < 1e-6
    assert revision["pusher_brep_difference_outside_pad_top_regions_mm3"] < 1e-6
    assert revision["middle_pad_root_overlap_mm3"] > 0
    assert revision["middle_pad_missing_from_pusher_mm3"] < 1e-6
    assert len(revision["outer_pad_release_sweep"]) == 38
    assert revision["minimum_outer_to_middle_pad_clearance_mm"] == pytest.approx(2.6)
    assert all(
        overlap < 1e-6
        for row in revision["outer_pad_release_sweep"]
        for overlap in row["outer_pad_overlap_volumes_mm3"]
    )

    normal = proof["normal_adjustment"]
    assert normal["extension_range_mm"] == [0, 48]
    assert normal["minimum_pad_to_keeper_longitudinal_gap_mm"] == 16
    assert len(normal["poses_checked"]) == 14
    assert all(row["base_overlap_volume_mm3"] < 1e-6 for row in normal["poses_checked"])
    assert all(row["keeper_overlap_volume_mm3"] < 1e-6 for row in normal["poses_checked"])

    installed = proof["keeper_installed_withdrawal"]
    assert installed["supported"] is False
    assert installed["last_lock_extension_mm"] == 48
    assert installed["carrier_contact_extension_mm"] == 49
    assert installed["controlled_overtravel_after_last_lock_mm"] == 1
    assert installed["carrier_contact_is_lock_position"] is False
    assert installed["first_checked_blocking_overrun_mm"] == pytest.approx(1.001)
    assert installed["blocking_overlap_volume_mm3"] > 0.01
    removed = proof["keeper_removed_withdrawal"]
    assert removed["supported"] is True
    assert removed["released_pusher"] is True
    assert removed["maximum_base_overlap_volume_mm3"] < 1e-6
    assert removed["final_y_clearance_mm"] > 0


def test_tooth_carriers_stop_overtravel_even_when_released():
    _, relaxed, keeper = make_adjustable_stop_parts(AdjustableStopSpec(48))
    _, released, released_keeper = make_adjustable_stop_parts(
        AdjustableStopSpec(48, released_illustration=True)
    )
    for pusher, stop in ((relaxed, keeper), (released, released_keeper)):
        before = pusher.moved(Location((0, -0.999, 0)))
        contact = pusher.moved(Location((0, -1.0, 0)))
        beyond = pusher.moved(Location((0, -1.001, 0)))
        assert _overlap_volume(before, stop) < 1e-6
        assert _overlap_volume(contact, stop) < 1e-6
        assert contact.distance_to(stop) < 1e-9
        assert _overlap_volume(beyond, stop) > 0.01


def test_obsolete_mini_removal_shoulders_are_absent():
    _, pusher, _ = make_adjustable_stop_parts(AdjustableStopSpec(0))
    probes = [
        Location((x0, 94.5, DIMENSIONS.finger_top_z + 0.01))
        * Box(
            x1 - x0,
            2.5,
            4,
            align=(Align.MIN, Align.MIN, Align.MIN),
        )
        for x0, x1 in (
            (
                -DIMENSIONS.outer_centre - DIMENSIONS.outer_width / 2,
                -DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2,
            ),
            (
                DIMENSIONS.outer_centre - DIMENSIONS.outer_width / 2,
                DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2,
            ),
        )
    ]
    assert all(_overlap_volume(pusher, probe) < 1e-6 for probe in probes)


def test_keeper_and_base_preserve_requested_fastener_geometry():
    base, _, keeper = make_adjustable_stop_parts()
    cones = [face for face in keeper.faces() if face.geom_type == GeomType.CONE]
    assert len(cones) == 4
    bottom_faces = [
        face
        for face in keeper.faces()
        if face.geom_type == GeomType.PLANE
        and face.bounding_box().min.Z == pytest.approx(DIMENSIONS.keeper_seat)
        and face.bounding_box().max.Z == pytest.approx(DIMENSIONS.keeper_seat)
        and face.bounding_box().size.X == pytest.approx(DIMENSIONS.keeper_width)
        and face.bounding_box().size.Y == pytest.approx(DIMENSIONS.keeper_length)
    ]
    assert len(bottom_faces) == 1
    for face in cones:
        circles = sorted(
            (edge for edge in face.edges() if edge.geom_type == GeomType.CIRCLE),
            key=lambda edge: edge.radius,
        )
        diameters = sorted({round(2 * edge.radius, 6) for edge in circles})
        assert diameters == pytest.approx((3.4, 7.0))
        assert face.bounding_box().size.Z == pytest.approx(1.8)
    pilots = []
    for face in base.faces():
        if face.geom_type != GeomType.CYLINDER:
            continue
        bounds = face.bounding_box()
        centre = (
            round((bounds.min.X + bounds.max.X) / 2, 6),
            round((bounds.min.Y + bounds.max.Y) / 2, 6),
        )
        if centre in SCREW_AXES_MM:
            pilots.append(face)
    assert len(pilots) == 4
    assert all(
        2 * face.edges().filter_by(GeomType.CIRCLE)[0].radius == pytest.approx(2.7)
        for face in pilots
    )
    assert all(face.bounding_box().size.Z == pytest.approx(11.85) for face in pilots)
    assert DIMENSIONS.screw_pilot_boss_radius - DIMENSIONS.screw_pilot / 2 == pytest.approx(2.0)


def test_feature_ordered_rounding_preserves_working_regions_and_finger_sections():
    spec = AdjustableStopSpec()
    parts = make_adjustable_stop_parts(spec)
    proof = _rounding_proofs(parts, spec)

    assert proof["radii_mm"] == {
        "free_exterior": FREE_EDGE_RADIUS_MM,
        "detail_and_constrained_transition": DETAIL_EDGE_RADIUS_MM,
    }
    radius_counts = proof["cylindrical_face_radius_counts_mm"]
    assert radius_counts["fixed_base"]["2"] >= 36
    assert radius_counts["moving_wall"]["2"] >= 33
    assert radius_counts["moving_wall"]["1"] == 53
    assert radius_counts["keeper"]["1"] >= 6
    assert all(counts["G1"] > 0 for counts in proof["adjacent_edge_continuity_counts"].values())
    sections = proof["finger_cross_sections"]
    assert sections["outer_each_mm2"] == pytest.approx(103.9915926535898)
    assert sections["centre_mm2"] == pytest.approx(115.64159265359)
    assert all(
        item["brep_difference_mm3"] < 1e-6
        for item in proof["protected_region_brep_differences"].values()
    )
    assert all(
        missing < 1e-6 for missing in proof["protected_functional_tool_missing_volumes"].values()
    )
    assert all(delta < 1e-5 for delta in proof["maximum_bounds_deltas_mm"].values())
    exceptions = {
        (item["part"], item["region"]): item["reason"] for item in proof["intentional_sharp_edges"]
    }
    assert "1.3 mm" in exceptions[("keeper", "two top side edges")]
    assert "R0.05" in exceptions[("moving_wall", "six wall-root side transitions")]


def test_export_refuses_nonempty_output_before_building_geometry(
    tmp_path: Path,
    monkeypatch,
):
    output = tmp_path / "occupied"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep")

    def fail_if_built(*_args, **_kwargs):
        pytest.fail("occupied-output refusal must happen before geometry construction")

    monkeypatch.setattr(
        "cargo_grid.adjustable_stop_export.make_adjustable_stop_parts",
        fail_if_built,
    )
    with pytest.raises(ValueError, match="output directory is not empty"):
        export_adjustable_stop(output)
    assert sentinel.read_text() == "keep"


@pytest.mark.slow
def test_complete_export_is_native_step_first_and_refuses_overwrite(tmp_path: Path):
    output = tmp_path / "job"
    manifest_path = export_adjustable_stop(output)
    manifest = json.loads(manifest_path.read_text())
    assert set(path.name for path in output.iterdir()) == {
        "fixed_base.stl",
        "manifest.json",
        "moving_wall.stl",
        "provenance.json",
        "keeper.stl",
        "adjustable_stop.3mf",
        "adjustable_stop.step",
    }
    assert manifest["kind"] == "adjustable-stop"
    assert not any(
        term in json.dumps(manifest).casefold() for term in ("accepted", "candidate", "v9c", "v10b")
    )
    assert manifest["design_mode"]["workflow"] == "native Cargo-Grid BREP"
    assert manifest["design_mode"]["catalogue_member"] is False
    assert manifest["parameters"]["wall_backing_thickness_mm"] == 8
    assert manifest["parameters"]["base_passage_floor_top_z_mm"] == 5
    assert manifest["parameters"]["finger_bottom_z_mm"] == pytest.approx(5.1)
    assert manifest["parameters"]["finger_top_z_mm"] == pytest.approx(16.75)
    assert manifest["parameters"]["pusher_bed_plane_z_mm"] == pytest.approx(5.1)
    assert manifest["parameters"]["push_pad_count"] == 3
    assert "lower_bearing_top_z_mm" not in manifest["parameters"]
    assert manifest["geometry"]["manufactured_parts"] == 3
    assert manifest["geometry"]["complete_representation"] is True
    anchors = manifest["geometry"]["underbody_base_anchors"]
    assert anchors["count"] == 2
    assert anchors["centres_y_mm"] == [40, 100]
    assert anchors["pitch_mm"] == 60
    assert anchors["projection_below_base_mm"] == 12.8
    assert anchors["translated_brep_difference_mm3"] < 1e-6
    pusher_bed = manifest["geometry"]["pusher_bed_facing_underside"]
    assert pusher_bed["downward_planar_face_count_within_0p5_mm"] == 1
    assert pusher_bed["world_bed_plane_z_mm"] == pytest.approx(5.1)
    assert pusher_bed["minimum_vertical_gap_to_base_floor_mm"] == pytest.approx(0.1)
    assert pusher_bed["upright_tile_overlap_volume_mm3"] < 1e-6
    assert all(
        part["valid"] and part["solids"] == 1
        for part in manifest["geometry"]["step_roundtrip_parts"].values()
    )
    assert all(
        proof["shared_panel_connector_brep_difference_mm3"] < 1e-6
        for proof in manifest["geometry"]["native_front_interfaces"]
    )
    assert (
        manifest["geometry"]["unaffected_backing_and_mechanism"][
            "protected_backing_and_mechanism_brep_difference_mm3"
        ]
        < 1e-6
    )
    edge_rounding = manifest["geometry"]["edge_rounding"]
    assert edge_rounding["radii_mm"]["free_exterior"] == 2
    assert edge_rounding["radii_mm"]["detail_and_constrained_transition"] == 1
    assert all(
        item["brep_difference_mm3"] < 1e-6
        for item in edge_rounding["protected_region_brep_differences"].values()
    )
    assert all(
        missing < 1e-6
        for missing in edge_rounding["protected_functional_tool_missing_volumes"].values()
    )
    ratchet = manifest["geometry"]["ratchet_geometry"]
    assert ratchet["shared_ramp_angle_degrees"]["maximum_measured_difference"] < 1e-9
    assert ratchet["profile"]["locking_engagement_mm"] == pytest.approx(3.0)
    assert ratchet["profile"]["full_release_tip_clearance_mm"] == pytest.approx(0.7)
    assert len(ratchet["backload_contact_positions"]) == 7
    disassembly = manifest["geometry"]["adjustment_and_disassembly"]
    assert disassembly["normal_adjustment"]["minimum_pad_to_keeper_longitudinal_gap_mm"] == 16
    assert disassembly["keeper_installed_withdrawal"]["supported"] is False
    assert disassembly["keeper_removed_withdrawal"]["supported"] is True
    assert manifest["export"]["primary_format"] == "STEP"
    assert manifest["export"]["step"] == "adjustable_stop.step"
    assert manifest["export"]["sliced"] is False

    restored = import_step(output / "adjustable_stop.step")
    assert restored.is_valid
    assert len(restored.solids()) == 3
    assert {part.label for part in restored.children} == {"fixed_base", "moving_wall", "keeper"}
    with ZipFile(output / "adjustable_stop.3mf") as archive:
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
    metadata = {item.get("name"): item.text for item in model.findall("{*}metadata")}
    assert metadata["CargoGridXSource"] == (
        "cargo_grid.accessories.make_bidirectional_panel_connector"
    )
    assert {item.get("name") for item in model.findall("{*}resources/{*}object")} == {
        "fixed_base",
        "moving_wall",
        "keeper",
    }

    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(ValueError, match="not empty"):
        export_adjustable_stop(output)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


@pytest.mark.slow
def test_export_measures_each_owned_part_before_reusing_one_checked_mesh(
    tmp_path: Path,
    monkeypatch,
):
    meshed = set()
    original_mesh = prepared.checked_mesh
    original_bounds = Shape.bounding_box

    def checked(shape):
        assert id(shape) not in meshed
        meshed.add(id(shape))
        return original_mesh(shape)

    def bounds(shape, *args, **kwargs):
        assert id(shape) not in meshed
        return original_bounds(shape, *args, **kwargs)

    monkeypatch.setattr(prepared, "checked_mesh", checked)
    monkeypatch.setattr(Shape, "bounding_box", bounds)
    export_adjustable_stop(tmp_path / "job")
    assert len(meshed) == 3
