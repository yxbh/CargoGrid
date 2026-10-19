"""Supported Bambu dual-nozzle printers and their audited system profile names."""

from dataclasses import dataclass

from cargo_grid.parameters import DEFAULT_HOLE_DIAMETER_MM, BuildVolume, Exclusion

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
class PrinterNozzle:
    """One nozzle in Bambu slicer-slot order, not physical-extruder order."""

    name: str
    extruder_type: str
    x_mm: tuple[float, float]
    height_mm: float
    is_master: bool


@dataclass(frozen=True)
class CatalogueSupportTuning:
    """Measured full-hole Auto-support catalogue settings for Bambu Studio 02.08.02.61."""

    tower_width_mm: float
    solid_bottoms_mm: tuple[float, ...]
    long_axis_cells: int
    long_right_inset_mm: float
    tile_rotations_z: tuple[tuple[tuple[int, int], float], ...]


@dataclass(frozen=True)
class DualNozzlePrinter:
    name: str
    build: BuildVolume
    nozzles: tuple[PrinterNozzle, PrinterNozzle]
    profiles: dict[float, NozzleProfiles]
    tower_prime_volume_mm3: float = 30.0
    diagonal_fallback: bool = False
    omit_unfit: bool = False
    common_omission_reason: str | None = None
    catalogue_support: CatalogueSupportTuning | None = None

    def __post_init__(self) -> None:
        if len(self.nozzles) != 2 or sum(nozzle.is_master for nozzle in self.nozzles) != 1:
            raise ValueError("dual-nozzle printers need two nozzle slots and exactly one master")

    @property
    def shared_x_mm(self) -> tuple[float, float]:
        return max(n.x_mm[0] for n in self.nozzles), min(n.x_mm[1] for n in self.nozzles)

    @property
    def shared_height_mm(self) -> float:
        return min(nozzle.height_mm for nozzle in self.nozzles)

    @property
    def model(self) -> str:
        return f"Bambu Lab {self.name}"

    def common_build(self) -> BuildVolume:
        """Shared nozzle reach, with another 5 mm inset for models."""
        left, right = self.shared_x_mm
        inset = COMMON_MODEL_INSET_MM
        return BuildVolume(
            self.build.x,
            self.build.y,
            self.shared_height_mm,
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
                "min_x": self.shared_x_mm[0],
                "max_x": self.shared_x_mm[1],
                "min_y": 0,
                "max_y": self.build.y,
                "max_z": self.shared_height_mm,
            },
            "common_model_inset_mm": COMMON_MODEL_INSET_MM,
        }

    def common_fit_reason(self) -> str:
        return self.common_omission_reason or (
            f"actual bounds exceed {self.name} common reach "
            f"(X {self.shared_x_mm[0]:g}..{self.shared_x_mm[1]:g}, "
            f"Y 0..{self.build.y:g}, Z <= {self.shared_height_mm:g}) "
            "with its 5 mm model inset; no scaling applied"
        )

    def catalogue_support_tuning(
        self,
        *,
        auto_roof_support: bool,
        solid_bottom_mm: float,
        layer_height_mm: float,
        hole_diameter: float | None,
        hole_scope: str,
    ) -> CatalogueSupportTuning | None:
        tuning = self.catalogue_support
        if (
            tuning is not None
            and auto_roof_support
            and solid_bottom_mm in tuning.solid_bottoms_mm
            and layer_height_mm in {p.layer_height_mm for p in self.profiles.values()}
            and hole_diameter == DEFAULT_HOLE_DIAMETER_MM
            and hole_scope == "full"
        ):
            return tuning
        return None


H2D = DualNozzlePrinter(
    "H2D",
    BuildVolume(350, 320, 325),
    (
        PrinterNozzle("left", "Direct Drive", (0, 325), 320, False),
        PrinterNozzle("right", "Direct Drive", (25, 350), 325, True),
    ),
    {
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
    },
    common_omission_reason=(
        "actual bounds exceed the 300 mm H2D common reach (X 25..325) with its 5 mm "
        "inset; make it with part and choose the nozzle yourself"
    ),
)
X2D = DualNozzlePrinter(
    "X2D",
    BuildVolume(256, 256, 261),
    (
        PrinterNozzle("main", "Direct Drive", (0, 256), 261, True),
        PrinterNozzle("auxiliary", "Bowden", (20.5, 256), 256, False),
    ),
    {
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
    },
    tower_prime_volume_mm3=45,
    diagonal_fallback=True,
    omit_unfit=True,
    catalogue_support=CatalogueSupportTuning(
        tower_width_mm=18.0,
        solid_bottoms_mm=(0.0, 1.92),
        long_axis_cells=4,
        # The measured 5.1 mm right inset leaves 1.65 mm to the complete wipe reservation.
        long_right_inset_mm=5.1,
        tile_rotations_z=(((3, 4), 180),),
    ),
)
PRINTERS = {"h2d": H2D, "x2d": X2D}
CATALOGUE_INVENTORY_BUILD = H2D.build


def printer_for_common_build(build: BuildVolume) -> DualNozzlePrinter | None:
    return next((printer for printer in PRINTERS.values() if build == printer.common_build()), None)
