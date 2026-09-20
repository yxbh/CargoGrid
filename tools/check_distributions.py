"""Check local wheel/sdist contents before any separately authorized release."""

import argparse
import ast
import tarfile
from configparser import ConfigParser
from email.parser import Parser
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = {"src", "tests", "examples", "docs"}
ROOT_FILES = {
    "pyproject.toml",
    "README.md",
    "LICENSE",
    ".gitignore",
    "PKG-INFO",
    ".github/workflows/ci.yml",
    "tools/check_distributions.py",
    "tools/render_docs.py",
    "AGENTS.md",
    "docs/images/attachments/manifest.json",
}
DOCUMENTATION_IMAGES = {
    "docs/images/hero.png",
    "docs/images/x-attachments.png",
    "docs/images/vertical-tile-brackets.png",
    "docs/images/vertical-stops.png",
    "docs/images/ramps.png",
    "docs/images/interface-sizes.png",
    "docs/images/rods-and-braces.png",
    *(f"docs/images/attachments/rod-{height}.png" for height in (120, 240)),
    *(f"docs/images/attachments/rod-brace-{spacing}-d10.png" for spacing in (60, 120)),
    *(
        f"docs/images/attachments/{family}-{number}.png"
        for family in ("edge-x", "edge-y", "support")
        for number in range(1, 6)
    ),
    *(f"docs/images/attachments/corner-in-v{n}.png" for n in range(1, 5)),
    *(f"docs/images/attachments/corner-out-v{n}.png" for n in range(1, 7)),
    *(f"docs/images/attachments/support-end-v{n}.png" for n in range(1, 5)),
    *(f"docs/images/attachments/support-bit-{n}mm.png" for n in (20, 30, 40, 50)),
    *(f"docs/images/attachments/plate-{grid}.png" for grid in ("1x1", "1x2", "2x2")),
    *(f"docs/images/attachments/lock-45-{grid}.png" for grid in ("1x1", "2x2")),
    *(
        f"docs/images/attachments/vertical-tile-bracket-{grid}.png"
        for grid in ("1x2", "2x1", "2x2")
    ),
    *(
        f"docs/images/attachments/vertical-tile-bracket-{variant}.png"
        for variant in ("base1x1-wall1x2", "base2x1-wall2x2")
    ),
    *(
        f"docs/images/attachments/vertical-stop-{grid}-h{height}.png"
        for grid in ("1x1", "1x2", "2x1", "2x2")
        for height in (60, 120)
    ),
    *(f"docs/images/attachments/ramp-{cells}.png" for cells in range(1, 6)),
    *(f"docs/images/attachments/ramp-male-{cells}.png" for cells in range(1, 6)),
}
FORBIDDEN_SUFFIXES = {
    ".step",
    ".stp",
    ".stl",
    ".3mf",
    ".gcode",
    ".glb",
    ".gltf",
    ".pyc",
    ".pyo",
    ".png",
    ".npz",
}


def source_version() -> str:
    module = ast.parse((ROOT / "src/cargo_grid/_version.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("No literal package version found")


def check_path(name: str, *, documentation_image: bool = False) -> PurePosixPath:
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError(f"Unsafe archive member: {name}")
    if (
        (
            path.suffix.lower() in FORBIDDEN_SUFFIXES
            and not (documentation_image and path.suffix.lower() == ".png")
        )
        or any(part in {"outputs", ".venv", ".local", "__pycache__", ".git"} for part in path.parts)
        or path.name in {"uv.lock", "result.json", ".env", ".pypirc"}
    ):
        raise ValueError(f"Runtime/generated/private artifact in distribution: {name}")
    return path


def check_metadata(text: str, expected_version: str) -> None:
    metadata = Parser().parsestr(text)
    if metadata["Name"] != "cargo-grid" or metadata["Version"] != expected_version:
        raise ValueError("Distribution name/version does not match source")
    if metadata["License-Expression"] != "MIT" or metadata["Requires-Python"] != ">=3.12":
        raise ValueError("Missing license or Python requirement metadata")
    if metadata["Description-Content-Type"] != "text/markdown":
        raise ValueError("README is missing from distribution metadata")
    if metadata.get_payload().strip() != (ROOT / "README.md").read_text(encoding="utf-8").strip():
        raise ValueError("Distribution README does not match current source")
    if any("://" in requirement for requirement in metadata.get_all("Requires-Dist", [])):
        raise ValueError("Direct dependency URLs must not be embedded in release metadata")


def check_wheel(path: Path, expected_version: str) -> None:
    with ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("Corrupt wheel")
        names = archive.namelist()
        for name in names:
            member = check_path(name)
            if archive.getinfo(name).is_dir():
                continue
            if len(member.parts) < 2 or not (
                member.parts[0] == "cargo_grid"
                and member.suffix == ".py"
                or member.parts[0] == f"cargo_grid-{expected_version}.dist-info"
                and str(PurePosixPath(*member.parts[1:]))
                in {
                    "METADATA",
                    "WHEEL",
                    "RECORD",
                    "entry_points.txt",
                    "licenses/LICENSE",
                }
            ):
                raise ValueError(f"Unexpected wheel member: {name}")
        prefix = f"cargo_grid-{expected_version}.dist-info/"
        if not {prefix + "WHEEL", prefix + "RECORD"} <= set(names):
            raise ValueError("Wheel format metadata is missing")
        check_metadata(archive.read(prefix + "METADATA").decode(), expected_version)
        entries = ConfigParser()
        entries.read_string(archive.read(prefix + "entry_points.txt").decode())
        if entries["console_scripts"]["cargo-grid"] != "cargo_grid.cli:main":
            raise ValueError("Console entry point is missing")
        if archive.read(prefix + "licenses/LICENSE") != (ROOT / "LICENSE").read_bytes():
            raise ValueError("Wheel license does not match the repository license")
        actual = {n for n in names if n.startswith("cargo_grid/") and n.endswith(".py")}
        expected = {
            str(p.relative_to(ROOT / "src")).replace("\\", "/")
            for p in (ROOT / "src/cargo_grid").rglob("*.py")
        }
        if actual != expected:
            raise ValueError("Wheel modules differ from the maintained source")
        if any(archive.read(name) != (ROOT / "src" / name).read_bytes() for name in expected):
            raise ValueError("Wheel module contents differ from current source")


def check_sdist(path: Path, expected_version: str) -> None:
    prefix = f"cargo_grid-{expected_version}"
    with tarfile.open(path, "r:gz") as archive:
        files = {}
        for member in archive.getmembers():
            raw = PurePosixPath(member.name)
            intended_image = (
                bool(raw.parts)
                and raw.parts[0] == prefix
                and str(PurePosixPath(*raw.parts[1:])) in DOCUMENTATION_IMAGES
            )
            name = check_path(member.name, documentation_image=intended_image)
            if member.isdir():
                continue
            if not member.isfile() or name.parts[0] != prefix:
                raise ValueError(f"Unexpected source archive member: {member.name}")
            relative = PurePosixPath(*name.parts[1:])
            if str(relative) not in ROOT_FILES | DOCUMENTATION_IMAGES and not (
                relative.parts
                and relative.parts[0] in SOURCE_ROOTS
                and relative.suffix in {".py", ".md"}
            ):
                raise ValueError(f"Unexpected source release file: {relative}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Unreadable source release file: {relative}")
            files[str(relative)] = stream.read()
        for required in (
            "PKG-INFO",
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "src/cargo_grid/_version.py",
            "src/cargo_grid/__main__.py",
            "AGENTS.md",
            "docs/attachments.md",
            "docs/images/attachments/manifest.json",
            *sorted(DOCUMENTATION_IMAGES),
        ):
            if required not in files:
                raise ValueError(f"Source archive missing {required}")
        check_metadata(files["PKG-INFO"].decode(), expected_version)
        if files["LICENSE"] != (ROOT / "LICENSE").read_bytes():
            raise ValueError("Source archive license was changed")
        for name, data in files.items():
            if name != "PKG-INFO" and data != (ROOT / name).read_bytes():
                raise ValueError(f"Source archive contains stale content: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args()
    wheels, sources = list(args.directory.glob("*.whl")), list(args.directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        parser.error(
            "expected exactly one wheel and one source archive in a fresh output directory"
        )
    try:
        version = source_version()
        check_wheel(wheels[0], version)
        check_sdist(sources[0], version)
    except (ValueError, KeyError, OSError, BadZipFile, tarfile.TarError) as error:
        parser.exit(1, f"distribution check: {error}\n")
    print(
        f"Checked wheel and source archive for cargo-grid {version}; only allowlisted source/docs assets, no runtime data."
    )


if __name__ == "__main__":
    main()
