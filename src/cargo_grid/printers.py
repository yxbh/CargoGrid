"""Supported Bambu dual-nozzle printers and their audited system profile names."""

from dataclasses import dataclass

from cargo_grid.parameters import BuildVolume, Exclusion

DEFAULT_PART_CLEARANCE_MM = 4.0
COMMON_MODEL_INSET_MM = 5.0


@dataclass(frozen=True)
class NozzleProfiles:
    layer_height_mm: float
    printer: str
    process: str
    petg: str
    pla: str


@dataclass(frozen=True)
class DualNozzlePrinter:
    name: str
    build: BuildVolume
    common_x_mm: tuple[float, float]
    common_height_mm: float
    profiles: dict[float, NozzleProfiles]
    tower_prime_volume_mm3: float = 30.0

    @property
    def model(self) -> str:
        return f"Bambu Lab {self.name}"

    def common_build(self) -> BuildVolume:
        """Shared nozzle reach, with another 5 mm inset for models."""
        left, right = self.common_x_mm
        inset = COMMON_MODEL_INSET_MM
        return BuildVolume(
            self.build.x,
            self.build.y,
            self.common_height_mm,
            margin=inset,
            exclusions=(
                Exclusion(0, 0, left + inset, self.build.y),
                Exclusion(right - inset, 0, self.build.x - right + inset, self.build.y),
            ),
        )

    def placement_policy(self) -> dict:
        return {
            "name": f"{self.name} dual-nozzle safe",
            "common_reach_mm": {
                "min_x": self.common_x_mm[0],
                "max_x": self.common_x_mm[1],
                "min_y": 0,
                "max_y": self.build.y,
                "max_z": self.common_height_mm,
            },
            "common_model_inset_mm": COMMON_MODEL_INSET_MM,
        }


# Keep these public dictionaries available through cli.H2D_PROFILES / cli.X2D_PROFILES too.
H2D_PROFILES = {
    0.8: NozzleProfiles(
        0.32,
        "Bambu Lab H2D 0.8 nozzle",
        "0.32mm Balanced Strength @BBL H2D 0.8 nozzle",
        "Bambu PETG Basic @BBL H2D 0.8 nozzle",
        "Bambu PLA Basic @BBL H2D 0.8 nozzle",
    ),
    # At 0.2 mm the larger tower conflicts with two H2D catalogue plates.
    0.4: NozzleProfiles(
        0.24,
        "Bambu Lab H2D 0.4 nozzle",
        "0.24mm Standard @BBL H2D",
        "Bambu PETG Basic @BBL H2D 0.4 nozzle",
        "Bambu PLA Basic @BBL H2D",
    ),
}
X2D_PROFILES = {
    0.8: NozzleProfiles(
        0.32,
        "Bambu Lab X2D 0.8 nozzle",
        "0.32mm Balanced Quality @BBL X2D 0.8 nozzle",
        "Bambu PETG Basic @BBL X2D 0.8 nozzle",
        "Bambu PLA Basic @BBL X2D 0.8 nozzle",
    ),
    0.4: NozzleProfiles(
        0.24,
        "Bambu Lab X2D 0.4 nozzle",
        "0.24mm Standard @BBL X2D",
        "Bambu PETG Basic @BBL X2D 0.4 nozzle",
        "Bambu PLA Basic @BBL X2D 0.4 nozzle",
    ),
}
H2D = DualNozzlePrinter("H2D", BuildVolume(350, 320, 325), (25, 325), 320, H2D_PROFILES)
X2D = DualNozzlePrinter("X2D", BuildVolume(256, 256, 261), (20.5, 256), 256, X2D_PROFILES, 45)
PRINTERS = {"h2d": H2D, "x2d": X2D}


def printer_for_common_build(build: BuildVolume) -> DualNozzlePrinter | None:
    return next((printer for printer in PRINTERS.values() if build == printer.common_build()), None)
