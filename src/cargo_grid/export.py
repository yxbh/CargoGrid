"""STEP primary exports and independently authored core/Bambu 3MF packaging."""

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from functools import cached_property
from math import ceil, sqrt
from pathlib import Path
from shutil import copyfileobj
from tempfile import TemporaryFile
from typing import BinaryIO, Callable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr
from zipfile import ZIP_DEFLATED, ZipFile

from build123d import PrecisionMode, Shape, export_step, import_step
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.Precision import Precision

from cargo_grid._version import __version__
from cargo_grid.accessories import (
    Accessory,
    accessory_datums,
    required_bambu_object_settings,
    required_bambu_print_rotation,
    required_bambu_print_rotation_y,
)
from cargo_grid.bambu import (
    BAMBU_PROCESS_DEFAULTS as BAMBU_PROCESS_DEFAULTS,
)
from cargo_grid.bambu import (
    BambuSettings,
    _bambu_project_settings,
)
from cargo_grid.bambu import (
    Material as Material,
)
from cargo_grid.cross_posts import CROSS_POST_FAMILY
from cargo_grid.footprints import (
    ProjectedFootprint,
    projected_meshes_footprint,
)
from cargo_grid.jobs import Design, Job
from cargo_grid.meshes import write_stl
from cargo_grid.packing import PrintPlacement, TowerClearance, pack_sizes
from cargo_grid.parameters import DEFAULT_HOLE_DIAMETER_MM, Interface, count, positive
from cargo_grid.placements import _explicit_placements
from cargo_grid.prepared import Bounds, PreparedShape, rotated_points, rotation_matrix_3mf
from cargo_grid.rods import ROD_FAMILIES, Rod, RodBrace, fit_evidence
from cargo_grid.roof_support import (
    effective_object_settings,
    female_roofs,
    global_auto_support_reason,
    validate_roof_job,
)
from cargo_grid.stacking import StackSettings, Volume, stack_volumes

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"
MANIFEST_SCHEMA_VERSION = 1
UNSUPPORTED_COMBINATIONS = {
    "roof_support_with_stacking": "Roof supports and stacked separator jobs cannot be combined.",
    "stacked_catalogues": "Stack repeated part/layout tile quantities, not mixed catalogue samples.",
}


def _xml(parent, tag, **attributes):
    return ET.SubElement(parent, f"{{{CORE}}}{tag}", {k: str(v) for k, v in attributes.items()})


def _metadata(parent, key, value):
    ET.SubElement(parent, "metadata", key=key, value=str(value))


def _bytes(root) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _sides(clearance: TowerClearance) -> str:
    return "/".join(f"{getattr(clearance, side):g}" for side in ("left", "front", "right", "back"))


def _adaptive_volume(shape) -> float:
    properties = GProp_GProps()
    error = BRepGProp.VolumeProperties_s(
        shape.wrapped,
        properties,
        1e-12,
        True,
        False,
    )
    if error >= 1e-10:
        raise ValueError("adaptive volume integration did not converge")
    return properties.Mass()


@dataclass(frozen=True)
class _StepSource:
    shape: Shape = field(repr=False, compare=False)
    bounds: Bounds
    adaptive_volume: float
    volume_budget: float

    @classmethod
    def measure(cls, shape, bounds: Bounds | None = None) -> "_StepSource":
        return cls(
            shape,
            bounds if bounds is not None else Bounds.measure(shape),
            _adaptive_volume(shape),
            max(1e-6, shape.area * Precision.Confusion_s()),
        )

    @cached_property
    def volume(self) -> float:
        """Default-integration volume; it only explains a rejected reimport."""
        return self.shape.volume


def _checked_step_roundtrip(shape, path: Path, *, _source: _StepSource | None = None) -> tuple:
    attempts = (
        ("average", PrecisionMode.AVERAGE),
        ("least", PrecisionMode.LEAST),
        ("greatest", PrecisionMode.GREATEST),
        ("session", PrecisionMode.SESSION),
    )
    source = _source if _source is not None else _StepSource.measure(shape)
    volume_budget = source.volume_budget
    last = None
    failures = []
    for precision_mode, mode in attempts:
        if not export_step(shape, path, precision_mode=mode):
            raise ValueError(f"STEP export failed: {path}")
        restored = import_step(path)
        adaptive_delta = abs(_adaptive_volume(restored) - source.adaptive_volume)
        restored_bounds = restored.bounding_box()
        bounds_delta = max(
            abs(a - b)
            for a, b in zip(
                (*source.bounds.minimum, *source.bounds.maximum),
                (*restored_bounds.min, *restored_bounds.max),
            )
        )
        valid = restored.is_valid
        solids = len(restored.solids())
        last = (
            restored,
            precision_mode,
            adaptive_delta,
            volume_budget,
            bounds_delta,
        )
        if valid and solids == 1 and adaptive_delta <= volume_budget and bounds_delta <= 1e-5:
            return last
        # The default-integration volumes are diagnostics for rejected attempts only.
        default_volume_delta = abs(restored.volume - source.volume)
        failures.append(
            f"{precision_mode}:valid={valid},"
            f"solids={solids},default_volume={default_volume_delta:.9g},"
            f"adaptive={adaptive_delta:.9g},bounds={bounds_delta:.9g}"
        )
    raise ValueError(
        f"STEP roundtrip failed: {path.stem}; budget={volume_budget:.9g}; "
        f"attempts=[{'; '.join(failures)}]"
    )


def _validate_request(job: Job, bambu: BambuSettings | None, stack: StackSettings | None) -> None:
    if not job.designs:
        raise ValueError("a job needs at least one design")
    positive("part gap", job.part_gap, zero=True)
    if (
        job.prime_tower_width_mm is not None
        and bambu
        and (
            bambu.printer is None
            or bambu.printer.catalogue_support is None
            or not bambu.roof_support
        )
    ):
        raise ValueError("rectangular prime towers are limited to X2D Auto-support catalogues")
    names = set()
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    for design in job.designs:
        count("design quantity", design.quantity)
        required_pose_x = required_bambu_print_rotation(design.parameters)
        required_pose_y = required_bambu_print_rotation_y(design.parameters)
        if (
            bambu
            and (required_pose_x is not None or required_pose_y is not None)
            and (
                not design.apply_orientation_to_bambu
                or design.recommended_print_rotation_x != required_pose_x
                or design.recommended_print_rotation_y != required_pose_y
            )
        ):
            raise ValueError(
                f"{design.name}: Bambu accessory export requires its validated print orientation; "
                "create the design with accessory_design()"
            )
        required_object_settings = required_bambu_object_settings(design.parameters)
        if bambu and design.bambu_object_settings != required_object_settings:
            raise ValueError(
                f"{design.name}: Bambu accessory export requires its validated object settings; "
                "create the design with accessory_design()"
            )
        if bambu and design.apply_orientation_to_bambu and stack:
            raise ValueError(
                f"Bambu-oriented models cannot use stacking: {design.name} "
                "is turned over for printing"
            )
        name = design.name
        if (
            not isinstance(name, str)
            or not name
            or name in (".", "..")
            or name.endswith((" ", "."))
            or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name)
            or name.split(".")[0].upper() in reserved
        ):
            raise ValueError(
                "design names must be portable filenames without paths or reserved characters"
            )
        if name.casefold() in names:
            raise ValueError(f"duplicate design filename: {name}")
        names.add(name.casefold())
        if bambu:
            display_name = design.display_name or name
            if any(character in '<>:/\\|?*"' for character in display_name):
                raise ValueError(f"{name}: Bambu object label contains a forbidden character")
    if bambu:
        for plate, plate_name in job.plate_names.items():
            if any(character in '<>:/\\|?*"' for character in plate_name):
                raise ValueError(f"Bambu plate {plate + 1} label contains a forbidden character")
    if stack and not bambu:
        raise ValueError(
            "support-aware stacks require the Bambu backend and explicit material roles"
        )
    if stack and job.kind == "catalogue":
        raise ValueError(
            "catalogue stacks are not supported; stack repeated part or layout quantities"
        )
    if stack and bambu:
        if max(stack.model_slot, stack.support_slot, stack.interface_slot) > len(bambu.materials):
            raise ValueError("stack material slot exceeds declared filament list")
        if any(
            not d.name.startswith("tile_")
            or "family" in d.parameters
            or "interface" not in d.parameters
            for d in job.designs
        ):
            raise ValueError("stacking only accepts repeated identical tiles")
        if any(d.parameters["interface"].get("solid_bottom_mm") for d in job.designs):
            raise ValueError("stacked separator jobs do not support solid-bottom tiles yet")
    if bambu and bambu.roof_support:
        if stack:
            raise ValueError("roof supports and stacked separator jobs cannot be combined")
        validate_roof_job(job, bambu.roof_support, bambu.layer_height)


class _PreparedProject:
    """Snapshot inputs once; plan actual CAD bounds before checking any mesh."""

    def __init__(
        self,
        job: Job,
        bambu: BambuSettings | None,
        stack: StackSettings | None,
        catalogue: bool = False,
    ):
        self.job = deepcopy(job)
        self.job.validate_plate_builds()
        self.geometries: dict[int, PreparedShape] = {}
        self.mesh_sources: dict[int, tuple[PreparedShape, float, float, float]] = {}
        self.project_geometry: dict[int, PreparedShape] = {}
        self.batches: list[tuple[Design, list[Volume], int]] = []
        for design in self.job.designs:
            source = self.geometry(design.shape)
            posed = design.bambu_shape if bambu else design.shape
            oriented = bool(bambu and design.apply_orientation_to_bambu)
            self.mesh_sources[id(posed)] = (
                source,
                (design.recommended_print_rotation_x or 0) if oriented else 0,
                (design.recommended_print_rotation_y or 0) if oriented else 0,
                (design.recommended_print_rotation_z or 0) if oriented else 0,
            )
            # Keep the posed object alive even when a stack uses separate volumes.
            self.project_geometry[id(design)] = self.geometry(posed)
            remaining = 1 if catalogue else design.quantity
            while remaining:
                n = min(stack.count, remaining) if stack else 1
                if stack:
                    settings = StackSettings(
                        n,
                        stack.gap,
                        stack.interface_thickness,
                        stack.model_slot,
                        stack.support_slot,
                        stack.interface_slot,
                    )
                    volumes = stack_volumes(design, settings, self.job.build)
                else:
                    volumes = [Volume(design.display_name or design.name, posed, "model", 1)]
                self.batches.append((design, volumes, n))
                remaining -= n
        self.last_mesh_use: dict[int, int] = {}
        for index, (design, volumes, _) in enumerate(self.batches):
            for geometry in self.batch_geometries(design, volumes):
                self.last_mesh_use[id(geometry)] = index
        self.bounds = [
            Bounds.union(
                [self.geometry(v.shape).bounds for v in volumes if v.subtype == "normal_part"]
            )
            for _, volumes, _ in self.batches
        ]
        self.sizes = [bounds.size for bounds in self.bounds]
        if self.job.print_placements is not None:
            projected_footprints = (
                self.actual_projected_footprints(self.job.print_placements)
                if self.job.projected_footprint_clearances
                else None
            )
            self.placements = _explicit_placements(
                self.job.print_placements,
                self.sizes,
                self.job.build,
                self.job.part_gap,
                projected_footprints,
                self.job.projected_footprint_clearances,
                self.job.plate_builds,
            )
        else:
            self.placements = pack_sizes(
                self.sizes,
                self.job.build,
                gap=self.job.part_gap,
                pack=self.job.kind == "catalogue",
            )
        self.plate_count = max(p.plate for p in self.placements) + 1
        self.check_prime_towers()
        if bambu and self.plate_count > 36:
            raise ValueError(
                f"Bambu Studio supports at most 36 plates; packed job needs {self.plate_count}. "
                "Use a larger envelope or export smaller separate jobs."
            )

    def check_prime_towers(self) -> None:
        """Keep each reserved tower envelope on its plate and clear of every placed model."""
        if self.job.prime_tower is None:
            return
        for plate in self.job.prime_tower_positions:
            if plate >= self.plate_count:
                raise ValueError(f"prime tower plate {plate + 1} has no placed model")
            bounds = self.job.tower_bounds(plate)
            x0, y0, x1, y1 = bounds
            build = self.job.build
            if x0 < 0 or y0 < 0 or x1 > build.x or y1 > build.y:
                raise ValueError(f"prime tower on plate {plate + 1} leaves the build plate")
            clearance = self.job.tower_clearance(plate)
            k0, l0, k1, l1 = clearance.grow(bounds)
            for placement, size in zip(self.placements, self.sizes):
                if placement.plate != plate:
                    continue
                width, depth = size[:2] if placement.rotation % 180 == 0 else size[1::-1]
                # Planned models may touch the clearance edge; allow for rounding.
                if (
                    k0 + 1e-6 < placement.x + width
                    and placement.x < k1 - 1e-6
                    and l0 + 1e-6 < placement.y + depth
                    and placement.y < l1 - 1e-6
                ):
                    raise ValueError(
                        f"prime tower on plate {plate + 1} is within {clearance.minimum:g} mm "
                        "of a placed model"
                        if clearance == TowerClearance.uniform(clearance.minimum)
                        else f"prime tower on plate {plate + 1} is inside the reserved "
                        f"clearance of a placed model ({_sides(clearance)} mm)"
                    )

    def geometry(self, shape) -> PreparedShape:
        if id(shape) not in self.geometries:
            self.geometries[id(shape)] = PreparedShape(shape)
        return self.geometries[id(shape)]

    def actual_projected_footprints(
        self,
        placements: list[PrintPlacement],
    ) -> list[ProjectedFootprint | None]:
        result = []
        nested_plates = set(self.job.projected_footprint_clearances)
        for (_, volumes, _), placement in zip(self.batches, placements):
            if placement.plate not in nested_plates:
                result.append(None)
                continue
            result.append(
                projected_meshes_footprint([self.mesh(volume.shape, 0) for volume in volumes])
            )
        return result

    def mesh(self, shape, packing_rotation: int):
        geometry, rx, ry, rz = self.mesh_source(shape)
        points, faces, _ = geometry.mesh
        return rotated_points(points, rx, ry, rz + packing_rotation), faces

    def mesh_source(self, shape) -> tuple[PreparedShape, float, float, float]:
        if id(shape) in self.mesh_sources:
            return self.mesh_sources[id(shape)]
        return self.geometry(shape), 0, 0, 0

    def batch_geometries(self, design: Design, volumes: list[Volume]) -> list[PreparedShape]:
        return [
            self.geometry(design.shape),
            *(self.mesh_source(volume.shape)[0] for volume in volumes),
        ]

    def release_batch_meshes(self, index: int, design: Design, volumes: list[Volume]) -> None:
        for geometry in self.batch_geometries(design, volumes):
            if self.last_mesh_use[id(geometry)] == index:
                geometry.release_mesh()


def write_3mf(
    job: Job,
    path: Path,
    *,
    bambu: BambuSettings | None = None,
    stack: StackSettings | None = None,
    catalogue: bool = False,
) -> dict:
    _validate_request(job, bambu, stack)
    if path.exists():
        raise ValueError(f"output file already exists: {path}; choose a new path")
    prepared = _PreparedProject(job, bambu, stack, catalogue)
    with TemporaryFile() as mesh_buffer:
        return _write_3mf(prepared, path, mesh_buffer, bambu=bambu)


def _write_archive_entry(archive: ZipFile, name: str, data: bytes) -> None:
    """Streaming entries use the fixed ZIP epoch rather than the current wall-clock time."""
    with archive.open(name, "w") as output:
        output.write(data)


def _write_3mf(
    prepared: _PreparedProject,
    path: Path,
    mesh_buffer: BinaryIO,
    *,
    bambu: BambuSettings | None,
    export_design: Callable[[Design], None] | None = None,
) -> dict:
    job = prepared.job
    ET.register_namespace("", CORE)
    model = ET.Element(f"{{{CORE}}}model", unit="millimeter")
    # Bambu's importer selects its dialect using this marker. Attribution stays
    # independently authored; these are diagnostic settings, not factory presets.
    _xml(model, "metadata", name="Application").text = (
        "BambuStudio-02.08.02.61" if bambu else f"Cargo-Grid {__version__}"
    )
    _xml(model, "metadata", name="Designer").text = "Cargo-Grid independent parametric generator"
    _xml(model, "metadata", name="CargoGridVersion").text = __version__
    styles = sorted(
        {
            d.parameters["interface"]["joint_style"]
            for d in job.designs
            if "interface" in d.parameters and "joint_style" in d.parameters["interface"]
        }
    )
    if styles:
        _xml(model, "metadata", name="CargoGridJointStyles").text = ",".join(styles)
    if bambu:
        _xml(model, "metadata", name="BambuStudio:3mfVersion").text = "1"
    resources = _xml(model, "resources")
    build = _xml(model, "build")
    config = ET.Element("config")
    plates = []
    next_id = 1
    batches, sizes, placements = prepared.batches, prepared.sizes, prepared.placements
    plate_count = prepared.plate_count
    cols = ceil(sqrt(plate_count))
    mesh_cache = {}
    configured_plates = {}
    plate_records = {}
    exported_designs = set()
    for batch_index, ((design, volumes, quantity), placement, size) in enumerate(
        zip(batches, placements, sizes)
    ):
        if export_design is not None and id(design) not in exported_designs:
            export_design(design)
            exported_designs.add(id(design))
        plate_index = placement.plate
        px, py, rotation = placement.x, placement.y, placement.rotation
        origin = (
            plate_index % cols * job.build.x * 1.2,
            -(plate_index // cols) * job.build.y * 1.2,
        )
        rotated_bounds = prepared.bounds[batch_index].rotated_z(rotation)
        translation = (
            origin[0] + px - rotated_bounds.minimum[0],
            origin[1] + py - rotated_bounds.minimum[1],
            -rotated_bounds.minimum[2],
        )
        children = []
        for volume in volumes:
            # Reuse identical BREP objects within this export, while each build
            # item retains its own object identity and quantity.
            cache_key = (id(volume.shape), rotation)
            if cache_key in mesh_cache:
                ident = mesh_cache[cache_key]
            else:
                ident = next_id
                next_id += 1
                points, faces = prepared.mesh(volume.shape, rotation)
                mesh_buffer.write(
                    f'<object id="{ident}" type="model" name={quoteattr(volume.name)}>'
                    "<mesh><vertices>".encode()
                )
                # Python floats and ints format exactly like their NumPy scalars, only faster.
                for start in range(0, len(points), 8192):
                    mesh_buffer.write(
                        "".join(
                            f'<vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>'
                            for x, y, z in points[start : start + 8192].tolist()
                        ).encode()
                    )
                mesh_buffer.write(b"</vertices><triangles>")
                for start in range(0, len(faces), 8192):
                    mesh_buffer.write(
                        "".join(
                            f'<triangle v1="{a}" v2="{b}" v3="{c}"/>'
                            for a, b, c in faces[start : start + 8192].tolist()
                        ).encode()
                    )
                mesh_buffer.write(b"</triangles></mesh></object>")
                mesh_cache[cache_key] = ident
                del points, faces
            children.append((ident, volume))
        prepared.release_batch_meshes(batch_index, design, volumes)
        object_id = next_id
        next_id += 1
        label = f"{design.display_name or design.name}_batch_{batch_index + 1}"
        obj = _xml(resources, "object", id=object_id, type="model", name=label)
        components = _xml(obj, "components")
        for ident, _ in children:
            _xml(components, "component", objectid=ident)
        _xml(
            build,
            "item",
            objectid=object_id,
            printable="1",
            transform="1 0 0 0 1 0 0 0 1 " + " ".join(f"{v:.9g}" for v in translation),
        )
        configured = ET.SubElement(config, "object", id=str(object_id))
        _metadata(configured, "name", label)
        _metadata(configured, "extruder", 1)
        for key, value in _object_settings(design, bambu).items():
            _metadata(configured, key, value)
        for ident, volume in children:
            part = ET.SubElement(configured, "part", id=str(ident), subtype=volume.subtype)
            _metadata(part, "name", volume.name)
            if volume.subtype == "normal_part":
                _metadata(part, "extruder", volume.slot)
        if plate_index not in configured_plates:
            plate = ET.SubElement(config, "plate")
            _metadata(plate, "plater_id", plate_index + 1)
            _metadata(
                plate,
                "plater_name",
                job.plate_names.get(
                    plate_index,
                    f"catalogue_plate_{plate_index + 1}" if job.kind == "catalogue" else label,
                ),
            )
            _metadata(plate, "locked", "false")
            if bambu:
                settings = job.plate_settings.get(plate_index, {})
                _metadata(
                    plate,
                    "filament_map_mode",
                    settings.get("filament_map_mode", "Auto For Match"),
                )
                for key in ("filament_maps", "filament_volume_maps"):
                    if key in settings:
                        _metadata(plate, key, settings[key])
            configured_plates[plate_index] = plate
            plate_records[plate_index] = {
                "number": plate_index + 1,
                "design": design.name,
                "quantity": 0,
                "print_rotation": rotation,
                "bounds_mm": size,
                "volumes": [],
                "items": [],
                "settings": dict(job.plate_settings.get(plate_index, {})),
            }
        plate = configured_plates[plate_index]
        instance = ET.SubElement(plate, "model_instance")
        _metadata(instance, "object_id", object_id)
        _metadata(instance, "instance_id", 0)
        _metadata(instance, "identify_id", batch_index + 1)
        record = plate_records[plate_index]
        record["quantity"] += quantity
        item = {
            "design": design.name,
            "quantity": quantity,
            "x": px,
            "y": py,
            "rotation": rotation,
            "size_mm": size,
        }
        if design.display_name is not None:
            item["display_name"] = design.display_name
        if bambu and _object_settings(design, bambu):
            item["object_settings"] = _object_settings(design, bambu)
        record["items"].append(item)
        if bambu and design.apply_orientation_to_bambu:
            angle_x = design.recommended_print_rotation_x or 0
            angle_y = design.recommended_print_rotation_y or 0
            angle_z = design.recommended_print_rotation_z or 0
            # 3MF stores basis columns; this translation includes the displayed plate origin.
            transform = {
                "packing_rotation_z_degrees": rotation,
                "translation_mm": translation,
                "packed_size_mm": tuple(rotated_bounds.size),
                "plate_local_lower_corner_mm": (px, py, 0),
                "matrix_3mf": (
                    *rotation_matrix_3mf(angle_x, angle_y, angle_z + rotation),
                    *translation,
                ),
                "applied_to_mesh": True,
            }
            if design.recommended_print_rotation_x is not None:
                transform["rotation_x_degrees"] = design.recommended_print_rotation_x
                _metadata(
                    configured,
                    "cargo_grid_source_rotation_x",
                    design.recommended_print_rotation_x,
                )
            if design.recommended_print_rotation_y is not None:
                transform["rotation_y_degrees"] = design.recommended_print_rotation_y
                _metadata(
                    configured,
                    "cargo_grid_source_rotation_y",
                    design.recommended_print_rotation_y,
                )
            if design.recommended_print_rotation_z is not None:
                transform["rotation_z_degrees"] = design.recommended_print_rotation_z
                _metadata(
                    configured,
                    "cargo_grid_source_rotation_z",
                    design.recommended_print_rotation_z,
                )
            record["items"][-1]["source_to_project_transform"] = transform
        record["volumes"].extend(
            {
                "name": v.name,
                "role": v.role,
                "filament_slot": v.slot if v.subtype == "normal_part" else None,
                "subtype": v.subtype,
                "printed_part": v.subtype == "normal_part",
            }
            for v in volumes
        )
    for plate in configured_plates.values():
        config.remove(plate)
    config.extend(configured_plates[i] for i in sorted(configured_plates))
    plates = [plate_records[i] for i in sorted(plate_records)]
    types = ET.Element("Types", xmlns=CONTENT)
    for extension, content_type in (
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("model", "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"),
        ("config", "application/xml"),
    ):
        ET.SubElement(types, "Default", Extension=extension, ContentType=content_type)
    rels = ET.Element("Relationships", xmlns=REL)
    ET.SubElement(
        rels,
        "Relationship",
        Target="/3D/3dmodel.model",
        Id="model",
        Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel",
    )
    with ZipFile(path, "x", ZIP_DEFLATED) as archive:
        _write_archive_entry(archive, "[Content_Types].xml", _bytes(types))
        _write_archive_entry(archive, "_rels/.rels", _bytes(rels))
        with archive.open("3D/3dmodel.model", "w", force_zip64=True) as output:
            output.write(
                f'<?xml version="1.0" encoding="utf-8"?>'
                f'<model xmlns="{CORE}" unit="millimeter">'.encode()
            )
            for child in model:
                if child is resources:
                    output.write(b"<resources>")
                    mesh_buffer.seek(0)
                    copyfileobj(mesh_buffer, output, length=1024 * 1024)
                    for obj in resources:
                        output.write(ET.tostring(obj, encoding="utf-8"))
                    output.write(b"</resources>")
                else:
                    output.write(ET.tostring(child, encoding="utf-8"))
            output.write(b"</model>")
        if bambu:
            _write_archive_entry(archive, "Metadata/model_settings.config", _bytes(config))
            _write_archive_entry(
                archive,
                "Metadata/project_settings.config",
                json.dumps(_bambu_project_settings(job, bambu, plate_count), indent=2).encode(),
            )
    tower_printer = bambu.printer.name if bambu and bambu.printer else "H2D"
    return {
        "format": "bambu-project" if bambu else "core-geometry",
        "plates": plates,
        "packing": (
            "explicit validated placements"
            if job.print_placements is not None
            else "first-fit rectangles"
            if job.kind == "catalogue"
            else "one batch per plate"
        ),
        "part_gap_mm": job.part_gap,
        "application_import_verified": False,
        "sliced": False,
        "physical_print_verified": False,
        "filament_assignment": (
            {
                "mode": "Auto For Match",
                "physical_map_requested": None,
            }
            if bambu
            else None
        ),
        "joint_styles": styles,
        **(
            {}
            if job.prime_tower is None
            else {
                "prime_tower": {
                    "tower": (
                        "Bambu default (no tower settings written)"
                        if job.prime_tower_width_mm is None
                        else "X2D catalogue rectangular tower"
                    ),
                    **(
                        {
                            "process_settings": {
                                "prime_tower_width": f"{job.prime_tower_width_mm:g}",
                                "prime_tower_rib_wall": "0",
                            },
                            "settings_scope": "project",
                            "physical_stability_verified": False,
                        }
                        if job.prime_tower_width_mm is not None
                        else {}
                    ),
                    "reserved_reach_from_origin_mm": {
                        "left": job.prime_tower.left,
                        "front": job.prime_tower.front,
                        "right": job.prime_tower.right,
                        "back": job.prime_tower.back,
                    },
                    "plates": [
                        {
                            "plate": plate + 1,
                            "origin_mm": (x, y),
                            "reserved_extrusion_bounds_mm": job.tower_bounds(plate),
                            "minimum_model_clearance_mm": job.tower_clearance(plate).minimum,
                            "model_clearance_mm": asdict(job.tower_clearance(plate)),
                        }
                        for plate, (x, y) in sorted(job.prime_tower_positions.items())
                    ],
                    "note": (
                        f"Each plate sets only the tower origin. On the {tower_printer} it is where Bambu Studio 02.08.02.61 places the tower when it opens the project, and the reserved bounds cover its estimated tower, automatic brim and the extra depth seen in local slices. They are not a guarantee for other profiles or materials."
                        if job.prime_tower_width_mm is None
                        else "X2D catalogue width and rib-wall settings are project-wide; plates with measured four-cell parts use a front-left origin and other plates use front-right. Prime volumes, automatic brim, support feet and process support thresholds are unchanged. Bounds reserve the tower body, brim and nozzle-change wipe lines. Tall-plate tower stability is physically untested."
                    ),
                }
            }
        ),
        "roof_support": (
            _auto_roof_support_record(job, bambu, batches, plates)
            if bambu and bambu.roof_support
            else None
        ),
    }


def _auto_roof_support_record(job: Job, bambu: BambuSettings, batches, plates) -> dict:
    settings = bambu.roof_support
    objects = []
    for batch, (design, _, _) in enumerate(batches):
        roofs = female_roofs(design)
        objects.append(
            {
                "design": design.name,
                "batch": batch + 1,
                "family": design.parameters.get("family", "tile"),
                "object_settings": effective_object_settings(design, settings),
                "reason": global_auto_support_reason(design),
                "female_roofs": [{"side": roof.side, "index": roof.index} for roof in roofs],
                "support_may_occupy_openings": sorted(
                    {
                        (round(float(x), 6), round(float(y), 6))
                        for roof in roofs
                        for x, y in roof.occupied_openings
                    }
                ),
            }
        )
    supported = {entry["design"] for entry in objects}
    return {
        "mode": "auto",
        "contact_mode": settings.contact_mode,
        "contact_material_intent": "PETG model/base and distinct PLA interface; actual material compatibility and physical release must be checked",
        "global_enable_support": True,
        "object_support": "global normal(auto); all objects inherit support",
        "supported_objects": objects,
        "unsupported_designs": sorted({d.name for d, _, _ in batches} - supported),
        "coverage_note": "Auto support covers whole overhangs as the slicer detects them. All supports on these objects get the PLA interface, including accessory supports that were PETG-only before. Not physically tested.",
        "scope_caveat": "Support may occupy round holes next to pocket roofs while printing; remove it from the underside before assembly.",
        "physical_detachment_verified": False,
    }


def _object_settings(design: Design, bambu: BambuSettings | None) -> dict:
    return effective_object_settings(design, bambu.roof_support if bambu else None)


def export_job(
    job: Job,
    output: Path,
    *,
    stl: bool = True,
    bambu: BambuSettings | None = None,
    stack: StackSettings | None = None,
) -> Path:
    _validate_request(job, bambu, stack)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"output directory is not empty: {output}; choose a new job directory")
    for design in job.designs:
        if not design.shape.is_valid or len(design.shape.solids()) != 1 or design.shape.volume <= 0:
            raise ValueError(f"{design.name} must be one valid positive-volume solid before export")
    prepared = _PreparedProject(job, bambu, stack)
    job = prepared.job
    recommendations = {
        id(design): (
            prepared.project_geometry[id(design)].bounds
            if bambu
            else Bounds.measure(design.bambu_shape)
        )
        for design in job.designs
        if design.recommended_print_rotation_x is not None
        or design.recommended_print_rotation_y is not None
        or design.recommended_print_rotation_z is not None
    }
    output.mkdir(parents=True, exist_ok=True)
    entries = []

    def export_design(design: Design) -> None:
        step = output / f"{design.name}.step"
        geometry = prepared.geometry(design.shape)
        source = _StepSource.measure(design.shape, geometry.bounds)
        (
            restored,
            step_precision_mode,
            volume_delta,
            volume_budget,
            bounds_delta,
        ) = _checked_step_roundtrip(design.shape, step, _source=source)
        points, faces, mesh_report = geometry.mesh
        if stl:
            write_stl(output / f"{design.name}.stl", points, faces)
        interface_data = design.parameters.get("interface")
        compatibility = Interface(**interface_data).compatibility() if interface_data else None
        if compatibility is not None:
            family = design.parameters.get("family", "tile")
            edge_present = design.parameters.get(
                "tile_edge_interface_present",
                family
                in (
                    "tile",
                    "edge-x",
                    "edge-y",
                    "corner-in",
                    "corner-out",
                    "ramp",
                ),
            )
            compatibility["tile_edge_interface_present"] = edge_present
            if not edge_present:
                compatibility["original_tile_edge_dimensions"] = None
                compatibility["edge_note"] = (
                    "No tile-edge dovetails on this part; physical support-rail joints are a separate unchanged interface."
                )
            compatibility["x_attachment_interface_present"] = design.parameters.get(
                "x_attachment_interface_present",
                family
                in (
                    "tile",
                    "plate",
                    "vertical-tile-bracket",
                    "lock-45",
                    "vertical-stop",
                    "pull-handle",
                )
                or (family == CROSS_POST_FAMILY and design.parameters.get("mount") == "x-plug"),
            )
            if family != "tile":
                compatibility["geometry_warning"] = None
            if family in ("edge-x", "edge-y", "corner-in", "corner-out"):
                compatibility["edge_outward_mm"] = design.parameters.get("edge_outward", 10.0)
                compatibility["complete_edge_holes"] = design.parameters.get(
                    "complete_edge_holes", False
                )
                compatibility["edge_hole_diameter_mm"] = (
                    design.parameters.get(
                        "edge_hole_diameter",
                        DEFAULT_HOLE_DIAMETER_MM,
                    )
                    if compatibility["complete_edge_holes"]
                    else None
                )
            if not compatibility["x_attachment_interface_present"]:
                compatibility["original_x_attachment_dimensions"] = None
                compatibility["attachment_seating_note"] = (
                    "This part has no X socket or plug interface."
                )
        entries.append(
            {
                "name": design.name,
                "display_name": design.display_name or design.name,
                "parameters": design.parameters,
                "quantity": design.quantity,
                "size_mm": geometry.bounds.size,
                "assembly_frames": design.assembly_frames,
                "hole_placements": design.holes,
                "volume_mm3": source.adaptive_volume,
                "step_roundtrip": "passed",
                "step_precision_mode": step_precision_mode,
                "step_volume_method": "adaptive BRepGProp at 1e-12",
                "step_volume_delta_mm3": volume_delta,
                "step_volume_budget_mm3": volume_budget,
                "step_bounds_delta_mm": bounds_delta,
                "mesh": mesh_report,
                "joint_style": interface_data.get("joint_style") if interface_data else None,
                "compatibility": compatibility,
            }
        )
        if design.mating_datums:
            entries[-1]["mating_datums"] = design.mating_datums
        elif design.parameters.get("family") == "ramp":
            entries[-1]["mating_datums"] = accessory_datums(
                Accessory(
                    **{
                        **design.parameters,
                        "interface": Interface(**interface_data),
                    }
                )
            )
        if design.parameters.get("family") in ROD_FAMILIES:
            spec_type = Rod if design.parameters["family"] == "rod" else RodBrace
            round_spec = spec_type(**{k: v for k, v in design.parameters.items() if k != "family"})
            entries[-1]["mating_datums"] = accessory_datums(round_spec)
            entries[-1]["fit_evidence"] = fit_evidence(round_spec)
        if (
            design.recommended_print_rotation_x is not None
            or design.recommended_print_rotation_y is not None
            or design.recommended_print_rotation_z is not None
        ):
            bounds = recommendations[id(design)]
            rotations = []
            if design.recommended_print_rotation_x is not None:
                rotations.append({"axis": "X", "degrees": design.recommended_print_rotation_x})
            if design.recommended_print_rotation_y is not None:
                rotations.append({"axis": "Y", "degrees": design.recommended_print_rotation_y})
            if design.recommended_print_rotation_z is not None:
                rotations.append({"axis": "Z", "degrees": design.recommended_print_rotation_z})
            recommendation = {
                "rotations": rotations,
                "translation_mm": tuple(-value for value in bounds.minimum),
                "size_mm": bounds.size,
                "applied_to_exports": {
                    "step": False,
                    "stl": False,
                    "core_3mf": False,
                    "bambu_3mf": bool(bambu and design.apply_orientation_to_bambu),
                },
                "note": "Standalone recommended pose. Bambu-oriented designs are rotated before packing; exact source-to-project transforms are recorded per plate item. Source STEP/STL and core 3MF retain model orientation.",
            }
            if len(rotations) == 1:
                recommendation["rotation_axis"] = rotations[0]["axis"]
                recommendation["rotation_degrees"] = rotations[0]["degrees"]
            entries[-1]["recommended_print_orientation"] = recommendation
        if design.bambu_object_settings:
            entries[-1]["recommended_bambu_object_settings"] = {
                "scope": "object",
                "settings": dict(design.bambu_object_settings),
                "applied_to_bambu_3mf": bool(bambu),
                "note": "Normal Auto is scoped to this accessory object. It is separate from tile-roof support and still requires sliced-path and removal review.",
            }

    with TemporaryFile() as mesh_buffer:
        project = _write_3mf(
            prepared,
            output / "job.3mf",
            mesh_buffer,
            bambu=bambu,
            export_design=export_design,
        )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generator": {"name": "cargo-grid", "version": __version__},
        "kind": job.kind,
        "design_mode": {
            "workflow": job.kind,
            "joint_styles": project["joint_styles"],
            "hole_scopes": sorted(
                {
                    d.parameters.get("hole_scope", "interior")
                    for d in job.designs
                    if d.parameters.get("hole_diameter") is not None
                }
            ),
            "stacked": stack is not None,
        },
        "units": "millimeter",
        "build": asdict(job.build),
        "footprint_mm": job.footprint,
        "placement_policy": job.placement_policy or None,
        "job_metadata": job.manifest_metadata or None,
        "designs": entries,
        "omitted": job.omitted,
        "export": project,
        "joint_styles": project["joint_styles"],
        "compatibility": {
            "tile_edges": "Original roofed tile-edge joints; custom unit size, thickness and fit offsets affect compatibility.",
            "x_attachments": "Socket/plug dimensions and seating datum are independent of tile-edge joints. Custom pitch/height/fit offsets still affect compatibility.",
            "evidence": "Geometric checks do not establish physical fit.",
        },
        "physical_fit_verified": False,
        "unsupported_combinations": UNSUPPORTED_COMBINATIONS,
    }
    path = output / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path
