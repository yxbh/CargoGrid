"""Record-independent test selection and its local command-line entry point."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import io
import json
import math
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

FORMAT_VERSION = 3
ROOT = Path(__file__).resolve().parents[1]
INFRASTRUCTURE = {
    "tests/selection.py",
    "tools/test_selection.py",
    "tools/ci_tests.py",
    "tools/publish_guard.py",
    "tools/check_test_durations.py",
    "tools/ci_memory_sampler.py",
    "tools/check_distributions.py",
    "pytest.ini",
    ".coveragerc",
}
CONFIG_SECTIONS = ("project", "dependency-groups", "build-system")
BENCHMARK_CASES = {
    "docs": ("README.md", None),
    "test": ("tests/test_ramp.py", "test_ramp_width_run_rise_rounding_step_and_mesh"),
    "cli": ("src/cargo_grid/cli.py", "parser"),
    "tower": ("src/cargo_grid/plates.py", "dual_nozzle_tower_reservation"),
    "stop": ("src/cargo_grid/accessories.py", "_vertical_stop"),
    "ramp": ("src/cargo_grid/accessories.py", "_ramp"),
    "handle": ("src/cargo_grid/pull_handles.py", "_body"),
    "rod-brace": ("src/cargo_grid/rods.py", "make_rod_brace"),
    "adjustable-stop": ("src/cargo_grid/adjustable_stop.py", "_make_pusher"),
    "planner": ("src/cargo_grid/plates.py", "plan_plates"),
    "export": ("src/cargo_grid/export.py", "_write_3mf"),
    "packing": ("src/cargo_grid/footprints.py", "_pack_order"),
    "assembly": ("src/cargo_grid/vehicles/zeekr_7x_contours.py", "_south_design"),
    "core": ("src/cargo_grid/tiles.py", "make_tile"),
    "infrastructure": ("tests/conftest.py", None),
}


def git(root, *arguments):
    return subprocess.check_output(["git", "-C", str(root), *arguments])


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def environment(root):
    lock = Path(root) / "uv.lock"
    return {
        "python": list(sys.version_info[:3]),
        "implementation": platform.python_implementation(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "packages": dict(
            sorted(
                (distribution.metadata["Name"].lower().replace("_", "-"), distribution.version)
                for distribution in importlib.metadata.distributions()
                if distribution.metadata["Name"].lower().replace("_", "-") != "cargo-grid"
            )
        ),
        "lock": digest(lock.read_bytes()) if lock.is_file() else None,
    }


def module_name(path):
    path = path.removeprefix("src/")
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def references(node):
    result = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            result.add(child.id)
        elif isinstance(child, ast.Attribute):
            result.add(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            if child.value.isidentifier():
                result.add(child.value)
        elif isinstance(child, ast.alias):
            result.add(child.name)
    return result


def function_regions(source):
    regions = []

    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                regions.append((child.body[0].lineno, child.end_lineno, name))
                walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")
            else:
                walk(child, prefix)

    walk(ast.parse(source))
    return sorted(regions)


def defined_names(node):
    result = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            result.add(child.id)
        elif isinstance(child, ast.alias):
            result.add(child.asname or child.name.split(".")[0])
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.add(child.name)
    return result


def analyse(source, path):
    tree = ast.parse(source)
    functions, items, imports, eager = {}, [], set(), set()
    module = module_name(path)

    def record_eager(node):
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                eager.update(references(child))
        for decorator in getattr(node, "decorator_list", []):
            eager.update(references(decorator))
        for base in getattr(node, "bases", []):
            eager.update(references(base))

    def add_function(node, name):
        signature = digest(ast.dump(node).encode())
        names = references(node)
        header = deepcopy(node)
        header.body = [
            statement
            for statement in node.body[:1]
            if isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ]
        definition = digest(ast.dump(header).encode())
        definition_refs = references(header)
        record_eager(header)
        if name in functions:
            signature = digest((functions[name]["digest"] + signature).encode())
            names.update(functions[name]["refs"])
            definition = digest((functions[name]["definition"] + definition).encode())
            definition_refs.update(functions[name]["definition_refs"])
        functions[name] = {
            "digest": signature,
            "refs": sorted(names),
            "definition": definition,
            "definition_refs": sorted(definition_refs),
        }

    def walk(body, prefix="", owner=None):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                add_function(node, name)
                nested(node, name + ".")
            elif isinstance(node, ast.ClassDef):
                header = ast.ClassDef(
                    name=node.name,
                    bases=node.bases,
                    keywords=node.keywords,
                    body=[],
                    decorator_list=node.decorator_list,
                    type_params=getattr(node, "type_params", []),
                )
                items.append(
                    {
                        "digest": digest((prefix + ast.dump(header)).encode()),
                        "names": [owner or node.name],
                        "refs": sorted(references(header)),
                        "bindings": {},
                    }
                )
                record_eager(header)
                walk(node.body, prefix + node.name + ".", owner or node.name)
            elif not (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                items.append(
                    {
                        "digest": digest((prefix + ast.dump(node)).encode()),
                        "names": [owner] if owner else sorted(defined_names(node)),
                        "refs": sorted(references(node)),
                        "bindings": {
                            alias.asname or alias.name.split(".")[0]: sorted(
                                references(ast.Import(names=[alias]))
                            )
                            for alias in node.names
                        }
                        if isinstance(node, (ast.Import, ast.ImportFrom))
                        else {},
                    }
                )
                if not path.endswith("/__main__.py") and not (
                    isinstance(node, ast.If)
                    and ast.unparse(node.test)
                    in {"__name__ == '__main__'", "'__main__' == __name__"}
                ):
                    record_eager(node)

    def nested(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                add_function(child, name)
                nested(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                nested(child, prefix + child.name + ".")
            else:
                nested(child, prefix)

    walk(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parent = module.split(".") if path.endswith("/__init__.py") else module.split(".")[:-1]
            base = (
                ".".join(parent[: len(parent) - node.level + 1] + [node.module or ""]).strip(".")
                if node.level
                else node.module or ""
            )
            imports.add(base)
            imports.update(f"{base}.{alias.name}".strip(".") for alias in node.names)
    return {
        "functions": functions,
        "items": items,
        "imports": sorted(imports),
        "eager": sorted(eager),
    }


def source_files(root, ref=None):
    if ref:
        with tarfile.open(fileobj=io.BytesIO(git(root, "archive", ref))) as archive:
            return {
                member.name: archive.extractfile(member).read()
                for member in archive.getmembers()
                if member.isfile()
            }
    paths = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    result = {}
    for raw in paths.split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = Path(root) / name
        if path.is_symlink():
            result[name] = os.fsencode(os.readlink(path))
        elif path.is_file():
            result[name] = path.read_bytes()
    return result


def snapshot(root, ref=None):
    files = source_files(root, ref)
    python = {}
    for path, content in files.items():
        if path.endswith(".py") and path.startswith(("src/", "tests/", "tools/")):
            try:
                python[path] = analyse(content, path)
            except SyntaxError as error:
                python[path] = {"error": str(error)}
    config = tomllib.loads(files.get("pyproject.toml", b"").decode())
    return {
        "files": {path: digest(content) for path, content in files.items()},
        "python": python,
        "config": {
            **{key: config.get(key) for key in CONFIG_SECTIONS},
            "pytest": config.get("tool", {}).get("pytest", {}).get("ini_options"),
        },
    }


def strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def valid_snapshot(value):
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key), dict) for key in ("files", "python", "config")
    ):
        return False
    if not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value["files"].items()
    ):
        return False
    for analysis in value["python"].values():
        if not isinstance(analysis, dict):
            return False
        if isinstance(analysis.get("error"), str):
            continue
        if (
            not isinstance(analysis.get("functions"), dict)
            or not isinstance(analysis.get("items"), list)
            or not strings(analysis.get("imports"))
            or not strings(analysis.get("eager"))
        ):
            return False
        for item in [*analysis["functions"].values(), *analysis["items"]]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("digest"), str)
                or not strings(item.get("refs"))
            ):
                return False
        if not all(
            isinstance(function.get("definition"), str) and strings(function.get("definition_refs"))
            for function in analysis["functions"].values()
        ):
            return False
        if not all(
            strings(item.get("names"))
            and isinstance(item.get("bindings"), dict)
            and all(
                isinstance(name, str) and strings(refs) for name, refs in item["bindings"].items()
            )
            for item in analysis["items"]
        ):
            return False
    return True


def load_map(path):
    try:
        mapping = json.loads(Path(path).read_text(encoding="utf-8"))
        if (
            mapping["version"] != FORMAT_VERSION
            or not isinstance(mapping["commit"], str)
            or not isinstance(mapping["tests"], dict)
            or not isinstance(mapping["fixtures"], dict)
            or not isinstance(mapping["environment"], dict)
            or not valid_snapshot(mapping["snapshot"])
            or "publish" in mapping
            and (
                not valid_snapshot(mapping["publish"]["snapshot"])
                or not isinstance(mapping["publish"].get("environment"), dict)
                or not isinstance(mapping["publish"]["functions"], dict)
                or not all(strings(names) for names in mapping["publish"]["functions"].values())
            )
        ):
            raise ValueError("unsupported or incomplete dependency map")
        recorded_environment = mapping["environment"]
        if (
            not isinstance(recorded_environment.get("python"), list)
            or len(recorded_environment["python"]) != 3
            or not all(
                isinstance(part, int) and part >= 0 for part in recorded_environment["python"]
            )
            or not isinstance(recorded_environment.get("packages"), dict)
            or not all(
                isinstance(name, str) and isinstance(version, str)
                for name, version in recorded_environment["packages"].items()
            )
            or not all(
                isinstance(recorded_environment.get(key), str)
                for key in ("platform", "machine", "implementation")
            )
        ):
            raise ValueError("invalid recorded environment")
        for record in [*mapping["tests"].values(), *mapping["fixtures"].values()]:
            if (
                not isinstance(record["functions"], dict)
                or not all(
                    isinstance(file, str) and strings(names)
                    for file, names in record["functions"].items()
                )
                or not strings(record["data"])
                or not isinstance(record["process"], bool)
            ):
                raise ValueError("invalid dependency record")
        for record in mapping["tests"].values():
            if (
                not isinstance(record["fixtures"], list)
                or not all(key in mapping["fixtures"] for key in record["fixtures"])
                or record["outcome"] not in {"passed", "failed", "skipped"}
                or not isinstance(record["duration"], (int, float))
                or not math.isfinite(record["duration"])
                or record["duration"] < 0
            ):
                raise ValueError("invalid test outcome, fixture or duration")
        return mapping, None
    except (OSError, ValueError, KeyError, TypeError) as error:
        return None, f"cannot use dependency map {path}: {error}"


def dependencies(mapping):
    """Include setup and every peer that may have populated a shared lazy fixture."""
    users = defaultdict(set)
    records = {}
    for test, record in mapping["tests"].items():
        records[test] = {
            "functions": defaultdict(set, {p: set(q) for p, q in record["functions"].items()}),
            "data": set(record["data"]),
            "process": record["process"],
        }
        for fixture in record["fixtures"]:
            users[fixture].add(test)
            setup = mapping["fixtures"].get(fixture, {})
            for path, names in setup.get("functions", {}).items():
                records[test]["functions"][path].update(names)
            records[test]["data"].update(setup.get("data", []))
            records[test]["process"] |= setup.get("process", False)
    pending = set(records)
    while pending:
        component = set()
        queue = [pending.pop()]
        while queue:
            test = queue.pop()
            if test in component:
                continue
            component.add(test)
            pending.discard(test)
            for fixture in mapping["tests"][test]["fixtures"]:
                queue.extend(users[fixture] - component)
        merged = {"functions": defaultdict(set), "data": set(), "process": False}
        for test in component:
            for path, names in records[test]["functions"].items():
                merged["functions"][path].update(names)
            merged["data"].update(records[test]["data"])
            merged["process"] |= records[test]["process"]
        for test in component:
            records[test] = merged
    return records


def visible_from(path, analysis, module):
    aliases = {module}
    if module.startswith(("tests.", "tools.")):
        aliases.update((module.split(".", 1)[1], module.rsplit(".", 1)[-1]))
    return module_name(path) == module or any(
        name == alias or name.startswith(alias + ".")
        for name in analysis["imports"]
        for alias in aliases
    )


def dependent_names(item, names):
    if item["bindings"]:
        return {name for name, refs in item["bindings"].items() if names.intersection(refs)}
    return set(item["names"]) if names.intersection(item["refs"]) else set()


def definition_time_dependency(old, new, affected):
    """Import/default/decorator calls lack a test context; qualify them with a full run."""
    pending, seen = defaultdict(set), defaultdict(set)
    for path, name in affected:
        if name != "<module>":
            pending[module_name(path)].update((name.split(".")[0], name.split(".")[-1]))
    while pending:
        module, names = pending.popitem()
        names -= seen[module]
        if not names:
            continue
        seen[module].update(names)
        for path in old["python"].keys() | new["python"].keys():
            for analysis in (old["python"].get(path), new["python"].get(path)):
                if not analysis:
                    continue
                if "error" in analysis:
                    return True
                if not visible_from(path, analysis, module):
                    continue
                if names.intersection(analysis["eager"]):
                    return True
                for name, function in analysis["functions"].items():
                    if names.intersection(function["refs"]):
                        pending[module_name(path)].update((name.split(".")[0], name.split(".")[-1]))
                for item in analysis["items"]:
                    pending[module_name(path)].update(dependent_names(item, names))
    return False


def affected_functions(old, new, changed):
    affected = set()
    pending = defaultdict(set)
    for path in changed:
        before = old["python"].get(path, {"functions": {}, "items": [], "imports": []})
        after = new["python"].get(path, {"functions": {}, "items": [], "imports": []})
        if "error" in before or "error" in after:
            return None
        for name in before["functions"].keys() | after["functions"].keys():
            first, second = before["functions"].get(name), after["functions"].get(name)
            if first != second:
                affected.add((path, name))
            if not first or not second or first["definition"] != second["definition"]:
                pending[module_name(path)].update((name.split(".")[0], name.split(".")[-1]))
        first = Counter(item["digest"] for item in before["items"])
        second = Counter(item["digest"] for item in after["items"])
        for item in before["items"] + after["items"]:
            if first[item["digest"]] != second[item["digest"]]:
                if not item["names"]:
                    return None
                pending[module_name(path)].update(item["names"])
                affected.add((path, "<module>"))
        if "__all__" in pending[module_name(path)]:
            pending[module_name(path)].update(
                name.split(".")[0] for name in before["functions"] | after["functions"]
            )
    seen = defaultdict(set)
    while pending:
        module, names = pending.popitem()
        names -= seen[module]
        if not names:
            continue
        seen[module].update(names)
        for path in old["python"].keys() | new["python"].keys():
            analyses = [
                value for value in (old["python"].get(path), new["python"].get(path)) if value
            ]
            for analysis in analyses:
                if "error" in analysis:
                    return None
                if not visible_from(path, analysis, module):
                    continue
                for name, function in analysis["functions"].items():
                    if names.intersection(function["refs"]):
                        affected.add((path, name))
                    if names.intersection(function["definition_refs"]):
                        pending[module_name(path)].update((name.split(".")[0], name.split(".")[-1]))
                for item in analysis["items"]:
                    pending[module_name(path)].update(dependent_names(item, names))
    return None if definition_time_dependency(old, new, affected) else affected


def select(mapping, current, collection, *, current_environment=None, full_reason=None):
    universe = {record["nodeid"] for record in collection}
    reasons = defaultdict(list)
    changed = []
    if mapping:
        old = mapping["snapshot"]
        changed = sorted(
            path
            for path in old["files"].keys() | current["files"].keys()
            if old["files"].get(path) != current["files"].get(path)
        )
        infra = [
            path
            for path in changed
            if Path(path).name == "conftest.py"
            or path in INFRASTRUCTURE
            or path.startswith(".github/workflows/")
            or path.startswith("src/")
            and not path.endswith(".py")
        ]
        if infra or old["config"] != current["config"]:
            full_reason = "infrastructure/dependency configuration changed: " + ", ".join(
                infra or ["pyproject.toml"]
            )
        if current_environment is not None and mapping["environment"] != current_environment:
            full_reason = "the recorded environment or lock differs"
    else:
        full_reason = full_reason or "no dependency map"
    affected = affected_functions(mapping["snapshot"], current, changed) if mapping else set()
    if affected is None:
        full_reason = "module/definition-time dependencies or Python syntax need a full run"
    if full_reason:
        selected = universe
        reasons["full"].append(full_reason)
    else:
        records = dependencies(mapping)
        selected = set()
        for node in universe:
            record = records.get(node)
            why = []
            if node.startswith("tests/contracts/"):
                why.append("default contract")
            if node not in mapping["tests"]:
                why.append("not in map")
            elif mapping["tests"][node]["outcome"] != "passed":
                why.append("previously failed or skipped")
            if record:
                if any(
                    (p, q) in affected for p, names in record["functions"].items() for q in names
                ):
                    why.append("changed function/name or shared fixture")
                if record["data"].intersection(changed):
                    why.append("changed data file")
                if changed and record["process"]:
                    why.append("process dependencies")
            if node.split("::", 1)[0] in changed and any(
                name == "<module>" for path, name in affected if path == node.split("::", 1)[0]
            ):
                why.append("test module configuration")
            if why:
                selected.add(node)
                reasons[node].extend(why)
    source_changed = any(
        path.startswith("src/") or path == "tools/makerworld_exports.py" for path in changed
    )
    publish = bool(full_reason)
    recorded_publish = mapping.get("publish") if mapping else None
    if source_changed and not publish:
        if not recorded_publish or recorded_publish.get("environment") != (
            current_environment if current_environment is not None else mapping["environment"]
        ):
            publish = True
        else:
            published = affected_functions(
                recorded_publish["snapshot"],
                current,
                [
                    path
                    for path in recorded_publish["snapshot"]["files"].keys()
                    | current["files"].keys()
                    if recorded_publish["snapshot"]["files"].get(path) != current["files"].get(path)
                ],
            )
            publish = published is None or any(
                (p, q) in published
                for p, names in recorded_publish["functions"].items()
                for q in names
            )
    return {
        "version": FORMAT_VERSION,
        "mode": "full" if full_reason else "selected",
        "changed": changed,
        "selected": sorted(selected),
        "reasons": dict(reasons),
        "publish_guard": publish,
    }


def shards(plan, collection, mapping, count=None):
    groups = defaultdict(list)
    durations = {}
    for record in collection:
        node = record["nodeid"]
        if node in plan["selected"]:
            groups[node.split("::", 1)[0]].append(node)
            durations[node] = (
                (mapping or {})
                .get("tests", {})
                .get(node, {})
                .get("duration", 20.0 if record["slow"] else 1.0)
            )
    count = min(count or min(4, max(1, math.ceil(sum(durations.values()) / 600))), len(groups) or 1)
    allocations, totals = [[] for _ in range(count)], [0.0] * count
    for path, nodes in sorted(
        groups.items(), key=lambda pair: (-sum(durations[n] for n in pair[1]), pair[0])
    ):
        index = min(range(count), key=lambda i: (totals[i], i))
        allocations[index].extend(nodes)
        totals[index] += sum(durations[n] for n in nodes)
    return [sorted(nodes) for nodes in allocations]


def merge_parts(paths, plan, baseline=None):
    if plan.get("measurement_only"):
        raise ValueError("measurement-only plans cannot refresh a dependency map")
    tests, fixtures = {}, {}
    for path in paths:
        part = json.loads(Path(path).read_text(encoding="utf-8"))
        if part["version"] != FORMAT_VERSION or part["environment"] != plan["environment"]:
            raise ValueError(f"incompatible worker part: {path}")
        if part["commit"] != plan["commit"]:
            raise ValueError(f"worker part belongs to another commit: {path}")
        for node, record in part["tests"].items():
            if node in tests:
                raise ValueError(f"test executed in more than one shard: {node}")
            tests[node] = record
        for key, record in part["fixtures"].items():
            target = fixtures.setdefault(key, {"functions": {}, "data": [], "process": False})
            for file, names in record["functions"].items():
                target["functions"][file] = sorted(
                    set(target["functions"].get(file, [])) | set(names)
                )
            target["data"] = sorted(set(target["data"]) | set(record["data"]))
            target["process"] |= record["process"]
    if set(tests) != set(plan["selected"]):
        raise ValueError("worker parts do not cover exactly the selected tests")
    merged = {
        **(baseline or {}),
        "version": FORMAT_VERSION,
        "commit": plan["commit"],
        "environment": plan["environment"],
        "snapshot": plan["snapshot"],
        "tests": {**(baseline or {}).get("tests", {}), **tests},
        "fixtures": {**(baseline or {}).get("fixtures", {}), **fixtures},
    }
    universe = {record["nodeid"] for record in plan["collection"]}
    merged["tests"] = {node: record for node, record in merged["tests"].items() if node in universe}
    if set(merged["tests"]) != universe:
        raise ValueError("the completed map does not cover the complete collected suite")
    return merged


def collect(root, destination):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests",
            "--collect-only",
            "-m",
            "not native",
            "--collect-test-plan",
            str(destination),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        result.check_returncode()
    return json.loads(Path(destination).read_text())


def benchmark_snapshot(mapping, current, current_environment, case):
    """Measure selected unchanged tests using a virtual one-function/input edit."""
    if mapping["snapshot"] != current or mapping["environment"] != current_environment:
        raise ValueError("benchmark needs a complete map of the exact current tree and environment")
    changed = deepcopy(current)
    path, name = BENCHMARK_CASES[case]
    changed["files"][path] = digest((current["files"][path] + case).encode())
    if name:
        function = changed["python"][path]["functions"][name]
        function["digest"] = digest((function["digest"] + case).encode())
    return changed


def make_plan(root, mapping, collection, *, reason=None, count=None, current=None):
    current = current or snapshot(root)
    result = select(
        mapping, current, collection, current_environment=environment(root), full_reason=reason
    )
    result.update(
        commit=git(root, "rev-parse", "HEAD").decode().strip(),
        snapshot=current,
        environment=environment(root),
        collection=collection,
    )
    result["shards"] = shards(result, collection, mapping, count)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("select", "run", "benchmark"):
        command = sub.add_parser(name)
        command.add_argument("--map", type=Path, default=ROOT / ".local/test-map/local.json")
        command.add_argument("--output", type=Path, default=ROOT / ".local/test-map/selection.json")
        command.add_argument("--full", action="store_true")
        if name in {"run", "benchmark"}:
            command.add_argument("--jobs", type=int, default=min(12, os.cpu_count() or 1))
        if name == "benchmark":
            command.add_argument("--case", choices=BENCHMARK_CASES, required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--output", type=Path, default=ROOT / ".local/test-map/main")
    merge = sub.add_parser("merge")
    merge.add_argument("--plan", type=Path, required=True)
    merge.add_argument("--baseline", type=Path)
    merge.add_argument("--parts", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "fetch":
        from ci_tests import fetch_baseline

        return fetch_baseline(ROOT, args.output)
    if args.command == "merge":
        baseline, error = load_map(args.baseline) if args.baseline else (None, None)
        if error:
            parser.error(error)
        write_json(
            args.output,
            merge_parts(
                sorted(args.parts.rglob("part-*.json")),
                json.loads(args.plan.read_text()),
                baseline,
            ),
        )
        return 0
    if args.command in {"run", "benchmark"} and args.jobs < 1:
        parser.error("--jobs must be positive")
    mapping, error = load_map(args.map)
    (ROOT / ".local").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="test-selection-", dir=ROOT / ".local") as temporary:
        scratch = Path(temporary)
        started = time.perf_counter()
        collection = collect(ROOT, scratch / "collection.json")
        current = None
        if args.command == "benchmark":
            if error:
                parser.error(error)
            current = benchmark_snapshot(mapping, snapshot(ROOT), environment(ROOT), args.case)
        plan = make_plan(
            ROOT,
            mapping,
            collection,
            reason="requested full run" if args.full else error,
            current=current,
        )
        if args.command == "benchmark":
            plan["measurement_only"] = args.case
        write_json(args.output, plan)
        print(
            f"{plan['mode']}: {len(plan['selected'])}/{len(collection)} tests; "
            f"local publish guard {'required' if plan['publish_guard'] else 'not selected'}"
        )
        if args.command == "select":
            return 0
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests",
                "-n",
                str(args.jobs),
                "--dist",
                "loadgroup",
                "-m",
                "not native",
                "--select-tests",
                str(args.output),
                "--record-test-map",
                str(scratch / "parts"),
            ],
            cwd=ROOT,
            check=False,
        )
        expected = mapping["snapshot"] if args.command == "benchmark" else plan["snapshot"]
        if snapshot(ROOT) != expected:
            raise ValueError(
                "source changed during the run; the previous dependency map is retained"
            )
        if args.command == "benchmark":
            write_json(
                args.output.with_suffix(".timing.json"),
                {
                    "case": args.case,
                    "seconds": time.perf_counter() - started,
                    "selected": len(plan["selected"]),
                    "total": len(collection),
                    "jobs": args.jobs,
                    "returncode": result.returncode,
                    "environment": environment(ROOT),
                },
            )
        elif result.returncode in (0, 1):
            write_json(
                args.map,
                merge_parts(sorted((scratch / "parts").glob("part-*.json")), plan, mapping),
            )
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
