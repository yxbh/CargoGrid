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
with PETG on each nozzle, before anything is written. A variant's plates are sliced as soon as
it is generated, on one pool of slices shared by every variant. Slices and generation share
the same memory budget; ``--slice-jobs`` limits how many slices run at once. The check copy
gets the full printer,
process and filament profiles from that installation and a fixed filament-to-nozzle map; the
written project is unchanged. Any G-code path conflict or unprintable plate stops the run.
Generated extrusion must stay inside the bed; only support features have a small XY
allowance beyond common nozzle reach. Rectangular towers must also stay inside the
manifest's reserved footprint.
MakerWorld re-slices uploads and rejects the same conflicts, which the Bambu Studio GUI only
shows as a warning on the plate being previewed.

Plates that slice cleanly are remembered in ``.local/makerworld/slice-cache``, keyed by
everything that decides the result: the plate's objects, meshes and placements, the project
and check settings, the nozzle map, the reserved tower footprint, the Bambu Studio executable
and profile files, and this checking code. An identical plate is not sliced again, whether it
comes from another variant in the same run or from an earlier run. Refused plates are always
sliced again. ``--no-slice-cache`` slices every plate and leaves the cache alone; the folder can
be deleted at any time.

Each finished variant, its manifest and a clean check result are kept in
``.local/makerworld/staging``, under a key made from the generator source files, the code in
this script that lists and generates variants, the Python environment and the variant's
arguments. A rerun after a crash or a refused plate
reuses them instead of building those variants again; ``--no-resume`` builds every variant
again. Staged builds from other source files are removed at the start of a run. The named
files are still written only once every selected variant is present and checked.

``--publish FOLDER`` writes into an existing folder, such as the one the MakerWorld files are
uploaded from, instead of ``--output``. It needs ``--bambu-studio``. Files with identical bytes
are left alone; each replaced file is moved into ``old-<date>`` in that folder first, dated by
the replaced files, and a line per file says what changed: which plates and designs in a
project, which fields in a manifest. Nothing is uploaded.

``--set NAME`` builds only that set, for every nozzle; repeat it for more than one set.
``--printer x2d`` selects the 256 mm printer instead of the default H2D; repeat the option
with ``--printer h2d --printer x2d`` to build both.

    uv run python tools/makerworld_exports.py (--output FOLDER [--replace] | --publish FOLDER)
        [--jobs N]
        [--printer h2d|x2d ...]
        [--set Full-Catalogue|Zeekr-7X-Expansion-Set|Pull-Handle-Set|Adjustable-Stop ...]
        [--bambu-studio /Applications/BambuStudio.app/Contents/MacOS/BambuStudio]
        [--slice-jobs N] [--no-slice-cache] [--no-resume]
"""

import argparse
import ast
import filecmp
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from pathlib import Path
from zipfile import ZIP64_LIMIT, ZIP_DEFLATED, BadZipFile, ZipFile

from publish_guard import manifest_changes
from test_selection import environment

import cargo_grid
from cargo_grid import parameters, printers, sliced_extrusion
from cargo_grid.footprints import PACKING_WORKERS_ENV
from cargo_grid.printers import PRINTERS, DualNozzlePrinter, NozzleProfiles, bambu_flush_settings
from cargo_grid.sliced_extrusion import reach_problem

PETG_COLOUR = "#688197"
PLA_COLOUR = "#0A2989"
SOLID_BOTTOM_MM = 1.92
SETS = ((("catalogue",), "Full-Catalogue"), (("extras", "zeekr-7x"), "Zeekr-7X-Expansion-Set"))
HANDLE_SET = (("extras", "pull-handle"), "Pull-Handle-Set")
ADJUSTABLE_STOP_SET = (("extras", "adjustable-stop"), "Adjustable-Stop")
SET_NAMES = tuple(title for _, title in (*SETS, HANDLE_SET, ADJUSTABLE_STOP_SET))
SUFFIXES = {"job.3mf": ".3mf", "manifest.json": ".manifest.json"}
# Budget for one generator, search worker or Bambu Studio process. A slice of an H2D
# catalogue plate, the largest project, peaked at 2.4 GB with Bambu Studio 02.08.02.61.
PROCESS_MEMORY_BYTES = 3 * 1024**3
# Local state: the slice cache and staged variants. Tests point this elsewhere.
STATE_ENV = "CARGO_GRID_MAKERWORLD_STATE"
CACHE_FORMAT = 1
STAGING_FORMAT = 1
# Modules whose code decides a check result, and where this script's own check code starts.
CHECK_MODULES = (parameters, printers, sliced_extrusion)
CHECK_ROOTS = ("bambu_check", "Slicer", "SliceCache", "effective_settings", "CACHE_FORMAT")
# Where this script's code that decides a generated variant starts.
GENERATION_ROOTS = ("variants", "generate", "staged_name", "STAGING_FORMAT")


def state_folder() -> Path:
    return Path(
        os.environ.get(STATE_ENV) or Path(__file__).resolve().parents[1] / ".local" / "makerworld"
    )


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _available_memory_bytes() -> int:
    if sys.platform == "darwin":
        stats = subprocess.check_output(["vm_stat"], text=True)
        page_size = int(re.search(r"page size of (\d+) bytes", stats)[1])
        pages = sum(
            int(value)
            for value in re.findall(r"Pages (?:free|inactive|speculative):\s+(\d+)", stats)
        )
        return page_size * pages
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.DWORD),
                ("load", wintypes.DWORD),
                *(
                    (name, ctypes.c_ulonglong)
                    for name in (
                        "total",
                        "available",
                        "page_total",
                        "page_available",
                        "virtual_total",
                        "virtual_available",
                        "extended",
                    )
                ),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ctypes.WinError()
        return status.available
    values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return int(values["MemAvailable"].split()[0]) * 1024


def _process_capacity() -> tuple[int, int]:
    cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    cores = max(1, (cores or 1) - 2)
    memory = _available_memory_bytes()
    usable = max(0, memory - max(2 * 1024**3, memory // 4))
    return cores, max(1, usable // PROCESS_MEMORY_BYTES)


class Capacity:
    """Memory slots shared by variant generation and Bambu Studio slices.

    Generation reserves its slots for the variants it is building or still has to build; a
    slice starts only when one of the remaining slots is free.
    """

    def __init__(self, slots: int):
        self.slots = max(1, slots)
        self._reserved = 0
        self._running = 0
        self._closed = False
        self._condition = threading.Condition()

    def reserve(self, count: int) -> None:
        with self._condition:
            self._reserved = count
            self._condition.notify_all()

    def close(self) -> None:
        """Refuse slices that have not started, after a failure elsewhere in the run."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @contextmanager
    def slot(self):
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed or self._reserved + self._running < self.slots
            )
            if self._closed:
                raise RuntimeError("the run stopped before this slice started")
            self._running += 1
        try:
            yield
        finally:
            with self._condition:
                self._running -= 1
                self._condition.notify_all()


@dataclass(frozen=True)
class Variant:
    name: str
    arguments: tuple[str, ...]
    set_name: str


def _printer_arguments(printer: DualNozzlePrinter, profiles: NozzleProfiles) -> list[str]:
    return [
        "--printer",
        printer.name.lower(),
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
                        "--auto-roof-support",
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


def generate(variant: Variant, scratch: Path, *, packing_workers: int = 1) -> Path:
    folder = scratch / variant.name
    log = scratch / f"{variant.name}.log"
    with log.open("w") as handle:
        result = subprocess.run(
            [sys.executable, "-m", "cargo_grid", *variant.arguments, "--output", str(folder)],
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
            env={**os.environ, PACKING_WORKERS_ENV: str(packing_workers)},
        )
    if result.returncode:
        tail = "".join(log.read_text().splitlines(keepends=True)[-20:])
        raise RuntimeError(f"{variant.name} failed ({result.returncode}):\n{tail}")
    return folder


# Bambu stores resolved filament-to-nozzle maps in the project; the check sets its own.
PROFILE_CACHES = {"filament_map", "filament_map_2", "filament_nozzle_map", "filament_volume_map"}
# PETG then PLA in slicer slots 1/2, then swapped; display names are printer-specific.
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


def check_settings(effective: dict, mapping: tuple[str, ...]) -> str:
    """The check copy's project settings: the effective settings with a fixed nozzle map."""
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
    )
    settings.update(bambu_flush_settings(count, extruders))
    return json.dumps(settings, indent=2)


def check_copy(project: Path, effective: dict, mapping: tuple[str, ...], target: Path) -> int:
    """Write a sliceable copy with a fixed nozzle map; return its plate count."""
    settings = check_settings(effective, mapping)
    mapping = tuple(mapping[: len(effective["filament_settings_id"])])
    plates = 0
    # The copy is only read by Bambu Studio, so the large model is streamed at a fast level.
    with (
        ZipFile(project) as original,
        ZipFile(target, "w", ZIP_DEFLATED, compresslevel=1) as copy,
    ):
        for entry in original.infolist():
            if entry.filename == "Metadata/project_settings.config":
                copy.writestr(entry.filename, settings)
            elif entry.filename == "Metadata/model_settings.config":
                text = original.read(entry).decode()
                plates = text.count("<plate>")
                copy.writestr(
                    entry.filename,
                    text.replace(
                        '<metadata key="filament_map_mode" value="Auto For Match" />',
                        '<metadata key="filament_map_mode" value="Manual" />'
                        f'<metadata key="filament_maps" value="{" ".join(mapping)}" />',
                    ),
                )
            else:
                with (
                    original.open(entry) as source,
                    copy.open(
                        entry.filename, "w", force_zip64=entry.file_size >= ZIP64_LIMIT
                    ) as destination,
                ):
                    shutil.copyfileobj(source, destination, 16 * 1024**2)
    return plates


def extrusion_problem(sliced: Path, reserved_tower: tuple[float, ...] | None = None) -> str | None:
    with ZipFile(sliced) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        printer = next(p for p in PRINTERS.values() if p.model == settings["printer_model"])
        names = [name for name in archive.namelist() if name.endswith(".gcode")]
        if len(names) != 1:
            return f"expected one sliced plate, found {len(names)} G-code files"
        gcode = archive.read(names[0]).decode()
    return reach_problem(gcode, printer, reserved_tower)


def slice_plate(
    bambu: Path,
    project: Path,
    plate: int,
    work: Path,
    reserved_tower: tuple[float, ...] | None = None,
) -> str | None:
    """Slice one plate; return Bambu's complaint, or None when it slices cleanly."""
    log = work / f"slice-{plate}.log"
    arguments = ["--datadir", str(work / f"datadir-{plate}"), "--debug", "1", "--arrange", "0"]
    arguments += ["--orient", "0", "--slice", str(plate)]
    arguments += ["--export-3mf", str(work / f"sliced-{plate}.3mf"), str(project)]
    code = _run(bambu, arguments, log)
    if code == 0:
        problem = extrusion_problem(work / f"sliced-{plate}.3mf", reserved_tower)
        (work / f"sliced-{plate}.3mf").unlink()
        return problem
    (work / f"sliced-{plate}.3mf").unlink(missing_ok=True)
    found = PROBLEMS.findall(log.read_text(errors="replace"))
    return found[0].strip() if found else f"Bambu Studio exited with code {code}"


def check_code() -> str:
    """Source of the definitions in this script that a check result depends on.

    Editing the variant list or how files are written keeps cached plates.
    """
    return script_code(CHECK_ROOTS)


def script_code(roots: tuple[str, ...]) -> str:
    """Source of the definitions in this script reached from ``roots`` by the names they use."""
    source = Path(__file__).read_text()
    definitions = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                definitions.update(
                    (name.id, node) for name in ast.walk(target) if isinstance(name, ast.Name)
                )
    pending, used = list(roots), {}
    while pending:
        name = pending.pop()
        if name in used or name not in definitions:
            continue
        used[name] = definitions[name]
        pending += [node.id for node in ast.walk(used[name]) if isinstance(node, ast.Name)]
    return "\n".join(ast.get_source_segment(source, used[name]) for name in sorted(used))


def checker_identity(bambu: Path) -> str:
    """Digest of the Bambu Studio executable, its profile files and the checking code."""
    resources = _profiles(bambu).parents[1]
    hasher = hashlib.sha256(f"{sys.version}\0{check_code()}\n".encode())
    named = [("executable", bambu)]
    info = bambu.parents[1] / "Info.plist"
    if info.is_file():
        named.append(("Info.plist", info))
    for folder in (resources / "profiles", resources / "printers"):
        named += [
            (path.relative_to(resources).as_posix(), path)
            for path in sorted(folder.rglob("*"))
            if path.is_file()
        ]
    named += [(module.__name__, Path(module.__file__)) for module in CHECK_MODULES]
    for name, path in named:
        hasher.update(f"{name}\0{digest(path.read_bytes())}\n".encode())
    return hasher.hexdigest()


MODEL = "3D/3dmodel.model"
PLATE_SETTINGS = "Metadata/model_settings.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"
# Component lists are read from objects up to this size; a larger object must be a mesh.
WHOLE_OBJECT_BYTES = 1024**2
TAG_ID = re.compile(rb'\sid="([^"]+)"')
COMPONENT = re.compile(rb'<component\b[^>]*\bobjectid="([^"]+)"')
ITEM = re.compile(rb"<item\b[^>]*/>")
ITEM_OBJECT = re.compile(rb'\bobjectid="([^"]+)"')
OBJECT_SETTINGS = re.compile(r'<object id="([^"]+)">.*?</object>', re.S)
PLATE = re.compile(r"<plate>.*?</plate>", re.S)
INSTANCE_OBJECT = re.compile(r'<metadata key="object_id" value="([^"]+)" />')


def model_digests(stream) -> tuple[str, dict[str, str], dict[str, list[str]], dict[str, list]]:
    """Digest each resource object and build item of a 3MF model, and everything else.

    Streams the model, which holds every mesh of a catalogue. Returns the digest of the bytes
    outside objects and items, object digests and component ids by object id, and item digests
    by the object they place. Anything unexpected raises ``ValueError``.
    """
    rest = bytearray()
    objects, components = {}, {}
    buffer = b""
    current = None
    for chunk in chain(iter(lambda: stream.read(16 * 1024**2), b""), [b""]):
        final = not chunk
        buffer += chunk
        view = memoryview(buffer)
        position = 0
        while True:
            if current is None:
                start = buffer.find(b"<object", position)
                if start < 0:
                    # Keep a possible partial "<object" for the next chunk.
                    stop = len(buffer) if final else max(position, len(buffer) - 6)
                    rest += view[position:stop]
                    position = stop
                    break
                rest += view[position:start]
                position = start
                close = buffer.find(b">", start)
                if close < 0:
                    if final:
                        raise ValueError("unterminated <object> tag")
                    break
                identity = TAG_ID.search(buffer, start, close + 1)
                if buffer[start + 7 : start + 8] not in b" \t\r\n>" or identity is None:
                    raise ValueError("unexpected <object> tag")
                current = [identity[1].decode(), hashlib.sha256(), bytearray(), 0]
            end = buffer.find(b"</object>", position)
            if end < 0 and final:
                raise ValueError(f"unterminated object {current[0]}")
            stop = end + 9 if end >= 0 else max(position, len(buffer) - 8)
            identity, hasher, head, size = current
            hasher.update(view[position:stop])
            head += view[position : min(stop, position + max(0, WHOLE_OBJECT_BYTES - len(head)))]
            current[3] = size = size + stop - position
            position = stop
            if end < 0:
                break
            if identity in objects:
                raise ValueError(f"object {identity} appears twice")
            if size > WHOLE_OBJECT_BYTES and b"<component" in head:
                raise ValueError(f"object {identity} is too large to read its components")
            components[identity] = [match.decode() for match in COMPONENT.findall(head)]
            objects[identity] = hasher.hexdigest()
            current = None
        view.release()
        buffer = buffer[position:]
        if final:
            break
    rest = bytes(rest)
    items = {}
    for match in ITEM.finditer(rest):
        items.setdefault(ITEM_OBJECT.search(match[0])[1].decode(), []).append(digest(match[0]))
    if rest.count(b"<item") != sum(map(len, items.values())):
        raise ValueError("build items with child elements")
    return digest(ITEM.sub(b"", rest)), objects, components, items


def plate_fingerprints(project: Path) -> tuple[dict, list[dict]]:
    """What one plate's slice depends on in a project, apart from its project settings.

    Returns what every plate shares (other archive members, the model and plate settings
    outside objects and plates, and the plate count), then each plate's own settings block
    with the settings, meshes, components and placements of its objects.
    """
    with ZipFile(project) as archive:
        members = [
            (entry.filename, digest(archive.read(entry)))
            for entry in archive.infolist()
            if entry.filename not in (MODEL, PLATE_SETTINGS, PROJECT_SETTINGS)
        ]
        with archive.open(MODEL) as stream:
            model, objects, components, items = model_digests(stream)
        text = archive.read(PLATE_SETTINGS).decode()
    plates = PLATE.findall(text)
    if len(plates) != text.count("<plate>"):
        raise ValueError("nested or unterminated plates")
    settings = {match[1]: digest(match[0].encode()) for match in OBJECT_SETTINGS.finditer(text)}

    def meshes(identity: str, seen: tuple[str, ...] = ()) -> list[str]:
        if identity in seen or identity not in objects:
            raise ValueError(f"object {identity} is missing or contains itself")
        return [objects[identity]] + [
            value for child in components[identity] for value in meshes(child, (*seen, identity))
        ]

    shared = {
        "members": members,
        "model": model,
        "plate_settings": digest(PLATE.sub("", OBJECT_SETTINGS.sub("", text)).encode()),
        "plates": len(plates),
    }
    return shared, [
        {
            "plate": digest(plate.encode()),
            "objects": [
                {
                    "settings": settings.get(identity),
                    "meshes": meshes(identity),
                    "items": items.get(identity, []),
                }
                for identity in INSTANCE_OBJECT.findall(plate)
            ],
        }
        for plate in plates
    ]


def plate_keys(
    project: Path, effective: dict, reservations: dict, checker: str
) -> dict[tuple[str, ...], list[str]]:
    """Cache keys of every plate for each nozzle map, in plate order."""
    shared, plates = plate_fingerprints(project)
    keys = {}
    for mapping in NOZZLE_MAPS:
        settings = digest(check_settings(effective, mapping).encode())
        keys[mapping] = [
            digest(
                json.dumps(
                    {
                        "format": CACHE_FORMAT,
                        "checker": checker,
                        "check_settings": settings,
                        "nozzle_map": mapping,
                        "project": shared,
                        "plate": plate,
                        "reserved_tower": reservations.get(number),
                    },
                    sort_keys=True,
                ).encode()
            )
            for number, plate in enumerate(plates, 1)
        ]
    return keys


class SliceCache:
    """Clean plate slices from earlier runs, and plates already claimed in this run."""

    def __init__(self, folder: Path, checker: str):
        self.folder = folder
        self.checker = checker
        self._lock = threading.Lock()
        self._claimed: dict[str, Future] = {}

    def _path(self, key: str) -> Path:
        return self.folder / f"{key}.json"

    def claim(self, key: str) -> tuple[Future, str]:
        """The plate's result and where it comes from: cached, shared or new.

        Whoever gets ``new`` must slice the plate and pass its future to ``follow``, or call
        ``abandon``; other variants in this run wait for that result.
        """
        with self._lock:
            if key in self._claimed:
                return self._claimed[key], "shared"
            future = Future()
            try:
                stored = json.loads(self._path(key).read_text())
            except (OSError, ValueError):
                stored = None
            if stored and stored.get("key") == key and stored.get("result") == "clean":
                future.set_result(None)
                return future, "cached"
            self._claimed[key] = future
            return future, "new"

    def follow(self, key: str, sliced: Future, record: dict) -> None:
        claimed = self._claimed[key]

        def settle(done: Future) -> None:
            if done.cancelled():
                claimed.cancel()
            elif done.exception() is not None:
                claimed.set_exception(done.exception())
            else:
                if done.result() is None:
                    self._store(key, record)
                claimed.set_result(done.result())

        sliced.add_done_callback(settle)

    def abandon(self, key: str, error: BaseException) -> None:
        self._claimed[key].set_exception(error)

    def _store(self, key: str, record: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        temporary = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        temporary.write_text(
            json.dumps(
                {"key": key, "result": "clean", **record, "checked": datetime.now().isoformat()},
                indent=2,
            )
        )
        os.replace(temporary, path)


class Slicer:
    """One pool of Bambu Studio slices shared by every variant in a run."""

    def __init__(
        self,
        bambu: Path,
        pool: ThreadPoolExecutor,
        capacity: Capacity,
        cache: SliceCache | None = None,
        checker: str | None = None,
    ):
        self.bambu = bambu
        self.pool = pool
        self.capacity = capacity
        self.cache = cache
        self.checker = checker if cache is None else cache.checker

    def submit(self, project: Path, plate: int, work: Path, reserved_tower=None):
        return self.pool.submit(self._slice, project, plate, work, reserved_tower)

    def _slice(self, project: Path, plate: int, work: Path, reserved_tower):
        with self.capacity.slot():
            return slice_plate(self.bambu, project, plate, work, reserved_tower=reserved_tower)


@dataclass
class Check:
    """What the Bambu Studio check found for one project."""

    problems: list[str]
    slices: int = 0
    cached: int = 0
    shared: int = 0


def bambu_check(
    project: Path,
    work: Path,
    slicer: Slicer,
    tower_bounds: dict[int, tuple[float, ...]] | None = None,
) -> Check:
    """Slice every plate with PETG on each nozzle, reusing clean results the cache holds."""
    work.mkdir(parents=True)
    effective = effective_settings(project, slicer.bambu, work)
    printer = next(p for p in PRINTERS.values() if p.model == effective["printer_model"])
    reservations = tower_bounds or {}
    keys = None
    if slicer.cache is not None:
        try:
            keys = plate_keys(project, effective, reservations, slicer.cache.checker)
        except ValueError as error:
            print(f"{project}: slicing every plate without the cache: {error}", flush=True)
    result = Check([])
    tasks = []
    for mapping in NOZZLE_MAPS:
        folder = work / f"petg-nozzle-{mapping[0]}"
        folder.mkdir()
        copy = folder / "check.3mf"
        if keys is None:
            plates = check_copy(project, effective, mapping, copy)
            tasks += [
                (mapping, plate, slicer.submit(copy, plate, folder, reservations.get(plate)))
                for plate in range(1, plates + 1)
            ]
            continue
        copied = False
        for plate, key in enumerate(keys[mapping], 1):
            future, origin = slicer.cache.claim(key)
            if origin == "new":
                try:
                    if not copied:
                        check_copy(project, effective, mapping, copy)
                        copied = True
                    sliced = slicer.submit(copy, plate, folder, reservations.get(plate))
                except BaseException as error:
                    slicer.cache.abandon(key, error)
                    raise
                slicer.cache.follow(key, sliced, {"plate": plate, "nozzle_map": list(mapping)})
            result.cached += origin == "cached"
            result.shared += origin == "shared"
            tasks.append((mapping, plate, future))
    result.slices = len(tasks)
    result.problems = [
        f"plate {plate} with PETG on the {printer.nozzles[int(mapping[0]) - 1].name} nozzle: "
        f"{problem}"
        for mapping, plate, future in tasks
        if (problem := future.result())
    ]
    return result


def bambu_problems(
    project: Path,
    bambu: Path,
    work: Path,
    jobs: int = 1,
    tower_bounds: dict[int, tuple[float, ...]] | None = None,
) -> list[str]:
    """Slice every plate with PETG on each nozzle; list what Bambu Studio refuses."""
    with ThreadPoolExecutor(jobs) as pool:
        return bambu_check(
            project, work, Slicer(bambu, pool, Capacity(jobs)), tower_bounds
        ).problems


def reserved_towers(manifest: dict) -> dict[int, tuple[float, ...]] | None:
    """Rectangular tower footprints the slices must stay inside, by plate."""
    tower = manifest["export"].get("prime_tower", {})
    if "prime_tower_width" not in tower.get("process_settings", {}):
        return None
    return {
        plate["plate"]: tuple(plate["reserved_extrusion_bounds_mm"]) for plate in tower["plates"]
    }


def check(variant: Variant, folder: Path, slicer: Slicer, scratch: Path) -> list[str]:
    manifest = json.loads((folder / "manifest.json").read_text())
    found = bambu_check(
        folder / "job.3mf", scratch / f"{variant.name}-check", slicer, reserved_towers(manifest)
    )
    result = (
        f"{len(found.problems)} plate slices refused" if found.problems else "every plate sliced"
    )
    reused = []
    if found.cached:
        reused.append(f"{found.cached} from the slice cache")
    if found.shared:
        reused.append(f"{found.shared} shared with another variant")
    detail = f"{found.slices} plate slices" + (f": {', '.join(reused)}" if reused else "")
    print(f"{variant.name}: Bambu Studio check, {result} ({detail})", flush=True)
    if not found.problems and slicer.checker is not None:
        _write_json(
            checked_record(folder, slicer.checker),
            {
                "checker": slicer.checker,
                "slices": found.slices,
                "checked": datetime.now().isoformat(),
            },
        )
    return [f"{variant.name}: {problem}" for problem in found.problems]


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, indent=2))
    os.replace(temporary, path)


def source_identity() -> dict:
    """What decides generated projects: the generator files, this script's variants and Python.

    Only the code reached from ``GENERATION_ROOTS`` counts, so editing the check or publishing
    steps keeps staged variants.
    """
    package = Path(cargo_grid.__file__).resolve().parent
    hasher = hashlib.sha256(f"{script_code(GENERATION_ROOTS)}\n".encode())
    files = [
        (path.relative_to(package).as_posix(), path)
        for path in sorted(package.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and not path.name.startswith(".")
    ]
    for name, path in files:
        hasher.update(f"{name}\0{digest(path.read_bytes())}\n".encode())
    return {
        "format": STAGING_FORMAT,
        "source": hasher.hexdigest(),
        "environment": environment(Path(__file__).resolve().parents[1]),
    }


def commit() -> str | None:
    """The checked-out commit, marked when there are local changes; recorded, not a key."""
    root = Path(__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return f"{head}+local-changes" if dirty else head


def _running(pid: int) -> bool:
    """Whether a process is alive, so a stopped run's partial variant can be removed."""
    if sys.platform == "win32":
        return True  # signal 0 is CTRL_C_EVENT there; leave partial folders alone
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def staged_name(variant: Variant) -> str:
    return f"{variant.name}-{digest(json.dumps(variant.arguments).encode())[:12]}"


def checked_record(folder: Path, checker: str) -> Path:
    return folder / f"check-{checker[:16]}.json"


def build(
    selected: list[Variant],
    staging: Path,
    scratch: Path,
    *,
    jobs: int,
    packing_workers: int,
    capacity: Capacity,
    slicer: Slicer | None = None,
    resume: bool = True,
    reuse_checks: bool = True,
    record: dict | None = None,
) -> tuple[list[Path], list[str]]:
    """Generate and check every variant, keeping each finished one in ``staging``.

    A variant already staged under the same key is reused when ``resume`` is set, and so is
    its clean check when ``reuse_checks`` is also set. Each check starts as soon as its variant
    exists.
    """
    staging.mkdir(parents=True, exist_ok=True)
    for partial in staging.glob(".partial-*"):
        if not _running(int(partial.name.split("-")[1])):
            shutil.rmtree(partial, ignore_errors=True)
    share = 1 + packing_workers
    folders = {variant: staging / staged_name(variant) for variant in selected}
    ready = [
        variant for variant in selected if resume and (folders[variant] / "variant.json").is_file()
    ]
    missing = [variant for variant in selected if variant not in ready]

    def staged_generate(variant: Variant) -> Path:
        partial = Path(tempfile.mkdtemp(prefix=f".partial-{os.getpid()}-", dir=staging))
        try:
            folder = generate(variant, partial, packing_workers=packing_workers)
            _write_json(
                folder / "variant.json",
                {
                    **(record or {}),
                    "name": variant.name,
                    "arguments": list(variant.arguments),
                    "generated": datetime.now().isoformat(),
                },
            )
            if folders[variant].exists():
                shutil.rmtree(folders[variant])
            folder.rename(folders[variant])
        finally:
            shutil.rmtree(partial, ignore_errors=True)
        return folders[variant]

    checks = {}
    with (
        ThreadPoolExecutor(jobs) as generation,
        ThreadPoolExecutor(len(selected)) as checking,
    ):
        pools = (generation, checking, *((slicer.pool,) if slicer else ()))

        def start_check(variant: Variant) -> None:
            if slicer is None:
                return
            if (
                resume
                and reuse_checks
                and slicer.checker is not None
                and checked_record(folders[variant], slicer.checker).is_file()
            ):
                print(f"{variant.name}: Bambu Studio check, every plate sliced earlier", flush=True)
                return
            checks[variant] = checking.submit(check, variant, folders[variant], slicer, scratch)

        try:
            for variant in ready:
                print(f"{variant.name}: generated earlier, in {folders[variant]}", flush=True)
                start_check(variant)
            pending = {generation.submit(staged_generate, variant): variant for variant in missing}
            capacity.reserve(min(jobs, len(pending)) * share)
            for done, future in enumerate(as_completed(pending), 1):
                variant = pending[future]
                future.result()
                capacity.reserve(min(jobs, len(pending) - done) * share)
                print(f"{variant.name}: generated", flush=True)
                start_check(variant)
            problems = [
                problem
                for variant in selected
                if variant in checks
                for problem in checks[variant].result()
            ]
        except BaseException:
            capacity.close()
            for pool in pools:
                pool.shutdown(wait=False, cancel_futures=True)
            raise
    return [folders[variant] for variant in selected], problems


BATCH_SUFFIX = re.compile(r"_batch_\d+$")
PLATE_NAME = re.compile(r'<metadata key="plater_name" value="([^"]*)" />')
OBJECT_NAME = re.compile(r'<metadata key="name" value="([^"]*)" />')


def _outline(project: Path) -> dict:
    """A project's plates with their names, its designs by name and its project settings."""
    _, fingerprints = plate_fingerprints(project)
    with ZipFile(project) as archive:
        text = archive.read(PLATE_SETTINGS).decode()
        settings = digest(archive.read(PROJECT_SETTINGS))
    names = {
        match[1]: BATCH_SUFFIX.sub("", name[1])
        for match in OBJECT_SETTINGS.finditer(text)
        if (name := OBJECT_NAME.search(match[0]))
    }
    plates, designs = [], {}
    for number, (plate, fingerprint) in enumerate(zip(PLATE.findall(text), fingerprints), 1):
        name = PLATE_NAME.search(plate)
        plates.append((f"{number} {name[1]}" if name else str(number), fingerprint))
        for identity, item in zip(INSTANCE_OBJECT.findall(plate), fingerprint["objects"]):
            designs.setdefault(names.get(identity, identity), []).append(item["meshes"])
    return {
        "plates": plates,
        "designs": {name: sorted(meshes) for name, meshes in designs.items()},
        "settings": settings,
    }


def project_changes(old: Path, new: Path) -> str:
    """Which plates, designs and settings differ between two versions of a project."""
    try:
        before, after = _outline(old), _outline(new)
    except (OSError, KeyError, ValueError, BadZipFile) as error:
        return f"contents differ ({error})"
    found = []
    plates = [
        label
        for (label, fingerprint), (_, previous) in zip(after["plates"], before["plates"])
        if fingerprint != previous
    ]
    if plates:
        found.append(f"plates changed: {', '.join(plates)}")
    if len(after["plates"]) != len(before["plates"]):
        found.append(f"{len(before['plates'])} plates before, {len(after['plates'])} now")
    old_designs, new_designs = before["designs"], after["designs"]
    for label, names in (
        (
            "designs changed",
            [n for n in new_designs if old_designs.get(n, new_designs[n]) != new_designs[n]],
        ),
        ("designs added", [n for n in new_designs if n not in old_designs]),
        ("designs removed", [n for n in old_designs if n not in new_designs]),
    ):
        if names:
            found.append(f"{label}: {', '.join(sorted(names))}")
    if after["settings"] != before["settings"]:
        found.append("project settings changed")
    return "; ".join(found) or "same plates, designs and settings; other archive bytes differ"


def manifest_differences(old: Path, new: Path) -> str:
    try:
        fields = [
            change["path"]
            for change in manifest_changes(json.loads(old.read_text()), json.loads(new.read_text()))
        ]
    except (OSError, ValueError) as error:
        return f"contents differ ({error})"
    if not fields:
        return "same fields, different formatting"
    return f"{len(fields)} fields changed, such as {', '.join(fields[:3])}"


def publish(folder: Path, selected: list[Variant], staged: list[Path]) -> list[tuple[str, str]]:
    """Copy new and changed files into ``folder``; return each file's status and summary.

    Before a file is replaced it moves to ``old-<date>``, dated by the earliest modification
    time of that variant's replaced files, or ``old-<date>-2`` and so on when that folder
    already holds a file of the same name. Identical files are not touched.
    """
    lines = []
    for variant, source in zip(selected, staged):
        targets = destinations(folder, variant)
        changed = {
            name: destination
            for name, destination in targets.items()
            if destination.exists() and not filecmp.cmp(source / name, destination, shallow=False)
        }
        archive = None
        if changed:
            date = min(
                datetime.fromtimestamp(path.stat().st_mtime) for path in changed.values()
            ).date()
            archive = next(
                candidate
                for count in itertools.count(1)
                if not any(
                    (candidate := folder / f"old-{date}{'' if count == 1 else f'-{count}'}")
                    .joinpath(path.name)
                    .exists()
                    for path in changed.values()
                )
            )
        for name, destination in targets.items():
            if destination.exists() and name not in changed:
                lines.append(("identical", f"{destination.name}: identical"))
                continue
            status, summary = "new", "new"
            if name in changed:
                describe = project_changes if name == "job.3mf" else manifest_differences
                details = describe(destination, source / name)
                archive.mkdir(exist_ok=True)
                shutil.move(destination, archive / destination.name)
                status = "changed"
                summary = f"changed, previous copy in {archive.name}: {details}"
            temporary = destination.with_name(f".{destination.name}.publishing")
            shutil.copyfile(source / name, temporary)
            os.replace(temporary, destination)
            lines.append((status, f"{destination.name}: {summary}"))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--output", type=Path, help="folder for the named files")
    target.add_argument(
        "--publish",
        type=Path,
        help="existing folder to update, keeping replaced files in old-<date>; needs --bambu-studio",
    )
    parser.add_argument(
        "--replace", action="store_true", help="replace files with the same names in --output"
    )
    parser.add_argument(
        "--jobs",
        type=int,
        help="variants at once; default uses available cores and memory with headroom",
    )
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
    parser.add_argument(
        "--slice-jobs",
        type=int,
        help="Bambu Studio slices at once; default one per core, within the shared memory budget",
    )
    parser.add_argument(
        "--no-slice-cache",
        action="store_true",
        help="slice every plate again and leave the slice cache untouched",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="build every variant again instead of reusing ones a stopped run finished",
    )
    args = parser.parse_args(argv)
    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.slice_jobs is not None and (args.slice_jobs < 1 or args.bambu_studio is None):
        parser.error("--slice-jobs must be at least 1 and needs --bambu-studio")
    selected = [
        variant
        for variant in variants(tuple(args.printers or ("h2d",)))
        if not args.sets or variant.set_name in args.sets
    ]
    if args.dry_run:
        for variant in selected:
            print(f"{variant.name}: cargo-grid {' '.join(variant.arguments)}")
        return 0
    cores, memory_slots = _process_capacity()
    jobs = min(args.jobs if args.jobs is not None else min(cores, memory_slots), len(selected))
    packing_workers = max(1, min(cores // jobs, memory_slots // jobs - 1))
    if args.publish is not None:
        if args.bambu_studio is None:
            parser.error("--publish needs --bambu-studio, so only checked files are published")
        if args.replace:
            parser.error("--publish keeps replaced files in old-<date>; --replace is for --output")
        if not args.publish.is_dir():
            parser.error(f"--publish must be an existing folder: {args.publish}")
    elif not args.output.is_dir():
        parser.error(f"--output must be an existing folder: {args.output}")
    if args.bambu_studio is not None and not args.bambu_studio.is_file():
        parser.error(f"--bambu-studio must be the Bambu Studio executable: {args.bambu_studio}")
    existing = [
        path
        for variant in selected
        for path in (destinations(args.output, variant).values() if args.output else ())
        if path.exists()
    ]
    if existing and not args.replace:
        parser.error(
            f"{len(existing)} files already exist, such as {existing[0].name}; "
            "pass --replace to overwrite them"
        )
    identity = source_identity()
    staging = state_folder() / "staging"
    current = staging / digest(json.dumps(identity, sort_keys=True).encode())[:16]
    if staging.is_dir():
        for other in staging.iterdir():
            if other != current and other.is_dir():
                shutil.rmtree(other)
                print(f"Removed {other.name}, staged from other source files", flush=True)
    capacity = Capacity(memory_slots)
    slice_jobs = args.slice_jobs if args.slice_jobs is not None else cores
    with (
        tempfile.TemporaryDirectory(prefix="cargo-grid-makerworld-") as temporary,
        ThreadPoolExecutor(slice_jobs) as slices,
    ):
        scratch = Path(temporary)
        print(
            f"Generating {len(selected)} variants with {jobs} jobs; "
            f"at most {packing_workers} search workers per variant",
            flush=True,
        )
        slicer = None
        if args.bambu_studio is not None:
            bambu = args.bambu_studio.resolve()
            checker = checker_identity(bambu)
            cache = (
                None if args.no_slice_cache else SliceCache(state_folder() / "slice-cache", checker)
            )
            slicer = Slicer(bambu, slices, capacity, cache, checker)
            print(
                f"Slicing with at most {min(slice_jobs, capacity.slots)} Bambu Studio processes "
                f"at once, within {capacity.slots} memory slots shared with generation",
                flush=True,
            )
        folders, problems = build(
            selected,
            current,
            scratch,
            jobs=jobs,
            packing_workers=packing_workers,
            capacity=capacity,
            slicer=slicer,
            resume=not args.no_resume,
            reuse_checks=not args.no_slice_cache,
            record={"commit": commit(), **identity},
        )
        if problems:
            raise RuntimeError(
                "Bambu Studio refused these plates; nothing was written:\n"
                + "\n".join(problems)
                + f"\nFinished variants stay in {current} for the next run."
            )
        if args.publish is not None:
            lines = publish(args.publish, selected, folders)
            print(f"Published to {args.publish}:")
            for _, line in lines:
                print(f"  {line}")
            print(
                ", ".join(
                    f"{sum(status == kind for status, _ in lines)} {kind}"
                    for kind in ("identical", "changed", "new")
                ),
                flush=True,
            )
        for variant, folder in zip(selected, folders):
            if args.output is not None:
                for source, destination in destinations(args.output, variant).items():
                    shutil.copyfile(folder / source, destination)
            export = json.loads((folder / "manifest.json").read_text())
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
