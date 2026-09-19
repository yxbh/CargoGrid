"""Round-hole divider rods and two-hole upper braces, in physical millimetres."""

from dataclasses import dataclass
from typing import ClassVar

from build123d import Location, Part, Solid

from cargo_grid.interfaces import horizontal_edges
from cargo_grid.parameters import positive

ROD_FAMILIES = ("rod", "rod-brace")
ROD_HEIGHTS_MM = (120.0, 240.0)
BRACE_SPACINGS_MM = (60.0, 120.0)
BRACE_BORES_MM = (10.0, 10.2, 10.4)
SHAFT_DIAMETER_MM = 10.0
UNDERSIDE_CLEARANCE_MM = 1.0
PEG_LEAD_IN_MM = 1.0
STOP_DIAMETER_MM = 18.0
STOP_THICKNESS_MM = 3.2
STOP_UPPER_RADIUS_MM = 1.0
ROD_TOP_RADIUS_MM = 2.0
BRACE_WIDTH_MM = 18.0
BRACE_THICKNESS_MM = 6.4
BORE_LEAD_IN_MM = 0.4
BRACE_UPPER_RADIUS_MM = 1.0
BRACE_LOWER_CHAMFER_MM = 0.4
LABEL_DEPTH_MM = 0.96
LABEL_HEIGHT_MM = 8.4
LABEL_DIGIT_WIDTH_MM = 4.8
LABEL_STROKE_MM = 1.4
LABEL_DIGIT_GAP_MM = 1.6
LABEL_GROUP_GAP_MM = 4.8
LABEL_DOT_MM = 1.6


@dataclass(frozen=True)
class Rod:
    """Height is above the flat stop; grid unit size does not scale this part."""

    above_mat_height_mm: float = 120.0
    peg_diameter_mm: float = 10.0
    tile_thickness_mm: float = 13.0
    family: ClassVar[str] = "rod"

    def __post_init__(self):
        for name in ("above_mat_height_mm", "peg_diameter_mm", "tile_thickness_mm"):
            positive(name, getattr(self, name))
        if self.above_mat_height_mm < 6:
            raise ValueError("rod above-mat height must be at least 6 mm")
        if not 9 <= self.peg_diameter_mm <= 10.5:
            raise ValueError("rod peg diameter must be between 9 and 10.5 mm")
        if self.tile_thickness_mm < 6:
            raise ValueError("rod tile thickness must be at least 6 mm")
        for name in ("above_mat_height_mm", "peg_diameter_mm", "tile_thickness_mm"):
            object.__setattr__(self, name, float(getattr(self, name)))

    @property
    def insertion_depth_mm(self) -> float:
        return self.tile_thickness_mm - UNDERSIDE_CLEARANCE_MM

    @property
    def straight_peg_length_mm(self) -> float:
        return self.insertion_depth_mm - PEG_LEAD_IN_MM

    @property
    def overall_length_mm(self) -> float:
        return self.insertion_depth_mm + self.above_mat_height_mm


@dataclass(frozen=True)
class RodBrace:
    """Two equal bores. Spacing is absolute, not a count of selected grid cells."""

    center_spacing_mm: float = 60.0
    bore_diameter_mm: float = 10.0
    family: ClassVar[str] = "rod-brace"

    def __post_init__(self):
        if self.center_spacing_mm not in BRACE_SPACINGS_MM:
            raise ValueError("rod-brace centre spacing must be 60 or 120 mm")
        if self.bore_diameter_mm not in BRACE_BORES_MM:
            raise ValueError("rod-brace bore diameter must be 10.0, 10.2 or 10.4 mm")
        object.__setattr__(self, "center_spacing_mm", float(self.center_spacing_mm))
        object.__setattr__(self, "bore_diameter_mm", float(self.bore_diameter_mm))


def _checked(shape, label: str) -> Part:
    if not shape.is_valid or len(shape.solids()) != 1 or shape.volume <= 0:
        raise ValueError(f"{label}: invalid or disconnected geometry")
    return Part(shape.solids(), label=label)


def make_rod(spec: Rod = Rod()) -> Part:
    """Upright source frame: peg tip Z0, seating shoulder Z=insertion depth."""
    shoulder = spec.insertion_depth_mm
    shaft = Solid.make_cylinder(
        SHAFT_DIAMETER_MM / 2, spec.above_mat_height_mm - STOP_THICKNESS_MM
    ).moved(Location((0, 0, shoulder + STOP_THICKNESS_MM)))
    shaft = shaft.fillet(ROD_TOP_RADIUS_MM, horizontal_edges(shaft, spec.overall_length_mm))
    stop = Solid.make_cylinder(STOP_DIAMETER_MM / 2, STOP_THICKNESS_MM).moved(
        Location((0, 0, shoulder))
    )
    stop = stop.fillet(STOP_UPPER_RADIUS_MM, horizontal_edges(stop, shoulder + STOP_THICKNESS_MM))
    peg = Solid.make_cylinder(spec.peg_diameter_mm / 2, spec.straight_peg_length_mm).moved(
        Location((0, 0, PEG_LEAD_IN_MM))
    )
    tip = Solid.make_cone(
        spec.peg_diameter_mm / 2 - PEG_LEAD_IN_MM,
        spec.peg_diameter_mm / 2,
        PEG_LEAD_IN_MM,
    )
    return _checked(
        shaft.fuse(stop, peg, tip).clean(),
        f"rod_h{spec.above_mat_height_mm:g}_peg{spec.peg_diameter_mm:g}",
    )


_SEGMENTS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abged",
    "3": "abgcd",
    "4": "fgbc",
    "5": "afgcd",
    "6": "afgecd",
    "7": "abc",
    "8": "abcdefg",
    "9": "abfgcd",
}


def _brace_label_tools(spec: RodBrace) -> list[Solid]:
    w, h, t = LABEL_DIGIT_WIDTH_MM, LABEL_HEIGHT_MM, LABEL_STROKE_MM
    half = (h - t) / 2
    bars = {
        "a": (0, h - t, w, t),
        "b": (w - t, half, t, h - half),
        "c": (w - t, 0, t, half + t),
        "d": (0, 0, w, t),
        "e": (0, 0, t, half + t),
        "f": (0, half, t, h - half),
        "g": (0, half, w, t),
    }
    groups = (f"{spec.center_spacing_mm:g}", f"{spec.bore_diameter_mm:.1f}")
    lengths = [
        sum(LABEL_DOT_MM if c == "." else w for c in group) + LABEL_DIGIT_GAP_MM * (len(group) - 1)
        for group in groups
    ]
    x = (spec.center_spacing_mm - sum(lengths) - LABEL_GROUP_GAP_MM) / 2
    tools = []
    for group in groups:
        for index, char in enumerate(group):
            boxes = (
                [(0, 0, LABEL_DOT_MM, LABEL_DOT_MM)]
                if char == "."
                else [bars[k] for k in _SEGMENTS[char]]
            )
            pieces = [
                Solid.make_box(dx, dy, LABEL_DEPTH_MM + 0.1).moved(
                    Location((x + bx, -h / 2 + by, BRACE_THICKNESS_MM - LABEL_DEPTH_MM))
                )
                for bx, by, dx, dy in boxes
            ]
            tools.append(pieces[0].fuse(*pieces[1:]).clean() if len(pieces) > 1 else pieces[0])
            x += LABEL_DOT_MM if char == "." else w
            if index < len(group) - 1:
                x += LABEL_DIGIT_GAP_MM
        x += LABEL_GROUP_GAP_MM
    return tools


def make_rod_brace(spec: RodBrace = RodBrace()) -> Part:
    """Flat source frame, bore axes Z through X0 and X=center spacing."""
    radius = BRACE_WIDTH_MM / 2
    body = Solid.make_box(spec.center_spacing_mm, BRACE_WIDTH_MM, BRACE_THICKNESS_MM).moved(
        Location((0, -radius, 0))
    )
    body = body.fuse(
        Solid.make_cylinder(radius, BRACE_THICKNESS_MM),
        Solid.make_cylinder(radius, BRACE_THICKNESS_MM).moved(
            Location((spec.center_spacing_mm, 0, 0))
        ),
    ).clean()
    body = body.fillet(BRACE_UPPER_RADIUS_MM, horizontal_edges(body, BRACE_THICKNESS_MM))
    body = body.chamfer(BRACE_LOWER_CHAMFER_MM, None, horizontal_edges(body, 0))
    r, lead = spec.bore_diameter_mm / 2, BORE_LEAD_IN_MM
    cutters = []
    for x in (0, spec.center_spacing_mm):
        cutters.extend(
            [
                Solid.make_cylinder(r, BRACE_THICKNESS_MM + 2).moved(Location((x, 0, -1))),
                Solid.make_cone(r + lead, r, lead).moved(Location((x, 0, 0))),
                Solid.make_cone(r, r + lead, lead).moved(
                    Location((x, 0, BRACE_THICKNESS_MM - lead))
                ),
            ]
        )
    return _checked(
        body.cut(*cutters, *_brace_label_tools(spec)).clean(),
        f"rod-brace_c{spec.center_spacing_mm:g}_bore{spec.bore_diameter_mm:g}",
    )


def rod_datums(spec: Rod) -> dict:
    return {
        "mount_centers": [(0, 0, spec.insertion_depth_mm)],
        "joins": [],
        "peg_tip_z": 0,
        "shoulder_z": spec.insertion_depth_mm,
        "top_z": spec.overall_length_mm,
        "above_mat_height_mm": spec.above_mat_height_mm,
        "shaft_diameter_mm": SHAFT_DIAMETER_MM,
        "peg_diameter_mm": spec.peg_diameter_mm,
        "straight_peg_z_mm": (PEG_LEAD_IN_MM, spec.insertion_depth_mm),
        "insertion_depth_mm": spec.insertion_depth_mm,
        "underside_clearance_mm": UNDERSIDE_CLEARANCE_MM,
        "stop_diameter_mm": STOP_DIAMETER_MM,
        "stop_thickness_mm": STOP_THICKNESS_MM,
        "top_round_mm": ROD_TOP_RADIUS_MM,
        "lead_in_mm": PEG_LEAD_IN_MM,
        "nominal_diametral_clearance_to_10mm_tile_hole_mm": 10 - spec.peg_diameter_mm,
        "assembly_translation_to_tile_mm": (0, 0, UNDERSIDE_CLEARANCE_MM),
    }


def brace_datums(spec: RodBrace) -> dict:
    return {
        "mount_centers": [(0, 0, 0), (spec.center_spacing_mm, 0, 0)],
        "joins": [],
        "bore_axis": "Z",
        "center_spacing_mm": spec.center_spacing_mm,
        "bore_diameter_mm": spec.bore_diameter_mm,
        "straight_bore_z_mm": (BORE_LEAD_IN_MM, BRACE_THICKNESS_MM - BORE_LEAD_IN_MM),
        "straight_bore_length_mm": BRACE_THICKNESS_MM - 2 * BORE_LEAD_IN_MM,
        "mouth_lead_in_mm": BORE_LEAD_IN_MM,
        "nominal_diametral_clearance_mm": round(spec.bore_diameter_mm - SHAFT_DIAMETER_MM, 6),
        "nominal_radial_clearance_mm": round((spec.bore_diameter_mm - SHAFT_DIAMETER_MM) / 2, 6),
        "label": f"{spec.center_spacing_mm:g} {spec.bore_diameter_mm:.1f}",
        "label_height_mm": LABEL_HEIGHT_MM,
        "label_depth_mm": LABEL_DEPTH_MM,
        "height_retention": "friction only; may slide or jam; not a positive lock",
        "standard_tile_hole_pair_mm": (
            ((30, 60), (90, 60)) if spec.center_spacing_mm == 60 else ((60, 30), (180, 30))
        ),
        "standard_tile_arrangement": (
            "one 2x2 tile"
            if spec.center_spacing_mm == 60
            else "two 2x1 tiles; second body origin translated X120"
        ),
        "spacing_policy": "physical millimetres, never grid-scaled; check usable holes on custom mats",
    }


def fit_evidence(spec: Rod | RodBrace) -> dict:
    if isinstance(spec, Rod):
        applies = (
            spec.above_mat_height_mm in ROD_HEIGHTS_MM
            and spec.peg_diameter_mm == 10
            and spec.tile_thickness_mm == 13
        )
        return {
            "status": "user-reported-fit" if applies else "not-tested",
            "interface": "rod mounting peg to existing printed mat",
            "reference_geometry": "120/240 mm above mat, nominal 10 mm peg, 13 mm tile",
            "reported_process": {
                "material_type": "PETG",
                "nozzle_mm": 0.8,
                "layer_mm": 0.32,
                "rod_print_rotation_y_degrees": 90,
            },
            "observation": "User completed the rod print and reported the nominal 10 mm fit was fine.",
            "applies_to_this_geometry": applies,
            "measured_diameter_or_force": None,
            "material_brand_confirmed": False,
            "strength_and_service_suitability": "not tested",
        }
    return {
        "status": "provisional" if spec.bore_diameter_mm == 10 else "not-tested",
        "interface": "brace bores to rod shafts",
        "user_accepted_nominal_10mm_bores": spec.bore_diameter_mm == 10,
        "physical_test_pending": True,
        "height_retention": "not tested; friction only, not a positive lock",
    }
