"""Compare source-derived published projects; never upload models or operate a printer."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

from test_selection import ROOT, digest, environment, function_regions, git, snapshot, write_json


def manifest_changes(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            child = f"{path}.{key}" if path else key
            if key not in before:
                yield {"path": child, "before_absent": True, "after": after[key]}
            elif key not in after:
                yield {"path": child, "before": before[key], "after_absent": True}
            else:
                yield from manifest_changes(before[key], after[key], child)
    elif isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child = f"{path}[{index}]"
            if index >= len(before):
                yield {"path": child, "before_absent": True, "after": after[index]}
            elif index >= len(after):
                yield {"path": child, "before": before[index], "after_absent": True}
            else:
                yield from manifest_changes(before[index], after[index], child)
    elif type(before) is not type(after) or before != after:
        yield {"path": path, "before": before, "after": after}


def fingerprints(folder):
    return {
        path.name: digest(path.read_bytes())
        for path in sorted(folder.iterdir())
        if path.suffix in {".3mf", ".json"}
    }


def validate_inventory(hashes):
    archives = {name.removesuffix(".3mf") for name in hashes if name.endswith(".3mf")}
    manifests = {
        name.removesuffix(".manifest.json") for name in hashes if name.endswith(".manifest.json")
    }
    if len(hashes) != 48 or len(archives) != 24 or archives != manifests:
        raise ValueError("publish guard requires all 24 archive/manifest pairs")


def objects(archive):
    names = {}
    settings_hashes = {}
    if "Metadata/model_settings.config" in archive.namelist():
        settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
        for entry in settings.iter():
            if entry.tag in {"object", "part"}:
                label = entry.find("metadata[@key='name']")
                if label is not None:
                    names[entry.attrib["id"]] = label.attrib["value"]
                settings_hashes[entry.attrib["id"]] = digest(ET.tostring(entry))
    model = ET.fromstring(archive.read("3D/3dmodel.model"))
    result = {}
    for entry in model.findall("{*}resources/{*}object"):
        identity = entry.attrib["id"]
        result[f"object:{identity}"] = {
            "name": names.get(identity, identity),
            "digest": digest(ET.tostring(entry)),
            "settings": settings_hashes.get(identity),
        }
    for index, entry in enumerate(model.findall("{*}build/{*}item")):
        result[f"placement:{index}"] = {
            "name": names.get(entry.attrib["objectid"], entry.attrib["objectid"]),
            "digest": digest(ET.tostring(entry)),
        }
    return result


def compare(before, after):
    old, new = fingerprints(before), fingerprints(after)
    changes = []
    for name in sorted(old.keys() | new.keys()):
        if old.get(name) == new.get(name):
            continue
        record = {"file": name, "before": old.get(name), "after": new.get(name)}
        if name not in old or name not in new:
            record["inventory_change"] = True
        elif name.endswith(".3mf"):
            with ZipFile(before / name) as first, ZipFile(after / name) as second:
                members = sorted(set(first.namelist()) | set(second.namelist()))
                record["changed_members"] = [
                    member
                    for member in members
                    if member not in first.namelist()
                    or member not in second.namelist()
                    or first.read(member) != second.read(member)
                ]
                record["zip_metadata_only"] = not record["changed_members"]
                if set(record["changed_members"]) & {
                    "3D/3dmodel.model",
                    "Metadata/model_settings.config",
                }:
                    first_objects, second_objects = objects(first), objects(second)
                    record["changed_objects"] = [
                        {
                            "id": identity,
                            "before": first_objects.get(identity),
                            "after": second_objects.get(identity),
                        }
                        for identity in sorted(first_objects.keys() | second_objects.keys())
                        if first_objects.get(identity) != second_objects.get(identity)
                    ]
        else:
            record["fields"] = list(
                manifest_changes(
                    json.loads((before / name).read_text()), json.loads((after / name).read_text())
                )
            )
        changes.append(record)
    return changes


def build(source, output, jobs):
    output.mkdir(parents=True)
    subprocess.run(
        [
            sys.executable,
            str(source / "tools/makerworld_exports.py"),
            "--printer",
            "h2d",
            "--printer",
            "x2d",
            "--jobs",
            str(jobs),
            "--output",
            str(output),
        ],
        cwd=source,
        env={**os.environ, "PYTHONPATH": str(source / "src")},
        check=True,
    )


def guard(args):
    base = git(ROOT, "merge-base", args.base, "HEAD").decode().strip()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="publish-", dir=args.output) as temporary:
        scratch = Path(temporary)
        source = scratch / "base-source"
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(git(ROOT, "archive", base))) as archive:
            archive.extractall(source, filter="data")
        build(source, scratch / "baseline", args.jobs)
        build(ROOT, scratch / "candidate", args.jobs)
        changes = compare(scratch / "baseline", scratch / "candidate")
        report = {
            "base": base,
            "head": git(ROOT, "rev-parse", "HEAD").decode().strip(),
            "environment": environment(ROOT),
            "baseline": fingerprints(scratch / "baseline"),
            "candidate": fingerprints(scratch / "candidate"),
            "changes": changes,
        }
        write_json(args.output / "report.json", report)
        validate_inventory(report["baseline"])
        validate_inventory(report["candidate"])
    print(f"{len(changes)} changed deliverables; report: {args.output / 'report.json'}")
    return 1 if changes else 0


def record_fingerprints(args):
    import coverage
    from makerworld_exports import destinations, generate, variants

    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fingerprints-", dir=args.output) as temporary:
        scratch = Path(temporary)
        config = scratch / "coverage.ini"
        config.write_text(
            "[run]\ncore = ctrace\nparallel = true\n"
            "concurrency = multiprocessing\npatch = subprocess, _exit\n"
            f"data_file = {scratch / '.coverage'}\n"
            f"source =\n    {ROOT / 'src'}\n    {ROOT / 'tools'}\n",
            encoding="utf-8",
        )
        measured = coverage.Coverage(config_file=str(config))
        measured.start()
        hashes = {}
        try:
            selected = [
                variant
                for variant in variants((args.printer,))
                if variant.arguments[variant.arguments.index("--nozzle-diameter-mm") + 1]
                == args.nozzle
                and (not args.variant or variant.name == args.variant)
            ]
            if len(selected) != (1 if args.variant else 6):
                raise ValueError(
                    "unexpected authoritative inventory for this printer/nozzle/variant"
                )
            for variant in selected:
                folder = generate(variant, scratch, packing_workers=1)
                hashes.update(
                    {
                        target.name: digest((folder / source).read_bytes())
                        for source, target in destinations(scratch, variant).items()
                    }
                )
        finally:
            measured.stop()
            measured.save()
        combined = coverage.Coverage(config_file=str(config))
        combined.combine()
        data = combined.get_data()
        functions = {}
        for filename in data.measured_files():
            path = Path(filename).resolve()
            if not path.is_relative_to(ROOT):
                continue
            relative = path.relative_to(ROOT).as_posix()
            if not relative.startswith(("src/", "tools/")):
                continue
            regions = function_regions(path.read_bytes())
            functions[relative] = sorted(
                {
                    next(
                        (name for start, end, name in reversed(regions) if start <= line <= end),
                        "<module>",
                    )
                    for line in data.lines(filename) or []
                }
            )
        write_json(
            args.output / "fingerprints.json",
            {
                "commit": git(ROOT, "rev-parse", "HEAD").decode().strip(),
                "environment": environment(ROOT),
                "snapshot": snapshot(ROOT),
                "functions": functions,
                "fingerprints": hashes,
            },
        )


def merge_fingerprints(paths, previous, approval, report_path):
    fingerprints_by_name, functions = {}, {}
    result = None
    for path in paths:
        part = json.loads(path.read_text())
        if result and (
            result["commit"] != part["commit"]
            or result["environment"] != part["environment"]
            or result["snapshot"] != part["snapshot"]
        ):
            raise ValueError("incompatible publish fingerprint parts")
        if fingerprints_by_name.keys() & part["fingerprints"].keys():
            raise ValueError("duplicate published variant in fingerprints")
        fingerprints_by_name.update(part["fingerprints"])
        for file, names in part["functions"].items():
            functions[file] = sorted(set(functions.get(file, [])) | set(names))
        result = part
    validate_inventory(fingerprints_by_name)
    result.update(functions=functions, fingerprints=fingerprints_by_name)
    changed = sorted(
        name
        for name in fingerprints_by_name.keys() | (previous or {}).get("fingerprints", {}).keys()
        if previous and fingerprints_by_name.get(name) != previous["fingerprints"].get(name)
    )
    approval_digest = digest(json.dumps(fingerprints_by_name, sort_keys=True).encode())
    report = {
        "previous_commit": (previous or {}).get("commit"),
        "commit": result["commit"],
        "changed": changed,
        "approval_digest": approval_digest,
        "previous_environment": (previous or {}).get("environment"),
        "environment": result["environment"],
    }
    write_json(report_path, report)
    if changed and approval != approval_digest:
        raise ValueError(
            f"weekly publish fingerprints changed: {changed}; review {report_path}, then "
            f"approve this exact fingerprint set with approve_fingerprints={approval_digest}"
        )
    print(
        "Weekly fingerprint baseline initialized"
        if not previous
        else f"{len(changed)} reviewed fingerprint changes"
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("compare")
    check.add_argument("--base", default="origin/main")
    check.add_argument("--jobs", type=int, default=4)
    check.add_argument("--output", type=Path, default=ROOT / ".local/publish-guard")
    record = sub.add_parser("fingerprints")
    record.add_argument("--printer", choices=("h2d", "x2d"), required=True)
    record.add_argument("--nozzle", choices=("0.8", "0.4"), required=True)
    record.add_argument("--variant", help="one exact name from the authoritative publish inventory")
    record.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "compare":
        if args.jobs < 1:
            parser.error("--jobs must be positive")
        return guard(args)
    record_fingerprints(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
