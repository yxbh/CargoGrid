"""Rebuild the H2D and X2D project files for MakerWorld.

Each variant is one ordinary ``cargo-grid`` command for every nozzle in
the selected printer's profiles: the catalogue and the Zeekr 7X expansion set, with and
without the 1.92 mm solid bottom and with auto roof support (PETG and a PLA interface), and
the pull-handle set and the adjustable-stop kit with PETG and a PLA support interface. The
handles have no tile-edge roofs or floor; both inherit the global Auto support and PLA interface.
Every project is generated in a temporary folder first; the
output folder only receives ``<name>.3mf`` and ``<name>.manifest.json`` after every variant
has succeeded. Names put underscores between their parts and hyphens inside a part, for
example
``CargoGrid_H2D_Full-Catalogue_Solid-Bottom-1.92mm_Auto-Support_0.8mm-Nozzle``.

With ``--bambu-studio PATH`` every plate is also sliced by that Bambu Studio executable, once
with PETG on each nozzle, before anything is written. The check copy gets the full printer,
process and filament profiles from that installation and a fixed filament-to-nozzle map; the
written project is unchanged. Any G-code path conflict or unprintable plate stops the run.
MakerWorld re-slices uploads and rejects the same conflicts, which the Bambu Studio GUI only
shows as a warning on the plate being previewed.

``--set NAME`` builds only that set, for every nozzle; repeat it for more than one set.
``--printer x2d`` selects the 256 mm printer instead of the default H2D; repeat the option
with ``--printer h2d --printer x2d`` to build both.

    uv run python tools/makerworld_exports.py --output FOLDER [--replace] [--jobs N]
        [--printer h2d|x2d ...]
        [--set Full-Catalogue|Zeekr-7X-Expansion-Set|Pull-Handle-Set|Adjustable-Stop ...]
        [--bambu-studio /Applications/BambuStudio.app/Contents/MacOS/BambuStudio]
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from math import atan2, cos, hypot, pi, sin
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from cargo_grid.printers import H2D, PRINTERS, DualNozzlePrinter, NozzleProfiles

PETG_COLOUR = "#688197"
PLA_COLOUR = "#0A2989"
SOLID_BOTTOM_MM = 1.92
SETS = ((("catalogue",), "Full-Catalogue"), (("extras", "zeekr-7x"), "Zeekr-7X-Expansion-Set"))
HANDLE_SET = (("extras", "pull-handle"), "Pull-Handle-Set")
ADJUSTABLE_STOP_SET = (("extras", "adjustable-stop"), "Adjustable-Stop")
SET_NAMES = tuple(title for _, title in (*SETS, HANDLE_SET, ADJUSTABLE_STOP_SET))
SUFFIXES = {"job.3mf": ".3mf", "manifest.json": ".manifest.json"}


@dataclass(frozen=True)
class Variant:
    name: str
    arguments: tuple[str, ...]
    set_name: str


def _printer_arguments(printer: DualNozzlePrinter, profiles: NozzleProfiles) -> list[str]:
    return [
        *(["--h2d-dual-safe"] if printer is H2D else ["--printer", printer.name.lower()]),
        "--build-width-mm",
        f"{printer.build.x:g}",
        "--build-depth-mm",
        f"{printer.build.y:g}",
        "--build-height-mm",
        f"{printer.build.z:g}",
        "--bambu",
        "--material",
        profiles.petg,
        "PETG",
        PETG_COLOUR,
    ]


def _layer_arguments(nozzle: float, profiles: NozzleProfiles) -> list[str]:
    return [
        "--nozzle-diameter-mm",
        f"{nozzle:g}",
        "--layer-height-mm",
        f"{profiles.layer_height_mm:g}",
    ]


def variants(printers: tuple[str, ...] = ("h2d",)) -> list[Variant]:
    result = []
    for key in dict.fromkeys(printers):
        printer = PRINTERS[key]
        for nozzle, profiles in sorted(printer.profiles.items(), reverse=True):
            for command, title in (*SETS, HANDLE_SET, ADJUSTABLE_STOP_SET):
                tiled_set = (command, title) in SETS
                for bottom in (0.0, SOLID_BOTTOM_MM) if tiled_set else (0.0,):
                    floor = f"_Solid-Bottom-{bottom:g}mm" if bottom else ""
                    support = "_Auto-Support" if tiled_set else ""
                    arguments = [
                        *command,
                        *_printer_arguments(printer, profiles),
                        "--material",
                        profiles.pla,
                        "PLA",
                        PLA_COLOUR,
                        *_layer_arguments(nozzle, profiles),
                        "--roof-support",
                        "--roof-support-mode",
                        "auto",
                        "--no-stl",
                    ]
                    if bottom:
                        arguments += ["--solid-bottom-thickness-mm", f"{bottom:g}"]
                    result.append(
                        Variant(
                            f"CargoGrid_{printer.name}_{title}{floor}{support}_{nozzle:g}mm-Nozzle",
                            tuple(arguments),
                            title,
                        )
                    )
    return result


def destinations(output: Path, variant: Variant) -> dict[str, Path]:
    return {source: output / f"{variant.name}{suffix}" for source, suffix in SUFFIXES.items()}


def generate(variant: Variant, scratch: Path) -> Path:
    folder = scratch / variant.name
    log = scratch / f"{variant.name}.log"
    with log.open("w") as handle:
        result = subprocess.run(
            [sys.executable, "-m", "cargo_grid", *variant.arguments, "--output", str(folder)],
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode:
        tail = "".join(log.read_text().splitlines(keepends=True)[-20:])
        raise RuntimeError(f"{variant.name} failed ({result.returncode}):\n{tail}")
    return folder


# Bambu stores resolved filament-to-nozzle maps in the project; the check sets its own.
PROFILE_CACHES = {"filament_map", "filament_map_2", "filament_nozzle_map", "filament_volume_map"}
# PETG then PLA: on the left (1) and right (2) nozzle, then the other way round.
NOZZLE_MAPS = (("1", "2"), ("2", "1"))
# Messages the CLI logs as errors on plates it slices successfully.
PROBLEMS = re.compile(
    r"gcode path conflicts found[^\n]*|found gcode unprintable[^\n]*|error_message=[^\n]*"
)


def _profiles(bambu: Path) -> Path:
    for root in (bambu.parents[1] / "Resources", bambu.parent / "resources"):
        if (root / "profiles" / "BBL").is_dir():
            return root / "profiles" / "BBL"
    raise ValueError(f"no Bambu profiles found next to {bambu}")


def _resolve(root: Path, kind: str, name: str, seen: tuple[Path, ...] = ()) -> dict:
    path = root / kind / f"{name}.json"
    if path in seen:
        raise ValueError(f"Bambu profile inheritance cycle at {path.name}")
    source = json.loads(path.read_text())
    result = (
        _resolve(root, kind, source["inherits"], (*seen, path)) if source.get("inherits") else {}
    )
    for include in source.get("include", []):
        result.update(_resolve(root, kind, include, (*seen, path)))
    result.update(source)
    result.pop("inherits", None)
    result.pop("include", None)
    return result


def _run(bambu: Path, arguments: list[str], log: Path) -> int:
    with log.open("w") as handle:
        return subprocess.run(
            [str(bambu), *arguments], stdout=handle, stderr=subprocess.STDOUT, check=False
        ).returncode


def effective_settings(project: Path, bambu: Path, work: Path) -> dict:
    """The project's settings on top of the full profiles it names, as Bambu Studio resolves them."""
    with ZipFile(project) as archive:
        generated = json.loads(archive.read("Metadata/project_settings.config"))
    root = _profiles(bambu)
    paths = []
    for kind, name in (
        ("machine", generated["printer_settings_id"]),
        ("process", generated["print_settings_id"]),
        *(("filament", name) for name in generated["filament_settings_id"]),
    ):
        path = work / f"{kind}-{len(paths)}.json"
        path.write_text(json.dumps(_resolve(root, kind, name)))
        paths.append(path)
    exported = work / "effective.json"
    arguments = ["--datadir", str(work / "datadir"), "--debug", "1", "--arrange", "0"]
    arguments += ["--orient", "0", "--load-settings", f"{paths[0]};{paths[1]}"]
    arguments += ["--load-filaments", ";".join(map(str, paths[2:])), "--export-settings"]
    if _run(bambu, [*arguments, str(exported)], work / "resolve.log"):
        raise RuntimeError(f"Bambu Studio could not resolve {project.name}'s profiles")
    effective = json.loads(exported.read_text())
    for key in PROFILE_CACHES:
        effective.pop(key, None)
    effective.update(generated)
    return effective


def check_copy(project: Path, effective: dict, mapping: tuple[str, ...], target: Path) -> int:
    """Write a sliceable copy with a fixed nozzle map; return its plate count."""
    settings = dict(effective)
    count = len(settings["filament_settings_id"])
    extruders = len(settings["nozzle_diameter"])
    mapping = tuple(mapping[:count])
    settings.update(
        filament_map_mode="Manual",
        filament_map=list(mapping),
        filament_map_2=list(mapping),
        filament_nozzle_map=list(mapping),
        filament_volume_map=["0"] * count,
        extruder_nozzle_stats=["Standard#1"] * extruders,
        # The CLI needs a purge table sized for these filaments on every nozzle.
        flush_volumes_matrix=[
            "0" if row == column else "280"
            for _ in range(extruders)
            for row in range(count)
            for column in range(count)
        ],
        flush_volumes_vector=["140"] * (2 * count),
        flush_multiplier=["1"] * extruders,
        flush_multiplier_fast=["1.2"] * extruders,
    )
    plates = 0
    with ZipFile(project) as original, ZipFile(target, "w", ZIP_DEFLATED) as copy:
        for entry in original.infolist():
            data = original.read(entry)
            if entry.filename == "Metadata/project_settings.config":
                data = json.dumps(settings, indent=2).encode()
            elif entry.filename == "Metadata/model_settings.config":
                text = data.decode()
                plates = text.count("<plate>")
                text = text.replace(
                    '<metadata key="filament_map_mode" value="Auto For Match" />',
                    '<metadata key="filament_map_mode" value="Manual" />'
                    f'<metadata key="filament_maps" value="{" ".join(mapping)}" />',
                )
                data = text.encode()
            copy.writestr(entry, data)
    return plates


def extrusion_bounds(gcode: str) -> tuple[float, ...] | None:
    """Bound generated extrusion, including line width and XY arc extrema, not startup macros."""
    x = y = z = e = width = 0.0
    absolute_e, absolute_xyz = False, True
    role, bounds = "unknown", None
    for line in gcode.splitlines():
        if line.startswith("; FEATURE:"):
            role = line.split(":", 1)[1].strip()
        elif line.startswith("; LINE_WIDTH:"):
            width = float(line.split(":", 1)[1])
        command = line.split(";", 1)[0].strip()
        operation = command.split(maxsplit=1)[0] if command else ""
        if operation in ("M82", "M83"):
            absolute_e = operation == "M82"
        elif operation in ("G90", "G91"):
            absolute_xyz = operation == "G90"
        fields = {
            key: float(value)
            for key, value in re.findall(r"([XYZEIJ])([+-]?(?:\d+(?:\.\d*)?|\.\d+))", command)
        }
        if operation == "G92":
            x, y, z, e = (fields.get(key, value) for key, value in zip("XYZE", (x, y, z, e)))
        elif operation in ("G0", "G1", "G2", "G3"):
            nx, ny, nz = (
                fields.get(key, value) if absolute_xyz else value + fields.get(key, 0)
                for key, value in zip("XYZ", (x, y, z))
            )
            extrusion = fields.get("E", e if absolute_e else 0)
            delta = extrusion - e if absolute_e else extrusion
            if (
                delta > 0
                and role not in ("unknown", "Custom")
                and (nx != x or ny != y or "I" in fields or "J" in fields)
            ):
                if width <= 0:
                    raise ValueError("generated extrusion has no positive line width")
                points = [(x, y), (nx, ny)]
                if operation in ("G2", "G3"):
                    cx, cy = x + fields.get("I", 0), y + fields.get("J", 0)
                    radius = hypot(x - cx, y - cy)
                    start, end = atan2(y - cy, x - cx), atan2(ny - cy, nx - cx)
                    direction = -1 if operation == "G2" else 1
                    sweep = (direction * (end - start)) % (2 * pi)
                    if (x, y) == (nx, ny):
                        sweep = 2 * pi
                    for angle in (0, pi / 2, pi, 3 * pi / 2):
                        if (direction * (angle - start)) % (2 * pi) <= sweep + 1e-9:
                            points.append((cx + radius * cos(angle), cy + radius * sin(angle)))
                box = (
                    min(p[0] for p in points) - width / 2,
                    min(p[1] for p in points) - width / 2,
                    min(z, nz),
                    max(p[0] for p in points) + width / 2,
                    max(p[1] for p in points) + width / 2,
                    max(z, nz),
                )
                bounds = (
                    tuple(min(a, b) for a, b in zip(bounds[:3], box[:3]))
                    + tuple(max(a, b) for a, b in zip(bounds[3:], box[3:]))
                    if bounds is not None
                    else box
                )
            x, y, z = nx, ny, nz
            if absolute_e:
                e = extrusion
    return bounds


def extrusion_problem(sliced: Path) -> str | None:
    with ZipFile(sliced) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        printer = next(p for p in PRINTERS.values() if p.model == settings["printer_model"])
        names = [name for name in archive.namelist() if name.endswith(".gcode")]
        if len(names) != 1:
            return f"expected one sliced plate, found {len(names)} G-code files"
        bounds = extrusion_bounds(archive.read(names[0]).decode())
    if bounds is None:
        return "sliced plate has no generated extrusion"
    x0, y0, z0, x1, y1, z1 = bounds
    if (
        x0 < printer.common_x_mm[0]
        or y0 < 0
        or z0 < 0
        or x1 > printer.common_x_mm[1]
        or y1 > printer.build.y
        or z1 > printer.common_height_mm
    ):
        return f"all-layer extrusion bounds {bounds} exceed {printer.name} common reach/bed"
    return None


def slice_plate(bambu: Path, project: Path, plate: int, work: Path) -> str | None:
    """Slice one plate; return Bambu's complaint, or None when it slices cleanly."""
    log = work / f"slice-{plate}.log"
    arguments = ["--datadir", str(work / f"datadir-{plate}"), "--debug", "1", "--arrange", "0"]
    arguments += ["--orient", "0", "--slice", str(plate)]
    arguments += ["--export-3mf", str(work / f"sliced-{plate}.3mf"), str(project)]
    code = _run(bambu, arguments, log)
    if code == 0:
        problem = extrusion_problem(work / f"sliced-{plate}.3mf")
        (work / f"sliced-{plate}.3mf").unlink()
        return problem
    (work / f"sliced-{plate}.3mf").unlink(missing_ok=True)
    found = PROBLEMS.findall(log.read_text(errors="replace"))
    return found[0].strip() if found else f"Bambu Studio exited with code {code}"


def bambu_problems(project: Path, bambu: Path, work: Path, jobs: int) -> list[str]:
    """Slice every plate with PETG on each nozzle; list what Bambu Studio refuses."""
    work.mkdir(parents=True)
    effective = effective_settings(project, bambu, work)
    tasks = []
    for mapping in NOZZLE_MAPS:
        folder = work / f"petg-nozzle-{mapping[0]}"
        folder.mkdir()
        copy = folder / "check.3mf"
        plates = check_copy(project, effective, mapping, copy)
        tasks += [(mapping, copy, plate, folder) for plate in range(1, plates + 1)]
    with ThreadPoolExecutor(jobs) as pool:
        results = list(pool.map(lambda task: slice_plate(bambu, *task[1:]), tasks))
    side = {"1": "left", "2": "right"}
    return [
        f"plate {plate} with PETG on the {side[mapping[0]]} nozzle: {problem}"
        for (mapping, _, plate, _), problem in zip(tasks, results)
        if problem
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, required=True, help="folder for the named files")
    parser.add_argument(
        "--replace", action="store_true", help="replace files with the same names in --output"
    )
    parser.add_argument("--jobs", type=int, default=4, help="variants generated at once")
    parser.add_argument(
        "--printer",
        dest="printers",
        action="append",
        choices=PRINTERS,
        help="build this printer's sets; repeat for both; default h2d",
    )
    parser.add_argument(
        "--set",
        dest="sets",
        action="append",
        choices=SET_NAMES,
        help="build only this set, for every nozzle; repeat for more sets; default every set",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list each file and its command without running"
    )
    parser.add_argument(
        "--bambu-studio",
        type=Path,
        help="Bambu Studio executable used to slice every plate before anything is written",
    )
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    selected = [
        variant
        for variant in variants(tuple(args.printers or ("h2d",)))
        if not args.sets or variant.set_name in args.sets
    ]
    if args.dry_run:
        for variant in selected:
            print(f"{variant.name}: cargo-grid {' '.join(variant.arguments)}")
        return 0
    if not args.output.is_dir():
        parser.error(f"--output must be an existing folder: {args.output}")
    if args.bambu_studio is not None and not args.bambu_studio.is_file():
        parser.error(f"--bambu-studio must be the Bambu Studio executable: {args.bambu_studio}")
    existing = [
        path
        for variant in selected
        for path in destinations(args.output, variant).values()
        if path.exists()
    ]
    if existing and not args.replace:
        parser.error(
            f"{len(existing)} files already exist, such as {existing[0].name}; "
            "pass --replace to overwrite them"
        )
    with tempfile.TemporaryDirectory(prefix="cargo-grid-makerworld-") as temporary:
        scratch = Path(temporary)
        with ThreadPoolExecutor(args.jobs) as pool:
            folders = list(pool.map(lambda variant: generate(variant, scratch), selected))
        if args.bambu_studio is not None:
            bambu = args.bambu_studio.resolve()
            problems = []
            for variant, folder in zip(selected, folders):
                found = bambu_problems(
                    folder / "job.3mf", bambu, scratch / f"{variant.name}-check", args.jobs
                )
                result = f"{len(found)} plate slices refused" if found else "every plate sliced"
                print(f"{variant.name}: Bambu Studio check, {result}")
                problems += [f"{variant.name}: {problem}" for problem in found]
            if problems:
                raise RuntimeError(
                    "Bambu Studio refused these plates; nothing was written:\n"
                    + "\n".join(problems)
                )
        for variant, folder in zip(selected, folders):
            for source, destination in destinations(args.output, variant).items():
                shutil.move(folder / source, destination)
            export = json.loads(destinations(args.output, variant)["manifest.json"].read_text())
            plates = export["export"]["plates"]
            parts = sum(item["quantity"] for plate in plates for item in plate["items"])
            print(f"{variant.name}: {len(plates)} plates, {parts} parts")
            if export.get("omitted"):
                print(
                    f"  {len(export['omitted'])} designs omitted; see {variant.name}.manifest.json"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
