"""Rebuild the H2D project files published on MakerWorld.

Each variant is one ordinary ``cargo-grid`` command for every nozzle in
``cargo_grid.cli.H2D_PROFILES``: the H2D catalogue and the Zeekr 7X expansion set, with and
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

    uv run python tools/makerworld_exports.py --output FOLDER [--replace] [--jobs N]
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
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from cargo_grid.cli import H2D_PROFILES

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


def _h2d_arguments(profiles) -> list[str]:
    return [
        "--h2d-dual-safe",
        "--build-width-mm",
        "350",
        "--build-depth-mm",
        "320",
        "--build-height-mm",
        "325",
        "--bambu",
        "--material",
        profiles.petg,
        "PETG",
        PETG_COLOUR,
    ]


def _layer_arguments(nozzle: float, profiles) -> list[str]:
    return [
        "--nozzle-diameter-mm",
        f"{nozzle:g}",
        "--layer-height-mm",
        f"{profiles.layer_height_mm:g}",
    ]


def variants() -> list[Variant]:
    result = []
    for nozzle, profiles in sorted(H2D_PROFILES.items(), reverse=True):
        for command, title in SETS:
            for bottom in (0.0, SOLID_BOTTOM_MM):
                floor = f"_Solid-Bottom-{bottom:g}mm" if bottom else ""
                arguments = [
                    *command,
                    *_h2d_arguments(profiles),
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
                        f"CargoGrid_H2D_{title}{floor}_Auto-Support_{nozzle:g}mm-Nozzle",
                        tuple(arguments),
                        title,
                    )
                )
        for command, title in (HANDLE_SET, ADJUSTABLE_STOP_SET):
            result.append(
                Variant(
                    f"CargoGrid_H2D_{title}_{nozzle:g}mm-Nozzle",
                    (
                        *command,
                        *_h2d_arguments(profiles),
                        "--material",
                        profiles.pla,
                        "PLA",
                        PLA_COLOUR,
                        *_layer_arguments(nozzle, profiles),
                        "--roof-support",
                        "--roof-support-mode",
                        "auto",
                        "--no-stl",
                    ),
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


def slice_plate(bambu: Path, project: Path, plate: int, work: Path) -> str | None:
    """Slice one plate; return Bambu's complaint, or None when it slices cleanly."""
    log = work / f"slice-{plate}.log"
    arguments = ["--datadir", str(work / f"datadir-{plate}"), "--debug", "1", "--arrange", "0"]
    arguments += ["--orient", "0", "--slice", str(plate)]
    arguments += ["--export-3mf", str(work / f"sliced-{plate}.3mf"), str(project)]
    code = _run(bambu, arguments, log)
    (work / f"sliced-{plate}.3mf").unlink(missing_ok=True)
    if code == 0:
        return None
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
    selected = [variant for variant in variants() if not args.sets or variant.set_name in args.sets]
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
