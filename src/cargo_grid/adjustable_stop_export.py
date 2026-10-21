"""Print-project export for the four-piece adjustable-stop kit."""

import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from cargo_grid.adjustable_stop import (
    AdjustableStopSpec,
    make_adjustable_stop_parts,
    make_adjustable_stop_prong_lock_clip,
)
from cargo_grid.export import (
    BAMBU_PROCESS_DEFAULTS,
    BambuSettings,
    Material,
    export_job,
    write_3mf,
)
from cargo_grid.footprints import (
    minimum_projected_clearance,
    pack_projected_footprints,
    projected_mesh_footprint,
)
from cargo_grid.jobs import Design, Job
from cargo_grid.meshes import checked_mesh
from cargo_grid.packing import PrimeTower
from cargo_grid.plates import (
    AUTO_SUPPORT_FOOT_ALLOWANCE_MM,
    AUTO_SUPPORT_PLATE_MARGIN_MM,
    FOOTPRINT_SEARCH_ALLOWANCE_MM,
    dual_nozzle_tower_reservation,
)
from cargo_grid.printers import H2D, DualNozzlePrinter, NozzleProfiles
from cargo_grid.roof_support import GLOBAL_AUTO_SUPPORT_SETTINGS, RoofSupportSettings

ADJUSTABLE_STOP_MODEL_CLEARANCE_MM = 4.0
ADJUSTABLE_STOP_SUPPORT_CLEARANCE_MM = (
    ADJUSTABLE_STOP_MODEL_CLEARANCE_MM + AUTO_SUPPORT_FOOT_ALLOWANCE_MM
)
ADJUSTABLE_STOP_PLATE_NAME = "ADJUSTABLE STOP - PETG WITH PLA INTERFACE"


def _adjustable_stop_profile(
    nozzle_diameter_mm: float, printer: DualNozzlePrinter
) -> NozzleProfiles:
    profile = printer.profiles.get(nozzle_diameter_mm)
    if profile is None:
        choices = ", ".join(f"{nozzle:g}" for nozzle in printer.profiles)
        raise ValueError(f"adjustable-stop {printer.name} nozzle must be one of: {choices} mm")
    return profile


def adjustable_stop_settings(
    nozzle_diameter_mm: float = 0.8,
    *,
    dual_material: bool,
    printer: DualNozzlePrinter = H2D,
) -> BambuSettings:
    """Use the selected printer's official profiles without changing the stop geometry."""
    profile = _adjustable_stop_profile(nozzle_diameter_mm, printer)
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
        printer=printer,
    )


def adjustable_stop_print_job(
    nozzle_diameter_mm: float = 0.8,
    spec: AdjustableStopSpec = AdjustableStopSpec(),
    *,
    printer: DualNozzlePrinter = H2D,
) -> Job:
    """Pack one complete kit on one supported plate; the default remains the H2D."""
    profile = _adjustable_stop_profile(nozzle_diameter_mm, printer)
    base, pusher, keeper = make_adjustable_stop_parts(spec)
    designs = [
        Design(
            "fixed_base",
            base,
            {"print_preparation": "adjustable-stop-complete-kit"},
            display_name="Fixed base - connectors down",
        ),
        Design(
            "moving_wall",
            pusher,
            {"print_preparation": "adjustable-stop-complete-kit"},
            display_name="Moving wall - fingers down",
        ),
        Design(
            "keeper",
            keeper,
            {"print_preparation": "adjustable-stop-complete-kit"},
            display_name="Keeper - countersinks up",
        ),
        Design(
            "prong_lock_clip",
            make_adjustable_stop_prong_lock_clip(spec),
            {"print_preparation": "adjustable-stop-complete-kit"},
            display_name="Prong lock clip - side lying",
            recommended_print_rotation_x=90,
            apply_orientation_to_bambu=True,
        ),
    ]
    footprints = []
    for design in designs:
        vertices, faces, _ = checked_mesh(design.bambu_shape)
        footprints.append(projected_mesh_footprint(vertices, faces))

    build = printer.common_build()
    tallest = max(design.bambu_size[2] for design in designs)
    tower = dual_nozzle_tower_reservation(printer, profile.layer_height_mm, tallest)
    tower_left = tower.bounds[0] - tower.clearance.left
    bounds = (
        printer.shared_x_mm[0] + build.margin,
        AUTO_SUPPORT_PLATE_MARGIN_MM,
        tower_left,
        build.y - AUTO_SUPPORT_PLATE_MARGIN_MM,
    )
    placements = pack_projected_footprints(
        footprints,
        bounds,
        gap=ADJUSTABLE_STOP_SUPPORT_CLEARANCE_MM,
        search_gap=(ADJUSTABLE_STOP_SUPPORT_CLEARANCE_MM - FOOTPRINT_SEARCH_ALLOWANCE_MM),
    )
    if {placement.plate for placement in placements} != {0}:
        raise ValueError(f"complete adjustable-stop kit must fit one {printer.name} plate")
    if (
        minimum_projected_clearance(footprints, placements, plate=0)
        < ADJUSTABLE_STOP_SUPPORT_CLEARANCE_MM - 1e-6
    ):
        raise ValueError("complete adjustable-stop kit lost its support-foot clearance")

    return prepare_adjustable_stop_print_job(
        Job(
            designs,
            build,
            "adjustable-stop",
            part_gap=ADJUSTABLE_STOP_SUPPORT_CLEARANCE_MM,
            print_placements=placements,
            plate_names={0: ADJUSTABLE_STOP_PLATE_NAME},
            projected_footprints=footprints,
            projected_footprint_clearances={
                0: ADJUSTABLE_STOP_SUPPORT_CLEARANCE_MM,
            },
            plate_builds={0: build},
            manifest_metadata={
                "print_guidance": {
                    "orientations": {
                        "fixed_base": "connectors down",
                        "moving_wall": "fingers down",
                        "keeper": "countersinks up",
                        "prong_lock_clip": "X=+90, flat side on the bed",
                    },
                    "support": (
                        "Global normal Auto support is on with a PLA Basic interface so "
                        "downloaders can see that support is intended. Objects inherit it."
                    ),
                    "assembly": (
                        "Slide the moving wall into the base, fasten the keeper with four "
                        "M3x16 DIN 7991 screws, then press the prong lock clip over the "
                        "centre squeeze tab."
                    ),
                    "use": (
                        "Bags remain supported by the mat. The stop has no load or crash rating."
                    ),
                },
                "physical_evidence": {
                    "profile": ("H2D 0.8 mm nozzle, 0.32 mm Balanced Strength, Bambu PETG Basic"),
                    "adjustable_stop": (
                        "The fixed base, moving wall and keeper were physically test-fitted."
                    ),
                    "clip": (
                        "The 0.00 mm-per-side prong lock clip held without wobble and was firm "
                        "to remove."
                    ),
                    "nozzle_0p4": (
                        "Export is supported, but fit, flex, bridge shape, retention and strength "
                        "have not been physically tested at 0.4 mm. Try the clip before the full kit."
                    ),
                },
            },
        ),
        material_counts_by_plate={0: 2},
        nozzle_diameter_mm=nozzle_diameter_mm,
        printer=printer,
    )


def _apply_adjustable_stop_support_settings(path: Path) -> None:
    with ZipFile(path) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    settings = json.loads(entries["Metadata/project_settings.config"])
    support = RoofSupportSettings()
    process_profile = settings["print_settings_id"]
    settings.update(support.native_settings(process_profile=process_profile))
    existing = settings.get("different_settings_to_system", [""])
    overrides = set(existing[0].split(";")) if existing and existing[0] else set()
    overrides.update(BAMBU_PROCESS_DEFAULTS)
    overrides.update(support.native_process_override_keys(process_profile=process_profile))
    settings["different_settings_to_system"] = [
        ";".join(sorted(overrides)),
        *[""] * (len(settings["filament_settings_id"]) + 1),
    ]
    entries["Metadata/project_settings.config"] = (json.dumps(settings, indent=2) + "\n").encode()

    prefixes = {
        "Fixed base": "fixed_base",
        "Moving wall": "moving_wall",
        "Keeper": "keeper",
        "Prong lock clip": "prong_lock_clip",
    }
    model = ET.fromstring(entries["Metadata/model_settings.config"])
    configured = {}
    for item in model.findall("object"):
        metadata = {
            child.get("key"): child for child in item.findall("metadata") if child.get("key")
        }
        name = metadata["name"].get("value", "")
        design = next(
            (value for prefix, value in prefixes.items() if name.startswith(prefix)),
            None,
        )
        if design is None:
            raise ValueError(f"unknown adjustable-stop print object: {name}")
        configured[design] = {child.get("key") for child in item.findall("metadata")}
    expected = {"fixed_base", "moving_wall", "keeper", "prong_lock_clip"}
    if set(configured) != expected:
        raise ValueError("adjustable-stop project does not contain its four expected objects")
    for design in expected:
        if configured[design] & GLOBAL_AUTO_SUPPORT_SETTINGS.keys():
            raise ValueError(f"{design} must not have an object support override")
    patched = path.with_suffix(".patched")
    with ZipFile(patched, "w", ZIP_DEFLATED) as target:
        for name, data in entries.items():
            with target.open(name, "w") as output:
                output.write(data)
    patched.replace(path)


def write_adjustable_stop_print_project(
    path: Path,
    nozzle_diameter_mm: float = 0.8,
    spec: AdjustableStopSpec = AdjustableStopSpec(),
    *,
    printer: DualNozzlePrinter = H2D,
) -> dict:
    """Write one complete PETG/PLA adjustable-stop project without partial output."""
    if path.exists():
        raise ValueError(f"output file already exists: {path}; choose a new path")
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent, prefix=f".{path.stem}-") as temporary:
        target = Path(temporary) / path.name
        result = write_3mf(
            adjustable_stop_print_job(nozzle_diameter_mm, spec, printer=printer),
            target,
            bambu=adjustable_stop_settings(
                nozzle_diameter_mm,
                dual_material=True,
                printer=printer,
            ),
        )
        _apply_adjustable_stop_support_settings(target)
        with ZipFile(target) as archive:
            if archive.testzip() is not None:
                raise ValueError("adjustable-stop print project ZIP failed integrity check")
        target.replace(path)
    return result


def export_adjustable_stop_print_project(
    output: Path,
    nozzle_diameter_mm: float = 0.8,
    spec: AdjustableStopSpec = AdjustableStopSpec(),
    *,
    stl: bool = True,
    printer: DualNozzlePrinter = H2D,
) -> Path:
    """Export the complete adjustable-stop kit through the ordinary job manifest path."""
    manifest = export_job(
        adjustable_stop_print_job(nozzle_diameter_mm, spec, printer=printer),
        output,
        stl=stl,
        bambu=adjustable_stop_settings(nozzle_diameter_mm, dual_material=True, printer=printer),
    )
    _apply_adjustable_stop_support_settings(output / "job.3mf")
    return manifest


def prepare_adjustable_stop_print_job(
    job: Job,
    *,
    material_counts_by_plate: dict[int, int],
    nozzle_diameter_mm: float = 0.8,
    printer: DualNozzlePrinter = H2D,
) -> Job:
    """Reserve the selected printer's default tower only on multi-material plates."""
    profile = _adjustable_stop_profile(nozzle_diameter_mm, printer)
    if job.print_placements is None:
        raise ValueError("adjustable-stop print jobs need explicit placements")
    plates = {placement.plate for placement in job.print_placements}
    if set(material_counts_by_plate) != plates:
        raise ValueError("material counts must cover exactly the placed adjustable-stop plates")
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
        raise ValueError("adjustable-stop print jobs must not supply a prime tower")

    tower_plates = {plate for plate, count in material_counts_by_plate.items() if count > 1}
    if not tower_plates:
        return job

    positions = {}
    reaches = {}
    clearances = {}
    for plate in sorted(tower_plates):
        build = job.plate_builds.get(plate, job.build)
        if build != printer.common_build():
            raise ValueError(
                f"multi-material adjustable-stop plates require the {printer.name} common build"
            )
        height = max(
            design.bambu_size[2]
            for design, placement in zip(job.designs, job.print_placements, strict=True)
            if placement.plate == plate
        )
        tower = dual_nozzle_tower_reservation(printer, profile.layer_height_mm, height)
        positions[plate] = tower.origin
        reaches[plate] = tower.reach
        clearances[plate] = tower.clearance

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
