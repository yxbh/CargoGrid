"""Bambu material, profile and project-setting policy; no geometry construction."""

from dataclasses import dataclass

from cargo_grid.jobs import Job
from cargo_grid.parameters import count
from cargo_grid.printers import DualNozzlePrinter, bambu_flush_settings
from cargo_grid.roof_support import RoofSupportSettings

BAMBU_PROCESS_DEFAULTS = {
    "resolution": "0.003",
    "slice_closing_radius": "0.01",
}


@dataclass(frozen=True)
class Material:
    name: str
    kind: str
    color: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.kind.strip():
            raise ValueError("material name and type must be explicit")
        if len(self.color) != 7 or self.color[0] != "#":
            raise ValueError("material color must be #RRGGBB")
        try:
            int(self.color[1:], 16)
        except ValueError as error:
            raise ValueError("material color must be #RRGGBB") from error


@dataclass(frozen=True)
class BambuSettings:
    materials: tuple[Material, ...]
    nozzle: float
    layer_height: float
    roof_support: RoofSupportSettings | None = None
    printer_settings_id: str | None = None
    print_settings_id: str | None = None
    bed_type: str | None = None
    machine_nozzle_count: int = 1
    printer: DualNozzlePrinter | None = None

    def __post_init__(self) -> None:
        from cargo_grid.parameters import positive

        positive("nozzle diameter", self.nozzle)
        positive("layer height", self.layer_height)
        count("machine nozzle count", self.machine_nozzle_count)
        if not self.materials:
            raise ValueError("Bambu project requires explicit filament slots")
        if (self.printer_settings_id is None) != (self.print_settings_id is None):
            raise ValueError("printer and process profile IDs must be supplied together")
        if self.printer_settings_id is not None and (
            not self.printer_settings_id.strip() or not self.print_settings_id.strip()
        ):
            raise ValueError("printer and process profile IDs must not be empty")
        if self.printer is not None and not isinstance(self.printer, DualNozzlePrinter):
            raise ValueError("printer must be DualNozzlePrinter")
        if self.roof_support is not None:
            if not isinstance(self.roof_support, RoofSupportSettings):
                raise ValueError("roof_support must be RoofSupportSettings")
            if len(self.materials) != 2 or [m.kind.upper() for m in self.materials] != [
                "PETG",
                "PLA",
            ]:
                raise ValueError(
                    "roof supports require exactly PETG slot 1 (model/base) and PLA slot 2 (interface)"
                )


def _bambu_project_settings(job: Job, bambu: BambuSettings, plate_count: int = 1) -> dict:
    nozzle_count = (
        len(bambu.printer.nozzles)
        if bambu.printer is not None
        else max(bambu.machine_nozzle_count, 2 if bambu.roof_support else 1)
    )
    settings = {
        "version": "2.8.2.61",
        "printer_settings_id": bambu.printer_settings_id
        or "Cargo-Grid explicit envelope (not calibrated)",
        "print_settings_id": bambu.print_settings_id
        or "Cargo-Grid diagnostic layout (not a print preset)",
        "printable_area": [
            "0x0",
            f"{job.build.x:g}x0",
            f"{job.build.x:g}x{job.build.y:g}",
            f"0x{job.build.y:g}",
        ],
        "printable_height": f"{job.build.z:g}",
        "nozzle_diameter": [f"{bambu.nozzle:g}"] * nozzle_count,
        "layer_height": f"{bambu.layer_height:g}",
        "filament_diameter": ["1.75"] * len(bambu.materials),
        "filament_type": [material.kind for material in bambu.materials],
        "filament_colour": [material.color for material in bambu.materials],
        "filament_settings_id": [material.name for material in bambu.materials],
        "filament_is_support": ["0"] * len(bambu.materials),
        "filament_map_mode": "Auto For Match",
    }
    settings.update(BAMBU_PROCESS_DEFAULTS)
    if bambu.printer is not None:
        settings["printer_model"] = bambu.printer.model
    if nozzle_count > 1:
        settings.update(bambu_flush_settings(len(bambu.materials), nozzle_count))
        settings["extruder_type"] = (
            [nozzle.extruder_type for nozzle in bambu.printer.nozzles]
            if bambu.printer
            else ["Direct Drive"] * nozzle_count
        )
        settings["default_nozzle_volume_type"] = ["Standard"] * nozzle_count
        settings["nozzle_volume_type"] = ["Standard"] * nozzle_count
    if bambu.bed_type is not None:
        settings["curr_bed_type"] = bambu.bed_type
    overrides = set(BAMBU_PROCESS_DEFAULTS)
    if job.prime_tower_width_mm is not None:
        tower_shape = {
            "prime_tower_width": f"{job.prime_tower_width_mm:g}",
            "prime_tower_rib_wall": "0",
        }
        settings.update(tower_shape)
        overrides.update(tower_shape)
    if bambu.roof_support:
        settings.update(bambu.roof_support.native_settings(process_profile=bambu.print_settings_id))
        overrides.update(
            bambu.roof_support.native_process_override_keys(process_profile=bambu.print_settings_id)
        )
    default = next(iter(job.prime_tower_positions.values()), None)
    if job.prime_tower is not None and default is not None:
        # Positions remain per plate; a catalogue's rectangular shape is project-wide.
        positions = [job.prime_tower_positions.get(plate, default) for plate in range(plate_count)]
        tower = {
            "wipe_tower_x": [f"{x:g}" for x, _ in positions],
            "wipe_tower_y": [f"{y:g}" for _, y in positions],
        }
        settings.update(tower)
        overrides.update(tower)
    settings["different_settings_to_system"] = [
        ";".join(sorted(overrides)),
        *[""] * (len(bambu.materials) + 1),
    ]
    return settings
