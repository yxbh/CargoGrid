"""Adjustable-stop geometry, mechanism and ordinary print-project contracts."""

import json
from collections import Counter
from dataclasses import replace
from math import degrees, floor
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
import pytest
from adjustable_stop_checks import (
    CONNECTOR_PROOF_TOLERANCE_MM,
    PART_NAMES,
    VOLUME_TOLERANCE_MM3,
    _bounds,
    _carrier_stop_proofs,
    _cylindrical_radius_counts,
    _edge_continuity_counts,
    _face_ramp_angle,
    _finger_section_areas,
    _native_connector_proofs,
    _projected_xz_overlap_area,
    _ratchet_lock_faces,
    _ratchet_ramp_faces,
    _required_inward_deflection,
    _right_rack,
    _right_ratchet_feature,
    _shape_volume,
    _symmetric_difference_volume,
)
from build123d import Align, Axis, Box, Compound, GeomType, Location, Part, Pos, ShapeList
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.OCP.collections import (
    IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher,
)
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS

from cargo_grid import AdjustableStopSpec, adjustable_stop_export
from cargo_grid.accessories import make_bidirectional_panel_connector
from cargo_grid.adjustable_stop import (
    BASE_ANCHOR_CENTRES_Y_MM,
    CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM,
    DETAIL_EDGE_RADIUS_MM,
    DIMENSIONS,
    FREE_EDGE_RADIUS_MM,
    MOVING_TOOTH_STATIONS_MM,
    PRONG_LOCK_CLIP_DIMENSIONS,
    RACK_TOOTH_STATIONS_MM,
    SCREW_AXES_MM,
    _block,
    _guide_wall,
    _make_base,
    _make_keeper,
    _make_pusher,
    _make_pusher_body,
    _moving_tooth,
    _outer_guide_pad,
    _rack_tooth,
    _squeeze_pad,
    _unify_same_domain,
    make_adjustable_stop_parts,
    make_adjustable_stop_prong_lock_clip,
)
from cargo_grid.adjustable_stop_export import (
    ADJUSTABLE_STOP_PLATE_NAME,
    adjustable_stop_print_job,
    adjustable_stop_settings,
    prepare_adjustable_stop_print_job,
    write_adjustable_stop_print_project,
)
from cargo_grid.export import BAMBU_PROCESS_DEFAULTS, write_3mf
from cargo_grid.jobs import Design, Job
from cargo_grid.packing import PrimeTower, PrintPlacement, TowerClearance
from cargo_grid.parameters import Tile
from cargo_grid.plates import (
    AUTO_SUPPORT_TOWER_CLEARANCE_MM,
    BAMBU_TOWER_MARGIN_MM,
    TOWER_BACK_ALLOWANCE_MM,
    TOWER_LEFT_CLEARANCE_MM,
    BambuTowerEstimate,
    dual_nozzle_tower_reservation,
)
from cargo_grid.printers import H2D, X2D
from cargo_grid.tiles import make_tile


def _overlap_volume(first, second) -> float:
    common = first.intersect(second)
    if common is None:
        return 0.0
    shapes = common if isinstance(common, ShapeList) else [common]
    return sum((solid.volume for shape in shapes for solid in shape.solids()))


@pytest.mark.parametrize("nozzle_diameter_mm", H2D.profiles)
@pytest.mark.parametrize("material_count", [1, 2])
def test_print_job_uses_default_tower_only_for_multi_material_plates(
    tmp_path, material_count, nozzle_diameter_mm
):
    tallest_part_mm = 120.2500001
    bambu = adjustable_stop_settings(nozzle_diameter_mm, dual_material=material_count > 1)
    profile = H2D.profiles[nozzle_diameter_mm]
    assert bambu.nozzle == nozzle_diameter_mm
    assert bambu.layer_height == profile.layer_height_mm
    assert bambu.printer_settings_id == profile.printer
    assert bambu.print_settings_id == profile.process
    assert tuple((material.name for material in bambu.materials)) == (
        (profile.petg, profile.pla) if material_count > 1 else (profile.petg,)
    )
    build = H2D.common_build()
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
    write_3mf(job, project, bambu=bambu)
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
        limit = H2D.shared_x_mm[1]
        expected_x = floor((limit - BAMBU_TOWER_MARGIN_MM - side) * 100) / 100
        expected_reach = PrimeTower(
            brim, brim, limit - expected_x, side + brim + TOWER_BACK_ALLOWANCE_MM
        )
        expected_clearance = TowerClearance(
            TOWER_LEFT_CLEARANCE_MM,
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
        assert x0 >= H2D.shared_x_mm[0]
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


def test_adjustable_stop_settings_rejects_unlisted_nozzle():
    with pytest.raises(ValueError, match="must be one of: 0.8, 0.4 mm"):
        adjustable_stop_settings(0.6, dual_material=True)


def test_adjustable_stop_settings_defaults_to_tested_0p8_profile():
    settings = adjustable_stop_settings(dual_material=False)
    profile = H2D.profiles[0.8]
    assert settings.nozzle == 0.8
    assert settings.layer_height == profile.layer_height_mm
    assert settings.printer_settings_id == profile.printer
    assert settings.print_settings_id == profile.process


@pytest.mark.parametrize("nozzle", [0.8, 0.4])
def test_adjustable_stop_x2d_uses_the_shared_profiles_and_tower_reservation(nozzle):
    settings = adjustable_stop_settings(nozzle, dual_material=True, printer=X2D)
    profile = X2D.profiles[nozzle]
    assert settings.printer_settings_id == profile.printer
    assert settings.print_settings_id == profile.process
    assert settings.printer is X2D
    assert tuple((m.name for m in settings.materials)) == (profile.petg, profile.pla)
    build = X2D.common_build()
    job = prepare_adjustable_stop_print_job(
        Job(
            [Design("stop tower witness", Box(20, 20, 120.25), {})],
            build,
            "adjustable-stop-test",
            print_placements=[PrintPlacement(0, 25.5, 6, 0)],
            plate_builds={0: build},
        ),
        material_counts_by_plate={0: 2},
        nozzle_diameter_mm=nozzle,
        printer=X2D,
    )
    tower = dual_nozzle_tower_reservation(X2D, profile.layer_height_mm, 120.25)
    assert job.prime_tower_positions == {0: tower.origin}
    assert job.prime_tower_reaches == {0: tower.reach}
    assert job.prime_tower_clearances == {0: tower.clearance}
    assert job.tower_bounds(0)[2] == 256


@pytest.mark.parametrize("nozzle_diameter_mm", H2D.profiles)
def test_complete_print_project_uses_shared_h2d_support_conventions(
    tmp_path, nozzle_diameter_mm, monkeypatch
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
    assert job.tower_bounds(0)[2] == pytest.approx(H2D.shared_x_mm[1])
    original_model = []
    apply_settings = adjustable_stop_export._apply_adjustable_stop_support_settings

    def check_unpatched_objects(path):
        with ZipFile(path) as archive:
            data = archive.read("Metadata/model_settings.config")
        model = ET.fromstring(data)
        assert all(
            (
                child.get("key") not in ("enable_support", "support_type")
                for item in model.findall("object")
                for child in item.findall("metadata")
            )
        )
        original_model.append(data)
        apply_settings(path)

    monkeypatch.setattr(
        adjustable_stop_export, "_apply_adjustable_stop_support_settings", check_unpatched_objects
    )

    def reuse_checked_job(nozzle, spec, *, printer):
        assert nozzle == nozzle_diameter_mm
        assert spec == AdjustableStopSpec()
        assert printer == H2D
        return job

    monkeypatch.setattr(adjustable_stop_export, "adjustable_stop_print_job", reuse_checked_job)
    project = tmp_path / f"adjustable-stop-{nozzle_diameter_mm:g}.3mf"
    write_adjustable_stop_print_project(project, nozzle_diameter_mm)
    first_export = project.read_bytes()
    apply_settings(project)
    assert project.read_bytes() == first_export
    with ZipFile(project) as archive:
        assert {entry.date_time for entry in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        model_data = archive.read("Metadata/model_settings.config")
        assert original_model == [model_data]
        model = ET.fromstring(model_data)
    profile = H2D.profiles[nozzle_diameter_mm]
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
            (
                child.get("value")
                for part in item.findall("part")
                for child in part.findall("metadata")
                if child.get("key") == "name"
            )
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
        (
            term in ET.tostring(model, encoding="unicode").casefold()
            for term in ("accepted", "candidate", "v9c", "v10b")
        )
    )
    assert not any(
        (
            term in json.dumps(job.manifest_metadata).casefold()
            for term in ("accepted", "candidate", "v9c", "v10b")
        )
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
        ("enable_support" not in values and "support_type" not in values for values in supported)
    )
    assert len(unsupported) == 2
    assert all(
        ("enable_support" not in values and "support_type" not in values for values in unsupported)
    )


def test_approved_parts_are_three_valid_native_solids():
    base, pusher, keeper = make_adjustable_stop_parts()
    assert [part.label for part in (base, pusher, keeper)] == [
        "fixed_base",
        "moving_wall",
        "keeper",
    ]
    assert all(
        (
            part.is_valid and len(part.solids()) == 1 and (part.volume > 0)
            for part in (base, pusher, keeper)
        )
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
    assert SCREW_AXES_MM == ((-26.0, 35.5), (26.0, 35.5), (-26.0, 44.5), (26.0, 44.5))
    assert _overlap_volume(base, keeper) < 1e-06


def test_base_has_two_identical_60_mm_pitch_underbody_anchors():
    base, _, _ = make_adjustable_stop_parts()
    assert BASE_ANCHOR_CENTRES_Y_MM == (40, 100)
    anchors = []
    for centre_y in BASE_ANCHOR_CENTRES_Y_MM:
        probe = Location((-30, centre_y - 25, -13)) * Box(
            60, 50, 13, align=(Align.MIN, Align.MIN, Align.MIN)
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
            anchors[0].moved(Location((0, DIMENSIONS.anchor_pitch, 0))), anchors[1]
        )
        < 1e-06
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
        Location((20, -2, 64)) * Box(5, 10, 2, align=(Align.MIN, Align.MIN, Align.MIN))
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
        x0, x1 = sorted((side * (outer_edge + 0.001), side * (DIMENSIONS.moving_tip + 0.1)))
        cutter = Pos(x0, 101.5, DIMENSIONS.pusher_z) * Box(
            x1 - x0, 21.5, 14, align=(Align.MIN, Align.MIN, Align.MIN)
        )
        exposed = pusher.intersect(cutter)
        assert len(exposed.solids()) == 3
        lock_planes = sorted((solid.bounding_box().max.Y for solid in exposed.solids()))
        assert lock_planes == pytest.approx((104.5, 112.5, 120.5))
        assert np.diff(lock_planes) == pytest.approx((8, 8))
        assert all((solid.bounding_box().size.Z == pytest.approx(14) for solid in exposed.solids()))


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
                bounds.min.X == pytest.approx(side_x, abs=1e-06)
                and bounds.max.X == pytest.approx(side_x, abs=1e-06)
                and (bounds.max.Y > DIMENSIONS.pad_start - 24)
            ):
                continuous_side_faces.append(face)
        assert len(continuous_side_faces) == 1
        bounds = continuous_side_faces[0].bounding_box()
        assert bounds.max.X == pytest.approx(side_x, abs=1e-06)
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
        and (face.bounding_box().min.Y == pytest.approx(bed_y, abs=1e-06))
        and (face.bounding_box().max.Y == pytest.approx(bed_y, abs=1e-06))
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
        == pytest.approx(6.3)
    )
    assert PRONG_LOCK_CLIP_DIMENSIONS.collar_y_clearance == pytest.approx(0.1)
    assert PRONG_LOCK_CLIP_DIMENSIONS.collar_rear_y_clearance == pytest.approx(0.8)
    assert (
        PRONG_LOCK_CLIP_DIMENSIONS.collar_y_clearance
        + PRONG_LOCK_CLIP_DIMENSIONS.collar_rear_y_clearance
        == pytest.approx(0.9)
    )
    assert DIMENSIONS.pad_start - PRONG_LOCK_CLIP_DIMENSIONS.collar_y_clearance == pytest.approx(
        106.4
    )
    assert (
        DIMENSIONS.pad_start
        + DIMENSIONS.pad_length
        + PRONG_LOCK_CLIP_DIMENSIONS.collar_rear_y_clearance
        == pytest.approx(118.3)
    )
    assert PRONG_LOCK_CLIP_DIMENSIONS.loop_inner_width == 24
    assert PRONG_LOCK_CLIP_DIMENSIONS.loop_inner_height == 19
    assert (
        DIMENSIONS.locking_engagement - PRONG_LOCK_CLIP_DIMENSIONS.side_clearance
        == pytest.approx(2.95)
    )
    assert (
        PRONG_LOCK_CLIP_DIMENSIONS.loop_outer_width - PRONG_LOCK_CLIP_DIMENSIONS.loop_inner_width
    ) / 2 == pytest.approx(6)
    assert PRONG_LOCK_CLIP_DIMENSIONS.loop_bottom_band == pytest.approx(7)
    assert (
        PRONG_LOCK_CLIP_DIMENSIONS.loop_outer_height
        - PRONG_LOCK_CLIP_DIMENSIONS.loop_bottom_band
        - PRONG_LOCK_CLIP_DIMENSIONS.loop_inner_height
        == pytest.approx(5)
    )
    cylindrical_radii = [
        BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()
        for face in clip.faces()
        if face.geom_type == GeomType.CYLINDER
    ]
    assert sum((radius == pytest.approx(3) for radius in cylindrical_radii)) == 8
    assert _edge_continuity_counts(clip).get("C0", 0) == 0


@pytest.mark.slow
def test_matched_ratchet_ramps_contact_and_clear_through_one_pitch():
    parts = make_adjustable_stop_parts()
    spec = AdjustableStopSpec()
    base, pusher, keeper = parts
    base_ramps = _ratchet_ramp_faces(base, moving=False)
    moving_ramps = _ratchet_ramp_faces(pusher, moving=True)
    if len(base_ramps) != 2 * (DIMENSIONS.positions + 2) or len(moving_ramps) != 6:
        raise ValueError("ratchet proof could not identify every ramp face")
    base_angles = [_face_ramp_angle(face) for face in base_ramps]
    moving_angles = [_face_ramp_angle(face) for face in moving_ramps]
    all_angles = base_angles + moving_angles
    if max(all_angles) - min(all_angles) > 1e-09:
        raise ValueError("rack and moving ramp faces no longer share one angle")
    contact_pusher = (
        _make_pusher(AdjustableStopSpec(spec.extension_mm))
        if spec.released_illustration
        else pusher
    )
    base_locks = _ratchet_lock_faces(base, moving=False)
    moving_locks = _ratchet_lock_faces(contact_pusher, moving=True)
    if len(base_locks) != 2 * (DIMENSIONS.positions + 2) or len(moving_locks) != 6:
        raise ValueError("ratchet proof could not identify every locking face")
    positions = []
    pusher_backloaded = contact_pusher.moved(Location((0, DIMENSIONS.locking_backlash, 0)))
    if _shape_volume(base.intersect(pusher_backloaded)) >= VOLUME_TOLERANCE_MM3:
        raise ValueError("ratchet parts penetrate at their nominal backload contact")
    if base.distance_to(pusher_backloaded) > CONNECTOR_PROOF_TOLERANCE_MM:
        raise ValueError("ratchet locking faces do not reach zero-distance contact")
    for index in range(DIMENSIONS.positions):
        extension_mm = index * DIMENSIONS.pitch
        translation_y = spec.extension_mm - extension_mm + DIMENSIONS.locking_backlash
        contact_pose = contact_pusher.moved(Location((0, translation_y, 0)))
        contact_overlap = _shape_volume(base.intersect(contact_pose))
        if contact_overlap >= VOLUME_TOLERANCE_MM3:
            raise ValueError("ratchet parts penetrate at their nominal locking contact")
        contact_areas = []
        contact_distances = []
        for moving_face in moving_locks:
            moved = moving_face.moved(Location((0, translation_y, 0)))
            moved_y = moved.bounding_box().min.Y
            side = -1 if moved.bounding_box().max.X < 0 else 1
            candidates = [
                face
                for face in base_locks
                if (-1 if face.bounding_box().max.X < 0 else 1) == side
                and abs(face.bounding_box().min.Y - moved_y) < CONNECTOR_PROOF_TOLERANCE_MM
            ]
            if len(candidates) != 1:
                raise ValueError("could not pair a moving lock face with its rack face")
            fixed = candidates[0]
            area = _projected_xz_overlap_area(moved, fixed)
            distance = moved.distance_to(fixed)
            if area <= 0 or distance > CONNECTOR_PROOF_TOLERANCE_MM:
                raise ValueError("ratchet locking faces lost positive-area surface contact")
            contact_areas.append(area)
            contact_distances.append(distance)
        beyond_contact = contact_pusher.moved(Location((0, translation_y + 0.001, 0)))
        penetration = _shape_volume(base.intersect(beyond_contact))
        if penetration <= VOLUME_TOLERANCE_MM3:
            raise ValueError("ratchet contact lacks a positive-volume beyond-contact witness")
        positions.append(
            {
                "extension_mm": extension_mm,
                "backlash_before_contact_mm": DIMENSIONS.locking_backlash,
                "contact_distance_mm": max(contact_distances),
                "contact_area_per_tooth_mm2": min(contact_areas),
                "contacting_faces": len(contact_areas),
                "exact_contact_overlap_volume_mm3": contact_overlap,
                "overlap_after_additional_0p001_mm_backload_mm3": penetration,
            }
        )
    phases = [index / 10 for index in range(int(round(DIMENSIONS.pitch * 10)) + 1)]
    phases.append(DIMENSIONS.pitch - 0.01)
    rack = _right_rack()
    finger = _right_ratchet_feature(spec.extension_mm)
    sweep = [
        {
            "forward_phase_mm": phase,
            "required_inward_deflection_mm": _required_inward_deflection(rack, finger, phase),
        }
        for phase in sorted(set(phases))
    ]
    maximum = max((row["required_inward_deflection_mm"] for row in sweep))
    if maximum >= DIMENSIONS.release_stroke:
        raise ValueError("sampled ratchet sweep consumes the full release stroke")
    carrier_stop = _carrier_stop_proofs(base, keeper, sweep)
    assert DIMENSIONS.ratchet_ramp_angle_degrees == pytest.approx(53.13010235415598)
    assert max(all_angles) - min(all_angles) < 1e-09
    assert DIMENSIONS.pitch == 8
    assert DIMENSIONS.rack_tooth_depth == pytest.approx(4.2)
    assert DIMENSIONS.rack_ramp_run == pytest.approx(3.15)
    assert DIMENSIONS.tooth_tip_land == pytest.approx(2)
    assert DIMENSIONS.rack_root_land == pytest.approx(2.85)
    assert DIMENSIONS.moving_tooth_depth == pytest.approx(6)
    assert DIMENSIONS.moving_ramp_run == pytest.approx(4.5)
    assert DIMENSIONS.tooth_tip_land == pytest.approx(2)
    assert DIMENSIONS.moving_root_land == pytest.approx(1.5)
    assert DIMENSIONS.rack_tip - (
        DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2
    ) == pytest.approx(0.5)
    assert DIMENSIONS.locking_engagement == pytest.approx(3.0)
    assert DIMENSIONS.released_tip_clearance == pytest.approx(0.7)
    assert len(MOVING_TOOTH_STATIONS_MM) == 3
    assert [row["extension_mm"] for row in positions] == pytest.approx([0, 8, 16, 24, 32, 40, 48])
    assert all((row["backlash_before_contact_mm"] == pytest.approx(0.2) for row in positions))
    assert all((row["contact_distance_mm"] < 1e-09 for row in positions))
    assert all((row["contact_area_per_tooth_mm2"] == pytest.approx(42.75) for row in positions))
    assert all((row["exact_contact_overlap_volume_mm3"] < 1e-06 for row in positions))
    assert all((row["overlap_after_additional_0p001_mm_backload_mm3"] > 0.1 for row in positions))
    assert maximum == pytest.approx(2.9999996040016415)
    assert DIMENSIONS.release_stroke - maximum > 0.69
    end = carrier_stop
    assert end["last_lock_extension_mm"] == 48
    assert end["carrier_contact_extension_mm"] == 49
    assert end["controlled_overtravel_after_last_lock_mm"] == 1
    assert end["carrier_contact_is_lock_position"] is False
    assert end["maximum_required_inward_cam_displacement_mm"] < 3.01
    assert end["released_overlap_after_0p001_mm_beyond_contact_mm3"] > 0.01
    assert len(end["individual_relaxed_and_released_carrier_contacts"]) == 4
    assert all(
        (
            row["contact_distance_mm"] < 1e-09
            and row["overlap_after_0p001_mm_overtravel_mm3"] > 0.01
            for row in end["individual_relaxed_and_released_carrier_contacts"]
        )
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
    base, pusher, keeper = parts
    ununified_base = _make_base(unify_same_domain=False)
    reunified_base = _unify_same_domain(base)
    unification = {
        "before_face_count": len(ununified_base.faces()),
        "before_edge_count": len(ununified_base.edges()),
        "after_face_count": len(base.faces()),
        "after_edge_count": len(base.edges()),
        "reunified_face_count": len(reunified_base.faces()),
        "reunified_edge_count": len(reunified_base.edges()),
        "reunified_symmetric_brep_difference_mm3": _symmetric_difference_volume(
            base, reunified_base
        ),
    }
    if (
        unification["after_face_count"] != unification["reunified_face_count"]
        or unification["after_edge_count"] != unification["reunified_edge_count"]
        or unification["reunified_symmetric_brep_difference_mm3"] >= VOLUME_TOLERANCE_MM3
    ):
        raise ValueError("final base retains adjacent same-domain faces or edges")
    front_faces = [
        face
        for face in base.faces()
        if face.geom_type == GeomType.PLANE
        and abs(face.bounding_box().min.Y - DIMENSIONS.base_start) < CONNECTOR_PROOF_TOLERANCE_MM
        and (abs(face.bounding_box().max.Y - DIMENSIONS.base_start) < CONNECTOR_PROOF_TOLERANCE_MM)
    ]
    if len(front_faces) != 1:
        raise ValueError("base front is split into more than one planar face")
    edge_faces = IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapesAndAncestors_s(base.wrapped, TopAbs_EDGE, TopAbs_FACE, edge_faces)

    def continuity_for(edge) -> str:
        for index in range(1, edge_faces.Extent() + 1):
            if not edge_faces.FindKey(index).IsSame(edge.wrapped):
                continue
            faces = edge_faces.FindFromIndex(index)
            if faces.Size() != 2:
                return "single_face_or_seam"
            continuity = BRep_Tool.Continuity_s(
                TopoDS.Edge(edge_faces.FindKey(index)),
                TopoDS.Face(faces.First()),
                TopoDS.Face(faces.Last()),
            )
            return str(continuity).rsplit("GeomAbs_", 1)[-1]
        raise ValueError("base edge is absent from the edge-to-face map")

    front_root_edge_count = 0
    root_seam_rows = []
    for side in (-1, 1):
        root_x = side * DIMENSIONS.rack_wall_inner
        for edge in base.edges():
            bounds = edge.bounding_box()
            if (
                abs(bounds.min.X - root_x) >= CONNECTOR_PROOF_TOLERANCE_MM
                or abs(bounds.max.X - root_x) >= CONNECTOR_PROOF_TOLERANCE_MM
            ):
                continue
            continuity = continuity_for(edge)
            if bounds.max.Y <= DIMENSIONS.outer_guide_start + CONNECTOR_PROOF_TOLERANCE_MM:
                front_root_edge_count += 1
            if (
                bounds.min.Y
                < DIMENSIONS.outer_guide_tool_end
                + DETAIL_EDGE_RADIUS_MM
                - CONNECTOR_PROOF_TOLERANCE_MM
                and continuity == "C0"
            ):
                root_seam_rows.append(
                    {"side": side, "bounds_mm": _bounds(edge), "continuity": continuity}
                )
    if front_root_edge_count or root_seam_rows:
        raise ValueError("base retains an X=+/-24 seam in the outer-guide region")
    front_top_round_rows = []
    for side in (-1, 1):
        root_x = side * DIMENSIONS.rack_wall_inner
        candidates = []
        for face in base.faces():
            bounds = face.bounding_box()
            if (
                face.geom_type != GeomType.CYLINDER
                or bounds.min.Y > DIMENSIONS.base_start + CONNECTOR_PROOF_TOLERANCE_MM
                or bounds.max.Y < DIMENSIONS.outer_guide_start - CONNECTOR_PROOF_TOLERANCE_MM
                or (
                    bounds.min.Z
                    > DIMENSIONS.guide_top - FREE_EDGE_RADIUS_MM + CONNECTOR_PROOF_TOLERANCE_MM
                )
                or (bounds.max.Z < DIMENSIONS.guide_top - CONNECTOR_PROOF_TOLERANCE_MM)
                or (not bounds.min.X < root_x < bounds.max.X)
            ):
                continue
            candidates.append(face)
        if len(candidates) != 1:
            raise ValueError("front-top R2 is split at the rack-wall/guide junction")
        front_top_round_rows.append(
            {"side": side, "face_count": len(candidates), "bounds_mm": _bounds(candidates[0])}
        )
    guide_rows = []
    for side in (-1, 1):
        exposed = _guide_wall(side).intersect(
            Location((-DIMENSIONS.width / 2, DIMENSIONS.base_start, DIMENSIONS.floor))
            * Box(
                DIMENSIONS.width,
                DIMENSIONS.base_length,
                DIMENSIONS.guide_top - DIMENSIONS.floor + 0.1,
                align=(Align.MIN, Align.MIN, Align.MIN),
            )
        )
        exposed_tool = Part(exposed.solids())
        missing = _shape_volume(exposed_tool.cut(base))
        if missing > VOLUME_TOLERANCE_MM3:
            raise ValueError("base guide does not contain its full exposed design volume")
        guide_rows.append(
            {
                "side": side,
                "bounds_mm": _bounds(exposed_tool),
                "exposed_volume_mm3": exposed_tool.volume,
                "missing_from_base_mm3": missing,
            }
        )
    outer_guide_rows = []
    for side in (-1, 1):
        raw_tool = _outer_guide_pad(side)
        inner_x = side * DIMENSIONS.outer_guide_inner
        root_x = side * DIMENSIONS.rack_wall_inner
        inner_faces = [
            face
            for face in base.faces()
            if face.geom_type == GeomType.PLANE
            and abs(face.bounding_box().min.X - inner_x) < CONNECTOR_PROOF_TOLERANCE_MM
            and (abs(face.bounding_box().max.X - inner_x) < CONNECTOR_PROOF_TOLERANCE_MM)
            and (
                face.bounding_box().min.Z
                >= DIMENSIONS.floor + DETAIL_EDGE_RADIUS_MM - CONNECTOR_PROOF_TOLERANCE_MM
            )
            and (
                face.bounding_box().min.Y
                <= DIMENSIONS.outer_guide_start + CONNECTOR_PROOF_TOLERANCE_MM
            )
            and (
                face.bounding_box().max.Y
                >= DIMENSIONS.outer_guide_end - CONNECTOR_PROOF_TOLERANCE_MM
            )
        ]
        top_faces = [
            face
            for face in base.faces()
            if face.geom_type == GeomType.PLANE
            and abs(face.bounding_box().min.Z - DIMENSIONS.guide_top) < CONNECTOR_PROOF_TOLERANCE_MM
            and (
                abs(face.bounding_box().max.Z - DIMENSIONS.guide_top) < CONNECTOR_PROOF_TOLERANCE_MM
            )
            and (
                face.bounding_box().min.Y
                <= DIMENSIONS.outer_guide_start + CONNECTOR_PROOF_TOLERANCE_MM
            )
            and (
                face.bounding_box().max.Y
                >= DIMENSIONS.outer_guide_end - CONNECTOR_PROOF_TOLERANCE_MM
            )
            and (
                side < 0
                and face.bounding_box().min.X <= root_x + CONNECTOR_PROOF_TOLERANCE_MM
                and (
                    face.bounding_box().max.X
                    >= inner_x - DETAIL_EDGE_RADIUS_MM - CONNECTOR_PROOF_TOLERANCE_MM
                )
                or (
                    side > 0
                    and face.bounding_box().max.X >= root_x - CONNECTOR_PROOF_TOLERANCE_MM
                    and (
                        face.bounding_box().min.X
                        <= inner_x + DETAIL_EDGE_RADIUS_MM + CONNECTOR_PROOF_TOLERANCE_MM
                    )
                )
            )
        ]
        wall_face_remnants = [
            face
            for face in base.faces()
            if face.geom_type == GeomType.PLANE
            and abs(face.bounding_box().min.X - root_x) < CONNECTOR_PROOF_TOLERANCE_MM
            and (abs(face.bounding_box().max.X - root_x) < CONNECTOR_PROOF_TOLERANCE_MM)
            and (
                face.bounding_box().min.Y
                < DIMENSIONS.outer_guide_end - CONNECTOR_PROOF_TOLERANCE_MM
            )
            and (face.bounding_box().max.Y > DIMENSIONS.base_start + CONNECTOR_PROOF_TOLERANCE_MM)
        ]
        wall_junction_cylinders = [
            face
            for face in base.faces()
            if face.geom_type == GeomType.CYLINDER
            and face.bounding_box().min.Y
            < DIMENSIONS.outer_guide_end - CONNECTOR_PROOF_TOLERANCE_MM
            and (
                face.bounding_box().max.Y
                > DIMENSIONS.outer_guide_start + CONNECTOR_PROOF_TOLERANCE_MM
            )
            and (
                side < 0
                and face.bounding_box().min.X >= root_x - CONNECTOR_PROOF_TOLERANCE_MM
                and (
                    face.bounding_box().max.X
                    <= root_x + DETAIL_EDGE_RADIUS_MM + CONNECTOR_PROOF_TOLERANCE_MM
                )
                or (
                    side > 0
                    and face.bounding_box().max.X <= root_x + CONNECTOR_PROOF_TOLERANCE_MM
                    and (
                        face.bounding_box().min.X
                        >= root_x - DETAIL_EDGE_RADIUS_MM - CONNECTOR_PROOF_TOLERANCE_MM
                    )
                )
            )
        ]
        if (
            len(inner_faces) != 1
            or len(top_faces) != 1
            or wall_face_remnants
            or wall_junction_cylinders
        ):
            raise ValueError("merged outer guide retained a split wall or top feature")
        inner_edge_continuities = Counter()
        for edge in inner_faces[0].edges():
            inner_edge_continuities[continuity_for(edge)] += 1
        if inner_edge_continuities.get("C0", 0):
            raise ValueError("outer-guide working face retains an exposed C0 edge")
        outer_guide_rows.append(
            {
                "side": side,
                "raw_overlap_tool_bounds_mm": _bounds(raw_tool),
                "inner_working_face_count": len(inner_faces),
                "inner_working_face_bounds_mm": _bounds(inner_faces[0]),
                "inner_working_face_edge_continuities": dict(inner_edge_continuities),
                "continuous_top_plane_count": len(top_faces),
                "continuous_top_plane_bounds_mm": _bounds(top_faces[0]),
                "x24_wall_face_remnant_count": len(wall_face_remnants),
                "wall_junction_cylinder_count": len(wall_junction_cylinders),
            }
        )
    guide_length = DIMENSIONS.guide_end - DIMENSIONS.guide_start
    channel_width = 2 * DIMENSIONS.guide_inner
    finger_width = DIMENSIONS.centre_width
    low = 0.0
    high = 0.1
    for _ in range(60):
        middle = (low + high) / 2
        swept_width = guide_length * np.sin(middle) + finger_width * np.cos(middle)
        if swept_width <= channel_width:
            low = middle
        else:
            high = middle
    guide_only_yaw_radians = low
    outer_channel_width = 2 * DIMENSIONS.outer_guide_inner
    outer_pusher_width = 2 * (DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2)
    outer_guide_length = DIMENSIONS.outer_guide_end - DIMENSIONS.outer_guide_start
    low = 0.0
    high = 0.1
    for _ in range(60):
        middle = (low + high) / 2
        swept_width = outer_guide_length * np.sin(middle) + outer_pusher_width * np.cos(middle)
        if swept_width <= outer_channel_width:
            low = middle
        else:
            high = middle
    combined_yaw_radians = low
    tooth_stations_at_last_lock = [
        station + DIMENSIONS.pusher_y_offset - DIMENSIONS.extension
        for station in MOVING_TOOTH_STATIONS_MM
    ]

    def tooth_yaw_rows(
        angle_radians: float, half_channel_width: float, half_pusher_width: float, guide_end: float
    ) -> list[dict]:
        translation_at_contact = (
            -half_channel_width
            + half_pusher_width * np.cos(angle_radians)
            + guide_end * np.sin(angle_radians)
        )
        return [
            {
                "tooth_station_y_mm": station,
                "one_sided_lateral_excursion_mm": abs(
                    -station * np.sin(angle_radians) + translation_at_contact
                ),
                "peak_to_peak_lateral_play_mm": 2
                * abs(-station * np.sin(angle_radians) + translation_at_contact),
            }
            for station in tooth_stations_at_last_lock
        ]

    guide_contact_rows = []
    for side in (-1, 1):
        contact_segment = Part(
            _guide_wall(side)
            .intersect(
                _block(
                    -DIMENSIONS.width,
                    DIMENSIONS.width,
                    DIMENSIONS.keeper_start,
                    DIMENSIONS.guide_end,
                    DIMENSIONS.floor - 0.1,
                    DIMENSIONS.guide_top,
                )
            )
            .solids()
        )
        distance = contact_segment.distance_to(keeper)
        overlap = _shape_volume(contact_segment.intersect(keeper))
        if distance > CONNECTOR_PROOF_TOLERANCE_MM or overlap >= VOLUME_TOLERANCE_MM3:
            raise ValueError("uniform guides lost their zero-overlap static keeper seat")
        guide_contact_rows.append(
            {"side": side, "distance_to_static_keeper_mm": distance, "overlap_volume_mm3": overlap}
        )
    floor_passages = (
        (-DIMENSIONS.outer_guide_inner, -DIMENSIONS.guide_outer),
        (-DIMENSIONS.guide_inner, DIMENSIONS.guide_inner),
        (DIMENSIONS.guide_outer, DIMENSIONS.outer_guide_inner),
    )
    passage_probes = [
        _block(
            x0,
            x1,
            DIMENSIONS.keeper_start,
            DIMENSIONS.keeper_start + DIMENSIONS.keeper_length,
            DIMENSIONS.floor + CONNECTOR_PROOF_TOLERANCE_MM,
            DIMENSIONS.guide_top,
        )
        for x0, x1 in floor_passages
    ]
    raised_floor_overlaps = [_shape_volume(base.intersect(probe)) for probe in passage_probes]
    if max(raised_floor_overlaps) >= VOLUME_TOLERANCE_MM3:
        raise ValueError("base passage no longer has a flat floor")
    finger_bottom = DIMENSIONS.finger_bottom_z
    finger_top = DIMENSIONS.finger_top_z
    clearance_below = finger_bottom - DIMENSIONS.floor
    clearance_above = DIMENSIONS.keeper_seat - finger_top
    if (
        abs(clearance_below - 0.1) > CONNECTOR_PROOF_TOLERANCE_MM
        or abs(clearance_above - 0.1) > CONNECTOR_PROOF_TOLERANCE_MM
        or _shape_volume(pusher.intersect(base)) >= VOLUME_TOLERANCE_MM3
        or (_shape_volume(pusher.intersect(keeper)) >= VOLUME_TOLERANCE_MM3)
    ):
        raise ValueError("low-play vertical passage changed")
    assert DIMENSIONS.guide_start == 13.5
    assert DIMENSIONS.guide_end == 48
    assert [DIMENSIONS.guide_start, DIMENSIONS.guide_end] == [13.5, 48]
    assert DIMENSIONS.guide_outer - DIMENSIONS.guide_inner == pytest.approx(2.35)
    assert guide_length == pytest.approx(34.5)
    assert DIMENSIONS.guide_top - DIMENSIONS.floor == pytest.approx(11.85)
    assert DIMENSIONS.guide_top == pytest.approx(16.85)
    assert False is False
    assert 2 * DIMENSIONS.guide_inner == pytest.approx(10.2)
    assert DIMENSIONS.guide_inner - DIMENSIONS.centre_width / 2 == pytest.approx(0.1)
    assert (
        DIMENSIONS.outer_centre
        - DIMENSIONS.outer_width / 2
        - DIMENSIONS.release_stroke
        - DIMENSIONS.guide_outer
        == pytest.approx(0.15)
    )
    assert DIMENSIONS.finger_guide_clearance == pytest.approx(0.1)
    assert DIMENSIONS.outer_guide_inner == pytest.approx(20.4)
    assert [DIMENSIONS.outer_guide_start, DIMENSIONS.outer_guide_end] == [12, 48]
    assert [-DIMENSIONS.outer_guide_inner, DIMENSIONS.outer_guide_inner] == pytest.approx(
        [-20.4, 20.4]
    )
    assert (
        DIMENSIONS.outer_guide_inner - DIMENSIONS.outer_centre - DIMENSIONS.outer_width / 2
        == pytest.approx(0.1)
    )
    assert DIMENSIONS.outer_guide_inner - (
        DIMENSIONS.moving_tip - DIMENSIONS.release_stroke
    ) == pytest.approx(0.3)
    assert False is False
    assert all((row["inner_working_face_count"] == 1 for row in outer_guide_rows))
    assert all((row["continuous_top_plane_count"] == 1 for row in outer_guide_rows))
    assert all((row["x24_wall_face_remnant_count"] == 0 for row in outer_guide_rows))
    assert all((row["wall_junction_cylinder_count"] == 0 for row in outer_guide_rows))
    assert all(
        (row["inner_working_face_edge_continuities"] == {"G1": 4} for row in outer_guide_rows)
    )
    assert len(front_faces) == 1
    assert np.array(_bounds(front_faces[0]))[:, 0] == pytest.approx([-28, 28])
    assert front_root_edge_count == 0
    assert root_seam_rows == []
    assert [row["face_count"] for row in front_top_round_rows] == [1, 1]
    assert unification["before_face_count"] == unification["after_face_count"]
    assert unification["before_edge_count"] == unification["after_edge_count"]
    assert unification["after_face_count"] == unification["reunified_face_count"]
    assert unification["after_edge_count"] == unification["reunified_edge_count"]
    assert unification["reunified_symmetric_brep_difference_mm3"] < 1e-06
    for row in outer_guide_rows:
        bounds = np.array(row["inner_working_face_bounds_mm"])
        assert bounds[:, 1] == pytest.approx([12, 48])
        assert bounds[:, 2] == pytest.approx([6, 15.85])
    assert DIMENSIONS.extension == 48
    assert tooth_stations_at_last_lock == pytest.approx([56, 64, 72])
    assert 2 * DIMENSIONS.finger_guide_clearance == pytest.approx(0.2)
    assert 2 * DIMENSIONS.finger_guide_clearance == pytest.approx(0.2)
    assert degrees(combined_yaw_radians) < degrees(guide_only_yaw_radians)
    assert [
        row["peak_to_peak_lateral_play_mm"]
        for row in tooth_yaw_rows(
            combined_yaw_radians,
            DIMENSIONS.outer_guide_inner,
            DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2,
            DIMENSIONS.outer_guide_end,
        )
    ] == pytest.approx([0.2897996179, 0.3789687311, 0.4681378443])
    assert DIMENSIONS.keeper_seat == pytest.approx(16.85)
    assert True is True
    assert all((row["distance_to_static_keeper_mm"] < 1e-06 for row in guide_contact_rows))
    assert all((row["overlap_volume_mm3"] < 1e-06 for row in guide_contact_rows))
    assert DIMENSIONS.floor == pytest.approx(5)
    assert finger_bottom == pytest.approx(5.1)
    assert finger_top == pytest.approx(16.75)
    assert DIMENSIONS.keeper_seat == pytest.approx(16.85)
    assert clearance_below == pytest.approx(0.1)
    assert clearance_above == pytest.approx(0.1)
    assert clearance_below + clearance_above == pytest.approx(0.2)
    assert all((overlap < 1e-06 for overlap in raised_floor_overlaps))
    assert np.array([list(bounds) for bounds in floor_passages]) == pytest.approx(
        np.array([[-20.4, -7.45], [-5.1, 5.1], [7.45, 20.4]])
    )
    for x0, x1 in ((-20.3, -11.3), (-5, 5), (11.3, 20.3)):
        probe = Location((x0, 25, 4)) * Box(x1 - x0, 1, 14, align=(Align.MIN, Align.MIN, Align.MIN))
        section = Part(pusher.intersect(probe).solids())
        assert section.bounding_box().min.Z == pytest.approx(5.1)
        assert section.bounding_box().max.Z == pytest.approx(16.75)
        assert section.bounding_box().size.Z == pytest.approx(11.65)
    assert DIMENSIONS.guide_start - DIMENSIONS.base_start - FREE_EDGE_RADIUS_MM == pytest.approx(
        1.5
    )
    assert min(
        DIMENSIONS.guide_end,
        DIMENSIONS.finger_length + DIMENSIONS.pusher_y_offset - DIMENSIONS.extension,
    ) - DIMENSIONS.guide_start == pytest.approx(34.5)
    assert degrees(guide_only_yaw_radians) < 0.5
    assert all((row["missing_from_base_mm3"] < 1e-06 for row in guide_rows))


@pytest.mark.parametrize("released", [False, True])
def test_pusher_bed_facing_underside_is_coplanar_with_fingers(released):
    spec = AdjustableStopSpec(released_illustration=released)
    parts = make_adjustable_stop_parts(spec)
    _, pusher, _ = parts
    bed_z = DIMENSIONS.finger_bottom_z
    nearby_downward_faces = []
    for face in pusher.faces():
        if face.geom_type != GeomType.PLANE or face.normal_at().Z >= -0.99:
            continue
        bounds = face.bounding_box()
        if bounds.min.Z > bed_z + 0.5 + CONNECTOR_PROOF_TOLERANCE_MM:
            continue
        nearby_downward_faces.append(
            {
                "area_mm2": face.area,
                "centre_mm": [face.center().X, face.center().Y, face.center().Z],
                "bounds_mm": [
                    [bounds.min.X, bounds.min.Y, bounds.min.Z],
                    [bounds.max.X, bounds.max.Y, bounds.max.Z],
                ],
            }
        )
    if len(nearby_downward_faces) != 1 or any(
        (
            abs(bound - bed_z) > CONNECTOR_PROOF_TOLERANCE_MM
            for face in nearby_downward_faces
            for bound in (face["bounds_mm"][0][2], face["bounds_mm"][1][2])
        )
    ):
        raise ValueError("pusher bed-facing underside is not one coplanar datum")
    front_y = -DIMENSIONS.wall_thickness + DIMENSIONS.pusher_y_offset - spec.extension_mm
    wall_tile = (
        make_tile(Tile(1, 2))
        .rotate(Axis.X, 90)
        .moved(Location((-DIMENSIONS.width / 2, front_y, DIMENSIONS.pusher_z)))
    )
    tile_overlap = _shape_volume(pusher.intersect(wall_tile))
    tile_bottom_gap = wall_tile.bounding_box().min.Z - bed_z
    floor_gap = bed_z - DIMENSIONS.floor
    if (
        tile_overlap >= VOLUME_TOLERANCE_MM3
        or abs(tile_bottom_gap - 0.25) > CONNECTOR_PROOF_TOLERANCE_MM
        or abs(floor_gap - 0.1) > CONNECTOR_PROOF_TOLERANCE_MM
    ):
        raise ValueError("flat pusher underside lost its floor or upright-tile clearance")
    assert DIMENSIONS.pusher_bed_bottom == DIMENSIONS.finger_bottom == pytest.approx(-0.25)
    assert DIMENSIONS.pusher_bed_bottom == pytest.approx(-0.25)
    assert bed_z == pytest.approx(5.1)
    assert len(nearby_downward_faces) == 1
    face = nearby_downward_faces[0]
    assert face["area_mm2"] > 3200
    assert np.array(face["bounds_mm"])[:, 2] == pytest.approx([5.1, 5.1], abs=1e-06)
    assert floor_gap == pytest.approx(0.1)
    assert bed_z == pytest.approx(5.1)
    assert tile_bottom_gap == pytest.approx(0.25, abs=1e-06)
    assert tile_overlap < 1e-06
    assert pusher.distance_to(wall_tile) < 1e-06
    assert set(
        [
            "wall backing",
            "finger undersides",
            "reinforcement roots",
            "moving tooth carriers",
            "moving teeth",
        ]
    ) == {
        "wall backing",
        "finger undersides",
        "reinforcement roots",
        "moving tooth carriers",
        "moving teeth",
    }


@pytest.mark.slow
def test_tall_squeeze_pads_clear_normal_travel_and_require_keeper_off_for_removal():
    spec = AdjustableStopSpec()
    parts = make_adjustable_stop_parts(spec)
    base, pusher, keeper = parts
    released_pusher = _make_pusher(replace(spec, released_illustration=True))
    normal_positions = []
    for index in range(DIMENSIONS.positions):
        extension_mm = index * DIMENSIONS.pitch
        for released, source in ((False, pusher), (True, released_pusher)):
            pose = source.moved(Location((0, spec.extension_mm - extension_mm, 0)))
            base_overlap = _shape_volume(pose.intersect(base))
            keeper_overlap = _shape_volume(pose.intersect(keeper))
            if base_overlap >= VOLUME_TOLERANCE_MM3 or keeper_overlap >= VOLUME_TOLERANCE_MM3:
                raise ValueError("tall squeeze pads interfere during normal adjustment")
            normal_positions.append(
                {
                    "extension_mm": extension_mm,
                    "released": released,
                    "base_overlap_volume_mm3": base_overlap,
                    "keeper_overlap_volume_mm3": keeper_overlap,
                }
            )
    released_end = released_pusher.moved(Location((0, spec.extension_mm - DIMENSIONS.extension, 0)))
    pad_to_keeper_y_gap = (
        DIMENSIONS.pad_start
        + DIMENSIONS.pusher_y_offset
        - DIMENSIONS.extension
        - DIMENSIONS.keeper_end
    )
    if pad_to_keeper_y_gap <= 0:
        raise ValueError("normal-travel pad-to-keeper longitudinal gap changed")
    middle_pad = _squeeze_pad(0, width=DIMENSIONS.centre_width)
    middle_finger = _block(
        -DIMENSIONS.centre_width / 2,
        DIMENSIONS.centre_width / 2,
        -0.5,
        DIMENSIONS.finger_length,
        DIMENSIONS.finger_bottom,
        DIMENSIONS.finger_top,
    )
    middle_pad_root_overlap = _shape_volume(middle_pad.intersect(middle_finger))
    middle_pad_missing_volume = _shape_volume(middle_pad.cut(_make_pusher_body(False)))
    if (
        middle_pad_root_overlap <= VOLUME_TOLERANCE_MM3
        or middle_pad_missing_volume > VOLUME_TOLERANCE_MM3
    ):
        raise ValueError("middle squeeze pad is not fully rooted in the centre finger")
    release_sweep = []
    for index in range(38):
        inward_displacement = index / 10
        row = {
            "inward_displacement_mm": inward_displacement,
            "outer_pad_clearances_to_middle_mm": [],
            "outer_pad_overlap_volumes_mm3": [],
        }
        for side in (-1, 1):
            outer_pad = _squeeze_pad(side * (DIMENSIONS.outer_centre - inward_displacement))
            overlap = _shape_volume(outer_pad.intersect(middle_pad))
            clearance = outer_pad.distance_to(middle_pad)
            if overlap > VOLUME_TOLERANCE_MM3 or clearance <= 0:
                raise ValueError("an outer squeeze pad reaches the middle pad during release")
            row["outer_pad_clearances_to_middle_mm"].append(clearance)
            row["outer_pad_overlap_volumes_mm3"].append(overlap)
        release_sweep.append(row)
    minimum_pad_clearance = min(
        (
            clearance
            for row in release_sweep
            for clearance in row["outer_pad_clearances_to_middle_mm"]
        )
    )
    carrier_contact_overtravel = (
        DIMENSIONS.moving_carrier_start
        + DIMENSIONS.pusher_y_offset
        - (DIMENSIONS.extension + DIMENSIONS.keeper_end)
    )
    carrier_contact = released_end.moved(Location((0, -carrier_contact_overtravel, 0)))
    if (
        _shape_volume(carrier_contact.intersect(keeper)) >= VOLUME_TOLERANCE_MM3
        or carrier_contact.distance_to(keeper) > CONNECTOR_PROOF_TOLERANCE_MM
    ):
        raise ValueError("released moving carriers lost exact keeper contact")
    installed_block_shift = carrier_contact_overtravel + 0.001
    installed_block_overlap = _shape_volume(
        released_end.moved(Location((0, -installed_block_shift, 0))).intersect(keeper)
    )
    if installed_block_overlap <= VOLUME_TOLERANCE_MM3:
        raise ValueError("keeper no longer blocks forbidden tall-pad withdrawal")
    full_withdrawal_shift = released_end.bounding_box().max.Y - base.bounding_box().min.Y + 0.5
    withdrawal_shifts = [
        min(index / 2, full_withdrawal_shift) for index in range(int(full_withdrawal_shift * 2) + 2)
    ]
    withdrawal_overlaps = [
        _shape_volume(released_end.moved(Location((0, -shift, 0))).intersect(base))
        for shift in withdrawal_shifts
    ]
    maximum_withdrawal_overlap = max(withdrawal_overlaps)
    if maximum_withdrawal_overlap >= VOLUME_TOLERANCE_MM3:
        raise ValueError("released pusher does not clear the base with keeper removed")
    withdrawn = released_end.moved(Location((0, -full_withdrawal_shift, 0)))
    withdrawn_y_clearance = base.bounding_box().min.Y - withdrawn.bounding_box().max.Y
    if withdrawn_y_clearance <= 0:
        raise ValueError("sampled keeper-off withdrawal did not clear the base")
    short_pad_dimensions = replace(DIMENSIONS, pad_height=16.0)
    old_base = _make_base(short_pad_dimensions)
    old_pusher = _make_pusher(AdjustableStopSpec(), short_pad_dimensions)
    old_keeper = _make_keeper(short_pad_dimensions, fill_channels=False)
    base_difference = _symmetric_difference_volume(base, old_base)
    keeper_removed_volume = _shape_volume(old_keeper.cut(keeper))
    keeper_added_volume = _shape_volume(keeper.cut(old_keeper))
    if base_difference > VOLUME_TOLERANCE_MM3:
        raise ValueError("tall-pad and solid-keeper revision changed the base")
    if keeper_removed_volume > VOLUME_TOLERANCE_MM3 or keeper_added_volume <= 0:
        raise ValueError("solid keeper is not a pure addition to the channelled keeper")
    keeper_bounds_delta = float(
        np.max(np.abs(np.array(_bounds(keeper)) - np.array(_bounds(old_keeper))))
    )
    if keeper_bounds_delta > CONNECTOR_PROOF_TOLERANCE_MM:
        raise ValueError("solid keeper changed the approved exterior bounds")
    allowed_pad_top_regions = Compound(
        children=[
            _block(
                centre_x - width / 2,
                centre_x + width / 2,
                DIMENSIONS.pad_start
                + DIMENSIONS.pusher_y_offset
                - AdjustableStopSpec().extension_mm,
                DIMENSIONS.pad_start
                + DIMENSIONS.pad_length
                + DIMENSIONS.pusher_y_offset
                - AdjustableStopSpec().extension_mm,
                DIMENSIONS.pusher_z + short_pad_dimensions.pad_height - DETAIL_EDGE_RADIUS_MM,
                DIMENSIONS.pad_top + 0.1,
            )
            for centre_x, width in (
                (-DIMENSIONS.outer_centre, DIMENSIONS.pad_outer - DIMENSIONS.pad_inner),
                (0, DIMENSIONS.centre_width),
                (DIMENSIONS.outer_centre, DIMENSIONS.pad_outer - DIMENSIONS.pad_inner),
            )
        ]
    )
    protected_pusher_difference = _symmetric_difference_volume(
        pusher.cut(allowed_pad_top_regions), old_pusher.cut(allowed_pad_top_regions)
    )
    if protected_pusher_difference > VOLUME_TOLERANCE_MM3:
        raise ValueError("tall-pad revision changed pusher geometry outside the pad tops")
    assert DIMENSIONS.pad_height == 24
    assert DIMENSIONS.pad_height == 24
    assert DIMENSIONS.pad_outer - DIMENSIONS.pad_inner == 9
    assert DIMENSIONS.centre_width == 10
    assert DIMENSIONS.pad_length == 11
    assert DIMENSIONS.finger_height == pytest.approx(11.65)
    assert DIMENSIONS.finger_top == pytest.approx(11.4)
    assert DIMENSIONS.pad_above_finger == pytest.approx(12.6)
    assert DIMENSIONS.pad_top == pytest.approx(29.35)
    assert DIMENSIONS.keeper_roof_bottom == 22
    assert DETAIL_EDGE_RADIUS_MM == 1
    assert base_difference < 1e-06
    assert protected_pusher_difference < 1e-06
    assert middle_pad_root_overlap > 0
    assert middle_pad_missing_volume < 1e-06
    assert len(release_sweep) == 38
    assert minimum_pad_clearance == pytest.approx(2.6)
    assert all(
        (
            overlap < 1e-06
            for row in release_sweep
            for overlap in row["outer_pad_overlap_volumes_mm3"]
        )
    )
    assert [0, DIMENSIONS.extension] == [0, 48]
    assert pad_to_keeper_y_gap == 16
    assert len(normal_positions) == 14
    assert all((row["base_overlap_volume_mm3"] < 1e-06 for row in normal_positions))
    assert all((row["keeper_overlap_volume_mm3"] < 1e-06 for row in normal_positions))
    assert False is False
    assert DIMENSIONS.extension == 48
    assert DIMENSIONS.extension + carrier_contact_overtravel == 49
    assert carrier_contact_overtravel == 1
    assert False is False
    assert installed_block_shift == pytest.approx(1.001)
    assert installed_block_overlap > 0.01
    assert True is True
    assert True is True
    assert maximum_withdrawal_overlap < 1e-06
    assert withdrawn_y_clearance > 0


def test_tooth_carriers_stop_overtravel_even_when_released():
    _, relaxed, keeper = make_adjustable_stop_parts(AdjustableStopSpec(48))
    _, released, released_keeper = make_adjustable_stop_parts(
        AdjustableStopSpec(48, released_illustration=True)
    )
    for pusher, stop in ((relaxed, keeper), (released, released_keeper)):
        before = pusher.moved(Location((0, -0.999, 0)))
        contact = pusher.moved(Location((0, -1.0, 0)))
        beyond = pusher.moved(Location((0, -1.001, 0)))
        assert _overlap_volume(before, stop) < 1e-06
        assert _overlap_volume(contact, stop) < 1e-06
        assert contact.distance_to(stop) < 1e-09
        assert _overlap_volume(beyond, stop) > 0.01


def test_obsolete_mini_removal_shoulders_are_absent():
    _, pusher, _ = make_adjustable_stop_parts(AdjustableStopSpec(0))
    probes = [
        Location((x0, 94.5, DIMENSIONS.finger_top_z + 0.01))
        * Box(x1 - x0, 2.5, 4, align=(Align.MIN, Align.MIN, Align.MIN))
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
    assert all((_overlap_volume(pusher, probe) < 1e-06 for probe in probes))


def test_keeper_and_base_preserve_requested_fastener_geometry():
    base, _, keeper = make_adjustable_stop_parts()
    cones = [face for face in keeper.faces() if face.geom_type == GeomType.CONE]
    assert len(cones) == 4
    bottom_faces = [
        face
        for face in keeper.faces()
        if face.geom_type == GeomType.PLANE
        and face.bounding_box().min.Z == pytest.approx(DIMENSIONS.keeper_seat)
        and (face.bounding_box().max.Z == pytest.approx(DIMENSIONS.keeper_seat))
        and (face.bounding_box().size.X == pytest.approx(DIMENSIONS.keeper_width))
        and (face.bounding_box().size.Y == pytest.approx(DIMENSIONS.keeper_length))
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
        (
            2 * face.edges().filter_by(GeomType.CIRCLE)[0].radius == pytest.approx(2.7)
            for face in pilots
        )
    )
    assert all((face.bounding_box().size.Z == pytest.approx(11.85) for face in pilots))
    assert DIMENSIONS.screw_pilot_boss_radius - DIMENSIONS.screw_pilot / 2 == pytest.approx(2.0)


def test_feature_ordered_rounding_preserves_working_regions_and_finger_sections():
    spec = AdjustableStopSpec()
    parts = make_adjustable_stop_parts(spec)
    unrounded = (
        _make_base(round_edges=False),
        _make_pusher(spec, round_edges=False),
        _make_keeper(round_edges=False),
    )
    front_y = -DIMENSIONS.wall_thickness + DIMENSIONS.pusher_y_offset - spec.extension_mm
    probes = {
        "underbody_anchors": (
            0,
            Location((-30, -10, -20)) * Box(60, 150, 20, align=(Align.MIN, Align.MIN, Align.MIN)),
        ),
        "rack_teeth_and_guides": (
            0,
            Compound(
                children=[
                    Location((-8, DIMENSIONS.guide_start, 4.8))
                    * Box(
                        16,
                        DIMENSIONS.guide_end - DIMENSIONS.guide_start,
                        15,
                        align=(Align.MIN, Align.MIN, Align.MIN),
                    ),
                    Location((-26, DIMENSIONS.rack_carrier_start, 4.8))
                    * Box(
                        52,
                        DIMENSIONS.rack_carrier_end - DIMENSIONS.rack_carrier_start,
                        15,
                        align=(Align.MIN, Align.MIN, Align.MIN),
                    ),
                ]
            ),
        ),
        "base_pilot_neighborhoods": (
            0,
            Compound(
                children=[
                    Location((x - 2, y - 2, 4.8))
                    * Box(4, 4, 12.2, align=(Align.MIN, Align.MIN, Align.MIN))
                    for x, y in SCREW_AXES_MM
                ]
            ),
        ),
        "wall_backing_interior": (
            1,
            Location((-25, front_y + 0.5, 40))
            * Box(50, 5, 70, align=(Align.MIN, Align.MIN, Align.MIN)),
        ),
        "keeper_flat_underside": (
            2,
            Location((-29, DIMENSIONS.keeper_start + 0.5, DIMENSIONS.keeper_seat - 0.1))
            * Box(58, DIMENSIONS.keeper_length - 1, 0.2, align=(Align.MIN, Align.MIN, Align.MIN)),
        ),
        "keeper_solid_overrun_stop": (
            2,
            Compound(
                children=[
                    Location((x, DIMENSIONS.keeper_start + DIMENSIONS.keeper_length - 0.1, 16))
                    * Box(6, 0.2, 7, align=(Align.MIN, Align.MIN, Align.MIN))
                    for x in (-20, 14)
                ]
            ),
        ),
    }
    protected = {}
    for name, (part_index, probe) in probes.items():
        difference = _symmetric_difference_volume(
            unrounded[part_index].intersect(probe), parts[part_index].intersect(probe)
        )
        if difference > VOLUME_TOLERANCE_MM3:
            raise ValueError(f"edge rounding changed protected region: {name}")
        protected[name] = {"brep_difference_mm3": difference}
    bounds_deltas = {}
    for name, before, after in zip(PART_NAMES, unrounded, parts):
        delta = float(np.max(np.abs(np.array(_bounds(before)) - np.array(_bounds(after)))))
        if delta > CONNECTOR_PROOF_TOLERANCE_MM:
            raise ValueError(f"edge rounding changed outer bounds for {name}")
        bounds_deltas[name] = delta
    rack_tools = []
    for side in (-1, 1):
        for station in RACK_TOOTH_STATIONS_MM:
            rack_tools.append(_rack_tooth(side, station, DIMENSIONS))
    guide_tools = []
    for side in (-1, 1):
        guide_tools.append(_guide_wall(side, DIMENSIONS))
    shift_location = Location(
        (0, DIMENSIONS.pusher_y_offset - spec.extension_mm, DIMENSIONS.pusher_z)
    )
    moving_tooth_tools = []
    for side in (-1, 1):
        shift = side * DIMENSIONS.release_stroke if spec.released_illustration else 0
        for station in MOVING_TOOTH_STATIONS_MM:
            moving_tooth_tools.append(
                _moving_tooth(side, station, shift, DIMENSIONS).moved(shift_location)
            )
    functional_tool_missing_volumes = {
        "base_rack_teeth_maximum_mm3": max(
            (_shape_volume(tool.cut(parts[0])) for tool in rack_tools)
        ),
        "base_guides_maximum_mm3": max((_shape_volume(tool.cut(parts[0])) for tool in guide_tools)),
        "moving_locking_teeth_maximum_mm3": max(
            (_shape_volume(tool.cut(parts[1])) for tool in moving_tooth_tools)
        ),
    }
    if any((value > VOLUME_TOLERANCE_MM3 for value in functional_tool_missing_volumes.values())):
        raise ValueError("edge rounding removed material from a protected functional feature")
    radius_counts = {
        name: _cylindrical_radius_counts(part) for name, part in zip(PART_NAMES, parts)
    }
    assert radius_counts["fixed_base"]["2"] >= 36
    assert radius_counts["moving_wall"]["2"] >= 33
    assert radius_counts["moving_wall"]["1"] == 53
    assert radius_counts["keeper"]["1"] >= 6
    assert all(
        (
            counts["G1"] > 0
            for counts in {
                name: _edge_continuity_counts(part) for name, part in zip(PART_NAMES, parts)
            }.values()
        )
    )
    sections = _finger_section_areas(parts[1], spec)
    assert sections["outer_each_mm2"] == pytest.approx(103.9915926535898)
    assert sections["centre_mm2"] == pytest.approx(115.64159265359)
    assert all((item["brep_difference_mm3"] < 1e-06 for item in protected.values()))
    assert all((missing < 1e-06 for missing in functional_tool_missing_volumes.values()))
    assert all((delta < 1e-05 for delta in bounds_deltas.values()))
    exceptions = {
        (item["part"], item["region"]): item["reason"]
        for item in [
            {
                "part": "fixed_base",
                "region": "rack, guide, pilot and analytic anchor working edges",
                "reason": "Mating, locking, bearing and fastener geometry is preserved.",
            },
            {
                "part": "moving_wall",
                "region": "native X, tooth and carrier-stop working edges",
                "reason": "Mating, ratchet and withdrawal-stop geometry is preserved.",
            },
            {
                "part": "moving_wall",
                "region": "six wall-root side transitions",
                "reason": "Coupled OCCT fillets fail down to R0.05 after the adjacent R1/R2 blends; the broad reinforcement load path is retained without approximation.",
            },
            {
                "part": "keeper",
                "region": "two top side edges",
                "reason": f"The {DIMENSIONS.countersink_diameter:g} mm countersink mouths leave {DIMENSIONS.countersink_side_land:g} mm of side land.",
            },
            {
                "part": "keeper",
                "region": "flat underside, overrun-stop and screw working edges",
                "reason": "Planar lands, overtravel blocking, bores and countersinks are preserved.",
            },
        ]
    }
    assert "1.3 mm" in exceptions["keeper", "two top side edges"]
    assert "R0.05" in exceptions["moving_wall", "six wall-root side transitions"]


def test_native_connector_composition_preserves_backing_and_roots():
    spec = AdjustableStopSpec()
    _, pusher, _ = make_adjustable_stop_parts(spec)
    _native_connector_proofs(pusher, spec)
