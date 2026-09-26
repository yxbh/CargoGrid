"""Measured Zeekr 7X rear lift-out-panel test section.

The measured wall stations own the nominal plan outline. The photogrammetry
registration is retained as review evidence, not as a source of fitted CAD
surfaces. This recipe intentionally stays separate from the generic CargoGrid
ramp and perimeter families.
"""

import json
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from hashlib import sha256
from math import atan, atan2, ceil, cos, pi, sin, sqrt, tan
from pathlib import Path

import numpy as np
from build123d import (
    Axis,
    Compound,
    Edge,
    Face,
    Location,
    Part,
    Plane,
    Solid,
    Vector,
    Wire,
    export_step,
    import_step,
    section,
)
from scipy.interpolate import CubicHermiteSpline, PchipInterpolator

from cargo_grid.accessories import (
    RAMP_CARRIER_RUN_MM,
    RAMP_FREE_EDGE_RADIUS_MM,
    RAMP_MINIMUM_FLAT_SHELF_MM,
    RAMP_SHELF_RADIUS_MM,
    accessory_datums,
    make_accessory,
    tile_matched_perimeter,
)
from cargo_grid.interfaces import socket_entry_tool, tile_join_tool, x_profile
from cargo_grid.jobs import Design, Job, tile_design
from cargo_grid.packing import PrintPlacement, h2d_common_build, pack_sizes
from cargo_grid.parameters import BuildVolume, Interface, Tile, positive
from cargo_grid.tiles import hole_placements

ORIGINAL_RIGHT_WALL_STATIONS_MM = (
    (0.0, 519.5),
    (490.0, 519.5),
    (518.0, 528.5),
    (540.0, 549.5),
    (559.0, 562.5),
    (572.0, 590.5),
    (595.0, 604.5),
    (620.0, 607.5),
    (850.0, 607.5),
    (860.0, 606.5),
    (880.0, 602.5),
    (900.0, 594.5),
    (920.0, 571.5),
    (940.0, 550.5),
    (955.0, 530.5),
)
SUPERSEDED_NE_ONLY_RIGHT_WALL_STATIONS_MM = (
    (620.0, 607.5),
    (820.0, 607.5),
    (835.0, 607.5),
    (840.0, 606.834),
    (845.0, 606.325),
    (850.0, 604.917),
    (855.0, 603.858),
    (860.0, 602.349),
    (865.0, 600.690),
    (870.0, 599.081),
    (875.0, 597.273),
    (920.0, 571.5),
    (940.0, 550.5),
    (955.0, 530.5),
)
DEFAULT_SE_ALONG_EDGE_SHIFT_MM = 0.0
DEFAULT_CENTRE_DEPTH_MM = 365.0
DEFAULT_PEN_OFFSET_MM = 5.0
DEFAULT_EAST_EDGE_X_MM = 602.5
TRACED_NORTH_CORNER_RADIUS_MM = 11.4
RAW_SE_TAPER_STATIONS_U_S_MM = (
    (0.0, 224.0),
    (1.0, 228.4),
    (2.0, 236.8),
    (5.0, 249.4),
    (10.0, 262.6),
    (20.0, 286.3),
    (30.0, 300.4),
    (40.0, 311.7),
    (60.0, 327.5),
    (80.0, 337.6),
    (100.0, 342.8),
    (120.0, 346.7),
    (140.0, 349.6),
    (160.0, 352.1),
)
RAW_NORTH_EDGE_BEND_STATIONS_U_S_MM = (
    (100.0, 0.0),
    (80.0, 1.4),
    (60.0, 2.9),
    (40.0, 4.7),
    (20.0, 6.2),
)
NORTH_EDGE_CORNER_SLOPE_DS_DU = -0.075
NORTH_EDGE_E_PROFILE_U_S_MM = (
    (10.0, 6.420062),
    (20.0, 5.594085),
    (30.0, 5.074964),
    (40.0, 4.349747),
    (50.0, 3.623004),
    (60.0, 2.901601),
    (70.0, 2.174858),
    (80.0, 1.654975),
    (90.0, 1.030517),
    (100.0, 0.209880),
    (110.0, 0.083119),
    (120.0, -0.040590),
    (130.0, -0.064302),
    (140.0, -0.088014),
    (150.0, 0.188265),
    (160.0, 0.064556),
    (170.0, 0.140079),
    (180.0, 0.017132),
    (190.0, -0.107340),
    (200.0, -0.228760),
    (210.0, 0.045993),
    (220.0, 0.121516),
    (230.0, -0.001431),
    (240.0, -0.025143),
    (250.0, 0.150377),
    (260.0, 0.027430),
    (270.0, 0.302947),
)
NORTH_EDGE_E_TERMINAL_SLOPE_DS_DU = 0.025332138974449723
FINAL_NE_A_PROFILE_U_S_MM = (
    (20.0, 6.934233),
    (40.0, 4.899442),
    (60.0, 2.863216),
    (80.0, 1.227887),
    (100.0, -0.009236),
    (120.0, -0.244209),
)
FINAL_NE_B_PROFILE_U_S_MM = (
    (20.0, 6.232840),
    (40.0, 4.815840),
    (60.0, 2.898861),
    (80.0, 1.278210),
    (100.0, -0.035136),
    (120.0, 0.050545),
    (140.0, 0.429811),
)
FINAL_SE_C_PROFILE_U_S_MM = (
    (10.0, 262.243706),
    (20.0, 285.395026),
    (40.0, 310.933027),
    (60.0, 326.887090),
    (80.0, 337.238418),
    (100.0, 342.387833),
    (120.0, 346.002864),
    (140.0, 348.474649),
    (150.0, 349.586804),
)
FINAL_SE_D_PROFILE_U_S_MM = (
    (5.0, 251.11),
    (10.0, 262.23),
    (20.0, 286.52),
    (40.0, 311.78),
    (80.0, 337.2),
    (120.0, 346.71),
    (160.0, 352.78),
    (200.0, 359.48),
    (240.0, 365.42),
    (260.0, 368.16),
    (300.0, 373.78),
)
FINAL_SE_D3_PROFILE_U_S_MM = (
    (10.0, 262.958608),
    (20.0, 285.800392),
    (30.0, 300.098762),
    (40.0, 311.250158),
    (50.0, 320.371642),
    (60.0, 327.139618),
    (70.0, 333.221970),
    (80.0, 337.933074),
    (90.0, 340.225204),
    (100.0, 342.367117),
    (110.0, 344.123841),
    (120.0, 345.714931),
    (130.0, 346.974753),
    (140.0, 348.018893),
    (150.0, 349.063033),
    (160.0, 350.157221),
    (170.0, 350.650538),
    (180.0, 351.914232),
    (190.0, 353.008420),
    (200.0, 353.971679),
    (210.0, 354.680679),
    (220.0, 355.682442),
    (230.0, 356.406858),
    (240.0, 357.397077),
    (250.0, 358.094532),
    (260.0, 358.973038),
    (270.0, 359.670493),
    (280.0, 360.398781),
    (290.0, 360.972979),
    (300.0, 361.755188),
    (310.0, 362.356346),
)
FINAL_SE_D2_PEN_CORRECTED_PROFILE_U_S_MM = (
    (602.5 - 426.9, 343.51),
    (602.5 - 406.9, 346.06),
    (602.5 - 386.9, 348.23),
    (602.5 - 367.0, 350.51),
    (602.5 - 347.0, 352.54),
    (602.5 - 327.0, 354.51),
    (602.5 - 307.0, 356.46),
)
REREGISTERED_TRACE_EVIDENCE = {
    "method": (
        "Overlay-only re-registration: NE-E uses its long straight front edge "
        "and R10.3 corner; NE-A/NE-B/SE-C/SE-D move with that corrected frame."
    ),
    "front_edge_paper_angle_deg": 0.43708551857180267,
    "front_edge_straightness_rms_mm": 0.12025963713513498,
    "corrected_front_edge_u110_265_mm": {
        "minimum_s": -0.22876019092591804,
        "maximum_s": 0.22640799566171665,
        "slope_deg": -0.005089720420583058,
    },
    "corner_radius_mm": 10.321844976910125,
    "realignment_s_change_mm": {
        "ne_a": -0.6712825609227107,
        "ne_b": -0.678001940941865,
        "se_c_and_se_d": -0.6712825609227107,
    },
    "residual_rms_mm": {
        "ne_a_before": 1.6273714255371394,
        "ne_a_after": 0.5820673542289427,
        "ne_b_before": 1.0553453766156824,
        "ne_b_after": 0.41791366443730044,
    },
    "cad_front_dip_agreement_mm": {
        "u20": {"cad": 6.2, "trace_range": (5.59, 6.83)},
        "u40": {"cad": 4.7, "trace_range": (4.35, 4.9)},
        "u60": {"cad": 2.9, "trace_range": (2.86, 2.9)},
        "u80": {"cad": 1.4, "trace_range": (1.23, 1.65)},
    },
    "cad_minus_se_c_range_u10_160_mm": (0.4, 1.4),
    "source_arrays_distributed": False,
    "geometry_changed": False,
}
PEN_OFFSET_CORRECTION_EVIDENCE = {
    "physical_test_result": (
        "User test-printed the corner and side caps: the shape was right, but the "
        "straight side and traced perimeter were uniformly about 5 mm too large."
    ),
    "cause": ("The pen barrel touched the panel while the tip traced outside the true edge."),
    "transform": "true_rel = traced_point - pen_offset*outward_normal - (pen_offset, pen_offset)",
    "pen_offset_mm": DEFAULT_PEN_OFFSET_MM,
    "east_edge_x_mm": DEFAULT_EAST_EDGE_X_MM,
    "superseded_true_edge_assumptions": {
        "east_edge_x_mm": 607.5,
        "north_corner_radius_mm": 11.4,
    },
    "se_d_registration": {
        "selected_rotation_deg": -2.0,
        "superseded_rotation_deg": -3.5,
        "overlap_p95_mm": 0.343,
        "crown_rms_mm": 0.84,
        "crown_max_abs_mm": 1.55,
        "rotation_note": (
            "The previous -3.5 degree angle compensated for the uncorrected pen offset."
        ),
    },
    "source_evidence_distributed": False,
}
CORRECTED_SE_SAMPLE_U_MM = (
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    30.0,
    40.0,
    60.0,
    80.0,
    100.0,
    120.0,
    140.0,
    155.571,
)
CORRECTED_NORTH_SAMPLE_U_MM = (95.35, 75.36, 55.41, 35.41, 15.37)
CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU = 0.115
PRIOR_FULL_BOOT_OUTLINE_INSET_MM = 5.0
TEST_TILE_MODULES = (4, 4, 2, 4, 4)
TEST_FIELD_WIDTH_MM = 1080.0
TEST_FIELD_DEPTH_MM = 240.0
TEST_FIELD_X_MIN_MM = -540.0
TEST_FIELD_Y_MIN_MM = 650.0
TEST_FIELD_Y_MAX_MM = 890.0
TEST_NORTH_LIMIT_MM = 620.0
TEST_REAR_LIMIT_MM = 950.0
SIDE_SPLIT_Y_MM = 770.0
H2D_REVIEW_GAP_MM = 10.0
CORNER_TIP_REGULARIZATION_MM = 0.1
SIDE_SOCKET_MINIMUM_WEB_MM = 1.5
PERIMETER_HOLE_MINIMUM_WEB_MM = 1.5
SCAN_OBJ_SHA256 = "16aeb59f14811f8954e4b47db878c1c62d886b18d42ac3892ec1b33bad4f1868"

SCAN_ALIGNMENT = {
    "authority": "comparison evidence only; measured mirrored PCHIP controls CAD",
    "obj_units_assumed": "metre",
    "literal_counts": {
        "vertices": 5164,
        "texture_coordinates": 6206,
        "normals": 5161,
        "triangular_faces": 10096,
    },
    "effective_surface": {
        "referenced_vertices": 5160,
        "nondegenerate_triangles": 10094,
        "connected_components": 1,
        "boundary_edges": 228,
        "watertight": False,
        "area_m2": 0.761114,
    },
    "pca": {
        "centroid_obj": (-0.141100908, 0.112952217, -0.048909491),
        "long_axis_obj": (0.905506551, -0.005511135, -0.424296492),
        "short_axis_obj": (0.424331750, 0.010175663, 0.905449624),
        "normal_axis_obj": (-0.000672558, -0.999933039, 0.011552679),
        "extents_obj": (1.332152, 0.398826, 0.046206),
    },
    "similarity_to_vehicle_xy": {
        "millimetres_per_obj_unit": 916.0885,
        "rotation_degrees": 0.067631,
        "translation_mm": (7.1815, 807.3693),
        "long_axis_maps_to": "+vehicle_x",
        "short_axis_maps_to": "+vehicle_y",
        "in_plane_reflection": False,
        "z_registration": "unresolved",
    },
    "side_wall_contour_method": {
        "source": "supplied 1200 px/m projected occupancy mask",
        "samples_scored": 815,
        "vehicle_y_range_mm": (643.584, 954.867),
        "filter": "five-row median on left/right silhouette",
        "fit": "8 mm soft-L1 similarity fit to mirrored measured PCHIP",
        "discarded": "seatward and tailward silhouette closure edges, not wall samples",
    },
    "lateral_residuals_mm": {
        "median_absolute": 1.881,
        "rms": 4.120,
        "p95_absolute": 9.537,
        "maximum_absolute": 12.483,
        "signed_mean": 0.066,
        "signed_median": -0.388,
    },
    "limitations": (
        "No comparable scan side contour exists below vehicle y=643.584 mm; "
        "normal sign and carpet z=0 are unresolved; rasterization, scan warp, "
        "station uncertainty and mirrored-left assumptions remain."
    ),
}

WITHDRAWN_SCAN_FOLLOWING_SAMPLES = (
    (-540.0, 950.000000000, 0.000000000),
    (-530.5, 955.000000000, 0.371182791),
    (-525.0, 956.637619554, 0.294835518),
    (-510.0, 961.001844664, 0.269264410),
    (-495.0, 964.760671260, 0.217268223),
    (-480.0, 967.637207618, 0.162920166),
    (-465.0, 969.761448382, 0.122355902),
    (-450.0, 971.377059616, 0.095357903),
    (-435.0, 972.660295018, 0.076030648),
    (-420.0, 973.686569153, 0.061448871),
    (-405.0, 974.523090274, 0.051458695),
    (-390.0, 975.239603409, 0.048862333),
    (-375.0, 975.989730671, 0.052887884),
    (-360.0, 976.831517533, 0.057705002),
    (-345.0, 977.722264310, 0.059421333),
    (-330.0, 978.614158274, 0.060947054),
    (-315.0, 979.551820879, 0.063907820),
    (-300.0, 980.532350860, 0.063824828),
    (-285.0, 981.467634310, 0.056697136),
    (-270.0, 982.247372276, 0.043370693),
    (-255.0, 982.805473251, 0.028023027),
    (-240.0, 983.142604779, 0.019888583),
    (-225.0, 983.410140947, 0.020799542),
    (-210.0, 983.784310492, 0.029753348),
    (-195.0, 984.337192817, 0.042805141),
    (-180.0, 985.102779687, 0.058227294),
    (-165.0, 986.119360761, 0.073639338),
    (-150.0, 987.328642598, 0.078928508),
    (-135.0, 988.488257340, 0.057492921),
    (-120.0, 989.174707165, 0.000000000),
    (-105.0, 989.072800976, -0.011980651),
    (-90.0, 988.312985666, -0.057888402),
    (-75.0, 987.299992024, -0.061969119),
    (-60.0, 986.441207382, -0.035169674),
    (-45.0, 986.060502812, 0.000000000),
    (-30.0, 986.346176909, 0.029496557),
    (-15.0, 987.326752450, 0.079131397),
    (0.0, 988.830168317, 0.101360711),
    (15.0, 990.367962404, 0.084427577),
    (30.0, 991.444411027, 0.040034441),
    (45.0, 991.860820040, 0.000000000),
    (60.0, 991.700300125, -0.014957146),
    (75.0, 991.327805198, -0.024483733),
    (90.0, 990.965642799, -0.020874196),
    (105.0, 990.689878055, -0.017024751),
    (120.0, 990.452091682, -0.020456652),
    (135.0, 990.019639036, -0.038207374),
    (150.0, 989.170263709, -0.069747156),
    (165.0, 987.808480218, -0.101335676),
    (180.0, 986.088576597, -0.112154455),
    (195.0, 984.442238858, -0.094793871),
    (210.0, 983.190912320, -0.059115040),
    (225.0, 982.504258182, -0.021048221),
    (240.0, 982.299269717, -0.003807068),
    (255.0, 982.266095899, -0.003740765),
    (270.0, 982.084247198, -0.018253208),
    (285.0, 981.530407055, -0.046787673),
    (300.0, 980.572716200, -0.070123571),
    (315.0, 979.406164143, -0.072281805),
    (330.0, 978.393408445, -0.052142448),
    (345.0, 977.756341078, -0.027776390),
    (360.0, 977.446795979, -0.014132933),
    (375.0, 977.285601405, -0.012702259),
    (390.0, 977.052671538, -0.020890067),
    (405.0, 976.574083797, -0.041298197),
    (420.0, 975.696174576, -0.073231471),
    (435.0, 974.229126478, -0.113956838),
    (450.0, 972.181593927, -0.150126730),
    (465.0, 969.680004197, -0.176252103),
    (480.0, 966.876892131, -0.194023598),
    (495.0, 963.850764088, -0.213394680),
    (510.0, 960.453621563, -0.241873097),
    (525.0, 956.560877850, -0.272998127),
    (530.5, 955.000000000, -0.359196476),
    (540.0, 950.000000000, 0.000000000),
)
SOUTH_ARCH_CENTER_Y_MM = 988.830168317
SOUTH_ARCH_ANCHOR_X_MM = 530.5
SOUTH_ARCH_ANCHOR_Y_MM = 955.0
SOUTH_ARCH_ENDPOINT_X_MM = 540.0
SOUTH_ARCH_ENDPOINT_Y_MM = 950.0


def _south_arch_y_slope(x_mm: float) -> tuple[float, float]:
    """Symmetric one-hump raised cosines; slope is measured dy/dx."""
    radius = abs(x_mm)
    sign = -1.0 if x_mm < 0 else 1.0
    if radius <= SOUTH_ARCH_ANCHOR_X_MM:
        phase = pi * radius / SOUTH_ARCH_ANCHOR_X_MM
        rise = SOUTH_ARCH_CENTER_Y_MM - SOUTH_ARCH_ANCHOR_Y_MM
        y = SOUTH_ARCH_ANCHOR_Y_MM + rise * (1 + cos(phase)) / 2
        radial_slope = -rise * pi * sin(phase) / (2 * SOUTH_ARCH_ANCHOR_X_MM)
    elif radius <= SOUTH_ARCH_ENDPOINT_X_MM:
        span = SOUTH_ARCH_ENDPOINT_X_MM - SOUTH_ARCH_ANCHOR_X_MM
        phase = pi * (radius - SOUTH_ARCH_ANCHOR_X_MM) / span
        rise = SOUTH_ARCH_ANCHOR_Y_MM - SOUTH_ARCH_ENDPOINT_Y_MM
        y = SOUTH_ARCH_ENDPOINT_Y_MM + rise * (1 + cos(phase)) / 2
        radial_slope = -rise * pi * sin(phase) / (2 * span)
    else:
        raise ValueError("analytic south arch is defined only over the tile field")
    if radius in (0.0, SOUTH_ARCH_ANCHOR_X_MM, SOUTH_ARCH_ENDPOINT_X_MM):
        radial_slope = 0.0
    return y, radial_slope * sign


SOUTH_CONTOUR_CAD_SAMPLES = tuple(
    (float(x), *_south_arch_y_slope(float(x)))
    for x in sorted(
        {
            *range(-540, 541, 15),
            -SOUTH_ARCH_ANCHOR_X_MM,
            SOUTH_ARCH_ANCHOR_X_MM,
        }
    )
)
WITHDRAWN_RAISED_COSINE_SAMPLES = SOUTH_CONTOUR_CAD_SAMPLES
SOUTH_PARABOLA_HEIGHT_MM = SOUTH_ARCH_CENTER_Y_MM - SOUTH_ARCH_ENDPOINT_Y_MM
SOUTH_PARABOLA_SECOND_DERIVATIVE = -2 * SOUTH_PARABOLA_HEIGHT_MM / SOUTH_ARCH_ENDPOINT_X_MM**2


def _south_parabola_y_slope(x_mm: float) -> tuple[float, float]:
    """User-selected single quadratic over the complete south edge."""
    if abs(x_mm) > SOUTH_ARCH_ENDPOINT_X_MM:
        raise ValueError("parabolic south crown is defined only over the tile field")
    normalized = x_mm / SOUTH_ARCH_ENDPOINT_X_MM
    y = SOUTH_ARCH_ENDPOINT_Y_MM + SOUTH_PARABOLA_HEIGHT_MM * (1 - normalized**2)
    slope = SOUTH_PARABOLA_SECOND_DERIVATIVE * x_mm
    return y, slope


SOUTH_CONTOUR_CAD_SAMPLES = tuple(
    (float(x), *_south_parabola_y_slope(float(x))) for x in range(-540, 541, 15)
)
WITHDRAWN_SINGLE_PARABOLA_SAMPLES = SOUTH_CONTOUR_CAD_SAMPLES


def _merged_taper_stations(
    pen_offset_mm: float,
    se_along_edge_shift_mm: float,
) -> tuple[tuple[float, float], ...]:
    curve = _corrected_taper_curve(pen_offset_mm, se_along_edge_shift_mm)
    return (
        (0.0, RAW_SE_TAPER_STATIONS_U_S_MM[0][1] - pen_offset_mm),
        *((u, float(curve(u))) for u in CORRECTED_SE_SAMPLE_U_MM),
    )


@lru_cache(maxsize=16)
def _corrected_taper_curve(
    pen_offset_mm: float,
    se_along_edge_shift_mm: float,
) -> PchipInterpolator:
    raw = np.asarray(RAW_SE_TAPER_STATIONS_U_S_MM)
    curve = PchipInterpolator(raw[:, 0], raw[:, 1] + se_along_edge_shift_mm)
    u = np.linspace(raw[0, 0], raw[-1, 0], 16001)
    s = curve(u)
    derivative = curve(u, 1)
    length = np.sqrt(1 + derivative**2)
    corrected_u = u + pen_offset_mm * derivative / length - pen_offset_mm
    corrected_s = s - pen_offset_mm / length - pen_offset_mm
    order = np.argsort(corrected_u)
    return PchipInterpolator(corrected_u[order], corrected_s[order])


def _crown_coefficients(
    centre_depth_mm: float,
    se_along_edge_shift_mm: float,
    outline_inset_mm: float = 0.0,
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM,
    east_edge_x_mm: float = DEFAULT_EAST_EDGE_X_MM,
) -> tuple[float, float, float, float]:
    junction_u, junction_s = _merged_taper_stations(
        pen_offset_mm,
        se_along_edge_shift_mm,
    )[-1]
    tangent = CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU
    distance = east_edge_x_mm - outline_inset_mm - junction_u
    depth = centre_depth_mm - junction_s
    minimum_depth = tangent * distance / 4
    maximum_depth = tangent * distance / 2
    if not minimum_depth - 1e-6 <= depth <= maximum_depth + 1e-6:
        raise ValueError(
            "centre depth must keep crown coefficients nonnegative: "
            f"{junction_s + minimum_depth:.6f} <= centre_depth_mm <= "
            f"{junction_s + maximum_depth:.6f}"
        )
    depth = min(max(depth, minimum_depth), maximum_depth)
    scaled_quadratic = 2 * depth - tangent * distance / 2
    scaled_quartic = tangent * distance / 2 - depth
    a = scaled_quadratic / distance**2
    b = scaled_quartic / distance**4
    return a, b, junction_s, tangent


def _south_crown_y_slope(
    x_mm: float,
    centre_depth_mm: float,
    se_along_edge_shift_mm: float,
    outline_inset_mm: float = 0.0,
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM,
    east_edge_x_mm: float = DEFAULT_EAST_EDGE_X_MM,
) -> tuple[float, float]:
    junction_u = _merged_taper_stations(
        pen_offset_mm,
        se_along_edge_shift_mm,
    )[-1][0]
    distance = east_edge_x_mm - outline_inset_mm - junction_u
    t = abs(x_mm)
    if t > distance + 1e-7:
        raise ValueError("south crown is defined only inside the merged taper junction")
    a, b, _, _ = _crown_coefficients(
        centre_depth_mm,
        se_along_edge_shift_mm,
        outline_inset_mm,
        pen_offset_mm,
        east_edge_x_mm,
    )
    s = centre_depth_mm - a * t**2 - b * t**4
    ds_dt = -2 * a * t - 4 * b * t**3
    slope_dy_dx = ds_dt if x_mm >= 0 else -ds_dt
    return 620 + s, slope_dy_dx


DEFAULT_CORRECTED_JUNCTION_U_MM = _merged_taper_stations(
    DEFAULT_PEN_OFFSET_MM,
    DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
)[-1][0]
DEFAULT_CORRECTED_JUNCTION_X_MM = DEFAULT_EAST_EDGE_X_MM - DEFAULT_CORRECTED_JUNCTION_U_MM
SOUTH_CONTOUR_CAD_SAMPLES = tuple(
    (
        float(x),
        *_south_crown_y_slope(
            float(x),
            DEFAULT_CENTRE_DEPTH_MM,
            DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
            0.0,
            DEFAULT_PEN_OFFSET_MM,
            DEFAULT_EAST_EDGE_X_MM,
        ),
    )
    for x in sorted(
        {
            *range(
                -int(DEFAULT_CORRECTED_JUNCTION_X_MM),
                int(DEFAULT_CORRECTED_JUNCTION_X_MM) + 1,
                5,
            ),
            -DEFAULT_CORRECTED_JUNCTION_X_MM,
            DEFAULT_CORRECTED_JUNCTION_X_MM,
        }
    )
)
(
    DEFAULT_CROWN_A,
    DEFAULT_CROWN_B,
    DEFAULT_CROWN_JUNCTION_S_MM,
    DEFAULT_CROWN_JUNCTION_SLOPE,
) = _crown_coefficients(
    DEFAULT_CENTRE_DEPTH_MM,
    DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
    0.0,
    DEFAULT_PEN_OFFSET_MM,
    DEFAULT_EAST_EDGE_X_MM,
)
SOUTH_CONTOUR_RAW_EXACT_SAMPLES = (
    (-540.0, 950.858318529),
    (-530.5, 955.030613315),
    (-300.0, 979.771441272),
    (-60.0, 986.166251452),
    (0.0, 989.694752181),
    (60.0, 992.130446324),
    (300.0, 981.322078034),
    (530.5, 955.814787949),
    (540.0, 952.134706115),
)
SOUTH_CONTOUR_ANALYSIS = {
    "source": "five registered pen traces with tape-measured symmetric crown",
    "curve_family": "one symmetric concave quartic crown inside the merged side-taper junction",
    "form": "s(t)=centre_depth-a*t^2-b*t^4, t=abs(vehicle_x)",
    "default_parameters": {
        "centre_depth_mm": DEFAULT_CENTRE_DEPTH_MM,
        "se_along_edge_shift_mm": DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
        "junction_t_mm": DEFAULT_CORRECTED_JUNCTION_X_MM,
        "junction_s_mm": DEFAULT_CROWN_JUNCTION_S_MM,
        "junction_slope_ds_du": DEFAULT_CROWN_JUNCTION_SLOPE,
        "a": DEFAULT_CROWN_A,
        "b": DEFAULT_CROWN_B,
    },
    "default_vehicle_center_y_mm": 620 + DEFAULT_CENTRE_DEPTH_MM,
    "centre_depth_status": "user tape measurement",
    "method": (
        "The merged monotone side PCHIP fixes the junction position and slope. "
        "The user tape depth fixes the south centre. The quartic coefficients are solved "
        "analytically for C1 continuity; nonnegative coefficients keep one maximum and no inflection."
    ),
    "supersedes": (
        "the straight baseline, scan-following Hermite, raised cosine, single parabola, "
        "original handover rear stations and NE-only provisional outline"
    ),
    "physical_fit_verified": False,
}


@dataclass(frozen=True)
class RearReviewParameters:
    """Named review dimensions in the measured vehicle coordinate frame."""

    outline_inset_mm: float = 0.0
    field_x_min_mm: float = TEST_FIELD_X_MIN_MM
    field_y_min_mm: float = TEST_FIELD_Y_MIN_MM
    north_limit_y_mm: float = TEST_NORTH_LIMIT_MM
    rear_limit_y_mm: float = TEST_REAR_LIMIT_MM
    side_split_y_mm: float = SIDE_SPLIT_Y_MM
    traced_north_corner_radius_mm: float = TRACED_NORTH_CORNER_RADIUS_MM
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM
    east_edge_x_mm: float = DEFAULT_EAST_EDGE_X_MM
    se_along_edge_shift_mm: float = DEFAULT_SE_ALONG_EDGE_SHIFT_MM
    centre_depth_mm: float = DEFAULT_CENTRE_DEPTH_MM
    interface: Interface = Interface()
    packing_gap_mm: float = H2D_REVIEW_GAP_MM

    def __post_init__(self) -> None:
        positive("outline inset", self.outline_inset_mm, zero=True)
        positive("packing gap", self.packing_gap_mm)
        positive("traced north corner radius", self.traced_north_corner_radius_mm)
        positive("pen offset", self.pen_offset_mm, zero=True)
        positive("east edge x", self.east_edge_x_mm)
        if self.traced_north_corner_radius_mm <= self.pen_offset_mm:
            raise ValueError("pen offset must be smaller than traced north corner radius")
        if self.interface != Interface():
            raise ValueError("Zeekr rear review requires the standard 60x13 original interface")
        if self.field_x_min_mm != -TEST_FIELD_WIDTH_MM / 2:
            raise ValueError("Zeekr rear review field must remain centred at x=0")
        if self.field_y_min_mm - self.north_limit_y_mm != 30:
            raise ValueError("Zeekr rear review requires the selected 30 mm north finishing band")
        if not (
            self.field_y_min_mm < self.side_split_y_mm < self.field_y_min_mm + TEST_FIELD_DEPTH_MM
        ):
            raise ValueError("side split must lie inside the four-row tile mating run")
        if self.rear_limit_y_mm <= self.field_y_min_mm + TEST_FIELD_DEPTH_MM:
            raise ValueError("rear limit must lie beyond the tile field")
        _crown_coefficients(
            self.centre_depth_mm,
            self.se_along_edge_shift_mm,
            self.outline_inset_mm,
            self.pen_offset_mm,
            self.east_edge_x_mm,
        )

    @property
    def north_corner_radius_mm(self) -> float:
        return self.traced_north_corner_radius_mm - self.pen_offset_mm

    @property
    def field_y_max_mm(self) -> float:
        return self.field_y_min_mm + TEST_FIELD_DEPTH_MM

    def assembly_y_mm(self, measured_vehicle_y_mm: float) -> float:
        """North-positive review coordinate from raw tailgate-positive measurements."""
        return self.rear_limit_y_mm - measured_vehicle_y_mm

    @property
    def field_assembly_y_min_mm(self) -> float:
        return self.assembly_y_mm(self.field_y_max_mm)

    @property
    def field_assembly_y_max_mm(self) -> float:
        return self.assembly_y_mm(self.field_y_min_mm)


@lru_cache(maxsize=16)
def _taper_interpolators(
    pen_offset_mm: float,
    se_along_edge_shift_mm: float,
) -> tuple[PchipInterpolator, CubicHermiteSpline]:
    stations = _merged_taper_stations(pen_offset_mm, se_along_edge_shift_mm)
    u = np.asarray([station[0] for station in stations])
    s = np.asarray([station[1] for station in stations])
    inverse_seed = PchipInterpolator(s, u)
    inverse_slopes = inverse_seed(s, 1)
    inverse_slopes[0] = 0
    inverse_slopes[-1] = 1 / CORRECTED_TAPER_JUNCTION_SLOPE_DS_DU
    inverse = CubicHermiteSpline(s, u, inverse_slopes)
    dense_s = np.linspace(s[0], s[-1], 8001)
    dense_u = inverse(dense_s)
    if np.any(np.diff(dense_u) <= 0):
        raise ValueError("regularized corrected taper is not strictly monotone")
    return PchipInterpolator(dense_u, dense_s), inverse


@lru_cache(maxsize=1)
def _raw_north_edge_curve() -> CubicHermiteSpline:
    stations = sorted(RAW_NORTH_EDGE_BEND_STATIONS_U_S_MM)
    u = np.asarray([station[0] for station in stations])
    s = np.asarray([station[1] for station in stations])
    slopes = PchipInterpolator(u, s)(u, 1)
    slopes[0] = NORTH_EDGE_CORNER_SLOPE_DS_DU
    slopes[-1] = 0
    return CubicHermiteSpline(u, s, slopes)


@lru_cache(maxsize=16)
def _corrected_north_edge_curve(pen_offset_mm: float) -> CubicHermiteSpline:
    corrected_u, corrected_s = _corrected_north_edge_station_arrays(pen_offset_mm)
    slopes = PchipInterpolator(corrected_u, corrected_s)(corrected_u, 1)
    slopes[0] = NORTH_EDGE_CORNER_SLOPE_DS_DU
    slopes[-1] = 0
    return CubicHermiteSpline(corrected_u, corrected_s, slopes)


def _corrected_north_edge_station_arrays(
    pen_offset_mm: float,
) -> tuple[np.ndarray, np.ndarray]:
    raw = np.asarray(sorted(RAW_NORTH_EDGE_BEND_STATIONS_U_S_MM), dtype=float)
    u = raw[:, 0]
    s = raw[:, 1]
    derivative = np.gradient(s, u)
    derivative[0] = NORTH_EDGE_CORNER_SLOPE_DS_DU
    length = np.sqrt(1 + derivative**2)
    corrected_u = u - pen_offset_mm * derivative / length - pen_offset_mm
    corrected_s = s + pen_offset_mm / length - pen_offset_mm
    corrected_s[-1] = 0
    return corrected_u, corrected_s


def _corrected_north_edge_stations(
    pen_offset_mm: float,
) -> tuple[tuple[float, float], ...]:
    u, s = _corrected_north_edge_station_arrays(pen_offset_mm)
    return tuple(
        (float(station_u), float(station_s))
        for station_u, station_s in zip(reversed(u), reversed(s), strict=True)
    )


def _north_edge_s_slope(
    u_mm: float,
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM,
) -> tuple[float, float]:
    curve = _corrected_north_edge_curve(pen_offset_mm)
    if u_mm <= curve.x[0]:
        return float(curve(curve.x[0])), float(curve(curve.x[0], 1))
    if u_mm >= curve.x[-1]:
        return 0.0, 0.0
    return float(curve(u_mm)), float(curve(u_mm, 1))


def _pen_corrected_trace(
    points: tuple[tuple[float, float], ...],
    *,
    boundary: str,
    pen_offset_mm: float,
) -> tuple[tuple[float, float], ...]:
    values = np.asarray(points, dtype=float)
    derivative = np.gradient(values[:, 1], values[:, 0])
    length = np.sqrt(1 + derivative**2)
    if boundary == "north":
        corrected_u = values[:, 0] - pen_offset_mm * derivative / length - pen_offset_mm
        corrected_s = values[:, 1] + pen_offset_mm / length - pen_offset_mm
    elif boundary == "south":
        corrected_u = values[:, 0] + pen_offset_mm * derivative / length - pen_offset_mm
        corrected_s = values[:, 1] - pen_offset_mm / length - pen_offset_mm
    else:
        raise ValueError(f"unsupported trace boundary {boundary!r}")
    return tuple((float(u), float(s)) for u, s in zip(corrected_u, corrected_s, strict=True))


def _north_corner_geometry(
    parameters: RearReviewParameters,
) -> dict[str, tuple[float, float] | float]:
    radius = parameters.north_corner_radius_mm
    straight_x = parameters.east_edge_x_mm - parameters.outline_inset_mm
    center_x = straight_x - radius
    corrected = _corrected_north_edge_curve(parameters.pen_offset_mm)
    line_u = float(corrected.x[0])
    line_s = float(corrected(line_u))
    line_slope_ds_du = float(corrected(line_u, 1))
    line_slope = -line_slope_ds_du
    line_intercept = 620 + line_s + line_slope_ds_du * (straight_x - line_u)
    center_y = radius * sqrt(1 + line_slope**2) + line_slope * center_x + line_intercept
    signed = -line_slope * center_x + center_y - line_intercept
    tangent_x = center_x + line_slope * signed / (1 + line_slope**2)
    tangent_y = center_y - signed / (1 + line_slope**2)
    tangent_u = straight_x - tangent_x
    return {
        "center": (center_x, center_y),
        "north_tangent": (tangent_x, tangent_y),
        "north_tangent_u": tangent_u,
        "east_tangent": (straight_x, center_y),
    }


def right_wall_x_mm(
    y_mm: float,
    *,
    outline_inset_mm: float = 0.0,
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM,
    east_edge_x_mm: float = DEFAULT_EAST_EDGE_X_MM,
    se_along_edge_shift_mm: float = DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
) -> float:
    """Merged straight wall and inverse monotone taper PCHIP."""
    positive("outline inset", outline_inset_mm, zero=True)
    s = y_mm - 620
    stations = _merged_taper_stations(pen_offset_mm, se_along_edge_shift_mm)
    if s < -1e-7 or s > stations[-1][1] + 1e-7:
        raise ValueError("wall sample y lies outside the merged station range")
    s = min(max(s, 0.0), stations[-1][1])
    u = (
        0.0
        if s <= stations[0][1]
        else float(_taper_interpolators(pen_offset_mm, se_along_edge_shift_mm)[1](s))
    )
    result = east_edge_x_mm - outline_inset_mm - u
    if result <= 0:
        raise ValueError("outline inset removes the measured half-width")
    return result


def right_review_outline_x_mm(
    y_mm: float,
    *,
    outline_inset_mm: float = 0.0,
    traced_north_corner_radius_mm: float = TRACED_NORTH_CORNER_RADIUS_MM,
    pen_offset_mm: float = DEFAULT_PEN_OFFSET_MM,
    east_edge_x_mm: float = DEFAULT_EAST_EDGE_X_MM,
    se_along_edge_shift_mm: float = DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
) -> float:
    """Merged right outline including the tangent north plan corner."""
    positive("outline inset", outline_inset_mm, zero=True)
    parameters = RearReviewParameters(
        outline_inset_mm=outline_inset_mm,
        traced_north_corner_radius_mm=traced_north_corner_radius_mm,
        pen_offset_mm=pen_offset_mm,
        east_edge_x_mm=east_edge_x_mm,
        se_along_edge_shift_mm=se_along_edge_shift_mm,
    )
    geometry = _north_corner_geometry(parameters)
    center_x, center_y = geometry["center"]
    _, north_y = geometry["north_tangent"]
    if north_y <= y_mm <= center_y:
        return center_x + sqrt(
            max(0.0, parameters.north_corner_radius_mm**2 - (y_mm - center_y) ** 2)
        )
    if y_mm < north_y:
        return float(geometry["north_tangent"][0])
    return right_wall_x_mm(
        y_mm,
        outline_inset_mm=outline_inset_mm,
        pen_offset_mm=pen_offset_mm,
        east_edge_x_mm=east_edge_x_mm,
        se_along_edge_shift_mm=se_along_edge_shift_mm,
    )


def tail_y_mm(x_mm: float, parameters: RearReviewParameters = RearReviewParameters()) -> float:
    """Merged taper outside the junction and concave quartic crown inside."""
    t = abs(x_mm)
    straight_x = parameters.east_edge_x_mm - parameters.outline_inset_mm
    junction_u = _merged_taper_stations(
        parameters.pen_offset_mm,
        parameters.se_along_edge_shift_mm,
    )[-1][0]
    junction_x = straight_x - junction_u
    if t <= junction_x + 1e-7:
        return _south_crown_y_slope(
            x_mm,
            parameters.centre_depth_mm,
            parameters.se_along_edge_shift_mm,
            parameters.outline_inset_mm,
            parameters.pen_offset_mm,
            parameters.east_edge_x_mm,
        )[0]
    if t > straight_x + 1e-7:
        raise ValueError("x lies outside the merged rear outline")
    u = straight_x - t
    s = float(
        _taper_interpolators(
            parameters.pen_offset_mm,
            parameters.se_along_edge_shift_mm,
        )[0](u)
    )
    return 620 + s


def _tail_slope_dy_dx(
    x_mm: float,
    parameters: RearReviewParameters,
) -> float:
    t = abs(x_mm)
    straight_x = parameters.east_edge_x_mm - parameters.outline_inset_mm
    corrected = _taper_interpolators(
        parameters.pen_offset_mm,
        parameters.se_along_edge_shift_mm,
    )[0]
    junction_u = _merged_taper_stations(
        parameters.pen_offset_mm,
        parameters.se_along_edge_shift_mm,
    )[-1][0]
    junction_x = straight_x - junction_u
    if t <= junction_x + 1e-7:
        return _south_crown_y_slope(
            x_mm,
            parameters.centre_depth_mm,
            parameters.se_along_edge_shift_mm,
            parameters.outline_inset_mm,
            parameters.pen_offset_mm,
            parameters.east_edge_x_mm,
        )[1]
    u = straight_x - t
    ds_du = float(corrected(u, 1))
    return -ds_du if x_mm >= 0 else ds_du


def _profile_face(
    x: float,
    raw_tip: float,
    carrier: float,
    height: float,
    y_offset: float,
) -> Face:
    return Face(
        Wire.make_polygon(
            [
                (x, y_offset, 0),
                (x, y_offset + raw_tip, 0),
                (x, y_offset + carrier, height),
                (x, y_offset, height),
            ],
            close=True,
        )
    )


def _ramp_profile_wire(
    x: float,
    run: float,
    interface: Interface,
    *,
    y_offset: float = 0,
) -> Wire:
    """Rounded filled-ramp section ending at an exact finished run."""
    positive("ramp run", run)
    height = interface.height
    carrier = _ramp_carrier_run_mm(run)
    nose_radius = min(RAMP_FREE_EDGE_RADIUS_MM, max(0.02, run * 0.15))
    flat_shelf = min(RAMP_MINIMUM_FLAT_SHELF_MM, max(0.02, carrier * 0.2))

    def nose_extent(raw_tip: float) -> float:
        face = _profile_face(x, raw_tip, carrier, height, y_offset)
        nose = max(face.vertices(), key=lambda vertex: vertex.Y)
        return face.fillet_2d(nose_radius, [nose]).bounding_box().max.Y - y_offset

    low, high = run, run + 4 * height
    while nose_extent(high) < run:
        high += max(10, height)
    for _ in range(48):
        middle = (low + high) / 2
        if nose_extent(middle) < run:
            low = middle
        else:
            high = middle
    raw_tip = (low + high) / 2
    face = _profile_face(x, raw_tip, carrier, height, y_offset)
    angle = atan(height / (raw_tip - carrier))
    shelf_radius = min(
        RAMP_SHELF_RADIUS_MM,
        (carrier - flat_shelf) / tan(angle / 2),
    )
    shelf = [
        vertex
        for vertex in face.vertices()
        if abs(vertex.Y - (y_offset + carrier)) < 1e-7 and abs(vertex.Z - height) < 1e-7
    ]
    face = face.fillet_2d(shelf_radius, shelf)
    face = face.fillet_2d(
        nose_radius,
        [max(face.vertices(), key=lambda vertex: vertex.Y)],
    )
    return face.outer_wire()


def _variable_ramp(
    x_runs: list[tuple[float, float]],
    interface: Interface,
    *,
    y_offset: float = 0,
) -> Part:
    if len(x_runs) < 2 or any(b[0] <= a[0] for a, b in zip(x_runs, x_runs[1:])):
        raise ValueError("variable ramp sections require increasing x positions")
    wires = [_ramp_profile_wire(x, run, interface, y_offset=y_offset) for x, run in x_runs]
    return Part(Solid.make_loft(wires, ruled=True).wrapped)


def _ramp_carrier_run_mm(run: float) -> float:
    return min(RAMP_CARRIER_RUN_MM, max(0.05, run * 0.25))


def _hole_cutter(
    x: float,
    y: float,
    interface: Interface,
) -> Solid:
    return Solid.make_cylinder(5, interface.height + 2).moved(Location((x, y, -1)))


def _south_boundary_hole_centers(
    cells: int,
    parameters: RearReviewParameters,
) -> tuple[tuple[float, float], ...]:
    tile = Tile(
        cells,
        4,
        parameters.interface,
        hole_diameter=10,
        hole_scope="full",
    )
    centers = tuple(
        (placement.x, placement.y)
        for placement in hole_placements(tile)
        if placement.accepted and abs(placement.y) < 1e-8
    )
    expected = tuple(
        (index * parameters.interface.pitch / 2, 0.0) for index in range(2 * cells + 1)
    )
    if centers != expected:
        raise ValueError("accepted tile south-boundary hole pattern changed")
    return centers


@lru_cache(maxsize=16)
def _male_tab_root_side_web_mm(interface: Interface) -> float:
    root = section(tile_join_tool(interface, male=True), section_by=Plane.XZ)
    half_width = min(abs(vertex.X) for vertex in root.vertices() if abs(vertex.X) > 5 + 1e-7)
    return half_width - 5


def _south_hole_feasibility(
    x0: float,
    cells: int,
    parameters: RearReviewParameters,
) -> list[dict]:
    centers = _south_boundary_hole_centers(cells, parameters)
    reports = []
    for local_x, local_y in centers:
        run = tail_y_mm(x0 + local_x, parameters) - parameters.field_y_max_mm
        carrier = _ramp_carrier_run_mm(run)
        nearest = min(
            sqrt((local_x - other_x) ** 2 + (local_y - other_y) ** 2)
            for other_x, other_y in centers
            if (other_x, other_y) != (local_x, local_y)
        )
        tab_center = abs((local_x / parameters.interface.pitch) % 1 - 0.5) < 1e-7
        reports.append(
            {
                "center": (local_x, local_y),
                "assembly_center": (
                    x0 + local_x,
                    parameters.field_assembly_y_min_mm,
                    0.0,
                ),
                "cut_depth_into_high_shelf_mm": 5.0,
                "high_shelf_carrier_run_mm": carrier,
                "minimum_high_shelf_material_behind_cut_mm": carrier - 5,
                "minimum_adjacent_hole_web_mm": nearest - 10,
                "cuts_male_tab": tab_center,
                "minimum_male_tab_root_side_web_mm": (
                    _male_tab_root_side_web_mm(parameters.interface) if tab_center else None
                ),
                "ownership": (
                    "outer side-corner quarter"
                    if abs(x0 + local_x) == TEST_FIELD_WIDTH_MM / 2
                    else (
                        "module-seam quarter"
                        if local_x in (0.0, cells * parameters.interface.pitch)
                        else "south-boundary half"
                    )
                ),
            }
        )
    return reports


def _tail_samples(
    x0: float,
    x1: float,
    parameters: RearReviewParameters,
    *,
    maximum_step_mm: float = 2.5,
) -> list[tuple[float, float]]:
    count = max(1, ceil((x1 - x0) / maximum_step_mm))
    samples = set(np.linspace(x0, x1, count + 1).tolist())
    samples.update(sample_x for sample_x, _, _ in SOUTH_CONTOUR_CAD_SAMPLES if x0 < sample_x < x1)
    values = {x: tail_y_mm(x, parameters) for x in sorted(samples)}
    ordered = sorted(values)
    # The ruled loft is piecewise linear in plan. Make the first chord on both
    # sides of every module seam equal to the analytic plan tangent.
    for boundary in (x0, x1):
        if not -TEST_FIELD_WIDTH_MM / 2 <= boundary <= TEST_FIELD_WIDTH_MM / 2:
            continue
        slope = _tail_slope_dy_dx(boundary, parameters)
        neighbor = ordered[1] if boundary == x0 else ordered[-2]
        values[neighbor] = values[boundary] + slope * (neighbor - boundary)
    return [(x - x0, values[x] - parameters.field_y_max_mm) for x in ordered]


def _south_ramp_shape(
    x0: float,
    cells: int,
    parameters: RearReviewParameters,
) -> tuple[Part, list[tuple[float, float]]]:
    width = cells * parameters.interface.pitch
    x_runs = _tail_samples(x0, x0 + width, parameters)
    source_runs = sorted((width - x, run) for x, run in x_runs)
    part = (
        _variable_ramp(source_runs, parameters.interface)
        .rotate(Axis.Z, 180)
        .moved(Location((width, 0, 0)))
    )
    joins = []
    for cell in range(cells):
        x = (cell + 0.5) * parameters.interface.pitch
        tool = tile_join_tool(parameters.interface, male=True).moved(Location((x, 0, 0)))
        outside = Solid.make_box(
            width,
            parameters.interface.male_join_depth,
            parameters.interface.height,
        )
        tab = Part(tool.intersect(outside).solids())
        part = part.fuse(tab)
        joins.append((x, 0.0))
    hole_centers = _south_boundary_hole_centers(cells, parameters)
    part = part.cut(*(_hole_cutter(x, y, parameters.interface) for x, y in hole_centers))
    result = part.clean()
    if not result.is_valid or len(result.solids()) != 1 or result.volume <= 0:
        raise ValueError("Zeekr south ramp segment is invalid or disconnected")
    return result, x_runs


def _side_outline_x(
    side: str,
    vehicle_y: float,
    parameters: RearReviewParameters,
) -> float:
    width = (
        right_review_outline_x_mm(
            vehicle_y,
            outline_inset_mm=parameters.outline_inset_mm,
            traced_north_corner_radius_mm=parameters.traced_north_corner_radius_mm,
            pen_offset_mm=parameters.pen_offset_mm,
            east_edge_x_mm=parameters.east_edge_x_mm,
            se_along_edge_shift_mm=parameters.se_along_edge_shift_mm,
        )
        - TEST_FIELD_WIDTH_MM / 2
    )
    return -width if side == "west" else width


def _candidate_side_socket_centers(
    side: str,
    segment: str,
    parameters: RearReviewParameters,
) -> tuple[tuple[float, float, float], ...]:
    x = -parameters.interface.pitch / 2 if side == "west" else parameters.interface.pitch / 2
    all_centers = tuple(
        (x, parameters.field_assembly_y_min_mm + (row + 0.5) * parameters.interface.pitch, 0)
        for row in range(4)
    )
    split = parameters.assembly_y_mm(parameters.side_split_y_mm)
    return tuple(
        center
        for center in all_centers
        if (center[1] < split if segment == "south" else center[1] > split)
    )


def _side_boundary_hole_centers(
    side: str,
    segment: str,
    parameters: RearReviewParameters,
) -> tuple[tuple[float, float], ...]:
    tile = Tile(
        4,
        4,
        parameters.interface,
        hole_diameter=10,
        hole_scope="full",
    )
    boundary_x = 0 if side == "west" else tile.body_size[0]
    local_y = tuple(
        placement.y
        for placement in hole_placements(tile)
        if placement.accepted and abs(placement.x - boundary_x) < 1e-8
    )
    if local_y != tuple(float(y) for y in range(0, 241, 30)):
        raise ValueError("accepted 4x4 side-boundary hole pattern changed")
    centers = tuple((0.0, parameters.field_assembly_y_min_mm + y) for y in local_y)
    split = parameters.assembly_y_mm(parameters.side_split_y_mm)
    return tuple(
        center
        for center in centers
        if (center[1] <= split if segment == "south" else center[1] >= split)
    )


def _side_interior_hole_candidates(
    side: str,
    segment: str,
    parameters: RearReviewParameters,
) -> tuple[dict, ...]:
    virtual_tile = Tile(
        1,
        4,
        parameters.interface,
        hole_diameter=10,
        hole_scope="full",
    )
    sign = -1 if side == "west" else 1
    split = parameters.assembly_y_mm(parameters.side_split_y_mm)
    candidates = []
    for placement in hole_placements(virtual_tile):
        if not placement.accepted or placement.x not in (
            parameters.interface.pitch / 2,
            parameters.interface.pitch,
        ):
            continue
        assembly_y = parameters.field_assembly_y_min_mm + placement.y
        if not (assembly_y <= split if segment == "south" else assembly_y >= split):
            continue
        candidates.append(
            {
                "center": (sign * placement.x, assembly_y),
                "column": (
                    "socket-column"
                    if placement.x == parameters.interface.pitch / 2
                    else "outer-edge-column"
                ),
            }
        )
    return tuple(candidates)


def _north_boundary_vehicle_y_mm(
    x_mm: float,
    parameters: RearReviewParameters,
) -> float:
    t = abs(x_mm)
    straight_x = parameters.east_edge_x_mm - parameters.outline_inset_mm
    if t <= 507.5:
        return parameters.north_limit_y_mm
    geometry = _north_corner_geometry(parameters)
    tangent_x = geometry["north_tangent"][0]
    if t <= tangent_x:
        u = straight_x - t
        return (
            parameters.north_limit_y_mm
            + _north_edge_s_slope(
                u,
                parameters.pen_offset_mm,
            )[0]
        )
    if t <= straight_x:
        center_x, center_y = geometry["center"]
        return center_y - sqrt(max(0.0, parameters.north_corner_radius_mm**2 - (t - center_x) ** 2))
    raise ValueError("north boundary sample lies outside the selected outline")


def _side_hole_transition_webs(
    side: str,
    center: tuple[float, float],
    parameters: RearReviewParameters,
) -> tuple[float, float, float]:
    local_x, assembly_y = center
    sign = -1 if side == "west" else 1
    frame_x = parameters.field_x_min_mm if side == "west" else -parameters.field_x_min_mm
    contour_web = float("inf")
    south_web = float("inf")
    north_web = float("inf")
    for offset in np.linspace(-5, 5, 101):
        chord = sqrt(max(0.0, 25 - offset**2))
        sample_y = assembly_y + offset
        vehicle_y = parameters.rear_limit_y_mm - sample_y
        contour_x = _side_outline_x(side, vehicle_y, parameters)
        contour_web = min(
            contour_web,
            sign * (contour_x - local_x) - chord,
        )
        global_x = frame_x + local_x + offset
        if abs(global_x) > parameters.east_edge_x_mm - parameters.outline_inset_mm:
            outside = parameters.east_edge_x_mm - parameters.outline_inset_mm - abs(global_x)
            south_web = min(south_web, outside)
            north_web = min(north_web, outside)
            continue
        south_web = min(
            south_web,
            assembly_y - chord - parameters.assembly_y_mm(tail_y_mm(global_x, parameters)),
        )
        north_web = min(
            north_web,
            parameters.assembly_y_mm(_north_boundary_vehicle_y_mm(global_x, parameters))
            - assembly_y
            - chord,
        )
    return contour_web, south_web, north_web


def _side_interior_hole_feasibility(
    side: str,
    segment: str,
    parameters: RearReviewParameters,
) -> list[dict]:
    interface = parameters.interface
    tile = Tile(1, 4, interface, hole_diameter=10, hole_scope="full")
    protected_socket = x_profile(
        interface,
        offset=3 + tile.minimum_web + interface.fit_offset,
    )
    conceptual_sockets = tuple(
        (
            -interface.pitch / 2 if side == "west" else interface.pitch / 2,
            parameters.field_assembly_y_min_mm + (row + 0.5) * interface.pitch,
        )
        for row in range(4)
    )
    boundary_holes = tuple(
        (0.0, parameters.field_assembly_y_min_mm + offset)
        for offset in range(0, int(TEST_FIELD_DEPTH_MM) + 1, 30)
    )
    join_tools = []
    for center_y in (
        parameters.field_assembly_y_min_mm + interface.pitch / 2,
        parameters.field_assembly_y_min_mm + 3 * interface.pitch / 2,
        parameters.field_assembly_y_min_mm + 5 * interface.pitch / 2,
        parameters.field_assembly_y_min_mm + 7 * interface.pitch / 2,
    ):
        join_tools.append(
            tile_join_tool(interface, male=side == "west")
            .rotate(Axis.Z, -90)
            .moved(Location((0, center_y, 0)))
        )
    reports = []
    for candidate in _side_interior_hole_candidates(side, segment, parameters):
        center = candidate["center"]
        cutter = _hole_cutter(*center, interface)
        socket_keepout = min(
            protected_socket.distance_to(Vector(center[0] - socket_x, center[1] - socket_y, 0)) - 5
            for socket_x, socket_y in conceptual_sockets
        )
        adjacent_boundary = min(
            sqrt((center[0] - hole_x) ** 2 + (center[1] - hole_y) ** 2) - 10
            for hole_x, hole_y in boundary_holes
        )
        join_web = min(cutter.distance_to(join) for join in join_tools)
        contour_web, south_web, north_web = _side_hole_transition_webs(
            side,
            center,
            parameters,
        )
        tile_boundary_web = abs(center[0]) - 5
        report = {
            **candidate,
            "assembly_center": (
                (parameters.field_x_min_mm if side == "west" else -parameters.field_x_min_mm)
                + center[0],
                center[1],
                0.0,
            ),
            "minimum_socket_keepout_web_mm": socket_keepout,
            "minimum_contour_web_mm": contour_web,
            "minimum_join_web_mm": join_web,
            "minimum_adjacent_boundary_hole_web_mm": adjacent_boundary,
            "minimum_tile_boundary_web_mm": tile_boundary_web,
            "minimum_south_transition_web_mm": south_web,
            "minimum_north_transition_web_mm": north_web,
            "ownership": (
                "split-seam half"
                if abs(center[1] - parameters.assembly_y_mm(parameters.side_split_y_mm)) < 1e-7
                else "full hole in one cap segment"
            ),
        }
        limiting = min(
            socket_keepout,
            contour_web,
            join_web,
            adjacent_boundary,
            tile_boundary_web,
            south_web,
            north_web,
        )
        report["minimum_reported_material_mm"] = limiting
        report["meets_minimum_web"] = (
            candidate["column"] == "socket-column"
            and limiting >= PERIMETER_HOLE_MINIMUM_WEB_MM - 1e-7
        )
        report["rejection_reason"] = (
            None
            if report["meets_minimum_web"]
            else (
                "outer column breaks through corrected contour"
                if contour_web < PERIMETER_HOLE_MINIMUM_WEB_MM
                else "perimeter-hole clearance below minimum"
            )
        )
        reports.append(report)
    return reports


def _side_outboard_edge(side: str, parameters: RearReviewParameters) -> Edge:
    sign = -1 if side == "west" else 1
    points = [
        Vector(
            sign
            * (
                right_review_outline_x_mm(
                    parameters.rear_limit_y_mm - assembly_y,
                    outline_inset_mm=parameters.outline_inset_mm,
                    traced_north_corner_radius_mm=parameters.traced_north_corner_radius_mm,
                    pen_offset_mm=parameters.pen_offset_mm,
                    east_edge_x_mm=parameters.east_edge_x_mm,
                    se_along_edge_shift_mm=parameters.se_along_edge_shift_mm,
                )
                - TEST_FIELD_WIDTH_MM / 2
            ),
            assembly_y,
            parameters.interface.height,
        )
        for assembly_y in np.linspace(
            parameters.field_assembly_y_min_mm,
            parameters.assembly_y_mm(parameters.north_limit_y_mm),
            271,
        )
    ]
    return Edge.make_spline(points)


def _side_socket_feasibility(
    side: str,
    segment: str,
    parameters: RearReviewParameters,
) -> list[dict]:
    interface = parameters.interface
    outboard = _side_outboard_edge(side, parameters)
    split = parameters.assembly_y_mm(parameters.side_split_y_mm)
    seam = Edge.make_line(
        (-100, split, interface.height),
        (100, split, interface.height),
    )
    hole_centers = (
        *_side_boundary_hole_centers(side, segment, parameters),
        *(
            report["center"]
            for report in _side_interior_hole_feasibility(
                side,
                segment,
                parameters,
            )
            if report["meets_minimum_web"]
        ),
    )
    reports = []
    for center in _candidate_side_socket_centers(side, segment, parameters):
        cutter = socket_entry_tool(interface).moved(Location(center))
        top = section(cutter, section_by=Plane.XY.offset(interface.height))
        hole_clearance = min(
            cutter.distance_to(
                Solid.make_cylinder(5, interface.height + 2).moved(Location((hole_x, hole_y, -1)))
            )
            for hole_x, hole_y in hole_centers
        )
        if side == "east":
            join = (
                tile_join_tool(interface, male=False)
                .rotate(Axis.Z, -90)
                .moved(Location((0, center[1], 0)))
            )
        else:
            join_intersection = (
                tile_join_tool(interface, male=True)
                .rotate(Axis.Z, -90)
                .moved(Location((0, center[1], 0)))
                .intersect(
                    Solid.make_box(
                        interface.male_join_depth,
                        interface.pitch,
                        interface.height,
                    ).moved(
                        Location(
                            (
                                0,
                                center[1] - interface.pitch / 2,
                                0,
                            )
                        )
                    )
                )
            )
            join = Part(join_intersection.solids())
        entry_half = socket_entry_tool(interface).bounding_box().max.X
        global_center_x = (
            parameters.field_x_min_mm + center[0]
            if side == "west"
            else -parameters.field_x_min_mm + center[0]
        )
        sample_x = np.linspace(
            global_center_x - entry_half,
            global_center_x + entry_half,
            201,
        )
        south_edge_y = max(
            parameters.assembly_y_mm(tail_y_mm(float(x), parameters)) for x in sample_x
        )
        south_clearance = center[1] - entry_half - south_edge_y
        north_clearance = (
            parameters.assembly_y_mm(parameters.north_limit_y_mm) - center[1] - entry_half
        )
        report = {
            "center": center,
            "minimum_outboard_contour_mm": top.distance_to(outboard),
            "minimum_tile_edge_join_mm": cutter.distance_to(join),
            "minimum_10mm_boundary_hole_mm": hole_clearance,
            "minimum_cap_split_seam_mm": top.distance_to(seam),
            "minimum_south_corner_transition_mm": south_clearance,
            "minimum_north_corner_transition_mm": north_clearance,
            "socket_entry_roundover_mm": interface.socket_entry_radius,
            "full_depth_mm": interface.height,
            "additional_cap_top_bottom_fillet_mm": 0,
        }
        limiting = min(
            report["minimum_outboard_contour_mm"],
            report["minimum_tile_edge_join_mm"],
            report["minimum_10mm_boundary_hole_mm"],
            report["minimum_cap_split_seam_mm"],
            report["minimum_south_corner_transition_mm"],
            report["minimum_north_corner_transition_mm"],
        )
        report["minimum_reported_material_mm"] = limiting
        report["meets_minimum_web"] = limiting >= SIDE_SOCKET_MINIMUM_WEB_MM - 1e-7
        reports.append(report)
    return reports


def _side_socket_centers(
    side: str,
    segment: str,
    parameters: RearReviewParameters,
) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        report["center"]
        for report in _side_socket_feasibility(side, segment, parameters)
        if report["meets_minimum_web"]
    )


def _side_body_face(
    side: str,
    local_y0: float,
    local_y1: float,
    parameters: RearReviewParameters,
) -> Face:
    count = max(2, ceil((local_y1 - local_y0) / 10))
    north_corner = abs(local_y0 - (parameters.north_limit_y_mm - parameters.field_y_min_mm)) < 1e-7
    corner_geometry = _north_corner_geometry(parameters) if north_corner else None
    outer_end_y = (
        corner_geometry["east_tangent"][1] - parameters.field_y_min_mm if north_corner else local_y0
    )
    inner_u = parameters.east_edge_x_mm - parameters.outline_inset_mm - TEST_FIELD_WIDTH_MM / 2
    inner_north_y = (
        parameters.north_limit_y_mm
        + _north_edge_s_slope(inner_u, parameters.pen_offset_mm)[0]
        - parameters.field_y_min_mm
        if north_corner
        else local_y0
    )
    ys = np.linspace(local_y1, outer_end_y, count + 1)
    outer = [
        Vector(
            _side_outline_x(side, parameters.field_y_min_mm + y, parameters),
            y,
            0,
        )
        for y in ys
    ]
    edges = [
        Edge.make_line((0, inner_north_y, 0), (0, local_y1, 0)),
        Edge.make_line((0, local_y1, 0), outer[0]),
        Edge.make_spline(outer),
    ]
    if north_corner:
        sign = -1 if side == "west" else 1
        center_global_x, center_global_y = corner_geometry["center"]
        tangent_global_x, tangent_global_y = corner_geometry["north_tangent"]
        center_x = sign * (center_global_x - TEST_FIELD_WIDTH_MM / 2)
        center_y = center_global_y - parameters.field_y_min_mm
        north_tangent = Vector(
            sign * (tangent_global_x - TEST_FIELD_WIDTH_MM / 2),
            tangent_global_y - parameters.field_y_min_mm,
            0,
        )
        radial_start = outer[-1] - Vector(center_x, center_y, 0)
        radial_end = north_tangent - Vector(center_x, center_y, 0)
        radial_sum = radial_start + radial_end
        radial_length = sqrt(radial_sum.X**2 + radial_sum.Y**2)
        middle = Vector(
            center_x + parameters.north_corner_radius_mm * radial_sum.X / radial_length,
            center_y + parameters.north_corner_radius_mm * radial_sum.Y / radial_length,
            0,
        )
        line_end_u = 20.0
        line_end = Vector(
            sign
            * (
                parameters.east_edge_x_mm
                - parameters.outline_inset_mm
                - line_end_u
                - TEST_FIELD_WIDTH_MM / 2
            ),
            parameters.north_limit_y_mm
            + _north_edge_s_slope(line_end_u, parameters.pen_offset_mm)[0]
            - parameters.field_y_min_mm,
            0,
        )
        north_u = np.linspace(line_end_u, inner_u, 20)
        north_points = [
            Vector(
                sign
                * (
                    parameters.east_edge_x_mm
                    - parameters.outline_inset_mm
                    - u
                    - TEST_FIELD_WIDTH_MM / 2
                ),
                parameters.north_limit_y_mm
                + _north_edge_s_slope(float(u), parameters.pen_offset_mm)[0]
                - parameters.field_y_min_mm,
                0,
            )
            for u in north_u
        ]
        start_tangent = Vector(
            -sign,
            _north_edge_s_slope(line_end_u, parameters.pen_offset_mm)[1],
            0,
        )
        end_tangent = Vector(
            -sign,
            _north_edge_s_slope(inner_u, parameters.pen_offset_mm)[1],
            0,
        )
        edges.extend(
            (
                Edge.make_three_point_arc(outer[-1], middle, north_tangent),
                Edge.make_line(north_tangent, line_end),
                Edge.make_spline(
                    north_points,
                    tangents=[start_tangent, end_tangent],
                ),
            )
        )
    else:
        edges.append(Edge.make_line(outer[-1], (0, local_y0, 0)))
    return Face(Wire(edges))


def _side_corner_ramp(
    side: str,
    parameters: RearReviewParameters,
) -> tuple[Part, list[tuple[float, float]]]:
    y_offset = TEST_FIELD_DEPTH_MM
    outer = _side_outline_x(side, parameters.field_y_max_mm, parameters)
    low, high = sorted((outer, 0.0))
    count = max(2, ceil((high - low) / 5))
    xs = np.linspace(low, high, count + 1)
    x_runs = []
    for local_x in xs:
        vehicle_x = (
            parameters.field_x_min_mm + local_x
            if side == "west"
            else -parameters.field_x_min_mm + local_x
        )
        run = tail_y_mm(vehicle_x, parameters) - parameters.field_y_max_mm
        x_runs.append((float(local_x), max(CORNER_TIP_REGULARIZATION_MM, run)))
    return (
        _variable_ramp(x_runs, parameters.interface, y_offset=y_offset),
        x_runs,
    )


def _side_shape(
    side: str,
    segment: str,
    parameters: RearReviewParameters,
) -> tuple[
    Part,
    list[dict],
    list[tuple[float, float]],
    tuple[tuple[float, float, float], ...],
    tuple[tuple[float, float], ...],
    list[dict],
]:
    split = parameters.side_split_y_mm - parameters.field_y_min_mm
    if segment == "north":
        local_y0 = parameters.north_limit_y_mm - parameters.field_y_min_mm
        local_y1 = split
        centers = (30.0, 90.0)
    elif segment == "south":
        local_y0 = split
        local_y1 = TEST_FIELD_DEPTH_MM
        centers = (150.0, 210.0)
    else:
        raise ValueError("side segment must be north or south")

    body = Part(
        Solid.extrude(
            _side_body_face(side, local_y0, local_y1, parameters),
            (0, 0, parameters.interface.height),
        ).wrapped
    )
    corner_samples: list[tuple[float, float]] = []
    if segment == "south":
        corner, corner_samples = _side_corner_ramp(side, parameters)
        body = body.fuse(corner).clean()

    male = side == "west"
    joins = []
    for y in centers:
        tool = tile_join_tool(parameters.interface, male=male).rotate(Axis.Z, -90)
        tool = tool.moved(Location((0, y, 0)))
        if male:
            outside = Solid.make_box(
                parameters.interface.male_join_depth,
                parameters.interface.pitch,
                parameters.interface.height,
            ).moved(Location((0, y - parameters.interface.pitch / 2, 0)))
            tab = Part(tool.intersect(outside).solids())
            body = body.fuse(tab)
        else:
            body = body.cut(tool)
        joins.append(
            {
                "position": (0.0, y, 0.0),
                "angle": -90,
                "sex": "male" if male else "female",
                "depth": (
                    parameters.interface.male_join_depth
                    if male
                    else parameters.interface.female_join_depth
                ),
                "height": (
                    parameters.interface.male_height
                    if male
                    else parameters.interface.female_opening_height
                ),
                "interface": "tile-dovetail",
                "joint_style": parameters.interface.joint_style,
                "open_through_top": False,
            }
        )
    result = body.clean()
    result = result.mirror(Plane.XZ).moved(Location((0, parameters.field_assembly_y_max_mm, 0)))
    joins = [
        {
            **join,
            "position": (
                join["position"][0],
                parameters.field_assembly_y_max_mm - join["position"][1],
                join["position"][2],
            ),
        }
        for join in joins
    ]
    socket_centers = _side_socket_centers(side, segment, parameters)
    hole_centers = _side_boundary_hole_centers(side, segment, parameters)
    interior_hole_reports = _side_interior_hole_feasibility(
        side,
        segment,
        parameters,
    )
    interior_hole_centers = tuple(
        report["center"] for report in interior_hole_reports if report["meets_minimum_web"]
    )
    result = result.cut(
        *(
            socket_entry_tool(parameters.interface).moved(Location(center))
            for center in socket_centers
        )
    )
    result = result.cut(*(_hole_cutter(x, y, parameters.interface) for x, y in hole_centers))
    result = result.cut(
        *(_hole_cutter(x, y, parameters.interface) for x, y in interior_hole_centers)
    ).clean()
    if not result.is_valid or len(result.solids()) != 1 or result.volume <= 0:
        raise ValueError(f"Zeekr {side} {segment} side segment is invalid or disconnected")
    return (
        result,
        joins,
        corner_samples,
        socket_centers,
        hole_centers,
        interior_hole_reports,
    )


def _tile_datums(tile: Tile) -> dict:
    p = tile.interface.pitch
    width, depth = tile.body_size
    joins = []
    for row in range(tile.ny):
        joins.extend(
            (
                {
                    "position": (0, (row + 0.5) * p, 0),
                    "angle": -90,
                    "sex": "female",
                    "side": "west",
                },
                {
                    "position": (width, (row + 0.5) * p, 0),
                    "angle": -90,
                    "sex": "male",
                    "side": "east",
                },
            )
        )
    for column in range(tile.nx):
        joins.extend(
            (
                {
                    "position": ((column + 0.5) * p, 0, 0),
                    "angle": 0,
                    "sex": "female",
                    "side": "south",
                },
                {
                    "position": ((column + 0.5) * p, depth, 0),
                    "angle": 0,
                    "sex": "male",
                    "side": "north",
                },
            )
        )
    for join in joins:
        male = join["sex"] == "male"
        join.update(
            depth=(tile.interface.male_join_depth if male else tile.interface.female_join_depth),
            height=(tile.interface.male_height if male else tile.interface.female_opening_height),
            interface="tile-dovetail",
            joint_style=tile.interface.joint_style,
            open_through_top=False,
        )
    return {"underside_z": 0, "top_z": tile.interface.height, "joins": joins}


def _named_tile_design(
    cells: int,
    index: int,
    x: float,
    parameters: RearReviewParameters,
) -> Design:
    spec = Tile(cells, 4, parameters.interface, hole_diameter=10, hole_scope="full")
    source = tile_design(spec)
    return replace(
        source,
        name=f"zeekr_rear_test_tile_{index + 1}_{cells}x4",
        display_name=f"Rear test tile {index + 1} - {cells}x4",
        assembly_frames=[(x, parameters.field_assembly_y_min_mm, 0)],
        mating_datums=_tile_datums(spec),
    )


def _north_design(
    cells: int,
    index: int,
    x: float,
    parameters: RearReviewParameters,
) -> Design:
    spec = tile_matched_perimeter(
        "edge-y",
        nx=cells,
        outward=30,
        interface=parameters.interface,
        hole_diameter=10,
        hole_scope="full",
    )
    shape = make_accessory(spec)
    source_datums = accessory_datums(spec)
    datums = {
        **source_datums,
        "vehicle_boundary": "NORTH",
        "mating_tile_edge": "canonical north male",
    }
    return Design(
        f"zeekr_rear_north_{index + 1}_{cells}cell",
        shape,
        {
            **asdict(spec),
            "review_role": "vehicle-north finishing band",
            "connector_sex": "female",
            "vehicle_boundary": "NORTH",
            "tile_edge_interface_present": True,
            "x_attachment_interface_present": False,
        },
        display_name=f"North female finishing band {index + 1} - {cells} cells",
        assembly_frames=[(x, parameters.field_assembly_y_max_mm, 0)],
        holes=[
            {
                "x": hole_x,
                "y": hole_y,
                "accepted": True,
                "reason": "matching accepted full-pattern tile boundary site",
            }
            for hole_x, hole_y in datums["edge_hole_centers"]
        ],
        mating_datums=datums,
    )


def _south_design(
    cells: int,
    index: int,
    x: float,
    parameters: RearReviewParameters,
) -> Design:
    shape, samples = _south_ramp_shape(x, cells, parameters)
    interface = parameters.interface
    joins = [
        {
            "position": ((cell + 0.5) * interface.pitch, 0, 0),
            "angle": 0,
            "sex": "male",
            "depth": interface.male_join_depth,
            "height": interface.male_height,
            "interface": "tile-dovetail",
            "joint_style": interface.joint_style,
            "open_through_top": False,
        }
        for cell in range(cells)
    ]
    runs = [run for _, run in samples]
    hole_reports = _south_hole_feasibility(x, cells, parameters)
    hole_centers = tuple(report["center"] for report in hole_reports)
    return Design(
        f"zeekr_rear_south_ramp_{index + 1}_{cells}cell",
        shape,
        {
            "family": "zeekr-rear-contour-ramp",
            "width_cells": cells,
            "interface": asdict(interface),
            "review_role": "vehicle-south contour ramp",
            "connector_sex": "male",
            "vehicle_boundary": "SOUTH",
            "outline_inset_mm": parameters.outline_inset_mm,
            "vehicle_x_span_mm": (x, x + cells * interface.pitch),
            "finished_run_range_mm": (min(runs), max(runs)),
            "south_contour_source": "merged three-trace taper plus C1 concave quartic crown",
            "south_contour_bow_mm": (
                parameters.centre_depth_mm
                - _merged_taper_stations(
                    parameters.pen_offset_mm,
                    parameters.se_along_edge_shift_mm,
                )[-1][1]
            ),
            "tile_edge_interface_present": True,
            "x_attachment_interface_present": False,
            "profile": {
                "carrier_run_mm": RAMP_CARRIER_RUN_MM,
                "shelf_radius_mm": RAMP_SHELF_RADIUS_MM,
                "nose_radius_mm": RAMP_FREE_EDGE_RADIUS_MM,
                "segment_caps": "flat butt seams; no seam connectors",
            },
        },
        display_name=f"South male contour ramp {index + 1} - {cells} cells",
        assembly_frames=[(x, parameters.field_assembly_y_min_mm, 0)],
        holes=[
            {
                "x": hole_x,
                "y": hole_y,
                "accepted": True,
                "reason": (
                    "accepted adjacent tile south-boundary site; ramp owns its outward "
                    "half and only its quarter at module seams"
                ),
            }
            for hole_x, hole_y in hole_centers
        ],
        mating_datums={
            "underside_z": 0,
            "top_z": interface.height,
            "ramp_direction": "-assembly Y toward tailgate/SOUTH",
            "mating_tile_edge": "canonical south female",
            "joins": joins,
            "completed_10mm_south_boundary_hole_centers": hole_centers,
            "south_boundary_hole_feasibility": hole_reports,
            "nose_samples_local_x_run_mm": samples,
            "global_contour_samples_x_y_slope": [
                (sample_x, sample_y, sample_slope)
                for sample_x, sample_y, sample_slope in SOUTH_CONTOUR_CAD_SAMPLES
                if x - 1e-7 <= sample_x <= x + cells * interface.pitch + 1e-7
            ],
        },
    )


def _side_design(
    side: str,
    segment: str,
    parameters: RearReviewParameters,
) -> Design:
    (
        shape,
        joins,
        corner_samples,
        socket_centers,
        hole_centers,
        interior_hole_reports,
    ) = _side_shape(side, segment, parameters)
    frame_x = parameters.field_x_min_mm if side == "west" else -parameters.field_x_min_mm
    male = side == "west"
    candidate_feasibility = _side_socket_feasibility(side, segment, parameters)
    feasibility = [report for report in candidate_feasibility if report["meets_minimum_web"]]
    return Design(
        f"zeekr_rear_{side}_{segment}_cap",
        shape,
        {
            "family": "zeekr-rear-contour-side",
            "side": side,
            "segment": segment,
            "interface": asdict(parameters.interface),
            "review_role": f"vehicle-{side} contour cap",
            "outline_inset_mm": parameters.outline_inset_mm,
            "aggregate_mating_run_mm": TEST_FIELD_DEPTH_MM,
            "connector_sex": "male" if male else "female",
            "standalone_local_x": (
                "contour body is at negative X; male tabs project +X into the tile"
                if side == "west"
                else "contour body is at positive X; female pockets open from X=0 into the cap"
            ),
            "tile_edge_interface_present": True,
            "x_attachment_interface_present": True,
            "complete_edge_holes": True,
            "edge_hole_diameter": 10,
            "accessory_socket_count": len(socket_centers),
            "socket_minimum_web_mm": SIDE_SOCKET_MINIMUM_WEB_MM,
            "corner_ownership": ("north" if segment == "north" else "south contour/ramp"),
            "segmentation": (
                "two printable cap segments with one flat butt seam; "
                "no separate corner part and no seam connector"
            ),
            "corner_tip_regularization_mm": (
                CORNER_TIP_REGULARIZATION_MM if segment == "south" else None
            ),
        },
        display_name=(
            f"{side.title()} {'male' if male else 'female'} contour cap - {segment} segment"
        ),
        assembly_frames=[(frame_x, 0, 0)],
        holes=[
            {
                "x": x,
                "y": y,
                "accepted": True,
                "reason": (
                    "accepted full-pattern 4x4 side-boundary site; this cap owns only "
                    "the material on its side of the assembly boundaries"
                ),
            }
            for x, y in hole_centers
        ]
        + [
            {
                "x": report["center"][0],
                "y": report["center"][1],
                "accepted": report["meets_minimum_web"],
                "reason": (
                    "accepted virtual-tile socket-column site"
                    if report["meets_minimum_web"]
                    else report["rejection_reason"]
                ),
            }
            for report in interior_hole_reports
        ],
        mating_datums={
            "underside_z": 0,
            "top_z": parameters.interface.height,
            "mating_tile_edge": (
                "tile canonical west female" if side == "west" else "tile canonical east male"
            ),
            "joins": joins,
            "accessory_socket_centers": tuple(
                (x, y, parameters.interface.height) for x, y, _ in socket_centers
            ),
            "socket_cutter_origins": socket_centers,
            "accessory_socket_interface": (
                "exact ordinary tile X female socket with 3 mm top entry roundover"
            ),
            "completed_10mm_boundary_hole_centers": hole_centers,
            "completed_10mm_interior_hole_centers": tuple(
                report["center"] for report in interior_hole_reports if report["meets_minimum_web"]
            ),
            "interior_hole_feasibility": tuple(
                report for report in interior_hole_reports if report["meets_minimum_web"]
            ),
            "rejected_interior_hole_candidates": tuple(
                report for report in interior_hole_reports if not report["meets_minimum_web"]
            ),
            "socket_feasibility": feasibility,
            "rejected_socket_candidates": [
                report for report in candidate_feasibility if not report["meets_minimum_web"]
            ],
            "corner_ramp_samples_local_x_run_mm": corner_samples,
        },
    )


def inspect_scan_obj(path: Path) -> dict:
    """Verify basic OBJ/PCA facts without importing or redistributing the scan."""
    path = path.resolve(strict=True)
    vertices = []
    referenced = set()
    counts = {"texture_coordinates": 0, "normals": 0, "faces": 0}
    digest = sha256()
    with path.open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            if raw.startswith(b"v "):
                values = raw.split()
                vertices.append(tuple(float(value) for value in values[1:4]))
            elif raw.startswith(b"vt "):
                counts["texture_coordinates"] += 1
            elif raw.startswith(b"vn "):
                counts["normals"] += 1
            elif raw.startswith(b"f "):
                counts["faces"] += 1
                for token in raw.split()[1:]:
                    index = int(token.split(b"/", 1)[0])
                    referenced.add(index - 1 if index > 0 else len(vertices) + index)
    points = np.asarray([vertices[index] for index in sorted(referenced)])
    centered = points - points.mean(axis=0)
    values, vectors = np.linalg.eigh(np.cov(centered, rowvar=False))
    projections = centered @ vectors[:, np.argsort(values)[::-1]]
    extents = np.ptp(projections, axis=0)
    expected = np.asarray(SCAN_ALIGNMENT["pca"]["extents_obj"])
    return {
        "source_name": path.name,
        "sha256": digest.hexdigest(),
        "literal_counts": {
            "vertices": len(vertices),
            **counts,
        },
        "referenced_vertices": len(referenced),
        "referenced_bounds_obj": {
            "minimum": points.min(axis=0).tolist(),
            "maximum": points.max(axis=0).tolist(),
        },
        "pca_extents_obj": extents.tolist(),
        "matches_review_source": bool(
            len(vertices) == SCAN_ALIGNMENT["literal_counts"]["vertices"]
            and counts["faces"] == SCAN_ALIGNMENT["literal_counts"]["triangular_faces"]
            and len(referenced) == SCAN_ALIGNMENT["effective_surface"]["referenced_vertices"]
            and np.allclose(extents, expected, atol=5e-6)
        ),
    }


def rear_panel_job(
    build: BuildVolume,
    *,
    interface: Interface = Interface(),
    placement_build: BuildVolume | None = None,
    part_gap: float = H2D_REVIEW_GAP_MM,
) -> Job:
    """Build only the nine measured contour pieces for normal CLI export."""
    positive("rear-panel packing gap", part_gap)
    parameters = RearReviewParameters(
        interface=interface,
        packing_gap_mm=part_gap,
    )
    envelope = placement_build or build
    side_designs = [
        _side_design(side, segment, parameters)
        for side in ("west", "east")
        for segment in ("north", "south")
    ]
    south_designs = []
    x = parameters.field_x_min_mm
    for index, cells in enumerate(TEST_TILE_MODULES):
        south_designs.append(_south_design(cells, index, x, parameters))
        x += cells * parameters.interface.pitch
    designs = side_designs + south_designs
    for design in designs:
        if envelope.placement(design.size) is None or build.placement(design.size) is None:
            raise ValueError(
                f"{design.name}: actual bounds {design.size} exceed the configured build envelope"
            )

    placements = []
    plate_names = {}
    plate_offset = 0
    for group, members in (
        ("Contour side caps", side_designs),
        ("South contour ramps", south_designs),
    ):
        packed = pack_sizes([design.size for design in members], envelope, gap=part_gap)
        count = max(placement.plate for placement in packed) + 1
        placements.extend(
            PrintPlacement(
                placement.plate + plate_offset,
                placement.x,
                placement.y,
                placement.rotation,
            )
            for placement in packed
        )
        for plate in range(count):
            title = f"Zeekr 7X rear panel - {group}"
            plate_names[plate_offset + plate] = title if count == 1 else f"{title} {plate + 1}"
        plate_offset += count

    return Job(
        designs,
        build,
        "zeekr-7x-rear-panel",
        footprint=(2 * parameters.east_edge_x_mm, parameters.centre_depth_mm),
        part_gap=part_gap,
        print_placements=placements,
        plate_names=plate_names,
        placement_policy={
            "collection": "zeekr-7x-rear-panel",
            "minimum_model_gap_mm": part_gap,
            "grouped_by_family": True,
            "outline_parameters_mm": {
                "pen_offset": parameters.pen_offset_mm,
                "east_edge_x": parameters.east_edge_x_mm,
                "centre_depth": parameters.centre_depth_mm,
                "traced_corner_radius": parameters.traced_north_corner_radius_mm,
                "true_corner_radius": parameters.north_corner_radius_mm,
            },
        },
        manifest_metadata={
            "scope": "Zeekr 7X rear lift-out-panel contour pieces only",
            "inventory": {
                "side_caps": 4,
                "south_contour_ramps": 5,
                "standard_tiles_and_north_edges_included": False,
            },
            "assembly": {
                "tile_modules_west_to_east_cells": TEST_TILE_MODULES,
                "tile_field_cells": (18, 4),
                "tile_male_directions": "NORTH and EAST",
                "west_cap": "male",
                "east_cap": "female",
                "south_ramps": "male",
            },
            "outline": {
                "source": (
                    "tape measurements and pen traces corrected by the measured "
                    "5 mm pen-barrel offset"
                ),
                "pen_offset_mm": parameters.pen_offset_mm,
                "east_edge_x_mm": parameters.east_edge_x_mm,
                "centre_depth_mm": parameters.centre_depth_mm,
                "traced_north_corner_radius_mm": (parameters.traced_north_corner_radius_mm),
                "true_north_corner_radius_mm": parameters.north_corner_radius_mm,
                "physical_test": (
                    "one user test fit of the corrected outline was judged good enough for now"
                ),
            },
            "limitations": (
                "No general vehicle-fit, strength, flatness or service guarantee. "
                "Print the documented standard tiles and 30 mm north edges separately."
            ),
        },
    )


def rear_review_job(
    parameters: RearReviewParameters = RearReviewParameters(),
    *,
    scan_inspection: dict | None = None,
) -> Job:
    """Create the measured rear-panel review job and actual H2D packing."""
    designs = []
    x = parameters.field_x_min_mm
    for index, cells in enumerate(TEST_TILE_MODULES):
        designs.append(_named_tile_design(cells, index, x, parameters))
        x += cells * parameters.interface.pitch
    x = parameters.field_x_min_mm
    for index, cells in enumerate(TEST_TILE_MODULES):
        designs.append(_north_design(cells, index, x, parameters))
        x += cells * parameters.interface.pitch
    x = parameters.field_x_min_mm
    for index, cells in enumerate(TEST_TILE_MODULES):
        designs.append(_south_design(cells, index, x, parameters))
        x += cells * parameters.interface.pitch
    designs.extend(
        _side_design(side, segment, parameters)
        for side in ("west", "east")
        for segment in ("north", "south")
    )
    side_designs = [
        design for design in designs if design.parameters.get("family") == "zeekr-rear-contour-side"
    ]
    socket_reports = [
        report for design in side_designs for report in design.mating_datums["socket_feasibility"]
    ]
    rejected_socket_reports = [
        report
        for design in side_designs
        for report in design.mating_datums["rejected_socket_candidates"]
    ]
    south_designs = [
        design for design in designs if design.parameters.get("family") == "zeekr-rear-contour-ramp"
    ]
    south_hole_reports = [
        report
        for design in south_designs
        for report in design.mating_datums["south_boundary_hole_feasibility"]
    ]
    side_hole_reports = [
        {
            **report,
            "side": design.parameters["side"],
            "segment": design.parameters["segment"],
        }
        for design in side_designs
        for report in design.mating_datums["interior_hole_feasibility"]
    ]
    rejected_side_hole_reports = [
        {
            **report,
            "side": design.parameters["side"],
            "segment": design.parameters["segment"],
        }
        for design in side_designs
        for report in design.mating_datums["rejected_interior_hole_candidates"]
    ]
    north_hole_centers = tuple(
        sorted(
            {
                (
                    design.assembly_frames[0][0] + hole["x"],
                    design.assembly_frames[0][1] + hole["y"],
                    0.0,
                )
                for design in designs[5:10]
                for hole in design.holes
                if hole["accepted"]
            }
        )
    )
    socket_centers_by_side = {
        side: tuple(
            sorted(
                (
                    center[0] + design.assembly_frames[0][0],
                    center[1] + design.assembly_frames[0][1],
                    center[2] + design.assembly_frames[0][2],
                )
                for design in side_designs
                if design.parameters["side"] == side
                for center in design.mating_datums["accessory_socket_centers"]
            )
        )
        for side in ("west", "east")
    }

    build = h2d_common_build()
    placements = pack_sizes(
        [design.size for design in designs],
        build,
        gap=parameters.packing_gap_mm,
    )
    plate_count = max(placement.plate for placement in placements) + 1
    outline_width = 2 * max(
        right_wall_x_mm(
            y,
            outline_inset_mm=parameters.outline_inset_mm,
            pen_offset_mm=parameters.pen_offset_mm,
            east_edge_x_mm=parameters.east_edge_x_mm,
            se_along_edge_shift_mm=parameters.se_along_edge_shift_mm,
        )
        for y in np.linspace(parameters.north_limit_y_mm, 620 + 219, 100)
    )
    south_y_max = 620 + parameters.centre_depth_mm
    return Job(
        designs,
        build,
        "zeekr-7x-rear-review",
        footprint=(outline_width, south_y_max - parameters.north_limit_y_mm),
        part_gap=parameters.packing_gap_mm,
        print_placements=placements,
        plate_names={
            plate: f"Zeekr 7X rear-panel review {plate + 1}" for plate in range(plate_count)
        },
        placement_policy={
            "collection": "zeekr-7x-rear-review",
            "printer_context": "Bambu H2D common dual-nozzle reach",
            "common_reach_mm": {
                "min_x": 25,
                "max_x": 325,
                "min_y": 0,
                "max_y": 320,
                "max_z": 320,
            },
            "additional_model_inset_mm": 5,
            "minimum_model_gap_mm": parameters.packing_gap_mm,
            "packing_rotations_degrees": (0, 90),
            "actual_generated_bounds_used": True,
        },
        manifest_metadata={
            "scope": "Zeekr 7X rear lift-out-panel test section only",
            "measured_vehicle_coordinate_frame": {
                "units": "millimeter",
                "origin": "boot centreline at seatback base on carpet",
                "x": "positive toward right/seat-release-button wall",
                "y": "positive toward tailgate",
                "z": "zero at carpet surface",
            },
            "review_assembly_coordinate_frame": {
                "units": "millimeter",
                "origin": "x=0 boot centreline; y=0 measured rear target y=950; z=0 carpet",
                "x": "positive EAST/right, identical to measured vehicle x",
                "y": "positive NORTH/seatback",
                "z": "positive up from carpet",
                "from_measured_vehicle": {
                    "x": "assembly_x = measured_vehicle_x",
                    "y": "assembly_y = 950 - measured_vehicle_y",
                    "z": "assembly_z = measured_vehicle_z",
                },
                "solid_transform_note": (
                    "Measured outlines are rebuilt in this frame. Ordinary tiles remain "
                    "top-side-up at canonical rotation 0; no tile solid is reflected."
                ),
            },
            "selected_test_placement": {
                "tile_male_directions_in_vehicle_assembly": "NORTH and EAST",
                "rear_panel_target_y_mm": (
                    parameters.north_limit_y_mm,
                    parameters.rear_limit_y_mm,
                ),
                "north_finishing_band_y_mm": (
                    parameters.north_limit_y_mm,
                    parameters.field_y_min_mm,
                ),
                "tile_field_bounds_mm": (
                    parameters.field_x_min_mm,
                    parameters.field_y_min_mm,
                    -parameters.field_x_min_mm,
                    parameters.field_y_max_mm,
                ),
                "tile_modules_west_to_east_cells": TEST_TILE_MODULES,
                "south_ramp_y_mm": (
                    parameters.field_y_max_mm,
                    parameters.rear_limit_y_mm,
                ),
                "assembly_tile_field_bounds_mm": (
                    parameters.field_x_min_mm,
                    parameters.field_assembly_y_min_mm,
                    -parameters.field_x_min_mm,
                    parameters.field_assembly_y_max_mm,
                ),
                "assembly_north_finishing_band_y_mm": (
                    parameters.field_assembly_y_max_mm,
                    parameters.assembly_y_mm(parameters.north_limit_y_mm),
                ),
                "assembly_south_ramp_y_mm": (
                    0,
                    parameters.field_assembly_y_min_mm,
                ),
                "supersedes_seam_aligned_test_offset": True,
            },
            "outline": {
                "source": (
                    "Pen-offset-corrected NE-E, NE-A, NE-B, SE-C and SE-D traced "
                    "paths; exact mirror on the left."
                ),
                "selected_parameters": {
                    "traced_north_corner_radius_mm": (parameters.traced_north_corner_radius_mm),
                    "true_north_corner_radius_mm": parameters.north_corner_radius_mm,
                    "pen_offset_mm": parameters.pen_offset_mm,
                    "east_edge_x_mm": parameters.east_edge_x_mm,
                    "se_along_edge_shift_mm": parameters.se_along_edge_shift_mm,
                    "centre_depth_mm": parameters.centre_depth_mm,
                },
                "merged_taper_stations_u_inboard_s_south_mm": (
                    _merged_taper_stations(
                        parameters.pen_offset_mm,
                        parameters.se_along_edge_shift_mm,
                    )
                ),
                "right_wall_stations_y_x_mm": tuple(
                    (
                        620 + s,
                        parameters.east_edge_x_mm - parameters.outline_inset_mm - u,
                    )
                    for u, s in _merged_taper_stations(
                        parameters.pen_offset_mm,
                        parameters.se_along_edge_shift_mm,
                    )
                ),
                "superseded_handover_right_wall_stations_y_x_mm": (ORIGINAL_RIGHT_WALL_STATIONS_MM),
                "superseded_ne_only_stations_y_x_mm": (SUPERSEDED_NE_ONLY_RIGHT_WALL_STATIONS_MM),
                "superseded_note": (
                    "Earlier handover and NE-only rear stations remain evidence but do not "
                    "drive this merged review."
                ),
                "trace_evidence": {
                    "ne_a_east_straight_fit_rms_mm": 0.09803558202627236,
                    "ne_b_east_straight_fit_rms_mm": 0.2904414178640267,
                    "ne_e_long_straight_fit_rms_mm": 0.3142525515078319,
                    "traced_east_straight_registered_x_mm": 607.5,
                    "pen_corrected_true_east_x_mm": parameters.east_edge_x_mm,
                    "ne_a_corner_radius_mm": 12.025083550129526,
                    "ne_b_corner_radius_mm": 10.793695224383361,
                    "selected_traced_corner_radius_mm": (parameters.traced_north_corner_radius_mm),
                    "pen_corrected_true_corner_radius_mm": (parameters.north_corner_radius_mm),
                    "corner_center_east_vehicle_x_y_mm": (
                        _north_corner_geometry(parameters)["center"]
                    ),
                    "north_edge_bend_stations_u_s_mm": (
                        _corrected_north_edge_stations(parameters.pen_offset_mm)
                    ),
                    "north_edge_corner_slope_ds_du": (NORTH_EDGE_CORNER_SLOPE_DS_DU),
                    "raw_ne_e_long_trace_u_s_mm": NORTH_EDGE_E_PROFILE_U_S_MM,
                    "raw_ne_a_trace_u_s_mm": FINAL_NE_A_PROFILE_U_S_MM,
                    "raw_ne_b_trace_u_s_mm": FINAL_NE_B_PROFILE_U_S_MM,
                    "raw_se_c_trace_u_s_mm": FINAL_SE_C_PROFILE_U_S_MM,
                    "se_d_original_trace_u_s_mm": FINAL_SE_D_PROFILE_U_S_MM,
                    "superseded_se_d_reangled_minus3_5deg_trace_u_s_mm": (
                        FINAL_SE_D3_PROFILE_U_S_MM
                    ),
                    "se_d_reangled_minus2deg_pen_corrected_trace_u_s_mm": (
                        FINAL_SE_D2_PEN_CORRECTED_PROFILE_U_S_MM
                    ),
                    "se_d_rotation_ambiguity": {
                        "forced_rotation_range_deg": (-4, 0),
                        "overlap_median_mm": (0.09, 0.11),
                        "best_p95_near_deg": -2,
                        "best_p95_mm": 0.34,
                        "selected_overlay_rotation_deg": -2.0,
                        "selected_overlay_p95_mm": 0.343,
                        "rms_to_pen_corrected_crown_mm": 0.84,
                        "maximum_to_pen_corrected_crown_mm": 1.55,
                    },
                    "overlay_reregistration": REREGISTERED_TRACE_EVIDENCE,
                    "pen_offset_correction": PEN_OFFSET_CORRECTION_EVIDENCE,
                    "se_pair_overlap_median_mm": 0.10797921549560635,
                    "handover_y955_x530_5_check_vehicle_y_mm": 956.48,
                    "left_outline": "exact mirror of right",
                },
                "outline_inset_mm": parameters.outline_inset_mm,
                "nominal_contour_gap_mm": 0,
                "prior_full_boot_inset_convention_mm": PRIOR_FULL_BOOT_OUTLINE_INSET_MM,
                "rear_limit_y_mm": parameters.rear_limit_y_mm,
                "measurement_accuracy": {
                    "rear_and_grille_mm": 3,
                    "sweep_offsets_mm": 3,
                    "sweep_station_group_y_mm": 10,
                    "centre_depth": "365 mm user tape measurement",
                    "left": "mirrored from measured right",
                },
            },
            "south_contour": {
                **SOUTH_CONTOUR_ANALYSIS,
                "authority": (
                    "Five merged contour gauges own the north dip, side taper and C1 "
                    "junction. The user tape-measured 365 mm centre depth owns the crown."
                ),
                "crown_coefficients": {
                    "a": _crown_coefficients(
                        parameters.centre_depth_mm,
                        parameters.se_along_edge_shift_mm,
                        parameters.outline_inset_mm,
                    )[0],
                    "b": _crown_coefficients(
                        parameters.centre_depth_mm,
                        parameters.se_along_edge_shift_mm,
                        parameters.outline_inset_mm,
                    )[1],
                },
                "module_seams_x_mm": (
                    -540,
                    -300,
                    -60,
                    60,
                    300,
                    540,
                ),
                "module_seam_continuity": (
                    "All pieces evaluate one merged taper/crown function. Adjacent ruled "
                    "lofts share exact seam endpoints and equal first plan chords from its derivative."
                ),
                "completed_10mm_tile_boundary_holes": {
                    "assembly_centers_mm": tuple(
                        (float(x), parameters.field_assembly_y_min_mm, 0.0)
                        for x in range(
                            int(parameters.field_x_min_mm),
                            int(-parameters.field_x_min_mm) + 1,
                            30,
                        )
                    ),
                    "derivation": (
                        "Each ramp uses the accepted y=0 full-scope hole_placements "
                        "from its exact adjacent 4x4 or 2x4 tile. Module endpoints are "
                        "quarter-owned by each touching ramp; all other sites complete "
                        "the tile's inboard half."
                    ),
                    "diameter_mm": 10,
                    "cut_depth_into_ramp_mm": 5,
                    "minimum_high_shelf_material_behind_cut_mm": min(
                        report["minimum_high_shelf_material_behind_cut_mm"]
                        for report in south_hole_reports
                    ),
                    "minimum_adjacent_hole_web_mm": min(
                        report["minimum_adjacent_hole_web_mm"] for report in south_hole_reports
                    ),
                    "minimum_male_tab_root_side_web_mm": min(
                        report["minimum_male_tab_root_side_web_mm"]
                        for report in south_hole_reports
                        if report["minimum_male_tab_root_side_web_mm"] is not None
                    ),
                    "ownership_reports": south_hole_reports,
                },
                "north_female_band_hole_completion": {
                    "assembly_centers_mm": north_hole_centers,
                    "diameter_mm": 10,
                    "status": (
                        "unchanged standard 30 mm female bands still complete every "
                        "accepted north tile-boundary site"
                    ),
                },
                "review_artifacts": {
                    "analysis_json": "south-contour-scan-comparison.json",
                    "analysis_svg": "south-contour-scan-comparison.svg",
                    "combined_assembly_preview": "assembly-preview.svg",
                },
            },
            "side_caps": {
                "aggregate_mating_run_mm": TEST_FIELD_DEPTH_MM,
                "print_segmentation": "north and south halves split at vehicle y=770",
                "corner_parts": "none; cap segments own both adjacent corners as an assembly",
                "seams": "one flat butt seam per side, without added connector",
                "corner_tip_regularization_mm": CORNER_TIP_REGULARIZATION_MM,
                "north_outer_corner_radius_mm": parameters.north_corner_radius_mm,
                "north_corner_tangency": (
                    "Selected round tangent to the dipped north-edge terminal line "
                    f"(ds/du={NORTH_EDGE_CORNER_SLOPE_DS_DU:g}) and straight side wall "
                    f"x=+-{parameters.east_edge_x_mm:g}."
                ),
                "north_band_overhang_note": (
                    "The standard straight north female bands are unchanged. Their outer "
                    "edge overhangs the selected north panel edge by "
                    "0 mm at x=0 and by "
                    f"{_north_edge_s_slope(parameters.east_edge_x_mm - 540, parameters.pen_offset_mm)[0]:.6f} "
                    "mm at x=+-540 onto the flush "
                    "front panel; review physically."
                ),
                "accessory_sockets": {
                    "interface": (
                        "exact ordinary tile X female socket; full 13 mm depth with "
                        "native 3 mm top entry roundover"
                    ),
                    "assembly_centers_mm": {
                        side: socket_centers_by_side[side] for side in ("west", "east")
                    },
                    "count_per_side": len(socket_centers_by_side["west"]),
                    "requested_minimum_web_mm": SIDE_SOCKET_MINIMUM_WEB_MM,
                    "minimum_measured_material_mm": min(
                        report["minimum_reported_material_mm"] for report in socket_reports
                    ),
                    "all_clearance_reports": socket_reports,
                    "removed_candidates": rejected_socket_reports,
                    "removal_policy": (
                        "Grid positions are never shifted. A socket is omitted symmetrically "
                        "when any exact clearance is below the 1.5 mm criterion."
                    ),
                    "physical_fit_verified": False,
                },
                "completed_10mm_side_boundary_holes": {
                    "assembly_centers_mm": {
                        side: tuple(
                            (
                                -540.0 if side == "west" else 540.0,
                                float(y),
                                0.0,
                            )
                            for y in range(60, 301, 30)
                        )
                        for side in ("west", "east")
                    },
                    "derivation": (
                        "Accepted full-pattern sites from exact adjacent standard 4x4 "
                        "hole_placements; cap halves, split-seam quarters, north-edge "
                        "quarters and south-ramp corner quarters are cut by their owners."
                    ),
                    "diameter_mm": 10,
                },
                "completed_10mm_socket_column_holes": {
                    "assembly_centers_mm": {
                        side: tuple(
                            sorted(
                                {
                                    tuple(report["assembly_center"])
                                    for report in side_hole_reports
                                    if report["side"] == side
                                }
                            )
                        )
                        for side in ("west", "east")
                    },
                    "derivation": (
                        "Accepted full-scope hole_placements from a virtual ordinary "
                        "1x4 continuation cell. Only its socket column survives the "
                        "1.5 mm contour/join/hole/edge checks."
                    ),
                    "diameter_mm": 10,
                    "minimum_required_web_mm": PERIMETER_HOLE_MINIMUM_WEB_MM,
                    "minimum_measured_material_mm": min(
                        report["minimum_reported_material_mm"] for report in side_hole_reports
                    ),
                    "clearance_reports": side_hole_reports,
                    "rejected_outer_column_candidates": rejected_side_hole_reports,
                    "split_ownership": (
                        "Y180 is half-owned by each cap segment; Y60 is wholly in the "
                        "south segment and Y300 wholly in the north segment."
                    ),
                    "removed_y90_socket_site": (
                        "plain by pattern: odd-column/odd-row socket site, not a hole site"
                    ),
                },
                "standalone_step_coordinates": (
                    "Part-local, not assembly-global. West cap bodies occupy negative local X "
                    "and use frame (-540,0,0); east cap bodies occupy positive local X and "
                    "use frame (540,0,0). Local Y is already north-positive assembly Y."
                ),
                "review_reference_artifacts": {
                    "complete_assembly_step": "assembly-reference.step",
                    "side_caps_step": "side-cap-assembly-reference.step",
                    "orientation_facts": "side-cap-orientation.json",
                    "orientation_diagram": "side-cap-orientation.svg",
                },
            },
            "perimeter_connector_contract": {
                "tile_male_directions_in_vehicle_assembly": "NORTH and EAST",
                "canonical_tile": {
                    "north": "male",
                    "east": "male",
                    "south": "female",
                    "west": "female",
                },
                "perimeter": {
                    "north_finishing_edges": "female",
                    "east_contour_caps": "female",
                    "south_contour_ramps": "male",
                    "west_contour_caps": "male",
                },
                "high_visibility_note": (
                    "Tile male directions in vehicle assembly: NORTH and EAST."
                ),
            },
            "scan_registration": SCAN_ALIGNMENT,
            "scan_source_inspection": scan_inspection,
            "review_caveats": (
                "No physical-fit, support-removal, flatness, strength or service claim. "
                "The pull-handle recess near x=0/y840..950 may be covered. Wall and seatback "
                "lean can add clearance above carpet, but no unapproved allowance was added "
                "to the nominal z=0 contour."
            ),
        },
    )


def assembly_preview_svg(
    parameters: RearReviewParameters = RearReviewParameters(),
    trace_overlays: dict[str, tuple[tuple[float, float], ...]] | None = None,
) -> str:
    """Plan preview in the north-positive assembly frame."""
    trace_overlays = trace_overlays or {}

    def svg_y(measured_y: float) -> float:
        return -parameters.assembly_y_mm(measured_y)

    junction_u, junction_s = _merged_taper_stations(
        parameters.pen_offset_mm,
        parameters.se_along_edge_shift_mm,
    )[-1]
    junction_y = 620 + junction_s
    junction_x = parameters.east_edge_x_mm - parameters.outline_inset_mm - junction_u
    corner_geometry = _north_corner_geometry(parameters)
    center_x, center_y = corner_geometry["center"]
    tangent_x, tangent_y = corner_geometry["north_tangent"]
    tangent_u = corner_geometry["north_tangent_u"]
    north_bend = [
        (
            parameters.east_edge_x_mm - parameters.outline_inset_mm - u,
            620 + _north_edge_s_slope(float(u), parameters.pen_offset_mm)[0],
        )
        for u in np.linspace(
            float(_corrected_north_edge_curve(parameters.pen_offset_mm).x[-1]),
            tangent_u,
            60,
        )
    ]
    start_angle = atan2(tangent_y - center_y, tangent_x - center_x)
    corner_arc = [
        (
            center_x + parameters.north_corner_radius_mm * cos(angle),
            center_y + parameters.north_corner_radius_mm * sin(angle),
        )
        for angle in np.linspace(start_angle, 0, 30)
    ]
    right_wall = [
        (
            right_wall_x_mm(
                y,
                outline_inset_mm=parameters.outline_inset_mm,
                pen_offset_mm=parameters.pen_offset_mm,
                east_edge_x_mm=parameters.east_edge_x_mm,
                se_along_edge_shift_mm=parameters.se_along_edge_shift_mm,
            ),
            float(y),
        )
        for y in np.linspace(center_y, junction_y, 140)
    ]
    right_side = north_bend + corner_arc[1:] + right_wall[1:]
    outline = (
        [(-507.5, svg_y(620)), (507.5, svg_y(620))]
        + [(x, svg_y(y)) for x, y in right_side]
        + [(x, svg_y(tail_y_mm(x, parameters))) for x in np.linspace(junction_x, -junction_x, 160)]
        + [(-x, svg_y(y)) for x, y in reversed(right_side)]
    )
    outline_path = " ".join(
        f"{'M' if index == 0 else 'L'} {x:.3f} {y:.3f}" for index, (x, y) in enumerate(outline)
    )
    ramp_points = [(x, svg_y(tail_y_mm(x, parameters))) for x in np.linspace(-540, 540, 121)]
    ramp_path = " ".join(
        f"{'M' if index == 0 else 'L'} {x:.3f} {y:.3f}" for index, (x, y) in enumerate(ramp_points)
    )

    def side_path(side: str) -> str:
        sign = -1 if side == "west" else 1
        inner_x = sign * 540
        inner_u = parameters.east_edge_x_mm - parameters.outline_inset_mm - 540
        inner_north_y = (
            620
            + _north_edge_s_slope(
                inner_u,
                parameters.pen_offset_mm,
            )[0]
        )
        outer_at_field = sign * right_review_outline_x_mm(
            parameters.field_y_max_mm,
            outline_inset_mm=parameters.outline_inset_mm,
            traced_north_corner_radius_mm=parameters.traced_north_corner_radius_mm,
            pen_offset_mm=parameters.pen_offset_mm,
            east_edge_x_mm=parameters.east_edge_x_mm,
            se_along_edge_shift_mm=parameters.se_along_edge_shift_mm,
        )
        tail = [
            (x, svg_y(tail_y_mm(x, parameters))) for x in np.linspace(inner_x, outer_at_field, 30)
        ]
        wall = [
            (
                sign
                * right_review_outline_x_mm(
                    y,
                    outline_inset_mm=parameters.outline_inset_mm,
                    traced_north_corner_radius_mm=parameters.traced_north_corner_radius_mm,
                    pen_offset_mm=parameters.pen_offset_mm,
                    east_edge_x_mm=parameters.east_edge_x_mm,
                    se_along_edge_shift_mm=parameters.se_along_edge_shift_mm,
                ),
                svg_y(y),
            )
            for y in np.linspace(parameters.field_y_max_mm, center_y, 60)
        ]
        side_arc = [
            (
                sign * (center_x + parameters.north_corner_radius_mm * cos(angle)),
                svg_y(center_y + parameters.north_corner_radius_mm * sin(angle)),
            )
            for angle in np.linspace(0, start_angle, 20)
        ]
        side_north = [
            (
                sign * (parameters.east_edge_x_mm - parameters.outline_inset_mm - u),
                svg_y(
                    620
                    + _north_edge_s_slope(
                        float(u),
                        parameters.pen_offset_mm,
                    )[0]
                ),
            )
            for u in np.linspace(tangent_u, inner_u, 35)
        ]
        points = [
            (inner_x, svg_y(inner_north_y)),
            (inner_x, tail[0][1]),
            *tail[1:],
            *wall,
            *side_arc[1:],
            *side_north[1:],
        ]
        return " ".join(
            f"{'M' if index == 0 else 'L'} {x:.3f} {y:.3f}" for index, (x, y) in enumerate(points)
        )

    west_path = side_path("west")
    east_path = side_path("east")
    corrected_ne_e = _pen_corrected_trace(
        trace_overlays.get("ne-e", NORTH_EDGE_E_PROFILE_U_S_MM),
        boundary="north",
        pen_offset_mm=parameters.pen_offset_mm,
    )
    corrected_ne_a = _pen_corrected_trace(
        trace_overlays.get("ne-a", FINAL_NE_A_PROFILE_U_S_MM),
        boundary="north",
        pen_offset_mm=parameters.pen_offset_mm,
    )
    corrected_ne_b = _pen_corrected_trace(
        trace_overlays.get("ne-b", FINAL_NE_B_PROFILE_U_S_MM),
        boundary="north",
        pen_offset_mm=parameters.pen_offset_mm,
    )
    corrected_se_c = _pen_corrected_trace(
        trace_overlays.get("se-c", FINAL_SE_C_PROFILE_U_S_MM),
        boundary="south",
        pen_offset_mm=parameters.pen_offset_mm,
    )
    corrected_se_d = trace_overlays.get(
        "se-d2-corrected",
        FINAL_SE_D2_PEN_CORRECTED_PROFILE_U_S_MM,
    )
    ne_e_path = " ".join(
        f"{'M' if index == 0 else 'L'} {parameters.east_edge_x_mm - u:.3f} {svg_y(620 + s):.3f}"
        for index, (u, s) in enumerate(corrected_ne_e)
    )
    ne_a_path = " ".join(
        f"{'M' if index == 0 else 'L'} {parameters.east_edge_x_mm - u:.3f} {svg_y(620 + s):.3f}"
        for index, (u, s) in enumerate(corrected_ne_a)
    )
    ne_b_path = " ".join(
        f"{'M' if index == 0 else 'L'} {parameters.east_edge_x_mm - u:.3f} {svg_y(620 + s):.3f}"
        for index, (u, s) in enumerate(corrected_ne_b)
    )
    se_c_path = " ".join(
        f"{'M' if index == 0 else 'L'} {parameters.east_edge_x_mm - u:.3f} {svg_y(620 + s):.3f}"
        for index, (u, s) in enumerate(corrected_se_c)
    )
    se_d_path = " ".join(
        f"{'M' if index == 0 else 'L'} {parameters.east_edge_x_mm - u:.3f} {svg_y(620 + s):.3f}"
        for index, (u, s) in enumerate(corrected_se_d)
    )
    west_split_x = -right_wall_x_mm(
        parameters.side_split_y_mm,
        outline_inset_mm=parameters.outline_inset_mm,
        pen_offset_mm=parameters.pen_offset_mm,
        east_edge_x_mm=parameters.east_edge_x_mm,
        se_along_edge_shift_mm=parameters.se_along_edge_shift_mm,
    )
    east_split_x = -west_split_x
    split_y = -parameters.assembly_y_mm(parameters.side_split_y_mm)
    tile_rectangles = []
    x = parameters.field_x_min_mm
    for index, cells in enumerate(TEST_TILE_MODULES):
        width = cells * parameters.interface.pitch
        tile_rectangles.append(
            f'<rect x="{x}" y="{-parameters.field_assembly_y_max_mm}" width="{width}" '
            f'height="{TEST_FIELD_DEPTH_MM}" class="tile"/><text x="{x + width / 2}" '
            f'y="{-parameters.field_assembly_y_min_mm - 115}" class="label">{cells}x4</text>'
        )
        x += width
    socket_markers = []
    socket_counts = {}
    for side in ("west", "east"):
        frame_x = parameters.field_x_min_mm if side == "west" else -parameters.field_x_min_mm
        centers = {
            (
                center[0] + frame_x,
                center[1],
            )
            for segment in ("north", "south")
            for center in _side_socket_centers(side, segment, parameters)
        }
        socket_counts[side] = len(centers)
        for socket_x, socket_y in sorted(centers):
            y = -socket_y
            socket_markers.append(
                f'<circle cx="{socket_x}" cy="{y}" r="25.278" class="socket"/>'
                f'<line x1="{socket_x - 12}" y1="{y - 12}" x2="{socket_x + 12}" '
                f'y2="{y + 12}" class="socket-x"/><line x1="{socket_x - 12}" '
                f'y1="{y + 12}" x2="{socket_x + 12}" y2="{y - 12}" class="socket-x"/>'
            )
    preview_holes = {
        *((hole_x, float(hole_y)) for hole_x in (-540, 540) for hole_y in range(60, 301, 30)),
        *((hole_x, float(hole_y)) for hole_x in (-570, 570) for hole_y in range(60, 301, 60)),
        *((float(hole_x), 60.0) for hole_x in range(-540, 541, 30)),
        *((float(hole_x), 300.0) for hole_x in range(-540, 541, 30)),
    }
    hole_markers = "".join(
        f'<circle cx="{hole_x}" cy="{-hole_y}" r="5" class="hole"/>'
        for hole_x, hole_y in sorted(preview_holes)
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="-630 -355 1260 420">
<style>
.outline{{fill:#ece7dc;stroke:#2d333b;stroke-width:2}} .tile{{fill:#96c7d9;stroke:#234;stroke-width:1.5}}
.north{{fill:#d9c28f;opacity:.8}} .ramp{{fill:#8fc79c;opacity:.8}} .west{{fill:#d95d5d;opacity:.85}}
.east{{fill:#9b6bc7;opacity:.85}} .guide{{stroke:#555;stroke-dasharray:5 4}} .split{{stroke:#fff;stroke-width:2}}
.cad-edge{{fill:none;stroke:#167447;stroke-width:2.5}}
.socket{{fill:#fff;fill-opacity:.55;stroke:#073b66;stroke-width:1.5}} .socket-x{{stroke:#073b66;stroke-width:2}}
.hole{{fill:#fff;stroke:#222;stroke-width:1.2}}
.ne-e{{fill:none;stroke:#d62728;stroke-width:2;stroke-dasharray:4 3}}
.ne-a{{fill:none;stroke:#e68600;stroke-width:2;stroke-dasharray:4 3}}
.ne-b{{fill:none;stroke:#b85c00;stroke-width:2;stroke-dasharray:4 3}}
.se-c{{fill:none;stroke:#7040c0;stroke-width:2;stroke-dasharray:4 3}}
.se-d{{fill:none;stroke:#276dcc;stroke-width:2;stroke-dasharray:4 3}}
.label{{font:16px sans-serif;text-anchor:middle;dominant-baseline:middle}} .note{{font:14px sans-serif}}
</style>
<path d="{outline_path} Z" class="outline"/>
<path d="{west_path} Z" class="west"/>
<path d="{east_path} Z" class="east"/>
<path d="{ne_e_path}" class="ne-e"/>
<path d="{ne_a_path}" class="ne-a"/>
<path d="{ne_b_path}" class="ne-b"/>
<path d="{se_c_path}" class="se-c"/>
<path d="{se_d_path}" class="se-d"/>
<line x1="-540" y1="{split_y}" x2="{west_split_x}" y2="{split_y}" class="split"/>
<line x1="540" y1="{split_y}" x2="{east_split_x}" y2="{split_y}" class="split"/>
<rect x="-540" y="{-parameters.assembly_y_mm(parameters.north_limit_y_mm)}" width="1080" height="30" class="north"/>
<path d="M -540 {-parameters.field_assembly_y_min_mm} {ramp_path[1:]} L 540 {-parameters.field_assembly_y_min_mm} Z" class="ramp"/>
<path d="{ramp_path}" class="cad-edge"/>
{"".join(tile_rectangles)}
{"".join(socket_markers)}
{hole_markers}
<line x1="-630" y1="-330" x2="630" y2="-330" class="guide"/>
<text x="-620" y="-337" class="note">measured y620 / assembly Y330</text>
<text x="0" y="-315" class="label">NORTH / FEMALE</text>
<text x="-575" y="-250" class="label">WEST / MALE</text>
<text x="575" y="-250" class="label">EAST / FEMALE</text>
<text x="-605" y="-50" class="note">{socket_counts["west"]} standard X sockets</text>
<text x="475" y="-50" class="note">{socket_counts["east"]} standard X sockets</text>
<text x="-230" y="52" class="note">10 mm completed holes: north/south rows, side boundaries and cap socket columns</text>
<text x="290" y="-180" class="note">red/orange/brown/purple/blue: NE-E / NE-A / NE-B / SE-C / SE-D</text>
<text x="290" y="-165" class="note">trace overlays corrected inward for 5 mm pen-barrel offset; SE-D -2.0°</text>
<text x="420" y="-320" class="note">exact tangent R{parameters.north_corner_radius_mm:g} corner</text>
<text x="0" y="-25" class="label">SOUTH / MALE RAMP</text>
<text x="0" y="17" class="label">tailgate contour / assembly Y0</text>
<text x="-150" y="35" class="note">centre depth={parameters.centre_depth_mm:g} mm from user tape measurement</text>
<text x="-260" y="-345" class="label">Tile male directions: NORTH and EAST</text>
<text x="-100" y="-335" class="note">assembly +X EAST/right →</text>
<text x="390" y="-337" class="note">assembly +Y NORTH/seatback ↑</text>
</svg>
"""


def south_contour_comparison_svg(
    raw_samples: tuple[tuple[float, float], ...] = SOUTH_CONTOUR_RAW_EXACT_SAMPLES,
    parameters: RearReviewParameters = RearReviewParameters(),
    trace_overlays: dict[str, tuple[tuple[float, float], ...]] | None = None,
) -> str:
    """Evidence-only contour comparison in measured vehicle coordinates."""
    trace_overlays = trace_overlays or {}
    width, height = 1400, 700
    left, right, top, bottom = 80, 1365, 45, 625
    x_min, x_max, s_min, s_max = -620.0, 620.0, -5.0, 385.0

    def point(x: float, s: float) -> tuple[float, float]:
        return (
            left + (x - x_min) / (x_max - x_min) * (right - left),
            top + (s - s_min) / (s_max - s_min) * (bottom - top),
        )

    def path(samples: list[tuple[float, float]] | tuple[tuple[float, float], ...]) -> str:
        return " ".join(
            f"{'M' if index == 0 else 'L'} {point(x, s)[0]:.2f} {point(x, s)[1]:.2f}"
            for index, (x, s) in enumerate(samples)
        )

    dense_x = np.linspace(-parameters.east_edge_x_mm, parameters.east_edge_x_mm, 600)
    final_south = [(float(x), tail_y_mm(float(x), parameters) - 620) for x in dense_x]
    raw_scan = [(x, y - 620) for x, y in raw_samples]
    withdrawn_scan = [(x, y - 620) for x, y, _ in WITHDRAWN_SCAN_FOLLOWING_SAMPLES]
    withdrawn_cosine = [(x, y - 620) for x, y, _ in WITHDRAWN_RAISED_COSINE_SAMPLES]
    withdrawn_parabola = [(x, y - 620) for x, y, _ in WITHDRAWN_SINGLE_PARABOLA_SAMPLES]
    raw_trace_profiles = {
        "ne-e": trace_overlays.get("ne-e", NORTH_EDGE_E_PROFILE_U_S_MM),
        "ne-a": trace_overlays.get("ne-a", FINAL_NE_A_PROFILE_U_S_MM),
        "ne-b": trace_overlays.get("ne-b", FINAL_NE_B_PROFILE_U_S_MM),
        "se-c": trace_overlays.get("se-c", FINAL_SE_C_PROFILE_U_S_MM),
    }
    raw_trace_paths = {
        name: [(607.5 - u, s) for u, s in samples] for name, samples in raw_trace_profiles.items()
    }
    trace_paths = {
        "ne-e": [
            (parameters.east_edge_x_mm - u, s)
            for u, s in _pen_corrected_trace(
                raw_trace_profiles["ne-e"],
                boundary="north",
                pen_offset_mm=parameters.pen_offset_mm,
            )
        ],
        "ne-a": [
            (parameters.east_edge_x_mm - u, s)
            for u, s in _pen_corrected_trace(
                raw_trace_profiles["ne-a"],
                boundary="north",
                pen_offset_mm=parameters.pen_offset_mm,
            )
        ],
        "ne-b": [
            (parameters.east_edge_x_mm - u, s)
            for u, s in _pen_corrected_trace(
                raw_trace_profiles["ne-b"],
                boundary="north",
                pen_offset_mm=parameters.pen_offset_mm,
            )
        ],
        "se-c": [
            (parameters.east_edge_x_mm - u, s)
            for u, s in _pen_corrected_trace(
                raw_trace_profiles["se-c"],
                boundary="south",
                pen_offset_mm=parameters.pen_offset_mm,
            )
        ],
        "se-d2": [
            (parameters.east_edge_x_mm - u, s)
            for u, s in trace_overlays.get(
                "se-d2-corrected",
                FINAL_SE_D2_PEN_CORRECTED_PROFILE_U_S_MM,
            )
        ],
    }
    grid = []
    for s in (0, 100, 200, 300, 365):
        y = point(0, s)[1]
        grid.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{right}" y2="{y:.2f}" class="grid"/>'
            f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end">{s}</text>'
        )
    for x in (-540, -300, -60, 0, 60, 300, 540):
        screen_x = point(x, 0)[0]
        grid.append(
            f'<line x1="{screen_x:.2f}" y1="{top}" x2="{screen_x:.2f}" '
            f'y2="{bottom}" class="seam"/><text x="{screen_x:.2f}" y="{bottom + 20}" '
            f'text-anchor="middle">{x:g}</text>'
        )
    tape_x, tape_y = point(0, parameters.centre_depth_mm)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">
<style>
text{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;fill:#222;font-size:12px}}
.title{{font-size:20px;font-weight:700}} .grid{{stroke:#d8dde3;stroke-width:1}}
.seam{{stroke:#9dc1dd;stroke-width:1;stroke-dasharray:4 5}}
.raw{{fill:none;stroke:#d56b27;stroke-width:1.6;stroke-dasharray:4 3}}
.final{{fill:none;stroke:#149447;stroke-width:3.2}} .old{{fill:none;stroke-width:1.3;stroke-dasharray:7 5}}
.ne-e{{stroke:#d62728}} .ne-a{{stroke:#e68600}} .ne-b{{stroke:#9a4b00}}
.se-c{{stroke:#7040c0}} .se-d2{{stroke:#276dcc}} .trace{{fill:none;stroke-width:2}}
.raw-trace{{fill:none;stroke:#777;stroke-width:1;opacity:.28}}
.tape{{fill:#111;stroke:#fff;stroke-width:1.5}}
</style>
<rect width="100%" height="100%" fill="#fbfbf8"/>
<text x="{left}" y="27" class="title">Zeekr rear panel contour evidence — measured frame</text>
{"".join(grid)}
<path d="{path(raw_scan)}" class="raw"/>
<path d="{path(withdrawn_scan)}" class="old" stroke="#b8792d"/>
<path d="{path(withdrawn_cosine)}" class="old" stroke="#8358ad"/>
<path d="{path(withdrawn_parabola)}" class="old" stroke="#666"/>
<path d="{path(final_south)}" class="final"/>
{"".join(f'<path d="{path(samples)}" class="raw-trace"/>' for samples in raw_trace_paths.values())}
<path d="{path(trace_paths["ne-e"])}" class="trace ne-e"/>
<path d="{path(trace_paths["ne-a"])}" class="trace ne-a"/>
<path d="{path(trace_paths["ne-b"])}" class="trace ne-b"/>
<path d="{path(trace_paths["se-c"])}" class="trace se-c"/>
<path d="{path(trace_paths["se-d2"])}" class="trace se-d2"/>
<circle cx="{tape_x:.2f}" cy="{tape_y:.2f}" r="6" class="tape"/>
<text x="{tape_x + 10:.2f}" y="{tape_y + 4:.2f}">365 mm centre tape point</text>
<text x="{left}" y="655">x: vehicle mm from centreline; s: mm south of straight north datum</text>
<text x="{left}" y="677">green final: true R6.4 + corrected taper + C1 quartic; faint gray raw 5 mm pen paths</text>
<text x="750" y="655">corrected traces: red NE-E, orange NE-A, brown NE-B, purple SE-C, blue SE-D -2.0°</text>
</svg>
"""


def _withdrawn_south_contour_comparison_svg(
    raw_samples: tuple[tuple[float, float], ...] = SOUTH_CONTOUR_RAW_EXACT_SAMPLES,
) -> str:
    """Withdrawn scan-era comparison retained only for source archaeology."""
    width, height = 1200, 520
    left, right, top, bottom = 75, 1170, 45, 455
    x_min, x_max, y_min, y_max = -540.0, 540.0, 948.0, 996.0

    def point(x: float, y: float) -> tuple[float, float]:
        return (
            left + (x - x_min) / (x_max - x_min) * (right - left),
            bottom - (y - y_min) / (y_max - y_min) * (bottom - top),
        )

    raw = " ".join(
        f"{'M' if index == 0 else 'L'} {point(x, y)[0]:.2f} {point(x, y)[1]:.2f}"
        for index, (x, y) in enumerate(raw_samples)
    )
    dense_x = np.linspace(-540, 540, 433)
    cad = " ".join(
        f"{'M' if index == 0 else 'L'} "
        f"{point(float(x), _south_parabola_y_slope(float(x))[0])[0]:.2f} "
        f"{point(float(x), _south_parabola_y_slope(float(x))[0])[1]:.2f}"
        for index, x in enumerate(dense_x)
    )
    raised = " ".join(
        f"{'M' if index == 0 else 'L'} "
        f"{point(float(x), _south_arch_y_slope(float(x))[0])[0]:.2f} "
        f"{point(float(x), _south_arch_y_slope(float(x))[0])[1]:.2f}"
        for index, x in enumerate(dense_x)
    )
    baseline_y = point(0, 950)[1]
    seams = []
    for x in (-540, -300, -60, 60, 300, 540):
        screen_x = point(x, 950)[0]
        seams.append(
            f'<line x1="{screen_x:.2f}" y1="{top}" x2="{screen_x:.2f}" y2="{bottom}" class="seam"/>'
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">
<style>
text{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;fill:#222}} .title{{font-size:20px;font-weight:700}}
.raw{{fill:none;stroke:#d56b27;stroke-width:2;stroke-dasharray:5 4}} .cad{{fill:none;stroke:#167447;stroke-width:3}}
.raised{{fill:none;stroke:#8358ad;stroke-width:2;stroke-dasharray:8 5}}
.old{{stroke:#333;stroke-width:1.5;stroke-dasharray:2 4}} .seam{{stroke:#8bb7d7;stroke-width:1;stroke-dasharray:4 5}}
</style>
<rect width="100%" height="100%" fill="#fbfbf8"/>
<text x="75" y="27" class="title">Zeekr registered south contour — measured vehicle coordinates</text>
{"".join(seams)}
<line x1="{left}" y1="{baseline_y:.2f}" x2="{right}" y2="{baseline_y:.2f}" class="old"/>
<path d="{raw}" class="raw"/><path d="{raised}" class="raised"/><path d="{cad}" class="cad"/>
<text x="75" y="492">orange: noisy scan evidence</text>
<text x="330" y="492">purple: withdrawn raised cosine</text>
<text x="650" y="492">green: final single parabola</text>
<text x="910" y="492">black: withdrawn straight y950</text>
<text x="75" y="512">H=38.830 mm; constant y''=-0.000266325/mm; scan waves/asymmetry and cosine shoulders rejected; no fit claim</text>
</svg>
"""


def _bounds(shape) -> dict:
    box = shape.bounding_box()
    return {
        "minimum": tuple(float(value) for value in box.min),
        "maximum": tuple(float(value) for value in box.max),
        "size": tuple(float(value) for value in box.size),
    }


def side_cap_orientation_facts(job: Job) -> dict:
    """Concrete part-local and assembly-frame facts for the four side caps."""
    records = []
    caps = [
        design
        for design in job.designs
        if design.parameters.get("family") == "zeekr-rear-contour-side"
    ]
    for design in caps:
        frame = design.assembly_frames[0]
        placed = design.shape.moved(Location(frame))
        joins = design.mating_datums["joins"]
        records.append(
            {
                "name": design.name,
                "side": design.parameters["side"],
                "segment": design.parameters["segment"],
                "connector_sex": design.parameters["connector_sex"],
                "standalone_local_x": design.parameters["standalone_local_x"],
                "local_bounds_mm": _bounds(design.shape),
                "assembly_frame_mm": frame,
                "assembly_bounds_mm": _bounds(placed),
                "joins_local_mm": joins,
                "joins_assembly_mm": [
                    {
                        **join,
                        "position": tuple(
                            join["position"][axis] + frame[axis] for axis in range(3)
                        ),
                    }
                    for join in joins
                ],
                "accessory_socket_centers_local_mm": design.mating_datums[
                    "accessory_socket_centers"
                ],
                "accessory_socket_centers_assembly_mm": [
                    (
                        center[0] + frame[0],
                        center[1] + frame[1],
                        center[2] + frame[2],
                    )
                    for center in design.mating_datums["accessory_socket_centers"]
                ],
                "completed_10mm_boundary_holes_local_mm": design.mating_datums[
                    "completed_10mm_boundary_hole_centers"
                ],
                "completed_10mm_boundary_holes_assembly_mm": [
                    (center[0] + frame[0], center[1] + frame[1])
                    for center in design.mating_datums["completed_10mm_boundary_hole_centers"]
                ],
                "socket_feasibility": design.mating_datums["socket_feasibility"],
                "rejected_socket_candidates": design.mating_datums["rejected_socket_candidates"],
                "corner_ramp_samples_local_x_run_mm": design.mating_datums[
                    "corner_ramp_samples_local_x_run_mm"
                ],
            }
        )
    west_south = next(
        record for record in records if record["side"] == "west" and record["segment"] == "south"
    )
    east_south = next(
        record for record in records if record["side"] == "east" and record["segment"] == "south"
    )
    west_samples = west_south["corner_ramp_samples_local_x_run_mm"]
    east_samples = east_south["corner_ramp_samples_local_x_run_mm"]
    mirror_matches = len(west_samples) == len(east_samples) and all(
        abs(-west_x - east_x) < 1e-7 and abs(west_run - east_run) < 1e-7
        for (west_x, west_run), (east_x, east_run) in zip(
            reversed(west_samples),
            east_samples,
        )
    )
    return {
        "coordinate_frame": {
            "x": "assembly +X points EAST/right",
            "y": "assembly +Y points NORTH/seatback",
            "z": "assembly +Z points up from carpet",
            "measured_transform": "assembly Y = 950 - measured vehicle y",
        },
        "standalone_warning": (
            "Individual STEP coordinates are part-local. Apply assembly_frame_mm "
            "or open side-cap-assembly-reference.step before judging vehicle side."
        ),
        "canonical_boundary_contract": {
            "west_tile_boundary": "female",
            "west_cap": "male",
            "east_tile_boundary": "male",
            "east_cap": "female",
            "north_tile_boundary": "male",
            "north_edge": "female",
            "south_tile_boundary": "female",
            "south_ramp": "male",
        },
        "south_contours_are_mirrored": mirror_matches,
        "accessory_socket_contract": {
            "interface": "ordinary tile X female socket",
            "count_per_side": sum(
                len(record["accessory_socket_centers_assembly_mm"])
                for record in records
                if record["side"] == "west"
            ),
            "west_assembly_centers_mm": tuple(
                center
                for record in records
                if record["side"] == "west"
                for center in record["accessory_socket_centers_assembly_mm"]
            ),
            "east_assembly_centers_mm": tuple(
                center
                for record in records
                if record["side"] == "east"
                for center in record["accessory_socket_centers_assembly_mm"]
            ),
            "removed_candidates": tuple(
                report for record in records for report in record["rejected_socket_candidates"]
            ),
        },
        "caps": records,
    }


def side_cap_orientation_svg(facts: dict) -> str:
    """Readable side/cap orientation card with exact bounds and frame directions."""
    rows = []
    for index, record in enumerate(facts["caps"]):
        y = 160 + index * 115
        color = "#d95d5d" if record["side"] == "west" else "#9b6bc7"
        bounds = record["local_bounds_mm"]
        assembly = record["assembly_bounds_mm"]
        rows.append(
            f'<rect x="45" y="{y - 34}" width="1110" height="92" rx="8" '
            f'fill="{color}" opacity=".16" stroke="{color}" stroke-width="2"/>'
            f'<text x="70" y="{y}" class="name">{record["name"]}</text>'
            f'<text x="520" y="{y}" class="fact">{record["side"].upper()} / '
            f"{record['connector_sex'].upper()}</text>"
            f'<text x="70" y="{y + 28}" class="fact">local X '
            f"{bounds['minimum'][0]:.3f}..{bounds['maximum'][0]:.3f}; "
            f"frame X {record['assembly_frame_mm'][0]:.0f}; assembly X "
            f"{assembly['minimum'][0]:.3f}..{assembly['maximum'][0]:.3f}</text>"
            f'<text x="820" y="{y + 28}" class="fact">join Y '
            f"{', '.join(f'{join["position"][1]:.0f}' for join in record['joins_assembly_mm'])}"
            f"</text>"
            f'<text x="70" y="{y + 48}" class="fact">X sockets '
            f"{', '.join(f'({center[0]:.0f},{center[1]:.0f})' for center in record['accessory_socket_centers_assembly_mm'])}; "
            f"10mm holes Y "
            f"{', '.join(f'{center[1]:.0f}' for center in record['completed_10mm_boundary_holes_assembly_mm'])}"
            f"</text>"
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 680">
<style>
.title{{font:700 28px sans-serif}} .subtitle{{font:17px sans-serif}} .name{{font:700 17px monospace}}
.fact{{font:15px monospace}} .axis{{stroke:#222;stroke-width:3}}
</style>
<text x="45" y="48" class="title">Zeekr rear review — side-cap orientation</text>
<text x="45" y="82" class="subtitle">Individual STEP files are part-local. Use the assembly frame or the assembled STEP before judging vehicle side.</text>
<line x1="70" y1="110" x2="260" y2="110" class="axis"/><text x="275" y="116" class="subtitle">assembly +X → EAST/RIGHT</text>
<line x1="770" y1="140" x2="770" y2="100" class="axis"/><text x="785" y="126" class="subtitle">assembly +Y ↑ NORTH/SEATBACK</text>
{"".join(rows)}
<text x="45" y="640" class="subtitle">Red = west/left/male. Purple = east/right/female. South contour sample arrays are exact X mirrors: {facts["south_contours_are_mirrored"]}.</text>
</svg>
"""


def _write_assembly_step(
    path: Path,
    designs: list[Design],
    *,
    label: str,
) -> dict:
    children = []
    for design in designs:
        placed = design.shape.moved(Location(design.assembly_frames[0]))
        placed.label = design.display_name or design.name
        children.append(placed)
    assembly = Compound(children=children, label=label)
    source_bounds = _bounds(assembly)
    if not export_step(assembly, path):
        raise ValueError(f"assembly STEP export failed: {path}")
    restored = import_step(path)
    restored_bounds = _bounds(restored)
    bounds_delta = max(
        abs(source_bounds[bound][axis] - restored_bounds[bound][axis])
        for bound in ("minimum", "maximum")
        for axis in range(3)
    )
    if not restored.is_valid or len(restored.solids()) != len(designs) or bounds_delta > 1e-5:
        raise ValueError(f"assembly STEP roundtrip failed: {path}")
    return {
        "file": path.name,
        "solid_count": len(restored.solids()),
        "bounds_mm": restored_bounds,
        "bounds_delta_mm": bounds_delta,
        "step_roundtrip": "passed",
        "printable_job_member": False,
    }


def export_rear_review(
    output: Path,
    *,
    parameters: RearReviewParameters = RearReviewParameters(),
    scan_obj: Path | None = None,
    south_contour_analysis_json: Path | None = None,
    south_contour_analysis_svg: Path | None = None,
) -> Path:
    """Export STEP sources, checked meshes, an unsliced Bambu 3MF and review aids."""
    from cargo_grid.export import BambuSettings, Material, export_job

    scan_inspection = inspect_scan_obj(scan_obj) if scan_obj is not None else None
    if scan_inspection is not None and not scan_inspection["matches_review_source"]:
        raise ValueError("scan OBJ does not match the reviewed photogrammetry source facts")
    if (south_contour_analysis_json is None) != (south_contour_analysis_svg is None):
        raise ValueError("south contour analysis JSON and SVG must be supplied together")
    contour_analysis = None
    source_analysis_svg_sha256 = None
    if south_contour_analysis_json is not None:
        contour_analysis = json.loads(south_contour_analysis_json.resolve(strict=True).read_text())
        raw_exact = tuple(
            (
                float(sample["x_mm"]),
                float(sample["raw_y_south_mm"]),
            )
            for sample in contour_analysis["extraction"]["raw_exact_samples"]
        )
        if raw_exact != SOUTH_CONTOUR_RAW_EXACT_SAMPLES:
            raise ValueError("south contour raw samples differ from the reviewed scan evidence")
        if (
            contour_analysis["source"]["obj_sha256"] != SCAN_OBJ_SHA256
            or not contour_analysis["confidence"]["credible_to_drive_cad"]
        ):
            raise ValueError("south contour analysis source or confidence changed")
        source_analysis_svg_sha256 = sha256(
            south_contour_analysis_svg.resolve(strict=True).read_bytes()
        ).hexdigest()
    job = rear_review_job(parameters, scan_inspection=scan_inspection)
    manifest = export_job(
        job,
        output,
        stl=False,
        bambu=BambuSettings(
            (Material("Diagnostic PETG", "PETG", "#637b70"),),
            nozzle=0.8,
            layer_height=0.32,
            printer_settings_id="Bambu Lab H2D 0.8 nozzle",
            print_settings_id="0.32mm Balanced Strength @BBL H2D 0.8 nozzle",
            bed_type="Textured PEI Plate",
            machine_nozzle_count=2,
            printer_model="Bambu Lab H2D",
        ),
    )
    (output / "assembly-preview.svg").write_text(assembly_preview_svg(parameters))
    facts = side_cap_orientation_facts(job)
    (output / "side-cap-orientation.json").write_text(json.dumps(facts, indent=2) + "\n")
    (output / "side-cap-orientation.svg").write_text(side_cap_orientation_svg(facts))
    if contour_analysis is not None:
        raw_samples = tuple(
            (float(sample["x_mm"]), float(sample["y_median_mm"]))
            for sample in contour_analysis["raw_binning"]["samples"]
        )
    else:
        raw_samples = SOUTH_CONTOUR_RAW_EXACT_SAMPLES
    (output / "south-contour-scan-comparison.json").write_text(
        json.dumps(
            {
                "registered_scan_evidence": contour_analysis,
                "source_analysis_svg_sha256": source_analysis_svg_sha256,
                "merged_gauge_cad": {
                    "analysis": SOUTH_CONTOUR_ANALYSIS,
                    "selected_parameters": {
                        "traced_north_corner_radius_mm": (parameters.traced_north_corner_radius_mm),
                        "true_north_corner_radius_mm": parameters.north_corner_radius_mm,
                        "pen_offset_mm": parameters.pen_offset_mm,
                        "east_edge_x_mm": parameters.east_edge_x_mm,
                        "se_along_edge_shift_mm": parameters.se_along_edge_shift_mm,
                        "centre_depth_mm": parameters.centre_depth_mm,
                    },
                    "taper_stations_u_s_mm": _merged_taper_stations(
                        parameters.pen_offset_mm, parameters.se_along_edge_shift_mm
                    ),
                    "crown_coefficients_a_b": _crown_coefficients(
                        parameters.centre_depth_mm,
                        parameters.se_along_edge_shift_mm,
                        parameters.outline_inset_mm,
                        parameters.pen_offset_mm,
                        parameters.east_edge_x_mm,
                    )[:2],
                    "raw_pen_traces_u_s_mm": {
                        "ne_e": NORTH_EDGE_E_PROFILE_U_S_MM,
                        "ne_a": FINAL_NE_A_PROFILE_U_S_MM,
                        "ne_b": FINAL_NE_B_PROFILE_U_S_MM,
                        "se_c": FINAL_SE_C_PROFILE_U_S_MM,
                    },
                    "se_d_reangled_minus2deg_pen_corrected_u_s_mm": (
                        FINAL_SE_D2_PEN_CORRECTED_PROFILE_U_S_MM
                    ),
                    "pen_offset_correction": PEN_OFFSET_CORRECTION_EVIDENCE,
                },
                "withdrawn_scan_following_candidate": {
                    "samples_x_y_slope": WITHDRAWN_SCAN_FOLLOWING_SAMPLES,
                    "reason": (
                        "User identified its local waves and asymmetry as photogrammetry error. "
                        "It is evidence only and does not drive geometry."
                    ),
                },
                "withdrawn_raised_cosine": {
                    "samples_x_y_slope": WITHDRAWN_RAISED_COSINE_SAMPLES,
                    "reason": (
                        "User rejected its separate endpoint blend and S-shaped shoulders "
                        "in favor of one quadratic over the complete south edge."
                    ),
                },
                "withdrawn_single_parabola": {
                    "samples_x_y_slope": WITHDRAWN_SINGLE_PARABOLA_SAMPLES,
                    "reason": (
                        "Superseded by the merged SE traces and tape-measured C1 quartic crown."
                    ),
                },
            },
            indent=2,
        )
        + "\n"
    )
    (output / "south-contour-scan-comparison.svg").write_text(
        south_contour_comparison_svg(raw_samples, parameters)
    )
    side_caps = [
        design
        for design in job.designs
        if design.parameters.get("family") == "zeekr-rear-contour-side"
    ]
    reference_steps = {
        "complete_assembly": _write_assembly_step(
            output / "assembly-reference.step",
            job.designs,
            label="Zeekr 7X rear test assembly reference - not a print plate",
        ),
        "side_caps": _write_assembly_step(
            output / "side-cap-assembly-reference.step",
            side_caps,
            label="Zeekr 7X side caps in north-positive assembly coordinates - not a print plate",
        ),
    }
    (output / "assembly-reference.json").write_text(
        json.dumps(
            {
                "coordinate_frame": facts["coordinate_frame"],
                "references": reference_steps,
                "note": (
                    "Review-only assembled-coordinate STEP files. They are not H2D print "
                    "plates and are not included as objects in job.3mf."
                ),
            },
            indent=2,
        )
        + "\n"
    )
    if scan_inspection is not None:
        (output / "scan-verification.json").write_text(
            json.dumps(
                {
                    "source_inspection": scan_inspection,
                    "alignment_and_residuals": SCAN_ALIGNMENT,
                },
                indent=2,
            )
            + "\n"
        )
    return manifest
