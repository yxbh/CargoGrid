"""Rebuild the H2D project files published on MakerWorld.

Each variant is one ordinary ``cargo-grid`` command: the H2D catalogue and the Zeekr 7X
expansion set, with and without the 1.92 mm solid bottom, for every nozzle in
``cargo_grid.cli.H2D_PROFILES``. All use auto roof support with PETG and a PLA interface.
Every project is generated in a temporary folder first; the output folder only receives
``<name>.3mf`` and ``<name>.manifest.json`` after every variant has succeeded. Names put
underscores between their parts and hyphens inside a part, for example
``CargoGrid_H2D_Full-Catalogue_Solid-Bottom-1.92mm_Auto-Support_0.8mm-Nozzle``.

    uv run python tools/makerworld_exports.py --output FOLDER [--replace] [--jobs N]
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from cargo_grid.cli import H2D_PROFILES

PETG_COLOUR = "#688197"
PLA_COLOUR = "#0A2989"
SOLID_BOTTOM_MM = 1.92
SETS = ((("catalogue",), "Full-Catalogue"), (("extras", "zeekr-7x"), "Zeekr-7X-Expansion-Set"))
SUFFIXES = {"job.3mf": ".3mf", "manifest.json": ".manifest.json"}


@dataclass(frozen=True)
class Variant:
    name: str
    arguments: tuple[str, ...]


def variants() -> list[Variant]:
    result = []
    for nozzle, profiles in sorted(H2D_PROFILES.items(), reverse=True):
        for command, title in SETS:
            for bottom in (0.0, SOLID_BOTTOM_MM):
                floor = f"_Solid-Bottom-{bottom:g}mm" if bottom else ""
                arguments = [
                    *command,
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
                    "--material",
                    profiles.pla,
                    "PLA",
                    PLA_COLOUR,
                    "--nozzle-diameter-mm",
                    f"{nozzle:g}",
                    "--layer-height-mm",
                    f"{profiles.layer_height_mm:g}",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, required=True, help="folder for the named files")
    parser.add_argument(
        "--replace", action="store_true", help="replace files with the same names in --output"
    )
    parser.add_argument("--jobs", type=int, default=4, help="variants generated at once")
    parser.add_argument(
        "--dry-run", action="store_true", help="list each file and its command without running"
    )
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    selected = variants()
    if args.dry_run:
        for variant in selected:
            print(f"{variant.name}: cargo-grid {' '.join(variant.arguments)}")
        return 0
    if not args.output.is_dir():
        parser.error(f"--output must be an existing folder: {args.output}")
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
