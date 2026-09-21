"""Command-line tools for making Cargo-Grid parts, layouts and catalogues."""

import argparse
import json
import sys
from math import floor
from pathlib import Path

from cargo_grid._version import __version__
from cargo_grid.accessories import (
    EDGE_FAMILIES,
    FAMILIES,
    STRAIGHT_EDGE_OUTWARD_OPTIONS_MM,
    Accessory,
)
from cargo_grid.catalogue import (
    H2D_DEFAULT_PART_CLEARANCE_MM,
    accessory_design,
    catalogue_job,
    h2d_dual_safe_catalogue_job,
)
from cargo_grid.export import BambuSettings, Material, export_job
from cargo_grid.jobs import Job, layout_job, tile_design
from cargo_grid.layout import exact_layout
from cargo_grid.packing import h2d_common_build
from cargo_grid.parameters import (
    DEFAULT_HOLE_DIAMETER_MM,
    BuildVolume,
    Exclusion,
    Interface,
    Tile,
    count,
    positive,
)
from cargo_grid.rods import ROD_FAMILIES, Rod, RodBrace
from cargo_grid.roof_support import RoofSupportSettings
from cargo_grid.stacking import StackSettings
from cargo_grid.trunk_blocker import TrunkBlockerSpec
from cargo_grid.vehicles import zeekr_7x, zeekr_7x_rear_review


def _positive_mm(value: str) -> float:
    try:
        millimeters = float(value)
        positive("dimension", millimeters)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "enter a finite number greater than 0, in millimeters"
        ) from error
    return millimeters


LEGACY_OPTION_REPLACEMENTS = {
    "--build": "--build-width-mm MM --build-depth-mm MM --build-height-mm MM",
    "--margin": "--build-margin-mm MM",
    "--part-gap": "--packing-gap-mm MM",
    "--reserve": "--build-reserve-width-mm MM --build-reserve-depth-mm MM --build-reserve-height-mm MM",
    "--exclude": "--exclude-rectangle-mm X_MM Y_MM WIDTH_MM DEPTH_MM",
    "--pitch": "--unit-size-mm MM",
    "--height": "--tile-thickness-mm MM",
    "--grid-pitch-mm": "--unit-size-mm MM",
    "--tile-height-mm": "--tile-thickness-mm MM",
    "--fit-offset": "--fit-offset-mm MM",
    "--hole-diameter": "--hole-diameter-mm MM",
    "--nozzle": "--nozzle-diameter-mm MM",
    "--layer-height": "--layer-height-mm MM",
    "--roof-top-gap": "--roof-top-gap-mm MM",
    "--roof-interface-layers": "--roof-interface-layer-count COUNT",
    "--roof-interface-spacing": "--roof-interface-spacing-mm MM",
    "--roof-nozzles": "--roof-nozzle-slots PETG_SLOT PLA_SLOT",
    "--roof-foot-expansion": "--roof-foot-expansion-mm MM",
    "--stack-gap": "--stack-gap-mm MM",
    "--interface-thickness": "--stack-interface-thickness-mm MM",
    "--material-roles": "--stack-material-slots MODEL_SLOT SUPPORT_BASE_SLOT RELEASE_INTERFACE_SLOT",
    "--cells": "--width-cells COUNT --depth-cells COUNT",
    "--variant": "--variant-number NUMBER",
    "--length": "--connector-length-mm MM",
    "--accessory-height": "--stop-height-mm MM",
    "--quantity": "--copy-count COUNT",
    "--footprint": "--layout-width-mm MM --layout-depth-mm MM",
    "--filler": "--filler-placement balanced|positive|negative",
}


class _ArgumentParser(argparse.ArgumentParser):
    def parse_known_args(self, args=None, namespace=None):
        arguments = list(sys.argv[1:] if args is None else args)
        for argument in arguments:
            option = argument.split("=", 1)[0]
            if option in LEGACY_OPTION_REPLACEMENTS:
                self.error(
                    f"unrecognized option {option}; use {LEGACY_OPTION_REPLACEMENTS[option]}"
                )
        return super().parse_known_args(arguments, namespace)


def _part_dimensions(args) -> tuple[int, int]:
    width = args.width_cells
    depth = args.depth_cells
    length = args.length_cells
    family = args.family
    two_axis = {"tile", "plate", "vertical-tile-bracket", "vertical-stop", "lock-45"}
    linear = {"edge-x", "edge-y", "support"}
    dimensionless = {"corner-in", "corner-out", "support-bit", "support-end", *ROD_FAMILIES}
    if family in two_axis:
        if length is not None:
            raise ValueError(f"{family} uses --width-cells and --depth-cells, not --length-cells")
        if (width is None) != (depth is None):
            missing = "--depth-cells" if depth is None else "--width-cells"
            raise ValueError(
                f"{family} needs both grid dimensions when either is specified; add {missing} COUNT"
            )
        return (
            (width, depth)
            if width is not None
            else (2, 1)
            if family in {"vertical-tile-bracket", "vertical-stop"}
            else (1, 1)
        )
    if family == "ramp":
        if depth is not None or length is not None:
            invalid = "--depth-cells" if depth is not None else "--length-cells"
            raise ValueError(
                f"{invalid} does not apply to ramp; use only --width-cells along the tile edge because its front-to-back run is fixed at 50 mm"
            )
        return 1 if width is None else width, 1
    if family in linear:
        if width is not None or depth is not None:
            invalid = "--width-cells" if width is not None else "--depth-cells"
            raise ValueError(f"{invalid} does not apply to {family}; use --length-cells")
        return length or 1, 1
    if family in dimensionless:
        if any(value is not None for value in (width, depth, length)):
            invalid = (
                "--width-cells"
                if width is not None
                else "--depth-cells"
                if depth is not None
                else "--length-cells"
            )
            raise ValueError(f"{invalid} does not apply to {family}")
        return 1, 1
    raise ValueError(f"unknown part family: {family}")


def _part_option(args, name: str, supported: set[str], default):
    value = getattr(args, name)
    if value is not None and args.family not in supported:
        option = "--" + name.replace("_", "-")
        raise ValueError(f"{option} does not apply to {args.family}")
    return default if value is None else value


def _build_volume(args) -> BuildVolume:
    return BuildVolume(
        args.build_width_mm,
        args.build_depth_mm,
        args.build_height_mm,
        margin=args.build_margin_mm,
        reserve_x=args.build_reserve_width_mm,
        reserve_y=args.build_reserve_depth_mm,
        reserve_z=args.build_reserve_height_mm,
        exclusions=tuple(Exclusion(*area) for area in args.exclude_rectangle_mm),
    )


def _interface(args) -> Interface:
    return Interface(
        args.unit_size_mm,
        args.tile_thickness_mm,
        args.fit_offset_mm,
        args.joint_style,
    )


def _roof_support(args) -> RoofSupportSettings | None:
    supplied = (
        args.roof_top_gap_mm,
        args.roof_interface_layer_count,
        args.roof_interface_spacing_mm,
        args.roof_coverage,
        args.roof_nozzle_slots,
        args.roof_foot_expansion_mm,
    )
    if not args.roof_support and not any(value is not None for value in supplied):
        return None
    if not args.roof_support:
        raise ValueError("roof contact options require --roof-support")
    if not args.bambu:
        raise ValueError("roof supports require --bambu; core 3MF has no native support semantics")
    if args.joint_style != "original":
        raise ValueError("roof supports require original roofed joints")
    if args.command in ("catalogue", "extras") or (
        args.command == "part" and args.family != "tile"
    ):
        raise ValueError("roof supports require a tile-only part or layout job")
    if any(
        value is not None
        for value in (
            args.stack_count,
            args.stack_gap_mm,
            args.stack_interface_thickness_mm,
            args.stack_material_slots,
        )
    ):
        raise ValueError("roof supports and stacked separator jobs cannot be combined")
    return RoofSupportSettings(
        args.roof_top_gap_mm if args.roof_top_gap_mm is not None else 0.0,
        args.roof_interface_layer_count if args.roof_interface_layer_count is not None else 2,
        args.roof_interface_spacing_mm if args.roof_interface_spacing_mm is not None else 0.0,
        tuple(args.roof_nozzle_slots) if args.roof_nozzle_slots is not None else None,
        coverage=args.roof_coverage or "critical",
        foot_expansion=args.roof_foot_expansion_mm,
    )


def _bambu_settings(args, roof_support: RoofSupportSettings | None) -> BambuSettings | None:
    if args.bambu:
        if args.nozzle_diameter_mm is None or args.layer_height_mm is None:
            raise ValueError("Bambu export requires --nozzle-diameter-mm and --layer-height-mm")
        materials = tuple(Material(*material) for material in args.material)
        if getattr(args, "h2d_dual_safe", False):
            if args.nozzle_diameter_mm != 0.8 or args.layer_height_mm != 0.32:
                raise ValueError(
                    "--h2d-dual-safe currently requires --nozzle-diameter-mm 0.8 "
                    "and --layer-height-mm 0.32"
                )
            if (
                len(materials) != 1
                or materials[0].kind.upper() != "PETG"
                or materials[0].name != "Bambu PETG Basic @BBL H2D 0.8 nozzle"
            ):
                raise ValueError(
                    "--h2d-dual-safe requires one official material declaration: "
                    '--material "Bambu PETG Basic @BBL H2D 0.8 nozzle" PETG "#RRGGBB"'
                )
            return BambuSettings(
                materials,
                args.nozzle_diameter_mm,
                args.layer_height_mm,
                roof_support=roof_support,
                printer_settings_id="Bambu Lab H2D 0.8 nozzle",
                print_settings_id="0.32mm Balanced Strength @BBL H2D 0.8 nozzle",
                bed_type="Textured PEI Plate",
                machine_nozzle_count=2,
                printer_model="Bambu Lab H2D",
            )
        return BambuSettings(
            materials,
            args.nozzle_diameter_mm,
            args.layer_height_mm,
            roof_support=roof_support,
        )
    if args.material or args.nozzle_diameter_mm or args.layer_height_mm:
        raise ValueError("material/nozzle/layer settings require --bambu")
    return None


def _stack_settings(args, bambu, build: BuildVolume, interface: Interface):
    supplied = (
        args.stack_count,
        args.stack_gap_mm,
        args.stack_interface_thickness_mm,
        args.stack_material_slots,
    )
    if not any(value is not None for value in supplied):
        return None
    if not all(value is not None for value in supplied) or not bambu:
        raise ValueError(
            "stacking requires --bambu, --stack-count, --stack-gap-mm, "
            "--stack-interface-thickness-mm and --stack-material-slots"
        )
    if args.command in ("catalogue", "extras"):
        raise ValueError("stack repeated part/layout quantities, not mixed catalogue samples")
    if args.command == "part" and args.family != "tile":
        raise ValueError("stacking is restricted to identical tile quantities")
    positive("stack gap", args.stack_gap_mm)
    stack_count = (
        floor((build.usable[2] + args.stack_gap_mm + 1e-8) / (interface.height + args.stack_gap_mm))
        if args.stack_count == "auto"
        else int(args.stack_count)
    )
    return StackSettings(
        stack_count,
        args.stack_gap_mm,
        args.stack_interface_thickness_mm,
        *args.stack_material_slots,
    )


def parser() -> argparse.ArgumentParser:
    root = _ArgumentParser(
        description="Make parametric cargo-mat parts and unsliced print projects.",
        epilog="Original roofed joints and full 10 mm round-hole tiles are the defaults; roof support remains off. No slicing or printer control.",
        allow_abbrev=False,
    )
    root.add_argument("--version", action="version", version=f"cargo-grid {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    descriptions = {
        "part": "Generate one tile or accessory, with as many copies as requested.",
        "layout": "Fill an exact rectangle with whole-unit tiles and built-in edge fillers.",
        "catalogue": "Generate every supported ordered tile size that fits, plus the finite accessory catalogue.",
        "extras": "Generate only a named extras recipe using the shared parts and export APIs.",
    }
    for command in ("part", "layout", "catalogue", "extras"):
        p = commands.add_parser(
            command,
            help=descriptions[command],
            description=descriptions[command],
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            allow_abbrev=False,
        )
        if command == "extras":
            p.add_argument(
                "vehicle",
                choices=("zeekr-7x", "zeekr-7x-rear-panel"),
                help=(
                    "Zeekr recipe: 40 mm straight edges, or measured rear-panel "
                    "contour pieces with a user-reviewed test fit"
                ),
            )
        for axis, meaning in (
            ("width", "X: build-plate left-right"),
            ("depth", "Y: build-plate front-back"),
            ("height", "Z: maximum print height"),
        ):
            p.add_argument(
                f"--build-{axis}-mm",
                type=_positive_mm,
                required=True,
                default=argparse.SUPPRESS,
                metavar="MM",
                help=f"required build {axis} in millimeters ({meaning}); no default",
            )
        p.add_argument(
            "--build-margin-mm",
            type=float,
            default=0,
            metavar="MM",
            help="inset on each X/Y side of the build plate, in mm",
        )
        p.add_argument(
            "--packing-gap-mm",
            type=float,
            default=argparse.SUPPRESS,
            metavar="MM",
            help=(
                "minimum packed-part separation in mm; default 2, or "
                f"{H2D_DEFAULT_PART_CLEARANCE_MM:g} with --h2d-dual-safe; "
                "the rear-panel recipe defaults to 10"
            ),
        )
        for axis, meaning in (
            ("width", "positive X edge"),
            ("depth", "positive Y edge"),
            ("height", "top Z"),
        ):
            p.add_argument(
                f"--build-reserve-{axis}-mm",
                type=float,
                default=0,
                metavar="MM",
                help=f"additional build space withheld at the {meaning}, in mm",
            )
        p.add_argument(
            "--exclude-rectangle-mm",
            type=float,
            nargs=4,
            action="append",
            default=[],
            metavar=("X_MM", "Y_MM", "WIDTH_MM", "DEPTH_MM"),
            help="repeatable build exclusion: lower-left X, lower-left Y, width, depth; e.g. --exclude-rectangle-mm 0 0 20 30",
        )
        p.add_argument(
            "--unit-size-mm",
            type=float,
            default=60,
            metavar="MM",
            help="cell size and matching local-plane interface scale in mm",
        )
        p.add_argument(
            "--tile-thickness-mm",
            type=float,
            default=13,
            metavar="MM",
            help="tile thickness and matching connector insertion depth in mm",
        )
        p.add_argument(
            "--joint-style",
            choices=("original", "full-height"),
            default="original",
            help="original roofed joints, or experimental open-through full-height edge joints",
        )
        p.add_argument(
            "--fit-offset-mm",
            type=float,
            default=0,
            metavar="MM",
            help="socket offset in mm; nonzero changes the compatibility preset",
        )
        holes = p.add_mutually_exclusive_group()
        holes.add_argument(
            "--holes",
            dest="holes",
            action="store_true",
            default=argparse.SUPPRESS,
            help="use round holes on tiles (the tile-workflow default); pair with diameter/scope options to customize",
        )
        holes.add_argument(
            "--no-holes",
            dest="holes",
            action="store_false",
            default=argparse.SUPPRESS,
            help="make tile webs without the additional round-hole pattern",
        )
        p.add_argument(
            "--hole-diameter-mm",
            type=float,
            default=argparse.SUPPRESS,
            metavar="MM",
            help="tile round-hole diameter in mm; defaults to 10 when holes are enabled",
        )
        p.add_argument(
            "--hole-scope",
            choices=("interior", "full"),
            default="full",
            help="tile holes at the full half-unit pattern including retained edge/corner sites, or interior-only",
        )
        p.add_argument("--output", type=Path, required=True, help="new or empty job directory")
        p.add_argument("--no-stl", action="store_true")
        p.add_argument(
            "--bambu", action="store_true", help="unsliced project, not calibrated print settings"
        )
        p.add_argument(
            "--material",
            action="append",
            nargs=3,
            default=[],
            metavar=("LABEL", "TYPE", "#RRGGBB"),
            help="declare a numbered filament slot in order: display label, material type and color; repeat per material",
        )
        p.add_argument(
            "--nozzle-diameter-mm",
            type=float,
            metavar="MM",
            help="nozzle diameter recorded in the unsliced project, in mm",
        )
        p.add_argument(
            "--layer-height-mm",
            type=float,
            metavar="MM",
            help="layer height recorded in the unsliced project, in mm",
        )
        p.add_argument(
            "--roof-support",
            action="store_true",
            help="add PETG/PLA support under retained west/south female roofs",
        )
        p.add_argument(
            "--roof-top-gap-mm",
            type=float,
            metavar="MM",
            help="roof contact gap in mm; default 0 for the intentional PETG/PLA workflow, positive values select gapped contact",
        )
        p.add_argument(
            "--roof-interface-layer-count",
            type=int,
            metavar="COUNT",
            help="top interface layers; default 2, at least 2 for zero contact",
        )
        p.add_argument(
            "--roof-coverage",
            choices=("critical", "full"),
            help="critical pads (default when roof support is enabled) or conservative full roof",
        )
        p.add_argument(
            "--roof-interface-spacing-mm",
            type=float,
            metavar="MM",
            help="interface line spacing in mm; default 0, required for zero contact",
        )
        p.add_argument(
            "--roof-nozzle-slots",
            type=int,
            nargs=2,
            metavar=("PETG_SLOT", "PLA_SLOT"),
            help="optional Custom nozzle assignment; omit to keep automatic matching",
        )
        p.add_argument(
            "--roof-foot-expansion-mm",
            type=float,
            metavar="MM",
            help="support first-layer expansion in mm; omit or -1 for native auto, 0 disables it",
        )
        p.add_argument(
            "--stack-count", help="maximum identical tiles per batch: positive integer or auto"
        )
        p.add_argument(
            "--stack-gap-mm",
            type=float,
            metavar="MM",
            help="vertical gap between stacked tiles, in mm",
        )
        p.add_argument(
            "--stack-interface-thickness-mm",
            type=float,
            metavar="MM",
            help="thickness of each stack release interface, in mm",
        )
        p.add_argument(
            "--stack-material-slots",
            type=int,
            nargs=3,
            metavar=("MODEL_SLOT", "SUPPORT_BASE_SLOT", "RELEASE_INTERFACE_SLOT"),
            help="1-based filament slot numbers for stacked models, support bases and release interfaces, in that order",
        )
        if command == "part":
            p.add_argument(
                "--family",
                default="tile",
                choices=["tile", *FAMILIES, *ROD_FAMILIES],
                help="ramp joins a tile edge; vertical-tile-bracket carries a separate tile; vertical-stop is a filled cargo wedge",
            )
            p.add_argument(
                "--width-cells",
                type=int,
                metavar="COUNT",
                help="one design's X width in grid cells; for a ramp, width along the tile edge",
            )
            p.add_argument(
                "--depth-cells",
                type=int,
                metavar="COUNT",
                help="one design's Y depth in grid cells; for a bracket, floor-base rows only",
            )
            p.add_argument(
                "--ramp-join",
                choices=("female", "male"),
                help="ramp tile-edge joint: female pockets (default) or male tabs; ramp only",
            )
            p.add_argument(
                "--panel-height-cells",
                type=int,
                metavar="COUNT",
                help="vertical-tile-bracket wall height in unit rows; omit to match --depth-cells",
            )
            p.add_argument(
                "--length-cells",
                type=int,
                metavar="COUNT",
                help="linear cell count for edge strips and support rails",
            )
            p.add_argument(
                "--variant-number",
                type=int,
                metavar="NUMBER",
                help="numbered corner or support-end variant; defaults to 1",
            )
            p.add_argument(
                "--connector-length-mm",
                type=float,
                metavar="MM",
                help="support-bit body length excluding its projecting join, in mm; defaults to 60",
            )
            p.add_argument(
                "--stop-height-mm",
                type=float,
                metavar="MM",
                help="lock-45 or vertical-stop Z height above its attachment shoulder, in mm; defaults: 50 or 60 respectively",
            )
            p.add_argument(
                "--edge-outward-mm",
                type=float,
                choices=STRAIGHT_EDGE_OUTWARD_OPTIONS_MM,
                metavar="MM",
                help="outward body width excluding tabs: 10 (default), 20 or 30 mm; 40 mm only for original-style straight edges",
            )
            edge_holes = p.add_mutually_exclusive_group()
            edge_holes.add_argument(
                "--complete-edge-holes",
                action="store_true",
                default=None,
                help="continue matching accepted full-pattern tile boundary sites through an edge/corner; diameter defaults to 10 mm",
            )
            edge_holes.add_argument(
                "--plain-edge",
                dest="complete_edge_holes",
                action="store_false",
                help="keep an edge/corner plain instead of matching the normal tile-hole pattern",
            )
            p.add_argument(
                "--copy-count",
                type=int,
                default=1,
                metavar="COUNT",
                help="copies of this one design; not width/depth cells",
            )
            p.add_argument(
                "--rod-height-mm",
                type=_positive_mm,
                metavar="MM",
                help="rod height above the flat stop, including its collar; defaults to 120 mm",
            )
            p.add_argument(
                "--peg-diameter-mm",
                type=_positive_mm,
                metavar="MM",
                help="rod mounting peg diameter, not shaft diameter; defaults to 10 mm",
            )
            p.add_argument(
                "--brace-spacing-mm",
                type=_positive_mm,
                metavar="MM",
                help="rod-brace hole centre spacing: 60 or 120 physical mm, never unit-scaled; defaults to 60",
            )
            p.add_argument(
                "--bore-diameter-mm",
                type=_positive_mm,
                metavar="MM",
                help="diameter of both rod-brace bores, in mm; standard/default 10 mm; custom allowance over the 10 mm shaft is diametral",
            )
        if command == "layout":
            p.add_argument(
                "--layout-width-mm",
                type=float,
                required=True,
                metavar="MM",
                help="assembled floor X width in mm; may span multiple printed parts",
            )
            p.add_argument(
                "--layout-depth-mm",
                type=float,
                required=True,
                metavar="MM",
                help="assembled floor Y depth in mm; may span multiple printed parts",
            )
            p.add_argument(
                "--filler-placement",
                choices=["balanced", "positive", "negative"],
                default="balanced",
                help="put leftover edge material on both ends, the positive X/Y ends, or the negative X/Y ends; unit spacing stays unchanged",
            )
        if command in ("catalogue", "extras"):
            p.add_argument(
                "--h2d-dual-safe",
                action="store_true",
                help=(
                    "H2D family-grouped plan: 5 mm inset inside common "
                    "X25..325/Y0..320/Z<=320 reach, "
                    f"{H2D_DEFAULT_PART_CLEARANCE_MM:g} mm actual-part XY clearance, "
                    "and an isolated left-nozzle-only 5x5 tile plate"
                ),
            )
    compare = commands.add_parser(
        "compare-reference",
        help="Compare measured interfaces with an explicitly supplied local reference; no upload.",
        allow_abbrev=False,
    )
    compare.add_argument(
        "--reference-file",
        type=Path,
        required=True,
        help="optional local 3MF reference file; read locally and never uploaded",
    )
    compare.add_argument(
        "--output", type=Path, required=True, help="new JSON report path; never overwritten"
    )
    blocker = commands.add_parser(
        "trunk-blocker",
        help="Generate the native-CAD experimental three-part blocker.",
        description=(
            "Generate the complete experimental three-part blocker as native STEP plus checked "
            "STL and core 3MF derivatives. Front plugs reuse Cargo-Grid's shared CAD interface."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    blocker.add_argument(
        "--extension-mm",
        type=float,
        default=24,
        metavar="MM",
        help="static wall extension within the approved 0..48 mm locking travel",
    )
    blocker.add_argument(
        "--released-illustration",
        action="store_true",
        help="prescribed squeezed-finger geometry illustration; not an elastic simulation",
    )
    blocker.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new or empty output directory; never overwritten",
    )
    return root


def _resolved_hole_diameter(args) -> float | None:
    tile_workflow = args.command != "part" or args.family == "tile"
    perimeter_workflow = args.command == "part" and args.family in EDGE_FAMILIES
    holes = getattr(args, "holes", None)
    requested_diameter = getattr(args, "hole_diameter_mm", None)
    if perimeter_workflow:
        if holes is not None:
            raise ValueError(
                "use --complete-edge-holes or --plain-edge for perimeter parts, "
                "not --holes/--no-holes"
            )
        if requested_diameter is not None and args.complete_edge_holes is False:
            raise ValueError("--hole-diameter-mm cannot be combined with --plain-edge")
        if requested_diameter is not None:
            return requested_diameter
        return DEFAULT_HOLE_DIAMETER_MM if args.complete_edge_holes is True else None
    if not tile_workflow:
        if holes is True or requested_diameter is not None:
            raise ValueError("round-hole options apply to tiles, not accessory bodies")
        return None
    if holes is False:
        if requested_diameter is not None:
            raise ValueError("--no-holes cannot be combined with --hole-diameter-mm")
        return None
    return requested_diameter if requested_diameter is not None else DEFAULT_HOLE_DIAMETER_MM


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == "compare-reference":
            from cargo_grid.validation import compare_reference

            if args.output.exists():
                raise ValueError(f"output file already exists: {args.output}; choose a new path")
            result = compare_reference(args.reference_file)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(json.dumps(result, indent=2) + "\n")
            print(
                f"reference comparison (joint-style original): {'PASS' if result['checked_pass'] else 'FAIL'}; {args.output}"
            )
            return 0 if result["checked_pass"] else 1
        if args.command == "trunk-blocker":
            from cargo_grid.trunk_blocker_export import export_trunk_blocker

            manifest = export_trunk_blocker(
                args.output,
                TrunkBlockerSpec(args.extension_mm, args.released_illustration),
            )
            print(manifest)
            print(
                "Generated complete native STEP, checked STL and core 3MF geometry; no slicer "
                "preset, physical fit or load qualification is claimed.",
                file=sys.stderr,
            )
            return 0
        hole_diameter = _resolved_hole_diameter(args)
        build = _build_volume(args)
        interface = _interface(args)
        roof_support = _roof_support(args)
        bambu = _bambu_settings(args, roof_support)
        stack = _stack_settings(args, bambu, build, interface)
        if args.command == "part":
            count("copy count", args.copy_count)
            cells = _part_dimensions(args)
            variant = _part_option(
                args,
                "variant_number",
                {"corner-in", "corner-out", "support-end"},
                1,
            )
            length = _part_option(
                args,
                "connector_length_mm",
                {"support-bit"},
                60,
            )
            stop_height = _part_option(
                args,
                "stop_height_mm",
                {"lock-45", "vertical-stop"},
                60 if args.family == "vertical-stop" else 50,
            )
            panel_height = _part_option(
                args,
                "panel_height_cells",
                {"vertical-tile-bracket"},
                None,
            )
            edge_outward = _part_option(
                args,
                "edge_outward_mm",
                set(EDGE_FAMILIES),
                10.0,
            )
            complete_edge_holes = _part_option(
                args,
                "complete_edge_holes",
                set(EDGE_FAMILIES),
                None,
            )
            ramp_join = _part_option(args, "ramp_join", {"ramp"}, "female")
            rod_height = _part_option(args, "rod_height_mm", {"rod"}, 120.0)
            peg_diameter = _part_option(args, "peg_diameter_mm", {"rod"}, 10.0)
            brace_spacing = _part_option(args, "brace_spacing_mm", {"rod-brace"}, 60.0)
            bore_diameter = _part_option(args, "bore_diameter_mm", {"rod-brace"}, 10.0)
            if args.family == "tile":
                design = tile_design(
                    Tile(*cells, interface, hole_diameter, hole_scope=args.hole_scope)
                )
            elif args.family == "rod":
                if interface.fit_offset:
                    raise ValueError("rod uses --peg-diameter-mm, not --fit-offset-mm")
                design = accessory_design(Rod(rod_height, peg_diameter, interface.height))
            elif args.family == "rod-brace":
                if interface.fit_offset:
                    raise ValueError("rod-brace uses --bore-diameter-mm, not --fit-offset-mm")
                design = accessory_design(RodBrace(brace_spacing, bore_diameter))
            else:
                design = accessory_design(
                    Accessory(
                        args.family,
                        *cells,
                        variant,
                        length,
                        stop_height,
                        interface,
                        panel_height_cells=panel_height,
                        ramp_join=ramp_join,
                        edge_outward=edge_outward,
                        complete_edge_holes=complete_edge_holes,
                        edge_hole_diameter=(
                            hole_diameter if args.family in EDGE_FAMILIES else None
                        ),
                    )
                )
            design.quantity = args.copy_count
            print_size = design.bambu_size if bambu else design.size
            if build.placement(print_size) is None:
                raise ValueError(f"actual part bounds {print_size} exceed usable print area")
            job = Job([design], build, "part")
        elif args.command == "layout":
            layout = exact_layout(
                args.layout_width_mm,
                args.layout_depth_mm,
                build,
                interface=interface,
                distribution=args.filler_placement,
                hole_diameter=hole_diameter,
                hole_scope=args.hole_scope,
            )
            job = layout_job(layout, build)
        else:
            requested_gap = getattr(args, "packing_gap_mm", None)
            default_gap = (
                zeekr_7x_rear_review.H2D_REVIEW_GAP_MM
                if args.command == "extras" and args.vehicle == "zeekr-7x-rear-panel"
                else (H2D_DEFAULT_PART_CLEARANCE_MM if args.h2d_dual_safe else 2)
            )
            packing_gap = default_gap if requested_gap is None else requested_gap
            if args.h2d_dual_safe:
                if not bambu:
                    raise ValueError("--h2d-dual-safe requires --bambu")
                if (
                    (build.x, build.y, build.z) != (350, 320, 325)
                    or args.build_margin_mm
                    or args.build_reserve_width_mm
                    or args.build_reserve_depth_mm
                    or args.build_reserve_height_mm
                    or args.exclude_rectangle_mm
                ):
                    raise ValueError(
                        "--h2d-dual-safe requires the unmodified H2D build envelope: "
                        "--build-width-mm 350 --build-depth-mm 320 --build-height-mm 325"
                    )
                if interface != Interface():
                    raise ValueError(
                        "--h2d-dual-safe requires original joints, --unit-size-mm 60, "
                        "--tile-thickness-mm 13 and zero fit offset"
                    )
                positive("H2D packing gap", packing_gap)
            if args.command == "extras":
                if args.vehicle == "zeekr-7x-rear-panel":
                    if hole_diameter != DEFAULT_HOLE_DIAMETER_MM or args.hole_scope != "full":
                        raise ValueError(
                            "Zeekr rear-panel contour pieces require the standard "
                            "full-scope 10 mm hole pattern"
                        )
                    job = zeekr_7x_rear_review.rear_panel_job(
                        build,
                        interface=interface,
                        placement_build=(h2d_common_build() if args.h2d_dual_safe else None),
                        part_gap=packing_gap,
                    )
                else:
                    job = zeekr_7x.extras_job(
                        build,
                        interface=interface,
                        hole_diameter=hole_diameter,
                        hole_scope=args.hole_scope,
                        placement_build=(h2d_common_build() if args.h2d_dual_safe else None),
                        part_gap=packing_gap,
                    )
                if args.h2d_dual_safe:
                    job.placement_policy.update(
                        name="H2D dual-nozzle safe",
                        common_reach_mm={
                            "min_x": 25,
                            "max_x": 325,
                            "min_y": 0,
                            "max_y": 320,
                            "max_z": 320,
                        },
                        common_model_inset_mm=5,
                        minimum_actual_part_xy_clearance_mm=packing_gap,
                        clearance_measurement="model bounds on rectangle-packed plates",
                    )
            elif args.h2d_dual_safe:
                job = h2d_dual_safe_catalogue_job(
                    hole_diameter=hole_diameter,
                    hole_scope=args.hole_scope,
                    packing_gap=packing_gap,
                )
            else:
                job = catalogue_job(
                    build,
                    interface=interface,
                    hole_diameter=hole_diameter,
                    hole_scope=args.hole_scope,
                    orient_for_bambu=bool(bambu),
                )
        if not job.print_placements:
            job.part_gap = getattr(args, "packing_gap_mm", 2)
        manifest = export_job(job, args.output, stl=not args.no_stl, bambu=bambu, stack=stack)
        print(manifest)
        rejected = sum(not h["accepted"] for d in job.designs for h in d.holes)
        if rejected:
            print(
                f"WARNING: {rejected} round-hole placements rejected by keep-outs; see manifest.",
                file=sys.stderr,
            )
        if any(
            design.parameters.get("hole_diameter") is not None
            and design.holes
            and not any(hole["accepted"] for hole in design.holes)
            for design in job.designs
        ):
            print(
                "WARNING: no requested round holes fit at least one tile; "
                "use a smaller --hole-diameter-mm or --no-holes.",
                file=sys.stderr,
            )
        if job.omitted:
            print(
                f"WARNING: {len(job.omitted)} oversized accessories omitted; see manifest.",
                file=sys.stderr,
            )
        geometry_warning = interface.compatibility()["geometry_warning"]
        if geometry_warning and (args.command != "part" or args.family == "tile"):
            print(f"WARNING: {geometry_warning}", file=sys.stderr)
        if roof_support:
            print(
                "WARNING: Native roof supports are uncalibrated dual-material requests. "
                "Generated support may temporarily cross edge round cutouts; remove from the underside before assembly. "
                "Verify nozzle assignments, sliced support paths and physical release.",
                file=sys.stderr,
            )
            if roof_support.contact_mode == "zero-contact":
                print(
                    "WARNING: Zero roof contact assumes the explicitly selected PETG/PLA interface pair. "
                    "Do not reuse it for same-material support or an unverified material substitution; it may fuse.",
                    file=sys.stderr,
                )
        print(
            "Generated geometry is not a print preset: inspect slicer output and verify physical fit separately.",
            file=sys.stderr,
        )
        return 0
    except (ValueError, OSError) as error:
        p.exit(2, f"cargo-grid: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
