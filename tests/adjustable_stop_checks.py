"""Geometry queries for the adjustable-stop mechanism tests."""

from collections import Counter
from dataclasses import replace
from math import atan2, degrees

from build123d import Align, Axis, Box, Compound, Face, GeomType, Location, Part, Plane, section
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.OCP.collections import (
    IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher,
)
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS

from cargo_grid import AdjustableStopSpec
from cargo_grid.accessories import make_bidirectional_panel_connector
from cargo_grid.adjustable_stop import (
    CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM,
    DIMENSIONS,
    MOVING_TOOTH_STATIONS_MM,
    RACK_TOOTH_STATIONS_MM,
    _block,
    _make_pusher,
    _make_pusher_body,
    _moving_tooth,
    _outer_finger,
    _rack_tooth,
)

PART_NAMES = ("fixed_base", "moving_wall", "keeper")
CONNECTOR_PROOF_TOLERANCE_MM = 1e-05
VOLUME_TOLERANCE_MM3 = 1e-06


def _bounds(shape) -> list[list[float]]:
    bounds = shape.bounding_box()
    return [[bounds.min.X, bounds.min.Y, bounds.min.Z], [bounds.max.X, bounds.max.Y, bounds.max.Z]]


def _shape_volume(shape) -> float:
    if shape is None:
        return 0.0
    if hasattr(shape, "solids"):
        return sum((solid.volume for solid in shape.solids()))
    return sum((solid.volume for item in shape for solid in item.solids()))


def _symmetric_difference_volume(first, second) -> float:
    first_shape = first if hasattr(first, "cut") else Compound(children=list(first))
    second_shape = second if hasattr(second, "cut") else Compound(children=list(second))
    return _shape_volume(first_shape.cut(second_shape)) + _shape_volume(
        second_shape.cut(first_shape)
    )


def _cylindrical_radius_counts(shape) -> dict[str, int]:
    radii = []
    for face in shape.faces():
        if face.geom_type == GeomType.CYLINDER:
            radius = BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()
            radii.append(f"{radius:.6f}".rstrip("0").rstrip("."))
    return dict(sorted(Counter(radii).items()))


def _edge_continuity_counts(shape) -> dict[str, int]:
    edge_faces = IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, edge_faces)
    counts = Counter()
    for index in range(1, edge_faces.Extent() + 1):
        faces = edge_faces.FindFromIndex(index)
        if faces.Extent() != 2:
            counts["single_face_or_seam"] += 1
            continue
        continuity = BRep_Tool.Continuity_s(
            TopoDS.Edge(edge_faces.FindKey(index)),
            TopoDS.Face(faces.First()),
            TopoDS.Face(faces.Last()),
        )
        counts[str(continuity).rsplit("GeomAbs_", 1)[-1]] += 1
    return dict(sorted(counts.items()))


def _finger_section_areas(pusher: Part, spec: AdjustableStopSpec) -> dict:
    section_y = 50.0 + DIMENSIONS.pusher_y_offset - spec.extension_mm
    cross_section = section(
        pusher, section_by=Plane(origin=(0, section_y, 0), x_dir=(1, 0, 0), z_dir=(0, 1, 0))
    )
    areas = sorted((Face(wire).area for wire in cross_section.wires()))
    if len(areas) != 3:
        raise ValueError("finger cross-section proof expected three closed profiles")
    return {
        "section_y_mm": section_y,
        "outer_each_mm2": areas[0],
        "centre_mm2": areas[2],
        "nominal_unrounded_outer_each_mm2": DIMENSIONS.outer_width * DIMENSIONS.finger_height,
        "nominal_unrounded_centre_mm2": DIMENSIONS.centre_width * DIMENSIONS.finger_height,
    }


def _ratchet_ramp_faces(part: Part, *, moving: bool) -> list[Face]:
    expected_x = (
        DIMENSIONS.moving_tip - (DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2)
        if moving
        else DIMENSIONS.rack_carrier_inner - DIMENSIONS.rack_tip
    )
    expected_y = expected_x / DIMENSIONS.ratchet_ramp_radial_per_axial
    expected_z = (
        DIMENSIONS.tooth_height - DIMENSIONS.pusher_bed_bottom
        if moving
        else DIMENSIONS.pusher_z + DIMENSIONS.tooth_height - DIMENSIONS.floor
    )
    return [
        face
        for face in part.faces()
        if face.geom_type == GeomType.PLANE
        and abs(face.bounding_box().size.X - expected_x) < CONNECTOR_PROOF_TOLERANCE_MM
        and (abs(face.bounding_box().size.Y - expected_y) < CONNECTOR_PROOF_TOLERANCE_MM)
        and (abs(face.bounding_box().size.Z - expected_z) < CONNECTOR_PROOF_TOLERANCE_MM)
    ]


def _ratchet_lock_faces(part: Part, *, moving: bool) -> list[Face]:
    expected_x = (
        DIMENSIONS.moving_tip - (DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2)
        if moving
        else DIMENSIONS.rack_carrier_inner - DIMENSIONS.rack_tip
    )
    expected_z = (
        DIMENSIONS.tooth_height - DIMENSIONS.pusher_bed_bottom
        if moving
        else DIMENSIONS.pusher_z + DIMENSIONS.tooth_height - DIMENSIONS.floor
    )
    return [
        face
        for face in part.faces()
        if face.geom_type == GeomType.PLANE
        and abs(face.bounding_box().size.X - expected_x) < CONNECTOR_PROOF_TOLERANCE_MM
        and (face.bounding_box().size.Y < CONNECTOR_PROOF_TOLERANCE_MM)
        and (abs(face.bounding_box().size.Z - expected_z) < CONNECTOR_PROOF_TOLERANCE_MM)
    ]


def _face_ramp_angle(face: Face) -> float:
    normal = face.normal_at()
    return degrees(atan2(abs(normal.Y), abs(normal.X)))


def _projected_xz_overlap_area(first: Face, second: Face) -> float:
    first_bounds = first.bounding_box()
    second_bounds = second.bounding_box()
    x_overlap = max(
        0.0,
        min(first_bounds.max.X, second_bounds.max.X) - max(first_bounds.min.X, second_bounds.min.X),
    )
    z_overlap = max(
        0.0,
        min(first_bounds.max.Z, second_bounds.max.Z) - max(first_bounds.min.Z, second_bounds.min.Z),
    )
    return x_overlap * z_overlap


def _right_ratchet_feature(extension_mm: float) -> Part:
    feature = _outer_finger(1, False, DIMENSIONS)
    for station in MOVING_TOOTH_STATIONS_MM:
        feature += _moving_tooth(1, station, 0, DIMENSIONS)
    return feature.moved(
        Location(
            (
                0,
                DIMENSIONS.pusher_y_offset - extension_mm,
                DIMENSIONS.pusher_z,
            )
        )
    )


def _right_rack() -> Part:
    rack = _block(
        DIMENSIONS.rack_root,
        DIMENSIONS.width / 2,
        DIMENSIONS.base_start,
        DIMENSIONS.base_end,
        DIMENSIONS.floor - 0.1,
        DIMENSIONS.rack_height,
    )
    for station in RACK_TOOTH_STATIONS_MM:
        rack += _rack_tooth(1, station, DIMENSIONS)
    return rack


def _required_inward_deflection(rack: Part, finger: Part, phase_mm: float) -> float:
    def overlap_volume(deflection_mm: float) -> float:
        pose = finger.moved(Location((-deflection_mm, -phase_mm, 0)))
        return _shape_volume(rack.intersect(pose))

    if overlap_volume(0) < VOLUME_TOLERANCE_MM3:
        return 0.0
    low = 0.0
    high = DIMENSIONS.release_stroke
    if overlap_volume(high) >= VOLUME_TOLERANCE_MM3:
        raise ValueError("full release no longer clears the rack during a pitch sweep")
    for _ in range(30):
        middle = (low + high) / 2
        overlap = overlap_volume(middle)
        if overlap < VOLUME_TOLERANCE_MM3:
            high = middle
        else:
            low = middle
    return high


def _carrier_stop_proofs(base: Part, keeper: Part, pitch_sweep: list[dict]) -> dict:
    contact_overtravel = (
        DIMENSIONS.moving_carrier_start
        + DIMENSIONS.pusher_y_offset
        - (DIMENSIONS.extension + DIMENSIONS.keeper_end)
    )
    if contact_overtravel <= 0:
        raise ValueError("moving carrier stop must remain beyond the last lock")
    required_by_phase = {
        round(row["forward_phase_mm"], 10): row["required_inward_deflection_mm"]
        for row in pitch_sweep
    }
    ratcheting_path = []
    maximum_required_deflection = 0.0
    overtravel_sample_count = int(round(contact_overtravel * 10))
    for index in range(overtravel_sample_count + 1):
        overtravel = index / 10
        required = required_by_phase[round(overtravel, 10)]
        maximum_required_deflection = max(maximum_required_deflection, required)
        clearance_deflection = (
            min(DIMENSIONS.release_stroke, required + 0.0001) if required > 0 else 0
        )
        motion_dimensions = replace(DIMENSIONS, release_stroke=clearance_deflection)
        pusher = _make_pusher(
            AdjustableStopSpec(
                DIMENSIONS.extension, released_illustration=clearance_deflection > 0
            ),
            motion_dimensions,
        ).moved(Location((0, -overtravel, 0)))
        base_overlap = _shape_volume(pusher.intersect(base))
        keeper_overlap = _shape_volume(pusher.intersect(keeper))
        if base_overlap >= VOLUME_TOLERANCE_MM3:
            raise ValueError(
                f"ratcheting overtravel path intersects the fixed base at {overtravel:g} mm with {clearance_deflection:g} mm inward displacement: {base_overlap:g} mm3"
            )
        if overtravel < contact_overtravel and keeper_overlap >= VOLUME_TOLERANCE_MM3:
            raise ValueError("moving carrier reaches the keeper before 4 mm overtravel")
        if abs(overtravel - contact_overtravel) < CONNECTOR_PROOF_TOLERANCE_MM:
            if (
                keeper_overlap >= VOLUME_TOLERANCE_MM3
                or pusher.distance_to(keeper) > CONNECTOR_PROOF_TOLERANCE_MM
            ):
                raise ValueError("moving carrier lost exact keeper contact at 4 mm overtravel")
        ratcheting_path.append(
            {
                "overtravel_past_last_lock_mm": overtravel,
                "required_inward_cam_displacement_mm": required,
                "checked_inward_displacement_mm": clearance_deflection,
                "base_overlap_mm3": base_overlap,
                "keeper_overlap_mm3": keeper_overlap,
            }
        )
    if maximum_required_deflection >= DIMENSIONS.release_stroke:
        raise ValueError("controlled overtravel consumes the full release stroke")
    released_end = _make_pusher(
        AdjustableStopSpec(DIMENSIONS.extension, released_illustration=True)
    )
    released_path = []
    for index in range(overtravel_sample_count + 1):
        overtravel = index / 10
        pusher = released_end.moved(Location((0, -overtravel, 0)))
        base_overlap = _shape_volume(pusher.intersect(base))
        keeper_overlap = _shape_volume(pusher.intersect(keeper))
        if base_overlap >= VOLUME_TOLERANCE_MM3:
            raise ValueError("fully released overtravel path intersects the fixed base")
        if overtravel < contact_overtravel and keeper_overlap >= VOLUME_TOLERANCE_MM3:
            raise ValueError("fully released carrier reaches the keeper before 4 mm overtravel")
        if abs(overtravel - contact_overtravel) < CONNECTOR_PROOF_TOLERANCE_MM:
            if (
                keeper_overlap >= VOLUME_TOLERANCE_MM3
                or pusher.distance_to(keeper) > CONNECTOR_PROOF_TOLERANCE_MM
            ):
                raise ValueError("fully released carrier lost exact keeper contact")
        released_path.append(
            {
                "overtravel_past_last_lock_mm": overtravel,
                "base_overlap_mm3": base_overlap,
                "keeper_overlap_mm3": keeper_overlap,
            }
        )
    beyond = released_end.moved(Location((0, -contact_overtravel - 0.001, 0)))
    beyond_overlap = _shape_volume(beyond.intersect(keeper))
    if beyond_overlap <= VOLUME_TOLERANCE_MM3:
        raise ValueError("moving carriers lack a positive-volume beyond-stop witness")
    individual_carriers = []
    final_extension = DIMENSIONS.extension + contact_overtravel
    for side in (-1, 1):
        for inward_deflection in (0.0, DIMENSIONS.release_stroke):
            shift = side * inward_deflection
            x0, x1 = sorted(
                (
                    side * (DIMENSIONS.outer_centre - DIMENSIONS.outer_width / 2) - shift,
                    side * (DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2) - shift,
                )
            )
            carrier = _block(
                x0,
                x1,
                DIMENSIONS.moving_carrier_start + DIMENSIONS.pusher_y_offset - final_extension,
                DIMENSIONS.moving_carrier_end + DIMENSIONS.pusher_y_offset - final_extension,
                DIMENSIONS.pusher_z,
                DIMENSIONS.pusher_z + DIMENSIONS.tooth_height,
            )
            distance = carrier.distance_to(keeper)
            penetration = _shape_volume(carrier.moved(Location((0, -0.001, 0))).intersect(keeper))
            if distance > CONNECTOR_PROOF_TOLERANCE_MM or penetration <= VOLUME_TOLERANCE_MM3:
                raise ValueError("an individual moving carrier no longer retains the pusher")
            individual_carriers.append(
                {
                    "side": side,
                    "inward_deflection_mm": inward_deflection,
                    "contact_distance_mm": distance,
                    "overlap_after_0p001_mm_overtravel_mm3": penetration,
                }
            )
    return {
        "last_lock_extension_mm": DIMENSIONS.extension,
        "carrier_contact_extension_mm": DIMENSIONS.extension + contact_overtravel,
        "controlled_overtravel_after_last_lock_mm": contact_overtravel,
        "carrier_contact_is_lock_position": False,
        "ratcheting_path": ratcheting_path,
        "maximum_required_inward_cam_displacement_mm": maximum_required_deflection,
        "fully_released_path": released_path,
        "individual_relaxed_and_released_carrier_contacts": individual_carriers,
        "released_overlap_after_0p001_mm_beyond_contact_mm3": beyond_overlap,
        "blocking_feature": "two 14 mm-high moving tooth carriers against the solid keeper",
    }


def _connector_transform(height_mm: float, extension_mm: float) -> list[list[float]]:
    return [
        [1.0, 0.0, 0.0, -DIMENSIONS.width / 2],
        [0.0, 0.0, 1.0, -DIMENSIONS.wall_thickness + DIMENSIONS.pusher_y_offset - extension_mm],
        [0.0, -1.0, 0.0, DIMENSIONS.pusher_z + height_mm + DIMENSIONS.width / 2],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _placed_connector(height_mm: float, extension_mm: float) -> Part:
    return (
        make_bidirectional_panel_connector()
        .rotate(Axis.X, -90)
        .moved(
            Location(
                (
                    -DIMENSIONS.width / 2,
                    -DIMENSIONS.wall_thickness + DIMENSIONS.pusher_y_offset - extension_mm,
                    DIMENSIONS.pusher_z + height_mm + DIMENSIONS.width / 2,
                )
            )
        )
    )


def _native_connector_proofs(pusher: Part, spec: AdjustableStopSpec) -> tuple[list[dict], dict]:
    body = _make_pusher_body(spec.released_illustration).moved(
        Location((0, DIMENSIONS.pusher_y_offset - spec.extension_mm, DIMENSIONS.pusher_z))
    )
    front_y = -DIMENSIONS.wall_thickness + DIMENSIONS.pusher_y_offset - spec.extension_mm
    protected = Location((-40, front_y + CONNECTOR_PROOF_TOLERANCE_MM, -20)) * Box(
        80, 180, 180, align=(Align.MIN, Align.MIN, Align.MIN)
    )
    protected_difference = _symmetric_difference_volume(
        body.intersect(protected), pusher.intersect(protected)
    )
    if protected_difference > VOLUME_TOLERANCE_MM3:
        raise ValueError("native connector composition changed protected mechanism geometry")
    rows = []
    for height_mm in CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM:
        connector = _placed_connector(height_mm, spec.extension_mm)
        centre_z = DIMENSIONS.pusher_z + height_mm
        exposed_region = Location((-29, front_y - 13, centre_z - 28)) * Box(
            58, 13 - CONNECTOR_PROOF_TOLERANCE_MM, 56, align=(Align.MIN, Align.MIN, Align.MIN)
        )
        difference = _symmetric_difference_volume(
            connector.intersect(exposed_region), pusher.intersect(exposed_region)
        )
        if difference > VOLUME_TOLERANCE_MM3:
            raise ValueError("pusher front does not preserve the shared native panel connector")
        root_overlap = _shape_volume(connector.intersect(body))
        if root_overlap <= VOLUME_TOLERANCE_MM3:
            raise ValueError("shared native panel connector is not rooted in the pusher backing")
        rows.append(
            {
                "connector_height_above_pusher_floor_mm": height_mm,
                "rigid_transform": _connector_transform(height_mm, spec.extension_mm),
                "rotation_determinant": 1.0,
                "native_plug_projection_mm": 12.8,
                "shared_panel_connector_brep_difference_mm3": difference,
                "hidden_root_overlap_mm3": root_overlap,
            }
        )
    return (
        rows,
        {
            "protected_backing_and_mechanism_brep_difference_mm3": protected_difference,
            "proof_region": "All pusher material at least 0.00001 mm behind the front mounting plane.",
        },
    )
