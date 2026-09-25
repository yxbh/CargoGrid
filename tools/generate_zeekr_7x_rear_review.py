"""Generate the local Zeekr 7X rear-panel review artifacts."""

import argparse
from pathlib import Path

from cargo_grid.vehicles.zeekr_7x_rear_review import (
    DEFAULT_CENTRE_DEPTH_MM,
    DEFAULT_EAST_EDGE_X_MM,
    DEFAULT_PEN_OFFSET_MM,
    DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
    TRACED_NORTH_CORNER_RADIUS_MM,
    RearReviewParameters,
    export_rear_review,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--scan-obj", type=Path)
    parser.add_argument("--south-contour-analysis-json", type=Path)
    parser.add_argument("--south-contour-analysis-svg", type=Path)
    parser.add_argument("--outline-inset-mm", type=float, default=0.0)
    parser.add_argument("--rear-limit-y-mm", type=float, default=950.0)
    parser.add_argument(
        "--traced-north-corner-radius-mm",
        type=float,
        default=TRACED_NORTH_CORNER_RADIUS_MM,
    )
    parser.add_argument("--pen-offset-mm", type=float, default=DEFAULT_PEN_OFFSET_MM)
    parser.add_argument("--east-edge-x-mm", type=float, default=DEFAULT_EAST_EDGE_X_MM)
    parser.add_argument(
        "--se-along-edge-shift-mm",
        type=float,
        default=DEFAULT_SE_ALONG_EDGE_SHIFT_MM,
    )
    parser.add_argument(
        "--centre-depth-mm",
        type=float,
        default=DEFAULT_CENTRE_DEPTH_MM,
    )
    arguments = parser.parse_args()
    manifest = export_rear_review(
        arguments.output,
        parameters=RearReviewParameters(
            outline_inset_mm=arguments.outline_inset_mm,
            rear_limit_y_mm=arguments.rear_limit_y_mm,
            traced_north_corner_radius_mm=arguments.traced_north_corner_radius_mm,
            pen_offset_mm=arguments.pen_offset_mm,
            east_edge_x_mm=arguments.east_edge_x_mm,
            se_along_edge_shift_mm=arguments.se_along_edge_shift_mm,
            centre_depth_mm=arguments.centre_depth_mm,
        ),
        scan_obj=arguments.scan_obj,
        south_contour_analysis_json=arguments.south_contour_analysis_json,
        south_contour_analysis_svg=arguments.south_contour_analysis_svg,
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
