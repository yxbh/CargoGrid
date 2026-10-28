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
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

FORMAT_VERSION = 3
ROOT = Path(__file__).resolve().parents[1]
INFRASTRUCTURE = {
    "tests/selection.py",
    "tools/test_selection.py",
    "tools/publish_guard.py",
    "tools/check_test_durations.py",
    "tools/check_distributions.py",
    "pytest.ini",
    ".coveragerc",
}
CONFIG_SECTIONS = ("project", "dependency-groups", "build-system")


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


def load_map(path):
    try:
        mapping = json.loads(Path(path).read_text(encoding="utf-8"))
        if mapping["version"] != FORMAT_VERSION:
            raise ValueError("unsupported dependency map version")
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
            setup = mapping["fixtures"][fixture]
            for path, names in setup["functions"].items():
                records[test]["functions"][path].update(names)
            records[test]["data"].update(setup["data"])
            records[test]["process"] |= setup["process"]
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


def _select(mapping, current, collection, *, current_environment=None, full_reason=None):
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
            or path.startswith((".github/workflows/", ".github/actions/"))
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
    return {
        "version": FORMAT_VERSION,
        "mode": "full" if full_reason else "selected",
        "changed": changed,
        "selected": sorted(selected),
        "reasons": dict(reasons),
        "publish_guard": bool(full_reason) or source_changed,
    }


def select(mapping, current, collection, *, current_environment=None, full_reason=None):
    try:
        return _select(
            mapping,
            current,
            collection,
            current_environment=current_environment,
            full_reason=full_reason,
        )
    except (KeyError, TypeError, AttributeError) as error:
        result = _select(None, current, collection, full_reason=f"damaged dependency map: {error}")
        result["map_error"] = True
        return result


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
            if (
                not isinstance(durations[node], (int, float))
                or not math.isfinite(durations[node])
                or durations[node] < 0
            ):
                raise ValueError(f"invalid recorded duration for {node}")
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


def make_plan(root, mapping, collection, *, reason=None, count=None):
    current = snapshot(root)
    recorded_environment = environment(root)
    result = select(
        mapping, current, collection, current_environment=recorded_environment, full_reason=reason
    )
    try:
        allocations = shards(
            result, collection, None if result.get("map_error") else mapping, count
        )
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        result = select(None, current, collection, full_reason=f"damaged dependency map: {error}")
        result["map_error"] = True
        allocations = shards(result, collection, None, count)
    result.update(
        commit=git(root, "rev-parse", "HEAD").decode().strip(),
        snapshot=current,
        environment=recorded_environment,
        collection=collection,
    )
    result["shards"] = allocations
    return result


def cached_map(folder):
    mapping, error = load_map(folder / "map.json")
    if mapping:
        try:
            if mapping["environment"]["lock"] != digest((folder / "uv.lock").read_bytes()):
                raise ValueError("cached map and lock differ")
        except (OSError, KeyError, TypeError, ValueError) as problem:
            return None, f"cannot use cached map {folder}: {problem}"
    return mapping, error


def ci_baseline(args):
    args.output.mkdir(parents=True, exist_ok=True)
    mapping, reason = cached_map(args.main)
    pinned, python = False, "3.12"
    if mapping:
        try:
            pinned = mapping["snapshot"]["config"] == snapshot(ROOT)["config"]
            version = mapping["environment"]["python"]
            if len(version) != 3 or any(not isinstance(part, int) or part < 0 for part in version):
                raise ValueError("invalid recorded Python version")
            python = ".".join(map(str, version))
            if args.same_repo == "true":
                overlay, error = cached_map(args.pr)
                if overlay and overlay["environment"] == mapping["environment"]:
                    mapping = overlay
                elif error:
                    print(error)
            shutil.copyfile(args.main / "uv.lock", args.output / "uv.lock")
            reason = "cached map" if pinned else "dependency configuration changed"
        except (OSError, KeyError, TypeError, ValueError) as error:
            mapping, pinned = None, False
            reason = f"cached baseline unusable; running full tests: {error}"
    write_json(args.output / "map.json", mapping or {"unavailable": reason})
    print(reason)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"pinned={str(pinned).lower()}\npython={python}\n")


def ci_plan(args):
    baseline, error = load_map(args.baseline / "map.json")
    collection = collect(ROOT, args.output / "collection.json")
    plan = make_plan(
        ROOT,
        baseline,
        collection,
        reason="requested full backstop" if args.mode in {"full", "weekly"} else error,
    )
    plan["weekly"] = args.mode == "weekly"
    plan["contracts_only"] = bool(plan["selected"]) and all(
        node.startswith("tests/contracts/") for node in plan["selected"]
    )
    if plan["contracts_only"]:
        plan["shards"] = [plan["selected"]]
    plan["wheel"] = plan["mode"] == "full" or any(
        path.startswith(("src/", "tools/")) or path in {"pyproject.toml", "README.md", "LICENSE"}
        for path in plan["changed"]
    )
    write_json(args.output / "plan.json", plan)
    if baseline and not plan.get("map_error"):
        write_json(args.output / "baseline.json", baseline)
    shutil.copyfile(ROOT / "uv.lock", args.output / "uv.lock")
    runners = 0 if plan["contracts_only"] else len(plan["shards"])
    print(
        f"{plan['mode']}: {len(plan['selected'])}/{len(collection)} tests; "
        f"{runners} test runner(s); publish guard={plan['publish_guard']}"
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(
            f"matrix={json.dumps({'shard': list(range(len(plan['shards'])))})}\n"
            f"wheel={str(plan['wheel']).lower()}\n"
            f"weekly={str(plan['weekly']).lower()}\n"
            f"contracts_only={str(plan['contracts_only']).lower()}\n"
            f"python={'.'.join(map(str, plan['environment']['python']))}\n"
        )
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(
                f"### Test selection\n\n{len(plan['selected'])}/{len(collection)} tests; "
                f"{runners} test runners; mode **{plan['mode']}**.\n\n"
                f"Contracts-only execution in checks: **{plan['contracts_only']}**.\n\n"
                f"Local publish guard: **{'required' if plan['publish_guard'] else 'not selected'}**.\n"
            )


def ci_complete(args):
    plan = json.loads((args.plan / "plan.json").read_text())
    if snapshot(ROOT) != plan["snapshot"]:
        raise ValueError("the completed source does not match the planned source")
    path = args.plan / "baseline.json"
    baseline, error = load_map(path) if path.is_file() else (None, None)
    if error:
        raise ValueError(error)
    mapping = merge_parts(sorted(args.parts.rglob("part-*.json")), plan, baseline)
    if plan["weekly"]:
        from publish_guard import merge_fingerprints

        mapping["publish"] = merge_fingerprints(
            sorted(args.publish.rglob("fingerprints.json")),
            baseline.get("publish") if baseline else None,
            args.approve,
            args.output.parent / "fingerprint-report.json",
        )
    write_json(args.output / "map.json", mapping)
    shutil.copyfile(args.plan / "uv.lock", args.output / "uv.lock")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("select", "run"):
        command = sub.add_parser(name)
        command.add_argument("--map", type=Path, default=ROOT / ".local/test-map/local.json")
        command.add_argument("--output", type=Path, default=ROOT / ".local/test-map/selection.json")
        command.add_argument("--full", action="store_true")
        if name == "run":
            command.add_argument("--jobs", type=int, default=min(12, os.cpu_count() or 1))
    merge = sub.add_parser("merge")
    merge.add_argument("--plan", type=Path, required=True)
    merge.add_argument("--baseline", type=Path)
    merge.add_argument("--parts", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    baseline = sub.add_parser("ci-baseline")
    baseline.add_argument("--main", type=Path, required=True)
    baseline.add_argument("--pr", type=Path, required=True)
    baseline.add_argument("--same-repo", choices=("true", "false"), default="false")
    baseline.add_argument("--output", type=Path, required=True)
    plan = sub.add_parser("ci-plan")
    plan.add_argument("--baseline", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--mode", choices=("selected", "full", "weekly"), required=True)
    finish = sub.add_parser("ci-complete")
    finish.add_argument("--plan", type=Path, required=True)
    finish.add_argument("--parts", type=Path, required=True)
    finish.add_argument("--publish", type=Path)
    finish.add_argument("--approve", default="")
    finish.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "ci-baseline":
        ci_baseline(args)
        return 0
    if args.command == "ci-plan":
        ci_plan(args)
        return 0
    if args.command == "ci-complete":
        ci_complete(args)
        return 0
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
    if args.command == "run" and args.jobs < 1:
        parser.error("--jobs must be positive")
    mapping, error = load_map(args.map)
    (ROOT / ".local").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="test-selection-", dir=ROOT / ".local") as temporary:
        scratch = Path(temporary)
        collection = collect(ROOT, scratch / "collection.json")
        plan = make_plan(
            ROOT,
            mapping,
            collection,
            reason="requested full run" if args.full else error,
        )
        if plan.get("map_error"):
            mapping = None
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
        if snapshot(ROOT) != plan["snapshot"]:
            raise ValueError(
                "source changed during the run; the previous dependency map is retained"
            )
        if result.returncode in (0, 1):
            write_json(
                args.map,
                merge_parts(sorted((scratch / "parts").glob("part-*.json")), plan, mapping),
            )
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
