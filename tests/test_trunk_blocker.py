"""Experimental blocker native-CAD geometry and export boundaries."""

import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
import pytest
from build123d import Align, Axis, Box, GeomType, Location, Part, Pos, Shape, ShapeList, import_step

from cargo_grid import TrunkBlockerSpec, prepared
from cargo_grid.accessories import make_bidirectional_panel_connector
from cargo_grid.trunk_blocker import (
    BASE_ANCHOR_CENTRES_Y_MM,
    CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM,
    DETAIL_EDGE_RADIUS_MM,
    DIMENSIONS,
    FREE_EDGE_RADIUS_MM,
    MOVING_TOOTH_STATIONS_MM,
    SCREW_AXES_MM,
    make_trunk_blocker_parts,
)
from cargo_grid.trunk_blocker_export import (
    _disassembly_proofs,
    _guide_proofs,
    _ratchet_proofs,
    _rounding_proofs,
    export_trunk_blocker,
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


def test_approved_parts_are_three_valid_native_solids():
    base, pusher, keeper = make_trunk_blocker_parts()
    assert [part.label for part in (base, pusher, keeper)] == [
        "fixed_base_analytic_anchor_candidate",
        "moving_wall",
        "short_screwed_keeper",
    ]
    assert all(
        part.is_valid and len(part.solids()) == 1 and part.volume > 0
        for part in (base, pusher, keeper)
    )
    assert DIMENSIONS.wall_thickness == 8
    assert tuple(pusher.bounding_box().size) == pytest.approx((60, 144.8, 120))
    assert pusher.bounding_box().min.Y == pytest.approx(-44.8)
    assert pusher.bounding_box().min.Z == pytest.approx(5.35)
    assert pusher.bounding_box().max.Z == pytest.approx(125.35)
    assert base.bounding_box().min.Z == pytest.approx(-12.8)
    assert tuple(base.bounding_box().size)[:2] == pytest.approx((60, 132))
    assert DIMENSIONS.extension == pytest.approx(48)
    assert len(SCREW_AXES_MM) == 4


def test_base_has_two_identical_60_mm_pitch_underbody_anchors():
    base, _, _ = make_trunk_blocker_parts()
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
    _, pusher, _ = make_trunk_blocker_parts(TrunkBlockerSpec(extension_mm=0))
    source = make_bidirectional_panel_connector()
    assert source.bounding_box().min.Z == pytest.approx(-12.8)
    assert source.bounding_box().max.Z == pytest.approx(2)
    front_y = -DIMENSIONS.wall_thickness
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


def test_moving_wall_thickens_outward_without_moving_rear_datum():
    _, pusher, _ = make_trunk_blocker_parts(TrunkBlockerSpec(extension_mm=0))
    wall_only = pusher.intersect(
        Location((20, -20, 64))
        * Box(
            5,
            22,
            2,
            align=(Align.MIN, Align.MIN, Align.MIN),
        )
    )
    bounds = Part(wall_only.solids()).bounding_box()
    assert bounds.min.Y == pytest.approx(-8)
    assert bounds.max.Y == pytest.approx(0)
    assert bounds.size.Y == pytest.approx(DIMENSIONS.wall_thickness)


def test_outer_fingers_have_three_equal_pitch_exposed_teeth():
    _, pusher, _ = make_trunk_blocker_parts(TrunkBlockerSpec(extension_mm=0))
    assert MOVING_TOOTH_STATIONS_MM == (104.0, 112.0, 120.0)
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
        assert np.diff(lock_planes) == pytest.approx((8, 8))
        assert all(solid.bounding_box().size.Z == pytest.approx(14) for solid in exposed.solids())


def test_matched_ratchet_ramps_contact_and_clear_through_one_pitch():
    parts = make_trunk_blocker_parts()
    proof = _ratchet_proofs(parts, TrunkBlockerSpec())
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
    assert all(row["contact_area_per_tooth_mm2"] == pytest.approx(42.0) for row in positions)
    assert all(row["exact_contact_overlap_volume_mm3"] < 1e-6 for row in positions)
    assert all(row["overlap_after_additional_0p001_mm_backload_mm3"] > 0.1 for row in positions)
    sweep = proof["one_pitch_geometric_sweep"]
    assert sweep["maximum_sampled_required_inward_deflection_mm"] == pytest.approx(
        2.9999996040016415
    )
    assert sweep["minimum_sampled_release_margin_mm"] > 0.69
    end = proof["end_retention_and_release"]
    assert end["last_lock_extension_mm"] == 48
    assert end["carrier_contact_extension_mm"] == 52
    assert end["controlled_overtravel_after_last_lock_mm"] == 4
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
    assert DIMENSIONS.pad_start == 113
    assert DIMENSIONS.pad_length == 11
    released_finger_inner = (
        DIMENSIONS.outer_centre - DIMENSIONS.outer_width / 2 - DIMENSIONS.release_stroke
    )
    released_pad_inner = DIMENSIONS.pad_inner - DIMENSIONS.release_stroke
    assert released_finger_inner - DIMENSIONS.guide_outer == pytest.approx(0.15)
    assert released_pad_inner - DIMENSIONS.guide_outer == pytest.approx(0.15)
    assert DIMENSIONS.rack_carrier_outer - DIMENSIONS.rack_carrier_inner == pytest.approx(2.8)


def test_plain_front_centre_guides_bound_rigid_yaw_without_preloading_fingers():
    parts = make_trunk_blocker_parts()
    proof = _guide_proofs(parts)
    assert DIMENSIONS.guide_start == 4
    assert DIMENSIONS.guide_end == 45
    assert proof["architecture"] == "two plain rectangular side walls"
    assert proof["span_y_mm"] == [4, 45]
    assert proof["wall_width_mm"] == pytest.approx(2.3)
    assert proof["wall_length_mm"] == pytest.approx(41)
    assert proof["exposed_height_mm"] == pytest.approx(11.85)
    assert proof["uniform_top_z_mm"] == pytest.approx(16.85)
    assert proof["inward_caps_or_overhangs"] is False
    assert proof["centre_channel_width_mm"] == pytest.approx(10.3)
    assert proof["centre_finger_clearance_each_side_mm"] == pytest.approx(0.15)
    assert proof["fully_released_outer_finger_clearance_mm"] == pytest.approx(0.15)
    seat = proof["guide_static_keeper_seat"]
    assert seat["keeper_underside_z_mm"] == pytest.approx(16.85)
    assert seat["surface_contact_expected"] is True
    assert all(row["distance_to_static_keeper_mm"] < 1e-6 for row in seat["checks"])
    assert all(row["overlap_volume_mm3"] < 1e-6 for row in seat["checks"])
    vertical = proof["low_play_vertical_passage"]
    assert vertical["lower_bearing_top_z_mm"] == pytest.approx(5.2)
    assert vertical["finger_bottom_z_mm"] == pytest.approx(5.35)
    assert vertical["finger_top_z_mm"] == pytest.approx(16.7)
    assert vertical["keeper_underside_z_mm"] == pytest.approx(16.85)
    assert vertical["clearance_below_mm"] == pytest.approx(0.15)
    assert vertical["clearance_above_mm"] == pytest.approx(0.15)
    assert vertical["total_nominal_clearance_mm"] == pytest.approx(0.3)
    assert vertical["lower_bearing_land_missing_volume_mm3"] < 1e-6
    assert proof["selected_front_edge_to_guide_start_land_mm"] == pytest.approx(1.5)
    assert proof["minimum_continuous_centre_overlap_mm"] == pytest.approx(41)
    assert proof["rigid_centre_finger_guide_only_yaw_limit_degrees"] < 0.5
    assert all(row["missing_from_base_mm3"] < 1e-6 for row in proof["walls"])


def test_tall_squeeze_pads_clear_normal_travel_and_require_keeper_off_for_removal():
    parts = make_trunk_blocker_parts()
    proof = _disassembly_proofs(parts)
    revision = proof["tall_pad_revision"]
    assert DIMENSIONS.pad_height == 24
    assert revision["pad_height_mm"] == 24
    assert revision["finger_height_mm"] == 11.35
    assert revision["pad_above_finger_mm"] == pytest.approx(12.65)
    assert revision["pad_top_world_z_mm"] == pytest.approx(29.35)
    assert revision["keeper_roof_bottom_z_mm"] == 22
    assert revision["pad_top_edge_radius_mm"] == 1
    assert revision["base_brep_difference_from_short_pad_revision_mm3"] < 1e-6
    assert revision["pusher_brep_difference_outside_pad_top_regions_mm3"] < 1e-6

    normal = proof["normal_adjustment"]
    assert normal["extension_range_mm"] == [0, 48]
    assert normal["minimum_pad_to_keeper_longitudinal_gap_mm"] == 19
    assert len(normal["poses_checked"]) == 14
    assert all(row["base_overlap_volume_mm3"] < 1e-6 for row in normal["poses_checked"])
    assert all(row["keeper_overlap_volume_mm3"] < 1e-6 for row in normal["poses_checked"])

    installed = proof["keeper_installed_withdrawal"]
    assert installed["supported"] is False
    assert installed["last_lock_extension_mm"] == 48
    assert installed["carrier_contact_extension_mm"] == 52
    assert installed["controlled_overtravel_after_last_lock_mm"] == 4
    assert installed["carrier_contact_is_lock_position"] is False
    assert installed["first_checked_blocking_overrun_mm"] == pytest.approx(4.001)
    assert installed["blocking_overlap_volume_mm3"] > 0.01
    removed = proof["keeper_removed_withdrawal"]
    assert removed["supported"] is True
    assert removed["released_pusher"] is True
    assert removed["maximum_base_overlap_volume_mm3"] < 1e-6
    assert removed["final_y_clearance_mm"] > 0


def test_tooth_carriers_stop_overtravel_even_when_released():
    _, relaxed, keeper = make_trunk_blocker_parts(TrunkBlockerSpec(48))
    _, released, released_keeper = make_trunk_blocker_parts(
        TrunkBlockerSpec(48, released_illustration=True)
    )
    for pusher, stop in ((relaxed, keeper), (released, released_keeper)):
        before = pusher.moved(Location((0, -3.999, 0)))
        contact = pusher.moved(Location((0, -4.0, 0)))
        beyond = pusher.moved(Location((0, -4.001, 0)))
        assert _overlap_volume(before, stop) < 1e-6
        assert _overlap_volume(contact, stop) < 1e-6
        assert contact.distance_to(stop) < 1e-9
        assert _overlap_volume(beyond, stop) > 0.01


def test_obsolete_mini_removal_shoulders_are_absent():
    _, pusher, _ = make_trunk_blocker_parts(TrunkBlockerSpec(0))
    probes = [
        Location((x0, 94.5, DIMENSIONS.pusher_z + DIMENSIONS.finger_height + 0.01))
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
    base, _, keeper = make_trunk_blocker_parts()
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
    spec = TrunkBlockerSpec()
    parts = make_trunk_blocker_parts(spec)
    proof = _rounding_proofs(parts, spec)

    assert proof["radii_mm"] == {
        "free_exterior": FREE_EDGE_RADIUS_MM,
        "detail_and_constrained_transition": DETAIL_EDGE_RADIUS_MM,
    }
    radius_counts = proof["cylindrical_face_radius_counts_mm"]
    assert radius_counts["fixed_base"]["2"] >= 36
    assert radius_counts["moving_wall"]["2"] >= 33
    assert radius_counts["moving_wall"]["1"] == 46
    assert radius_counts["short_screwed_keeper"]["1"] >= 6
    assert all(counts["G1"] > 0 for counts in proof["adjacent_edge_continuity_counts"].values())
    sections = proof["finger_cross_sections"]
    assert sections["outer_each_mm2"] == pytest.approx(101.2915926535898)
    assert sections["centre_mm2"] == pytest.approx(112.64159265359)
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
    assert "1.3 mm" in exceptions[("short_screwed_keeper", "two top side edges")]
    assert "R0.05" in exceptions[("moving_wall", "six wall-root side transitions")]


def test_complete_export_is_native_step_first_and_refuses_overwrite(tmp_path: Path):
    output = tmp_path / "job"
    manifest_path = export_trunk_blocker(output)
    manifest = json.loads(manifest_path.read_text())
    assert set(path.name for path in output.iterdir()) == {
        "fixed_base.stl",
        "manifest.json",
        "moving_wall.stl",
        "provenance.json",
        "short_screwed_keeper.stl",
        "trunk_blocker.3mf",
        "trunk_blocker.step",
    }
    assert manifest["kind"] == "experimental-trunk-blocker"
    assert manifest["design_mode"]["workflow"] == "native Cargo-Grid BREP"
    assert manifest["design_mode"]["catalogue_member"] is False
    assert manifest["parameters"]["wall_backing_thickness_mm"] == 8
    assert manifest["geometry"]["manufactured_parts"] == 3
    assert manifest["geometry"]["complete_representation"] is True
    anchors = manifest["geometry"]["underbody_base_anchors"]
    assert anchors["count"] == 2
    assert anchors["centres_y_mm"] == [40, 100]
    assert anchors["pitch_mm"] == 60
    assert anchors["projection_below_base_mm"] == 12.8
    assert anchors["translated_brep_difference_mm3"] < 1e-6
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
    assert disassembly["normal_adjustment"]["minimum_pad_to_keeper_longitudinal_gap_mm"] == 19
    assert disassembly["keeper_installed_withdrawal"]["supported"] is False
    assert disassembly["keeper_removed_withdrawal"]["supported"] is True
    assert manifest["export"]["primary_format"] == "STEP"
    assert manifest["export"]["step"] == "trunk_blocker.step"
    assert manifest["export"]["sliced"] is False

    restored = import_step(output / "trunk_blocker.step")
    assert restored.is_valid
    assert len(restored.solids()) == 3
    with ZipFile(output / "trunk_blocker.3mf") as archive:
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
    metadata = {item.get("name"): item.text for item in model.findall("{*}metadata")}
    assert metadata["CargoGridXSource"] == (
        "cargo_grid.accessories.make_bidirectional_panel_connector"
    )

    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(ValueError, match="not empty"):
        export_trunk_blocker(output)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


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
    export_trunk_blocker(tmp_path / "job")
    assert len(meshed) == 3
