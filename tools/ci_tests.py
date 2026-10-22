"""GitHub artifact transport and CI planning for the dependency map."""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from test_selection import (
    BENCHMARK_CASES,
    ROOT,
    benchmark_snapshot,
    collect,
    digest,
    environment,
    load_map,
    make_plan,
    merge_parts,
    snapshot,
    write_json,
)


def api(path):
    return subprocess.check_output(["gh", "api", path], cwd=ROOT)


def ancestor(root, older, newer):
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", older, newer],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def artifact_candidates(repo, name, *, main):
    response = json.loads(api(f"repos/{repo}/actions/artifacts?name={name}&per_page=30"))
    for artifact in sorted(response["artifacts"], key=lambda value: -value["id"]):
        if artifact["expired"]:
            continue
        run = json.loads(api(f"repos/{repo}/actions/runs/{artifact['workflow_run']['id']}"))
        if (
            run["conclusion"] == "success"
            and run["path"] == ".github/workflows/ci.yml"
            and run["head_repository"]["full_name"] == repo
            and (
                run["head_branch"] == "main"
                and run["event"] in {"push", "schedule", "workflow_dispatch"}
                if main
                else run["event"] == "pull_request"
            )
        ):
            yield {**artifact, "run": run}


def download_map(repo, artifact, destination):
    payload = api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip")
    with ZipFile(io.BytesIO(payload)) as archive:
        if len(archive.namelist()) != 2 or set(archive.namelist()) != {"map.json", "uv.lock"}:
            raise ValueError("unexpected dependency artifact inventory")
        mapping = json.loads(archive.read("map.json"))
        lock = archive.read("uv.lock")
    if mapping["environment"]["lock"] != digest(lock):
        raise ValueError("dependency map and lock do not match")
    write_json(destination / "map.json", mapping)
    _, error = load_map(destination / "map.json")
    if error:
        raise ValueError(error)
    (destination / "uv.lock").write_bytes(lock)
    return mapping


def fetch_baseline(
    root, destination, *, repo=None, head=None, base=None, pr=None, benchmark_run=None
):
    destination.mkdir(parents=True, exist_ok=True)
    repo = (
        repo
        or subprocess.check_output(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=root,
            text=True,
        ).strip()
    )
    result = {"pinned": False, "python": "3.12", "reason": "no green ancestor map"}
    mapping = None
    try:
        if benchmark_run:
            run = json.loads(api(f"repos/{repo}/actions/runs/{benchmark_run}"))
            if (
                run["conclusion"] != "success"
                or run["path"] != ".github/workflows/ci.yml"
                or run["head_repository"]["full_name"] != repo
            ):
                raise ValueError("benchmark baseline must be a successful owned CI run")
            artifacts = json.loads(api(f"repos/{repo}/actions/runs/{benchmark_run}/artifacts"))
            candidates = [
                artifact
                for artifact in artifacts["artifacts"]
                if artifact["name"].startswith("test-map-") and not artifact["expired"]
            ]
            if len(candidates) != 1:
                raise ValueError("benchmark run needs exactly one complete map/lock artifact")
            mapping = download_map(repo, candidates[0], destination)
            result.update(
                pinned=True,
                python=".".join(map(str, mapping["environment"]["python"])),
                reason="measurement-only baseline; not merge qualification",
            )
            write_json(destination / "fetch.json", result)
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                stream.write(f"pinned=true\npython={result['python']}\n")
            return 0
        for artifact in artifact_candidates(repo, "test-map-main", main=True):
            candidate = download_map(repo, artifact, destination / "candidate")
            if candidate["commit"] != artifact["run"]["head_sha"]:
                raise ValueError("main artifact commit differs from its trusted run")
            if ancestor(root, candidate["commit"], base or "HEAD"):
                mapping = candidate
                shutil.copyfile(destination / "candidate/uv.lock", destination / "uv.lock")
                break
        if mapping:
            if pr and head:
                pull = json.loads(api(f"repos/{repo}/pulls/{pr}"))
                for artifact in artifact_candidates(repo, f"test-map-pr-{pr}", main=False):
                    candidate = download_map(repo, artifact, destination / "candidate")
                    metadata = candidate.get("ci", {})
                    if (
                        metadata.get("pr") == pr
                        and metadata.get("base") == base
                        and candidate["environment"] == mapping["environment"]
                        and metadata.get("main_commit") == mapping["commit"]
                        and metadata.get("head") == artifact["run"]["head_sha"]
                        and artifact["run"]["head_branch"] == pull["head"]["ref"]
                        and pull["head"]["repo"]["full_name"] == repo
                        and (
                            not artifact["run"]["pull_requests"]
                            or any(
                                item["number"] == pr for item in artifact["run"]["pull_requests"]
                            )
                        )
                        and ancestor(root, metadata.get("head", ""), head)
                    ):
                        mapping = candidate
                        break
            write_json(destination / "map.json", mapping)
            result["pinned"] = mapping["snapshot"]["config"] == snapshot(root)["config"]
            result["python"] = ".".join(map(str, mapping["environment"]["python"]))
            result["reason"] = (
                "green ancestor map" if result["pinned"] else "dependency configuration changed"
            )
        else:
            write_json(destination / "map.json", {"unavailable": result["reason"]})
    except (subprocess.CalledProcessError, BadZipFile, ValueError, OSError, KeyError) as error:
        result["reason"] = f"artifact unavailable or rejected; running full tests: {error}"
        write_json(destination / "map.json", {"unavailable": result["reason"]})
    write_json(destination / "fetch.json", result)
    print(result["reason"])
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"pinned={str(result['pinned']).lower()}\npython={result['python']}\n")
    return 0


def ci_plan(args):
    if args.benchmark and args.mode == "weekly":
        raise ValueError("measurement-only scenarios cannot replace the weekly backstop")
    baseline, error = load_map(args.baseline / "map.json")
    reason = "requested full backstop" if args.mode in {"full", "weekly"} else error
    collection = collect(ROOT, args.output / "collection.json")
    current = None
    if args.benchmark:
        if error:
            raise ValueError(error)
        current = benchmark_snapshot(baseline, snapshot(ROOT), environment(ROOT), args.benchmark)
        reason = None
    plan = make_plan(ROOT, baseline, collection, reason=reason, current=current)
    if args.benchmark:
        plan["measurement_only"] = args.benchmark
    metadata = {
        "head": args.head,
        "base": args.base,
        "pr": args.pr,
        "main_commit": baseline.get("ci", {}).get("main_commit", baseline["commit"])
        if baseline
        else None,
    }
    plan["ci"] = metadata
    plan["weekly"] = args.mode == "weekly"
    from makerworld_exports import variants

    publish_matrix = {
        "include": [
            {
                "name": variant.name,
                "printer": variant.arguments[variant.arguments.index("--printer") + 1],
                "nozzle": variant.arguments[variant.arguments.index("--nozzle-diameter-mm") + 1],
            }
            for variant in variants(("h2d", "x2d"))
        ]
    }
    plan["wheel"] = plan["mode"] == "full" or any(
        path.startswith(("src/", "tools/")) or path in {"pyproject.toml", "README.md", "LICENSE"}
        for path in plan["changed"]
    )
    write_json(args.output / "plan.json", plan)
    if baseline:
        write_json(args.output / "baseline.json", baseline)
    shutil.copyfile(ROOT / "uv.lock", args.output / "uv.lock")
    print(
        f"{plan['mode']}: {len(plan['selected'])}/{len(collection)} tests; "
        f"{len(plan['shards'])} runner(s); publish guard={plan['publish_guard']}"
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(
            f"matrix={json.dumps({'shard': list(range(len(plan['shards'])))})}\n"
            f"publish_matrix={json.dumps(publish_matrix)}\n"
            f"wheel={str(plan['wheel']).lower()}\n"
            f"weekly={str(plan['weekly']).lower()}\n"
            f"python={'.'.join(map(str, plan['environment']['python']))}\n"
        )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(
                f"### {'Measurement only; not merge qualification' if args.benchmark else 'Test selection'}\n\n"
                f"{len(plan['selected'])}/{len(collection)} tests; "
                f"{len(plan['shards'])} runners; mode **{plan['mode']}**.\n\n"
                f"Local publish guard: **{'required' if plan['publish_guard'] else 'not selected'}**.\n"
            )


def complete(args):
    plan = json.loads((args.plan / "plan.json").read_text())
    if snapshot(ROOT) != plan["snapshot"]:
        raise ValueError("the completed source does not match the planned source")
    baseline = (
        load_map(args.plan / "baseline.json")[0]
        if (args.plan / "baseline.json").is_file()
        else None
    )
    mapping = merge_parts(sorted(args.parts.rglob("part-*.json")), plan, baseline)
    mapping["ci"] = plan["ci"]
    if not mapping["ci"]["pr"]:
        mapping["ci"]["main_commit"] = mapping["commit"]
    if plan["weekly"]:
        from publish_guard import merge_fingerprints

        mapping["publish"] = merge_fingerprints(
            sorted(args.publish.rglob("fingerprints.json")),
            baseline.get("publish") if baseline else None,
            args.approve,
            args.output.parent / "fingerprint-report.json",
        )
        published_environment = mapping["publish"]["environment"]
        planned_environment = plan["environment"]
        if (
            any(mapping["publish"][key] != plan[key] for key in ("commit", "snapshot"))
            or any(
                published_environment[key] != planned_environment[key]
                for key in ("python", "implementation", "lock")
            )
            or any(
                version != planned_environment["packages"][name]
                for name, version in published_environment["packages"].items()
                if name in planned_environment["packages"]
            )
        ):
            raise ValueError("publish fingerprints belong to another source or environment")
    write_json(args.output / "map.json", mapping)
    shutil.copyfile(args.plan / "uv.lock", args.output / "uv.lock")


def resources():
    print("Platform:", platform.platform())
    cpuinfo = Path("/proc/cpuinfo")
    print(
        "CPU:",
        next(
            (
                line.split(":", 1)[1].strip()
                for line in cpuinfo.read_text().splitlines()
                if line.startswith("model name")
            ),
            subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
            if platform.system() == "Darwin"
            else platform.processor(),
        )
        if cpuinfo.is_file()
        else platform.processor(),
    )
    print("Logical CPUs:", os.cpu_count())
    if hasattr(os, "sched_getaffinity"):
        print("Allowed CPUs:", sorted(os.sched_getaffinity(0)))
    print("Python:", sys.version)
    print("Load:", os.getloadavg())
    if Path("/proc/meminfo").is_file():
        print(
            "\n".join(
                line
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith(("MemTotal:", "MemAvailable:"))
            )
        )
    for name in ("cpu.max", "memory.max", "memory.current"):
        path = Path("/sys/fs/cgroup") / name
        print(name, path.read_text().strip() if path.is_file() else "unavailable")
    siblings = Path("/sys/devices/system/cpu/cpu0/topology/thread_siblings_list")
    print(
        "CPU 0 thread siblings:",
        siblings.read_text().strip() if siblings.is_file() else "unavailable",
    )
    packages = environment(ROOT)["packages"]
    for name in (
        "build123d",
        "cadquery-ocp-novtk",
        "numpy",
        "scipy",
        "pytest",
        "pytest-xdist",
        "coverage",
    ):
        print(name, packages[name])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--output", type=Path, required=True)
    fetch.add_argument("--repo", required=True)
    fetch.add_argument("--head")
    fetch.add_argument("--base")
    fetch.add_argument("--pr", type=int)
    fetch.add_argument("--benchmark-run", type=int)
    plan = sub.add_parser("plan")
    plan.add_argument("--baseline", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--mode", choices=("selected", "full", "weekly"), required=True)
    plan.add_argument("--head", required=True)
    plan.add_argument("--base", required=True)
    plan.add_argument("--pr", type=int, default=0)
    plan.add_argument("--benchmark", choices=("", *BENCHMARK_CASES), default="")
    finish = sub.add_parser("complete")
    finish.add_argument("--plan", type=Path, required=True)
    finish.add_argument("--parts", type=Path, required=True)
    finish.add_argument("--publish", type=Path)
    finish.add_argument("--approve", default="")
    finish.add_argument("--output", type=Path, required=True)
    sub.add_parser("resources")
    args = parser.parse_args(argv)
    if args.command == "fetch":
        return fetch_baseline(
            ROOT,
            args.output,
            repo=args.repo,
            head=args.head,
            base=args.base,
            pr=args.pr,
            benchmark_run=args.benchmark_run,
        )
    if args.command == "plan":
        ci_plan(args)
    elif args.command == "complete":
        complete(args)
    else:
        resources()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
