"""Native CAD export for the experimental three-part trunk blocker."""

import hashlib
import json
from collections import Counter
from dataclasses import replace
from itertools import permutations
from math import atan2, degrees, floor
from pathlib import Path
from tempfile import TemporaryFile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

import numpy as np
from build123d import (
    Align,
    Axis,
    Box,
    Compound,
    Face,
    GeomType,
    Location,
    Part,
    Plane,
    PrecisionMode,
    export_step,
    import_step,
    section,
)
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.OCP.collections import (
    IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher,
)
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS

from cargo_grid._version import __version__
from cargo_grid.accessories import make_bidirectional_panel_connector
from cargo_grid.cli import H2D_PROFILES, H2DProfiles
from cargo_grid.export import BambuSettings, Material
from cargo_grid.jobs import Job
from cargo_grid.meshes import write_stl
from cargo_grid.packing import PrimeTower, TowerClearance, h2d_common_build
from cargo_grid.parameters import Tile
from cargo_grid.plates import (
    AUTO_SUPPORT_TOWER_CLEARANCE_MM,
    BAMBU_TOWER_MARGIN_MM,
    H2D_SHARED_REACH_X_MM,
    H2D_TOWER_LEFT_CLEARANCE_MM,
    TOWER_BACK_ALLOWANCE_MM,
    BambuTowerEstimate,
)
from cargo_grid.prepared import PreparedShape
from cargo_grid.tiles import make_tile
from cargo_grid.trunk_blocker import (
    BASE_ANCHOR_CENTRES_Y_MM,
    CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM,
    DETAIL_EDGE_RADIUS_MM,
    DIMENSIONS,
    FREE_EDGE_RADIUS_MM,
    MOVING_TOOTH_STATIONS_MM,
    RACK_TOOTH_STATIONS_MM,
    SCREW_AXES_MM,
    TrunkBlockerSpec,
    _block,
    _guide_wall,
    _make_base,
    _make_keeper,
    _make_pusher,
    _make_pusher_body,
    _moving_tooth,
    _outer_finger,
    _outer_guide_pad,
    _rack_tooth,
    _squeeze_pad,
    _unify_same_domain,
    make_trunk_blocker_parts,
)

CORE_NAMESPACE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
RELATIONSHIPS_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
CORE = f"{{{CORE_NAMESPACE}}}"
PART_NAMES = ("fixed_base", "moving_wall", "short_screwed_keeper")
CONNECTOR_PROOF_TOLERANCE_MM = 1e-5
VOLUME_TOLERANCE_MM3 = 1e-6


def _trunk_blocker_h2d_profile(nozzle_diameter_mm: float) -> H2DProfiles:
    profile = H2D_PROFILES.get(nozzle_diameter_mm)
    if profile is None:
        choices = ", ".join(f"{nozzle:g}" for nozzle in H2D_PROFILES)
        raise ValueError(f"trunk-blocker H2D nozzle must be one of: {choices} mm")
    return profile


def trunk_blocker_h2d_settings(
    nozzle_diameter_mm: float = 0.8,
    *,
    dual_material: bool,
) -> BambuSettings:
    """Use one of the official H2D profile pairs for a blocker print project."""
    profile = _trunk_blocker_h2d_profile(nozzle_diameter_mm)
    materials = (Material(profile.petg, "PETG", "#637B70"),)
    if dual_material:
        materials += (Material(profile.pla, "PLA", "#DDDDDD"),)
    return BambuSettings(
        materials,
        nozzle=nozzle_diameter_mm,
        layer_height=profile.layer_height_mm,
        printer_settings_id=profile.printer,
        print_settings_id=profile.process,
        bed_type="Textured PEI Plate",
        machine_nozzle_count=2,
        printer_model="Bambu Lab H2D",
    )


def prepare_trunk_blocker_print_job(
    job: Job,
    *,
    material_counts_by_plate: dict[int, int],
    nozzle_diameter_mm: float = 0.8,
) -> Job:
    """Reserve Bambu's default H2D tower only on plates using multiple materials."""
    profile = _trunk_blocker_h2d_profile(nozzle_diameter_mm)
    if job.print_placements is None:
        raise ValueError("trunk-blocker print jobs need explicit placements")
    plates = {placement.plate for placement in job.print_placements}
    if set(material_counts_by_plate) != plates:
        raise ValueError("material counts must cover exactly the placed trunk-blocker plates")
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 1
        for count in material_counts_by_plate.values()
    ):
        raise ValueError("plate material counts must be positive integers")
    if (
        job.prime_tower is not None
        or job.prime_tower_positions
        or job.prime_tower_reaches
        or job.prime_tower_clearances
    ):
        raise ValueError("trunk-blocker print jobs must not supply a prime tower")

    tower_plates = {plate for plate, count in material_counts_by_plate.items() if count > 1}
    if not tower_plates:
        return job

    positions = {}
    reaches = {}
    clearances = {}
    for plate in sorted(tower_plates):
        build = job.plate_builds.get(plate, job.build)
        if build != h2d_common_build():
            raise ValueError("multi-material blocker plates require the H2D common build")
        height = max(
            design.bambu_size[2]
            for design, placement in zip(job.designs, job.print_placements, strict=True)
            if placement.plate == plate
        )
        position, reach, clearance = _h2d_tower_reservation(
            height,
            profile.layer_height_mm,
        )
        positions[plate] = position
        reaches[plate] = reach
        clearances[plate] = clearance

    return replace(
        job,
        prime_tower=PrimeTower(
            max(reach.left for reach in reaches.values()),
            max(reach.front for reach in reaches.values()),
            max(reach.right for reach in reaches.values()),
            max(reach.back for reach in reaches.values()),
        ),
        prime_tower_positions=positions,
        prime_tower_reaches=reaches,
        prime_tower_clearances=clearances,
    )


def _h2d_tower_reservation(
    height_mm: float,
    layer_height_mm: float,
) -> tuple[tuple[float, float], PrimeTower, TowerClearance]:
    estimate = BambuTowerEstimate(layer_height_mm)
    side = estimate.side_mm(height_mm)
    brim = estimate.brim_mm(height_mm)
    limit = H2D_SHARED_REACH_X_MM[1]
    x = floor((limit - BAMBU_TOWER_MARGIN_MM - side) * 100) / 100
    reach = PrimeTower(
        brim,
        brim,
        limit - x,
        side + brim + TOWER_BACK_ALLOWANCE_MM,
    )
    clearance = TowerClearance(
        H2D_TOWER_LEFT_CLEARANCE_MM,
        AUTO_SUPPORT_TOWER_CLEARANCE_MM,
        AUTO_SUPPORT_TOWER_CLEARANCE_MM,
        AUTO_SUPPORT_TOWER_CLEARANCE_MM,
    )
    return (x, BAMBU_TOWER_MARGIN_MM), reach, clearance


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bounds(shape) -> list[list[float]]:
    bounds = shape.bounding_box()
    return [
        [bounds.min.X, bounds.min.Y, bounds.min.Z],
        [bounds.max.X, bounds.max.Y, bounds.max.Z],
    ]


def _shape_facts(shape) -> dict:
    return {
        "solids": len(shape.solids()),
        "bounds_mm": _bounds(shape),
        "volume_mm3": shape.volume,
        "valid": shape.is_valid,
    }


def _mesh_facts(vertices: np.ndarray, faces: np.ndarray) -> dict:
    if (
        vertices.ndim != 2
        or vertices.shape[1] != 3
        or faces.ndim != 2
        or faces.shape[1] != 3
        or not len(vertices)
        or not len(faces)
        or not np.isfinite(vertices).all()
        or faces.min() < 0
        or faces.max() >= len(vertices)
    ):
        raise ValueError("triangle mesh arrays are invalid")
    triangles = vertices[faces]
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    area = np.linalg.norm(cross, axis=1) / 2
    if np.any(area < 1e-14):
        raise ValueError("triangle mesh contains zero-area faces")
    directed = np.concatenate(
        (faces[:, (0, 1)], faces[:, (1, 2)], faces[:, (2, 0)]),
        axis=0,
    )
    undirected = np.sort(directed, axis=1)
    order = np.lexsort((undirected[:, 1], undirected[:, 0]))
    sorted_edges = undirected[order]
    starts = np.concatenate(
        ([0], np.flatnonzero(np.any(np.diff(sorted_edges, axis=0), axis=1)) + 1)
    )
    counts = np.diff(np.append(starts, len(sorted_edges)))
    signs = np.where(directed[:, 0] < directed[:, 1], 1, -1)[order]
    balances = np.add.reduceat(signs, starts)
    if np.any(counts != 2) or np.any(balances != 0):
        raise ValueError("triangle mesh is not a closed consistently oriented two-manifold")
    signed_volume = float(
        np.einsum(
            "ij,ij->i",
            triangles[:, 0],
            np.cross(triangles[:, 1], triangles[:, 2]),
        ).sum()
        / 6
    )
    if signed_volume <= 0:
        raise ValueError("triangle mesh has nonpositive signed volume")
    return {
        "vertices": len(vertices),
        "triangles": len(faces),
        "bounds_mm": [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()],
        "volume_mm3": signed_volume,
        "closed_oriented_manifold": True,
    }


def _shape_volume(shape) -> float:
    if shape is None:
        return 0.0
    if hasattr(shape, "solids"):
        return sum(solid.volume for solid in shape.solids())
    return sum(solid.volume for item in shape for solid in item.solids())


def _symmetric_difference_volume(first, second) -> float:
    first_shape = first if hasattr(first, "cut") else Compound(children=list(first))
    second_shape = second if hasattr(second, "cut") else Compound(children=list(second))
    return _shape_volume(first_shape.cut(second_shape)) + _shape_volume(
        second_shape.cut(first_shape)
    )


def _part_intersections(parts: tuple[Part, Part, Part]) -> list[dict]:
    result = []
    for first in range(len(parts)):
        for second in range(first + 1, len(parts)):
            volume = _shape_volume(parts[first].intersect(parts[second]))
            if volume >= VOLUME_TOLERANCE_MM3:
                raise ValueError("manufactured blocker parts intersect in the requested pose")
            result.append(
                {
                    "parts": [PART_NAMES[first], PART_NAMES[second]],
                    "overlap_volume_mm3": volume,
                }
            )
    return result


def _cylindrical_radius_counts(shape) -> dict[str, int]:
    radii = []
    for face in shape.faces():
        if face.geom_type == GeomType.CYLINDER:
            radius = BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()
            radii.append(f"{radius:.6f}".rstrip("0").rstrip("."))
    return dict(sorted(Counter(radii).items()))


def _edge_continuity_counts(shape) -> dict[str, int]:
    edge_faces = IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapesAndAncestors_s(
        shape.wrapped,
        TopAbs_EDGE,
        TopAbs_FACE,
        edge_faces,
    )
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


def _finger_section_areas(pusher: Part, spec: TrunkBlockerSpec) -> dict:
    section_y = 50.0 + DIMENSIONS.pusher_y_offset - spec.extension_mm
    cross_section = section(
        pusher,
        section_by=Plane(
            origin=(0, section_y, 0),
            x_dir=(1, 0, 0),
            z_dir=(0, 1, 0),
        ),
    )
    areas = sorted(Face(wire).area for wire in cross_section.wires())
    if len(areas) != 3:
        raise ValueError("finger cross-section proof expected three closed profiles")
    return {
        "section_y_mm": section_y,
        "outer_each_mm2": areas[0],
        "centre_mm2": areas[2],
        "nominal_unrounded_outer_each_mm2": DIMENSIONS.outer_width * DIMENSIONS.finger_height,
        "nominal_unrounded_centre_mm2": DIMENSIONS.centre_width * DIMENSIONS.finger_height,
    }


def _base_anchor_proofs(base: Part) -> dict:
    half_window = (
        DIMENSIONS.anchor_lobe_centre + DIMENSIONS.anchor_lobe_radius + DIMENSIONS.anchor_end_radius
    )
    anchors = []
    rows = []
    for centre_y in BASE_ANCHOR_CENTRES_Y_MM:
        probe = Location(
            (
                -DIMENSIONS.width / 2,
                centre_y - half_window,
                -DIMENSIONS.anchor_depth - 1,
            )
        ) * Box(
            DIMENSIONS.width,
            2 * half_window,
            DIMENSIONS.anchor_depth + 1,
            align=(Align.MIN, Align.MIN, Align.MIN),
        )
        anchor = Part(base.intersect(probe).solids())
        bounds = _bounds(anchor)
        measured_centre_y = (bounds[0][1] + bounds[1][1]) / 2
        projection = bounds[1][2] - bounds[0][2]
        if (
            not anchor.is_valid
            or len(anchor.solids()) != 1
            or abs(measured_centre_y - centre_y) > CONNECTOR_PROOF_TOLERANCE_MM
            or abs(bounds[0][2] + DIMENSIONS.anchor_depth) > CONNECTOR_PROOF_TOLERANCE_MM
            or abs(bounds[1][2]) > CONNECTOR_PROOF_TOLERANCE_MM
            or abs(projection - DIMENSIONS.anchor_depth) > CONNECTOR_PROOF_TOLERANCE_MM
        ):
            raise ValueError("underbody anchor lost its approved centre, projection or root")
        anchors.append(anchor)
        rows.append(
            {
                "centre_y_mm": centre_y,
                "bounds_mm": bounds,
                "projection_below_base_mm": projection,
                "valid": True,
                "solids": 1,
            }
        )
    pitch = BASE_ANCHOR_CENTRES_Y_MM[1] - BASE_ANCHOR_CENTRES_Y_MM[0]
    translated_difference = _symmetric_difference_volume(
        anchors[0].moved(Location((0, pitch, 0))),
        anchors[1],
    )
    if translated_difference > VOLUME_TOLERANCE_MM3:
        raise ValueError("underbody anchors no longer share one translated BREP")
    return {
        "count": len(anchors),
        "centres_y_mm": list(BASE_ANCHOR_CENTRES_Y_MM),
        "pitch_mm": pitch,
        "projection_below_base_mm": DIMENSIONS.anchor_depth,
        "translated_brep_difference_mm3": translated_difference,
        "anchors": rows,
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
        and abs(face.bounding_box().size.Y - expected_y) < CONNECTOR_PROOF_TOLERANCE_MM
        and abs(face.bounding_box().size.Z - expected_z) < CONNECTOR_PROOF_TOLERANCE_MM
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
        and face.bounding_box().size.Y < CONNECTOR_PROOF_TOLERANCE_MM
        and abs(face.bounding_box().size.Z - expected_z) < CONNECTOR_PROOF_TOLERANCE_MM
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


def _right_ratchet_feature(
    *,
    extension_mm: float,
    forward_phase_mm: float,
    inward_deflection_mm: float,
) -> Part:
    feature = _outer_finger(1, False, DIMENSIONS)
    for station in MOVING_TOOTH_STATIONS_MM:
        feature += _moving_tooth(1, station, 0, DIMENSIONS)
    return feature.moved(
        Location(
            (
                -inward_deflection_mm,
                DIMENSIONS.pusher_y_offset - extension_mm - forward_phase_mm,
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


def _required_inward_deflection(rack: Part, phase_mm: float) -> float:
    if (
        _shape_volume(
            rack.intersect(
                _right_ratchet_feature(
                    extension_mm=24,
                    forward_phase_mm=phase_mm,
                    inward_deflection_mm=0,
                )
            )
        )
        < VOLUME_TOLERANCE_MM3
    ):
        return 0.0
    low = 0.0
    high = DIMENSIONS.release_stroke
    if (
        _shape_volume(
            rack.intersect(
                _right_ratchet_feature(
                    extension_mm=24,
                    forward_phase_mm=phase_mm,
                    inward_deflection_mm=high,
                )
            )
        )
        >= VOLUME_TOLERANCE_MM3
    ):
        raise ValueError("full release no longer clears the rack during a pitch sweep")
    for _ in range(30):
        middle = (low + high) / 2
        overlap = _shape_volume(
            rack.intersect(
                _right_ratchet_feature(
                    extension_mm=24,
                    forward_phase_mm=phase_mm,
                    inward_deflection_mm=middle,
                )
            )
        )
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
            min(DIMENSIONS.release_stroke, required + 1e-4) if required > 0 else 0
        )
        motion_dimensions = replace(
            DIMENSIONS,
            release_stroke=clearance_deflection,
        )
        pusher = _make_pusher(
            TrunkBlockerSpec(
                DIMENSIONS.extension,
                released_illustration=clearance_deflection > 0,
            ),
            motion_dimensions,
        ).moved(Location((0, -overtravel, 0)))
        base_overlap = _shape_volume(pusher.intersect(base))
        keeper_overlap = _shape_volume(pusher.intersect(keeper))
        if base_overlap >= VOLUME_TOLERANCE_MM3:
            raise ValueError(
                "ratcheting overtravel path intersects the fixed base at "
                f"{overtravel:g} mm with {clearance_deflection:g} mm inward "
                f"displacement: {base_overlap:g} mm3"
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

    released_end = _make_pusher(TrunkBlockerSpec(DIMENSIONS.extension, released_illustration=True))
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


def _ratchet_proofs(parts: tuple[Part, Part, Part], spec: TrunkBlockerSpec) -> dict:
    base, pusher, keeper = parts
    base_ramps = _ratchet_ramp_faces(base, moving=False)
    moving_ramps = _ratchet_ramp_faces(pusher, moving=True)
    if len(base_ramps) != 2 * (DIMENSIONS.positions + 2) or len(moving_ramps) != 6:
        raise ValueError("ratchet proof could not identify every ramp face")
    base_angles = [_face_ramp_angle(face) for face in base_ramps]
    moving_angles = [_face_ramp_angle(face) for face in moving_ramps]
    all_angles = base_angles + moving_angles
    if max(all_angles) - min(all_angles) > 1e-9:
        raise ValueError("rack and moving ramp faces no longer share one angle")

    contact_pusher = (
        _make_pusher(TrunkBlockerSpec(spec.extension_mm)) if spec.released_illustration else pusher
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
    sweep = [
        {
            "forward_phase_mm": phase,
            "required_inward_deflection_mm": _required_inward_deflection(rack, phase),
        }
        for phase in sorted(set(phases))
    ]
    maximum = max(row["required_inward_deflection_mm"] for row in sweep)
    if maximum >= DIMENSIONS.release_stroke:
        raise ValueError("sampled ratchet sweep consumes the full release stroke")

    carrier_stop = _carrier_stop_proofs(base, keeper, sweep)

    return {
        "shared_ramp_angle_degrees": {
            "nominal": DIMENSIONS.ratchet_ramp_angle_degrees,
            "base_face_min": min(base_angles),
            "base_face_max": max(base_angles),
            "moving_face_min": min(moving_angles),
            "moving_face_max": max(moving_angles),
            "maximum_measured_difference": max(all_angles) - min(all_angles),
        },
        "profile": {
            "pitch_mm": DIMENSIONS.pitch,
            "base_tooth_depth_mm": DIMENSIONS.rack_tooth_depth,
            "base_ramp_run_mm": DIMENSIONS.rack_ramp_run,
            "base_tip_land_mm": DIMENSIONS.tooth_tip_land,
            "base_root_land_between_teeth_mm": DIMENSIONS.rack_root_land,
            "moving_tooth_depth_mm": DIMENSIONS.moving_tooth_depth,
            "moving_ramp_run_mm": DIMENSIONS.moving_ramp_run,
            "moving_tip_land_mm": DIMENSIONS.tooth_tip_land,
            "moving_root_land_between_teeth_mm": DIMENSIONS.moving_root_land,
            "outer_finger_to_rack_tip_gap_mm": (
                DIMENSIONS.rack_tip - (DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2)
            ),
            "locking_engagement_mm": DIMENSIONS.locking_engagement,
            "full_release_tip_clearance_mm": DIMENSIONS.released_tip_clearance,
            "moving_tooth_count_per_outer_finger": len(MOVING_TOOTH_STATIONS_MM),
        },
        "backload_contact_positions": positions,
        "one_pitch_geometric_sweep": {
            "method": (
                "Rigid cross-section collision screen at 0.1 mm travel increments plus "
                "a pitch-minus-0.01 mm pre-snap sample; this is not an elastic-force model."
            ),
            "samples": sweep,
            "maximum_sampled_required_inward_deflection_mm": maximum,
            "available_release_stroke_mm": DIMENSIONS.release_stroke,
            "minimum_sampled_release_margin_mm": DIMENSIONS.release_stroke - maximum,
        },
        "end_retention_and_release": carrier_stop,
        "end_retention_note": (
            f"The last locking station remains {DIMENSIONS.extension:g} mm. The two moving "
            f"tooth carriers reach the solid keeper at "
            f"{carrier_stop['carrier_contact_extension_mm']:g} mm and block further overtravel "
            "in relaxed, cammed and released states; the contact is not another locking "
            "station. Complete withdrawal requires removing the keeper."
        ),
        "load_note": (
            "Contact area is measured per tooth. The geometry does not establish simultaneous "
            "load sharing among all three teeth on either outer finger."
        ),
    }


def _guide_proofs(parts: tuple[Part, Part, Part]) -> dict:
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
            base,
            reunified_base,
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
        and abs(face.bounding_box().max.Y - DIMENSIONS.base_start) < CONNECTOR_PROOF_TOLERANCE_MM
    ]
    if len(front_faces) != 1:
        raise ValueError("base front is split into more than one planar face")

    edge_faces = IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapesAndAncestors_s(
        base.wrapped,
        TopAbs_EDGE,
        TopAbs_FACE,
        edge_faces,
    )

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
                    {
                        "side": side,
                        "bounds_mm": _bounds(edge),
                        "continuity": continuity,
                    }
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
                or bounds.min.Z
                > DIMENSIONS.guide_top - FREE_EDGE_RADIUS_MM + CONNECTOR_PROOF_TOLERANCE_MM
                or bounds.max.Z < DIMENSIONS.guide_top - CONNECTOR_PROOF_TOLERANCE_MM
                or not bounds.min.X < root_x < bounds.max.X
            ):
                continue
            candidates.append(face)
        if len(candidates) != 1:
            raise ValueError("front-top R2 is split at the rack-wall/guide junction")
        front_top_round_rows.append(
            {
                "side": side,
                "face_count": len(candidates),
                "bounds_mm": _bounds(candidates[0]),
            }
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
            and abs(face.bounding_box().max.X - inner_x) < CONNECTOR_PROOF_TOLERANCE_MM
            and face.bounding_box().min.Z
            >= DIMENSIONS.floor + DETAIL_EDGE_RADIUS_MM - CONNECTOR_PROOF_TOLERANCE_MM
            and face.bounding_box().min.Y
            <= DIMENSIONS.outer_guide_start + CONNECTOR_PROOF_TOLERANCE_MM
            and face.bounding_box().max.Y
            >= DIMENSIONS.outer_guide_end - CONNECTOR_PROOF_TOLERANCE_MM
        ]
        top_faces = [
            face
            for face in base.faces()
            if face.geom_type == GeomType.PLANE
            and abs(face.bounding_box().min.Z - DIMENSIONS.guide_top) < CONNECTOR_PROOF_TOLERANCE_MM
            and abs(face.bounding_box().max.Z - DIMENSIONS.guide_top) < CONNECTOR_PROOF_TOLERANCE_MM
            and face.bounding_box().min.Y
            <= DIMENSIONS.outer_guide_start + CONNECTOR_PROOF_TOLERANCE_MM
            and face.bounding_box().max.Y
            >= DIMENSIONS.outer_guide_end - CONNECTOR_PROOF_TOLERANCE_MM
            and (
                side < 0
                and face.bounding_box().min.X <= root_x + CONNECTOR_PROOF_TOLERANCE_MM
                and face.bounding_box().max.X
                >= inner_x - DETAIL_EDGE_RADIUS_MM - CONNECTOR_PROOF_TOLERANCE_MM
                or side > 0
                and face.bounding_box().max.X >= root_x - CONNECTOR_PROOF_TOLERANCE_MM
                and face.bounding_box().min.X
                <= inner_x + DETAIL_EDGE_RADIUS_MM + CONNECTOR_PROOF_TOLERANCE_MM
            )
        ]
        wall_face_remnants = [
            face
            for face in base.faces()
            if face.geom_type == GeomType.PLANE
            and abs(face.bounding_box().min.X - root_x) < CONNECTOR_PROOF_TOLERANCE_MM
            and abs(face.bounding_box().max.X - root_x) < CONNECTOR_PROOF_TOLERANCE_MM
            and face.bounding_box().min.Y
            < DIMENSIONS.outer_guide_end - CONNECTOR_PROOF_TOLERANCE_MM
            and face.bounding_box().max.Y > DIMENSIONS.base_start + CONNECTOR_PROOF_TOLERANCE_MM
        ]
        wall_junction_cylinders = [
            face
            for face in base.faces()
            if face.geom_type == GeomType.CYLINDER
            and face.bounding_box().min.Y
            < DIMENSIONS.outer_guide_end - CONNECTOR_PROOF_TOLERANCE_MM
            and face.bounding_box().max.Y
            > DIMENSIONS.outer_guide_start + CONNECTOR_PROOF_TOLERANCE_MM
            and (
                side < 0
                and face.bounding_box().min.X >= root_x - CONNECTOR_PROOF_TOLERANCE_MM
                and face.bounding_box().max.X
                <= root_x + DETAIL_EDGE_RADIUS_MM + CONNECTOR_PROOF_TOLERANCE_MM
                or side > 0
                and face.bounding_box().max.X <= root_x + CONNECTOR_PROOF_TOLERANCE_MM
                and face.bounding_box().min.X
                >= root_x - DETAIL_EDGE_RADIUS_MM - CONNECTOR_PROOF_TOLERANCE_MM
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
    furthest_tooth_y = max(MOVING_TOOTH_STATIONS_MM)
    guide_only_tooth_envelope = (
        DIMENSIONS.guide_inner
        + max(0.0, furthest_tooth_y - DIMENSIONS.guide_end) * np.sin(guide_only_yaw_radians)
        - DIMENSIONS.centre_width / 2
    )
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
        angle_radians: float,
        half_channel_width: float,
        half_pusher_width: float,
        guide_end: float,
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
            {
                "side": side,
                "distance_to_static_keeper_mm": distance,
                "overlap_volume_mm3": overlap,
            }
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
        or _shape_volume(pusher.intersect(keeper)) >= VOLUME_TOLERANCE_MM3
    ):
        raise ValueError("low-play vertical passage changed")
    return {
        "architecture": "two plain rectangular side walls",
        "span_y_mm": [DIMENSIONS.guide_start, DIMENSIONS.guide_end],
        "walls": guide_rows,
        "wall_width_mm": DIMENSIONS.guide_outer - DIMENSIONS.guide_inner,
        "wall_length_mm": guide_length,
        "exposed_height_mm": DIMENSIONS.guide_top - DIMENSIONS.floor,
        "uniform_top_z_mm": DIMENSIONS.guide_top,
        "inward_caps_or_overhangs": False,
        "centre_channel_width_mm": 2 * DIMENSIONS.guide_inner,
        "centre_finger_clearance_each_side_mm": (
            DIMENSIONS.guide_inner - DIMENSIONS.centre_width / 2
        ),
        "fully_released_outer_finger_clearance_mm": (
            DIMENSIONS.outer_centre
            - DIMENSIONS.outer_width / 2
            - DIMENSIONS.release_stroke
            - DIMENSIONS.guide_outer
        ),
        "outer_prong_guides": {
            "span_y_mm": [DIMENSIONS.outer_guide_start, DIMENSIONS.outer_guide_end],
            "inner_faces_x_mm": [
                -DIMENSIONS.outer_guide_inner,
                DIMENSIONS.outer_guide_inner,
            ],
            "nominal_relaxed_clearance_each_side_mm": (
                DIMENSIONS.outer_guide_inner - DIMENSIONS.outer_centre - DIMENSIONS.outer_width / 2
            ),
            "released_tooth_tip_clearance_mm": (
                DIMENSIONS.outer_guide_inner - (DIMENSIONS.moving_tip - DIMENSIONS.release_stroke)
            ),
            "front_limit": "R2 front-perimeter tangent",
            "rear_limit": "existing guide/keeper seat, 1 mm before carriers at the 49 mm stop",
            "same_domain_unification": unification,
            "front_planar_face_count": len(front_faces),
            "front_planar_face_bounds_mm": _bounds(front_faces[0]),
            "front_top_r2_rounds": front_top_round_rows,
            "front_x24_edge_count": front_root_edge_count,
            "x24_pad_region_c0_seam_edges": root_seam_rows,
            "pads": outer_guide_rows,
            "inward_caps_or_overhangs": False,
        },
        "rigid_last_lock_play_comparison": {
            "extension_mm": DIMENSIONS.extension,
            "tooth_stations_y_mm": tooth_stations_at_last_lock,
            "centre_guide_only": {
                "nominal_peak_to_peak_lateral_translation_mm": (
                    2 * DIMENSIONS.finger_guide_clearance
                ),
                "maximum_yaw_degrees": degrees(guide_only_yaw_radians),
                "tooth_station_yaw_excursions": tooth_yaw_rows(
                    guide_only_yaw_radians,
                    DIMENSIONS.guide_inner,
                    DIMENSIONS.centre_width / 2,
                    DIMENSIONS.guide_end,
                ),
            },
            "centre_and_outer_guides": {
                "nominal_peak_to_peak_lateral_translation_mm": (
                    2 * DIMENSIONS.finger_guide_clearance
                ),
                "maximum_yaw_degrees": degrees(combined_yaw_radians),
                "tooth_station_yaw_excursions": tooth_yaw_rows(
                    combined_yaw_radians,
                    DIMENSIONS.outer_guide_inner,
                    DIMENSIONS.outer_centre + DIMENSIONS.outer_width / 2,
                    DIMENSIONS.outer_guide_end,
                ),
            },
            "stiffness_note": (
                "Each outer pad is 3.6 mm thick in X and merges into a rack wall and the "
                "pilot-boss region, so its nominal lateral load path is broader and more "
                "buttressed than either 2.35 mm centre-guide wall. This is a qualitative "
                "geometry comparison, not a measured stiffness result."
            ),
            "scope_note": (
                "These are rigid planar bounds with the pusher centred in nominal gaps. "
                "The pads block whole-pusher lateral translation and yaw, but do not stop "
                "an individual outer prong bending inward at its own teeth."
            ),
        },
        "guide_static_keeper_seat": {
            "keeper_underside_z_mm": DIMENSIONS.keeper_seat,
            "surface_contact_expected": True,
            "checks": guide_contact_rows,
        },
        "low_play_vertical_passage": {
            "base_floor_top_z_mm": DIMENSIONS.floor,
            "finger_bottom_z_mm": finger_bottom,
            "finger_top_z_mm": finger_top,
            "keeper_underside_z_mm": DIMENSIONS.keeper_seat,
            "clearance_below_mm": clearance_below,
            "clearance_above_mm": clearance_above,
            "total_nominal_clearance_mm": clearance_below + clearance_above,
            "former_pad_region_overlap_volumes_mm3": raised_floor_overlaps,
            "flat_floor_passage_ranges_x_mm": [list(bounds) for bounds in floor_passages],
        },
        "selected_front_edge_y_mm": DIMENSIONS.base_start + FREE_EDGE_RADIUS_MM,
        "selected_front_edge_to_guide_start_land_mm": (
            DIMENSIONS.guide_start - DIMENSIONS.base_start - FREE_EDGE_RADIUS_MM
        ),
        "minimum_continuous_centre_overlap_mm": (
            min(
                DIMENSIONS.guide_end,
                DIMENSIONS.finger_length + DIMENSIONS.pusher_y_offset - DIMENSIONS.extension,
            )
            - DIMENSIONS.guide_start
        ),
        "rigid_centre_finger_guide_only_yaw_limit_degrees": degrees(guide_only_yaw_radians),
        "guide_only_maximum_lateral_edge_envelope_at_furthest_tooth_mm": (
            guide_only_tooth_envelope
        ),
        "scope_note": (
            "The yaw result is an exact planar rectangle-in-channel bound for the rigid "
            "centre finger only. Rack contact, keeper contact and independent outer-finger "
            "deflection can reduce or redistribute motion, so this is not a claim that the "
            "assembled mechanism achieves the bound or that yaw caused both sides to disengage."
        ),
    }


def _pusher_bed_proofs(parts: tuple[Part, Part, Part], spec: TrunkBlockerSpec) -> dict:
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
        abs(bound - bed_z) > CONNECTOR_PROOF_TOLERANCE_MM
        for face in nearby_downward_faces
        for bound in (face["bounds_mm"][0][2], face["bounds_mm"][1][2])
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

    return {
        "named_local_datum_mm": DIMENSIONS.pusher_bed_bottom,
        "world_bed_plane_z_mm": bed_z,
        "downward_planar_faces_within_0p5_mm": nearby_downward_faces,
        "downward_planar_face_count_within_0p5_mm": len(nearby_downward_faces),
        "base_floor_top_z_mm": DIMENSIONS.floor,
        "minimum_vertical_gap_to_base_floor_mm": floor_gap,
        "minimum_vertical_gap_to_mat_z0_mm": bed_z,
        "upright_tile_bottom_z_mm": wall_tile.bounding_box().min.Z,
        "wall_bottom_below_upright_tile_mm": tile_bottom_gap,
        "upright_tile_overlap_volume_mm3": tile_overlap,
        "upright_tile_surface_distance_mm": pusher.distance_to(wall_tile),
        "coplanar_features": [
            "wall backing",
            "finger undersides",
            "reinforcement roots",
            "moving tooth carriers",
            "moving teeth",
        ],
    }


def _disassembly_proofs(parts: tuple[Part, Part, Part]) -> dict:
    base, pusher, keeper = parts
    normal_positions = []
    for index in range(DIMENSIONS.positions):
        extension_mm = index * DIMENSIONS.pitch
        for released in (False, True):
            pose = _make_pusher(TrunkBlockerSpec(extension_mm, released))
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

    released_end = _make_pusher(TrunkBlockerSpec(DIMENSIONS.extension, True))
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
        clearance for row in release_sweep for clearance in row["outer_pad_clearances_to_middle_mm"]
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
    old_pusher = _make_pusher(TrunkBlockerSpec(), short_pad_dimensions)
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
                DIMENSIONS.pad_start + DIMENSIONS.pusher_y_offset - TrunkBlockerSpec().extension_mm,
                DIMENSIONS.pad_start
                + DIMENSIONS.pad_length
                + DIMENSIONS.pusher_y_offset
                - TrunkBlockerSpec().extension_mm,
                DIMENSIONS.pusher_z + short_pad_dimensions.pad_height - DETAIL_EDGE_RADIUS_MM,
                DIMENSIONS.pad_top + 0.1,
            )
            for centre_x, width in (
                (
                    -DIMENSIONS.outer_centre,
                    DIMENSIONS.pad_outer - DIMENSIONS.pad_inner,
                ),
                (0, DIMENSIONS.centre_width),
                (
                    DIMENSIONS.outer_centre,
                    DIMENSIONS.pad_outer - DIMENSIONS.pad_inner,
                ),
            )
        ]
    )
    protected_pusher_difference = _symmetric_difference_volume(
        pusher.cut(allowed_pad_top_regions),
        old_pusher.cut(allowed_pad_top_regions),
    )
    if protected_pusher_difference > VOLUME_TOLERANCE_MM3:
        raise ValueError("tall-pad revision changed pusher geometry outside the pad tops")

    return {
        "normal_adjustment": {
            "extension_range_mm": [0, DIMENSIONS.extension],
            "station_pitch_mm": DIMENSIONS.pitch,
            "poses_checked": normal_positions,
            "minimum_pad_to_keeper_longitudinal_gap_mm": pad_to_keeper_y_gap,
            "keeper_and_base_overlap_volume_mm3": 0.0,
        },
        "keeper_installed_withdrawal": {
            "supported": False,
            "last_lock_extension_mm": DIMENSIONS.extension,
            "carrier_contact_extension_mm": DIMENSIONS.extension + carrier_contact_overtravel,
            "controlled_overtravel_after_last_lock_mm": carrier_contact_overtravel,
            "carrier_contact_is_lock_position": False,
            "first_checked_blocking_overrun_mm": installed_block_shift,
            "blocking_overlap_volume_mm3": installed_block_overlap,
            "blocking_feature": ("two 14 mm-high moving tooth carriers against the solid keeper"),
            "instruction": (
                "Do not force the pusher beyond the carrier-to-keeper stop. Unscrew and "
                "lift off the keeper before complete pusher withdrawal."
            ),
        },
        "keeper_removed_withdrawal": {
            "supported": True,
            "released_pusher": True,
            "sample_increment_mm": 0.5,
            "sampled_shift_range_mm": [0, full_withdrawal_shift],
            "maximum_base_overlap_volume_mm3": maximum_withdrawal_overlap,
            "final_y_clearance_mm": withdrawn_y_clearance,
        },
        "tall_pad_revision": {
            "pad_count": 3,
            "pad_height_mm": DIMENSIONS.pad_height,
            "outer_pad_width_mm": DIMENSIONS.pad_outer - DIMENSIONS.pad_inner,
            "centre_pad_width_mm": DIMENSIONS.centre_width,
            "pad_length_mm": DIMENSIONS.pad_length,
            "finger_height_mm": DIMENSIONS.finger_height,
            "finger_top_above_pusher_floor_mm": DIMENSIONS.finger_top,
            "pad_above_finger_mm": DIMENSIONS.pad_above_finger,
            "pad_top_world_z_mm": DIMENSIONS.pad_top,
            "keeper_roof_bottom_z_mm": DIMENSIONS.keeper_roof_bottom,
            "base_brep_difference_from_short_pad_revision_mm3": base_difference,
            "pusher_brep_difference_outside_pad_top_regions_mm3": (protected_pusher_difference),
            "pad_top_edge_radius_mm": DETAIL_EDGE_RADIUS_MM,
            "middle_pad_root_overlap_mm3": middle_pad_root_overlap,
            "middle_pad_missing_from_pusher_mm3": middle_pad_missing_volume,
            "outer_pad_release_sweep": release_sweep,
            "minimum_outer_to_middle_pad_clearance_mm": minimum_pad_clearance,
        },
        "solid_keeper_revision": {
            "bounds_mm": _bounds(keeper),
            "bottom_world_z_mm": DIMENSIONS.keeper_seat,
            "top_world_z_mm": (DIMENSIONS.keeper_roof_bottom + DIMENSIONS.keeper_roof_thickness),
            "removed_volume_from_channelled_keeper_mm3": keeper_removed_volume,
            "added_volume_over_channelled_keeper_mm3": keeper_added_volume,
            "exterior_bounds_delta_mm": keeper_bounds_delta,
            "screw_geometry": (
                "Four existing 3.4 mm clearance bores and pilots remain in the same "
                "positions; the keeper has 90-degree countersinks with the recorded "
                "provisional mouth and depth."
            ),
        },
    }


def _rounding_proofs(
    parts: tuple[Part, Part, Part],
    spec: TrunkBlockerSpec,
) -> dict:
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
            * Box(
                58,
                DIMENSIONS.keeper_length - 1,
                0.2,
                align=(Align.MIN, Align.MIN, Align.MIN),
            ),
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
            unrounded[part_index].intersect(probe),
            parts[part_index].intersect(probe),
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
            _shape_volume(tool.cut(parts[0])) for tool in rack_tools
        ),
        "base_guides_maximum_mm3": max(_shape_volume(tool.cut(parts[0])) for tool in guide_tools),
        "moving_locking_teeth_maximum_mm3": max(
            _shape_volume(tool.cut(parts[1])) for tool in moving_tooth_tools
        ),
    }
    if any(value > VOLUME_TOLERANCE_MM3 for value in functional_tool_missing_volumes.values()):
        raise ValueError("edge rounding removed material from a protected functional feature")

    return {
        "radii_mm": {
            "free_exterior": FREE_EDGE_RADIUS_MM,
            "detail_and_constrained_transition": DETAIL_EDGE_RADIUS_MM,
        },
        "applied_regions": {
            "fixed_base_r2": "outer body perimeter",
            "fixed_base_r1": "outer-prong guide-pad edges and lead-ins",
            "moving_wall_r2": "wall envelope and reinforcement diagonal ridges",
            "moving_wall_r1": (
                "finger longitudinal and tip edges, squeeze-pad edges, and reinforcement "
                "root/finger transitions"
            ),
            "keeper_r1": "top front/rear edges and four outer vertical edges",
        },
        "cylindrical_face_radius_counts_mm": {
            name: _cylindrical_radius_counts(part) for name, part in zip(PART_NAMES, parts)
        },
        "adjacent_edge_continuity_counts": {
            name: _edge_continuity_counts(part) for name, part in zip(PART_NAMES, parts)
        },
        "finger_cross_sections": _finger_section_areas(parts[1], spec),
        "protected_region_brep_differences": protected,
        "protected_functional_tool_missing_volumes": functional_tool_missing_volumes,
        "maximum_bounds_deltas_mm": bounds_deltas,
        "intentional_sharp_edges": [
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
                "reason": (
                    "Coupled OCCT fillets fail down to R0.05 after the adjacent R1/R2 blends; "
                    "the broad reinforcement load path is retained without approximation."
                ),
            },
            {
                "part": "short_screwed_keeper",
                "region": "two top side edges",
                "reason": (
                    f"The {DIMENSIONS.countersink_diameter:g} mm countersink mouths leave "
                    f"{DIMENSIONS.countersink_side_land:g} mm of side land."
                ),
            },
            {
                "part": "short_screwed_keeper",
                "region": "flat underside, overrun-stop and screw working edges",
                "reason": "Planar lands, overtravel blocking, bores and countersinks are preserved.",
            },
        ],
    }


def _connector_transform(height_mm: float, extension_mm: float) -> list[list[float]]:
    return [
        [1.0, 0.0, 0.0, -DIMENSIONS.width / 2],
        [
            0.0,
            0.0,
            1.0,
            -DIMENSIONS.wall_thickness + DIMENSIONS.pusher_y_offset - extension_mm,
        ],
        [
            0.0,
            -1.0,
            0.0,
            DIMENSIONS.pusher_z + height_mm + DIMENSIONS.width / 2,
        ],
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


def _native_connector_proofs(pusher: Part, spec: TrunkBlockerSpec) -> tuple[list[dict], dict]:
    body = _make_pusher_body(spec.released_illustration).moved(
        Location(
            (
                0,
                DIMENSIONS.pusher_y_offset - spec.extension_mm,
                DIMENSIONS.pusher_z,
            )
        )
    )
    front_y = -DIMENSIONS.wall_thickness + DIMENSIONS.pusher_y_offset - spec.extension_mm
    protected = Location((-40, front_y + CONNECTOR_PROOF_TOLERANCE_MM, -20)) * Box(
        80,
        180,
        180,
        align=(Align.MIN, Align.MIN, Align.MIN),
    )
    protected_difference = _symmetric_difference_volume(
        body.intersect(protected),
        pusher.intersect(protected),
    )
    if protected_difference > VOLUME_TOLERANCE_MM3:
        raise ValueError("native connector composition changed protected mechanism geometry")

    rows = []
    for height_mm in CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM:
        connector = _placed_connector(height_mm, spec.extension_mm)
        centre_z = DIMENSIONS.pusher_z + height_mm
        exposed_region = Location((-29, front_y - 13, centre_z - 28)) * Box(
            58,
            13 - CONNECTOR_PROOF_TOLERANCE_MM,
            56,
            align=(Align.MIN, Align.MIN, Align.MIN),
        )
        difference = _symmetric_difference_volume(
            connector.intersect(exposed_region),
            pusher.intersect(exposed_region),
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
    return rows, {
        "protected_backing_and_mechanism_brep_difference_mm3": protected_difference,
        "proof_region": (
            "All pusher material at least 0.00001 mm behind the front mounting plane."
        ),
    }


def _checked_step_roundtrip(
    parts: tuple[Part, Part, Part],
    path: Path,
) -> tuple[dict[str, dict], str]:
    assembly = Compound(children=list(parts))
    if not export_step(assembly, path):
        raise ValueError(f"STEP export failed: {path}")
    restored = import_step(path)
    precision_mode = "average"
    if not restored.is_valid or len(restored.solids()) != len(parts):
        if not export_step(assembly, path, precision_mode=PrecisionMode.LEAST):
            raise ValueError(f"STEP fallback export failed: {path}")
        restored = import_step(path)
        precision_mode = "least"
    restored_solids = list(restored.solids())
    if not restored.is_valid or len(restored_solids) != len(parts):
        raise ValueError("complete blocker STEP must round-trip as three valid solids")

    source_bounds = [np.array(_bounds(part)) for part in parts]
    restored_bounds = [np.array(_bounds(part)) for part in restored_solids]
    best = min(
        permutations(range(len(parts))),
        key=lambda order: sum(
            float(np.max(np.abs(source_bounds[index] - restored_bounds[candidate])))
            for index, candidate in enumerate(order)
        ),
    )
    result = {}
    for index, candidate in enumerate(best):
        source = parts[index]
        imported = restored_solids[candidate]
        volume_delta = abs(imported.volume - source.volume)
        volume_budget = max(1e-6, source.area * Precision.Confusion_s())
        symmetric_difference = _symmetric_difference_volume(source, imported)
        bounds_delta = float(np.max(np.abs(source_bounds[index] - restored_bounds[candidate])))
        if (
            not imported.is_valid
            or symmetric_difference > volume_budget
            or bounds_delta > CONNECTOR_PROOF_TOLERANCE_MM
        ):
            raise ValueError(f"STEP roundtrip failed for {PART_NAMES[index]}")
        result[PART_NAMES[index]] = {
            **_shape_facts(imported),
            "volume_delta_mm3": volume_delta,
            "symmetric_brep_difference_mm3": symmetric_difference,
            "volume_budget_mm3": volume_budget,
            "maximum_bounds_delta_mm": bounds_delta,
        }
    return result, precision_mode


def _xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _write_complete_3mf(
    path: Path,
    meshes: list[tuple[str, np.ndarray, np.ndarray]],
) -> None:
    content_types = ET.Element("Types", xmlns=CONTENT_TYPES_NAMESPACE)
    for extension, content_type in (
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("model", "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"),
    ):
        ET.SubElement(
            content_types,
            "Default",
            Extension=extension,
            ContentType=content_type,
        )
    relationships = ET.Element("Relationships", xmlns=RELATIONSHIPS_NAMESPACE)
    ET.SubElement(
        relationships,
        "Relationship",
        Target="/3D/3dmodel.model",
        Id="model",
        Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel",
    )
    with TemporaryFile() as model:
        model.write(
            (
                '<?xml version="1.0" encoding="utf-8"?>'
                f'<model xmlns="{CORE_NAMESPACE}" unit="millimeter">'
                f'<metadata name="Application">{escape(f"Cargo-Grid {__version__}")}</metadata>'
                '<metadata name="Designer">Cargo-Grid native-BREP trunk blocker</metadata>'
                f'<metadata name="CargoGridVersion">{escape(__version__)}</metadata>'
                '<metadata name="CargoGridXSource">'
                "cargo_grid.accessories.make_bidirectional_panel_connector</metadata>"
                "<resources>"
            ).encode()
        )
        for object_id, (name, vertices, faces) in enumerate(meshes, start=1):
            model.write(
                f'<object id="{object_id}" type="model" name={quoteattr(name)}>'
                "<mesh><vertices>".encode()
            )
            for start in range(0, len(vertices), 8192):
                model.write(
                    "".join(
                        f'<vertex x="{point[0]:.17g}" y="{point[1]:.17g}" z="{point[2]:.17g}"/>'
                        for point in vertices[start : start + 8192]
                    ).encode()
                )
            model.write(b"</vertices><triangles>")
            for start in range(0, len(faces), 8192):
                model.write(
                    "".join(
                        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>'
                        for a, b, c in faces[start : start + 8192]
                    ).encode()
                )
            model.write(b"</triangles></mesh></object>")
        model.write(b"</resources><build>")
        for object_id in range(1, len(meshes) + 1):
            model.write(f'<item objectid="{object_id}" printable="1"/>'.encode())
        model.write(b"</build></model>")
        model.seek(0)
        with ZipFile(path, "x", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _xml_bytes(content_types))
            archive.writestr("_rels/.rels", _xml_bytes(relationships))
            with archive.open("3D/3dmodel.model", "w", force_zip64=True) as output:
                while chunk := model.read(1024 * 1024):
                    output.write(chunk)


def _read_complete_3mf(path: Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    try:
        with ZipFile(path) as archive:
            root = ET.fromstring(archive.read("3D/3dmodel.model"))
    except (BadZipFile, KeyError, ET.ParseError) as error:
        raise ValueError("generated trunk-blocker 3MF is unreadable") from error
    if root.get("unit") != "millimeter":
        raise ValueError("generated trunk-blocker 3MF lost millimeter units")
    resources = {int(obj.get("id")): obj for obj in root.findall(f"{CORE}resources/{CORE}object")}
    result = {}
    for item in root.findall(f"{CORE}build/{CORE}item"):
        obj = resources[int(item.get("objectid"))]
        name = obj.get("name")
        vertices = np.array(
            [
                [float(node.get(axis)) for axis in ("x", "y", "z")]
                for node in obj.findall(f"{CORE}mesh/{CORE}vertices/{CORE}vertex")
            ],
            dtype=np.float64,
        )
        faces = np.array(
            [
                [int(node.get(axis)) for axis in ("v1", "v2", "v3")]
                for node in obj.findall(f"{CORE}mesh/{CORE}triangles/{CORE}triangle")
            ],
            dtype=np.int64,
        )
        result[name] = (vertices, faces)
    if set(result) != set(PART_NAMES):
        raise ValueError("generated trunk-blocker 3MF must contain the three manufactured parts")
    return result


def export_trunk_blocker(
    output: Path,
    spec: TrunkBlockerSpec = TrunkBlockerSpec(),
) -> Path:
    """Export the complete native-CAD blocker and its checked mesh derivatives."""

    if not isinstance(spec, TrunkBlockerSpec):
        raise ValueError("spec must be a TrunkBlockerSpec")
    output = Path(output)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"output directory is not empty: {output}; choose a new job directory")

    parts = make_trunk_blocker_parts(spec)
    prepared_parts = tuple(PreparedShape(part) for part in parts)
    brep_facts = {name: _shape_facts(part) for name, part in zip(PART_NAMES, parts)}
    if not all(
        facts["valid"] and facts["solids"] == 1 and facts["volume_mm3"] > 0
        for facts in brep_facts.values()
    ):
        raise ValueError("each manufactured blocker part must be one valid positive-volume solid")
    intersections = _part_intersections(parts)
    connector_proofs, protected_proof = _native_connector_proofs(parts[1], spec)
    base_anchor_proofs = _base_anchor_proofs(parts[0])
    rounding_proofs = _rounding_proofs(parts, spec)
    ratchet_proofs = _ratchet_proofs(parts, spec)
    guide_proofs = _guide_proofs(parts)
    pusher_bed_proofs = _pusher_bed_proofs(parts, spec)
    disassembly_proofs = _disassembly_proofs(parts)

    output.mkdir(parents=True, exist_ok=True)
    step_path = output / "trunk_blocker.step"
    step_facts, step_precision_mode = _checked_step_roundtrip(parts, step_path)
    meshes = []
    mesh_facts = {}
    mesh_reports = {}
    for name, prepared in zip(PART_NAMES, prepared_parts):
        vertices, faces, report = prepared.mesh
        meshes.append((name, vertices, faces))
        mesh_facts[name] = _mesh_facts(vertices, faces)
        mesh_reports[name] = report
    stl_paths = {}
    for name, vertices, faces in meshes:
        path = output / f"{name}.stl"
        write_stl(path, vertices, faces)
        stl_paths[name] = path
    package = output / "trunk_blocker.3mf"
    _write_complete_3mf(package, meshes)
    roundtrip = _read_complete_3mf(package)
    roundtrip_facts = {
        name: _mesh_facts(vertices, faces) for name, (vertices, faces) in roundtrip.items()
    }

    provenance = {
        "representation": (
            "Complete native build123d/OCCT BREP assembly with checked STEP as the primary "
            "representation and STL/core 3MF derived from the same three source solids."
        ),
        "front_connector_source": {
            "profile": "cargo_grid.interfaces.x_profile(plug=True)",
            "plug": "cargo_grid.interfaces.make_plug",
            "panel_connector": "cargo_grid.accessories.make_bidirectional_panel_connector",
            "construction": (
                "The shared bidirectional panel post uses the accepted inset stem and unchanged "
                "native 12.8 mm R2-tipped plug. Two rigid copies are fused into the 8 mm wall."
            ),
            "external_reference_asset_required": False,
            "literal_original_mesh_equivalence_claimed": False,
        },
        "connector_copies": connector_proofs,
        "underbody_anchor_copies": base_anchor_proofs,
        "unaffected_geometry_proof": protected_proof,
        "edge_rounding": rounding_proofs,
        "ratchet_geometry": ratchet_proofs,
        "centre_guides": guide_proofs,
        "pusher_bed_facing_underside": pusher_bed_proofs,
        "step_roundtrip": {
            "precision_mode": step_precision_mode,
            "parts": step_facts,
        },
        "mesh_reports": mesh_reports,
    }
    provenance_path = output / "provenance.json"
    provenance_path.write_text(json.dumps(provenance, indent=2) + "\n")

    files = {
        step_path.name: _sha256(step_path),
        package.name: _sha256(package),
        provenance_path.name: _sha256(provenance_path),
        **{path.name: _sha256(path) for path in stl_paths.values()},
    }
    manifest = {
        "schema_version": 1,
        "generator": {"name": "cargo-grid", "version": __version__},
        "kind": "experimental-trunk-blocker",
        "design_mode": {
            "workflow": "native Cargo-Grid BREP",
            "released_static_illustration": spec.released_illustration,
            "catalogue_member": False,
            "bambu_project": False,
        },
        "units": "millimeter",
        "parameters": {
            "extension_mm": spec.extension_mm,
            "wall_height_mm": DIMENSIONS.wall_height,
            "wall_width_mm": DIMENSIONS.width,
            "wall_backing_thickness_mm": DIMENSIONS.wall_thickness,
            "base_bounds_y_mm": [DIMENSIONS.base_start, DIMENSIONS.base_end],
            "finger_length_mm": DIMENSIONS.finger_length,
            "prong_shortening_mm": DIMENSIONS.prong_shortening,
            "flexible_beam_length_mm": DIMENSIONS.flexible_beam_length,
            "outer_finger_section_mm": [
                DIMENSIONS.outer_width,
                DIMENSIONS.finger_height,
            ],
            "centre_finger_section_mm": [
                DIMENSIONS.centre_width,
                DIMENSIONS.finger_height,
            ],
            "centre_guide_span_y_mm": [DIMENSIONS.guide_start, DIMENSIONS.guide_end],
            "centre_guide_wall_width_mm": (DIMENSIONS.guide_outer - DIMENSIONS.guide_inner),
            "centre_guide_channel_width_mm": 2 * DIMENSIONS.guide_inner,
            "centre_guide_top_z_mm": DIMENSIONS.guide_top,
            "base_passage_floor_top_z_mm": DIMENSIONS.floor,
            "finger_bottom_z_mm": DIMENSIONS.finger_bottom_z,
            "finger_top_z_mm": DIMENSIONS.finger_top_z,
            "pusher_bed_plane_z_mm": DIMENSIONS.finger_bottom_z,
            "keeper_underside_z_mm": DIMENSIONS.keeper_seat,
            "locking_teeth_per_outer_finger": len(MOVING_TOOTH_STATIONS_MM),
            "locking_pitch_mm": DIMENSIONS.pitch,
            "locking_tooth_tip_land_mm": DIMENSIONS.tooth_tip_land,
            "shared_ratchet_ramp_angle_degrees": DIMENSIONS.ratchet_ramp_angle_degrees,
            "locking_backlash_mm": DIMENSIONS.locking_backlash,
            "locking_engagement_mm": DIMENSIONS.locking_engagement,
            "released_tip_clearance_mm": DIMENSIONS.released_tip_clearance,
            "squeeze_pad_height_mm": DIMENSIONS.pad_height,
            "squeeze_pad_above_finger_mm": DIMENSIONS.pad_above_finger,
            "squeeze_pad_width_mm": DIMENSIONS.pad_outer - DIMENSIONS.pad_inner,
            "centre_squeeze_pad_width_mm": DIMENSIONS.centre_width,
            "squeeze_pad_length_mm": DIMENSIONS.pad_length,
            "push_pad_count": 3,
            "nominal_locking_stations": DIMENSIONS.positions,
            "nominal_travel_mm": DIMENSIONS.extension,
            "moving_tooth_carrier_y_mm": [
                DIMENSIONS.moving_carrier_start,
                DIMENSIONS.moving_carrier_end,
            ],
            "moving_tooth_carrier_height_mm": DIMENSIONS.tooth_height,
            "carrier_stop_extension_mm": (
                DIMENSIONS.moving_carrier_start + DIMENSIONS.pusher_y_offset - DIMENSIONS.keeper_end
            ),
            "connector_centres_above_pusher_floor_mm": list(
                CONNECTOR_CENTRES_ABOVE_PUSHER_FLOOR_MM
            ),
            "connector_projection_mm": 12.8,
            "base_anchor_centres_y_mm": list(BASE_ANCHOR_CENTRES_Y_MM),
            "base_anchor_pitch_mm": DIMENSIONS.anchor_pitch,
            "base_anchor_projection_mm": DIMENSIONS.anchor_depth,
            "keeper_length_mm": DIMENSIONS.keeper_length,
            "keeper_bounds_y_mm": [DIMENSIONS.keeper_start, DIMENSIONS.keeper_end],
            "keeper_back_shift_mm": DIMENSIONS.keeper_back_shift,
            "keeper_width_mm": DIMENSIONS.keeper_width,
            "keeper_clearance_bore_mm": DIMENSIONS.screw_clearance,
            "keeper_countersink_mouth_mm": DIMENSIONS.countersink_diameter,
            "keeper_countersink_depth_mm": DIMENSIONS.countersink_depth,
            "keeper_countersink_side_land_mm": DIMENSIONS.countersink_side_land,
            "keeper_countersink_included_angle_degrees": DIMENSIONS.countersink_angle,
            "base_blind_pilot_diameter_mm": DIMENSIONS.screw_pilot,
            "base_blind_pilot_depth_mm": (DIMENSIONS.rack_height - DIMENSIONS.screw_pilot_bottom),
            "base_blind_pilot_local_boss_radius_mm": DIMENSIONS.screw_pilot_boss_radius,
            "base_blind_pilot_minimum_radial_ligament_mm": (
                DIMENSIONS.screw_pilot_boss_radius - DIMENSIONS.screw_pilot / 2
            ),
        },
        "geometry": {
            "manufactured_parts": 3,
            "brep_parts": brep_facts,
            "step_roundtrip_parts": step_facts,
            "mesh_parts": mesh_facts,
            "parts_after_3mf_roundtrip": roundtrip_facts,
            "part_intersections": intersections,
            "native_front_interfaces": connector_proofs,
            "underbody_base_anchors": base_anchor_proofs,
            "centre_guides": guide_proofs,
            "pusher_bed_facing_underside": pusher_bed_proofs,
            "unaffected_backing_and_mechanism": protected_proof,
            "edge_rounding": rounding_proofs,
            "ratchet_geometry": ratchet_proofs,
            "adjustment_and_disassembly": disassembly_proofs,
            "complete_representation": True,
        },
        "export": {
            "primary_format": "STEP",
            "files": files,
            "step": step_path.name,
            "stl_parts": [path.name for path in stl_paths.values()],
            "core_3mf": package.name,
            "sliced": False,
            "physical_print_verified": False,
        },
        "compatibility": {
            "front_connectors": (
                "Two rigid copies of Cargo-Grid's shared bidirectional native-BREP panel post "
                "at 30 mm and 90 mm above the pusher floor; both tile face orientations pass "
                "the native geometry check, but physical tile fit is unverified."
            ),
            "underbody_base_plug": (
                "The independent analytic candidate is retained at Y=100 mm and duplicated at "
                "Y=40 mm on the same 60 mm pitch; physical mat fit is unverified."
            ),
            "upright_tile": (
                "The original 66 x 126 x 13 mm two-opening tile remains a separate accessory "
                "and is not included in blocker output."
            ),
        },
        "limitations": [
            "Organizer geometry only; not a rated cargo restraint.",
            "No literal geometry equality to the earlier copied connector mesh is claimed.",
            (
                "The sampled rigid-geometry sweep checks clearance and matching contact but "
                "does not establish elastic finger force, automatic ratcheting, creep, fatigue "
                "or load capacity."
            ),
            (
                f"The {DIMENSIONS.pad_height:g} mm squeeze pads block complete withdrawal "
                "while the keeper is installed; "
                "remove the four keeper screws and keeper before withdrawing the released pusher."
            ),
            (
                f"The current flat-floor geometry uses {DIMENSIONS.pitch:g} mm locking increments, "
                f"{DIMENSIONS.positions} lock positions and {DIMENSIONS.extension:g} mm total "
                "travel; the taller fingers can change flex and feel, and this revision has "
                "not passed physical fit."
            ),
            "No physical connector fit, screw torque, support removal, flatness or service suitability was verified.",
            "The STEP, STL and geometry-only 3MF contain no printer, filament or process preset.",
        ],
        "unsupported_combinations": [
            "standard part/layout/catalogue generation",
            "Bambu project metadata and automatic print orientation",
            "tile roof support",
            "stacking",
        ],
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    for prepared in prepared_parts:
        prepared.release_mesh()
    return manifest_path
