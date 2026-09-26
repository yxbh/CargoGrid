"""Check per-test durations from a pytest JUnit XML report against the portable CI budget.

Each test's reported time covers its setup, call and teardown, including any module fixture
it is the first to request. A portable test over the budget fails the check unless it is in
the reviewed allowlist below, which gives it its own upper limit. Mark genuinely exhaustive
checks ``slow`` instead of adding entries. A pytest session longer than the target only
produces a warning, because hosted-runner speed varies between runs.
"""

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Four workers share the hosted runner's cores, so a test there takes about five times as long
# as the same test alone on a fast workstation, and runners differ by up to about 1.4x between
# runs. The slowest representative checks that cannot be split further build one real contour
# ramp or whole catalogue group in roughly 50-90 s, so a lower budget would fail on slow
# runners. New exhaustive sweeps and multi-part exports take several minutes and still fail.
DEFAULT_BUDGET_SECONDS = 120.0
DEFAULT_TARGET_SECONDS = 780.0

H2D = "tests/test_h2d_catalogue.py::"
# Reviewed exceptions: node id -> (limit in seconds, reason).
ALLOWLIST: dict[str, tuple[float, str]] = {
    H2D + "test_h2d_dual_safe_plan_keeps_full_family_inventory_and_hardware_zones": (
        600.0,
        "first user of the shared H2D plan fixture, which builds and packs the whole catalogue",
    ),
    H2D + "test_h2d_dual_safe_prepared_project_keeps_the_planned_plates": (
        240.0,
        "replans every catalogue design through the export snapshot",
    ),
}


def node_id(case: ET.Element, root: Path) -> str:
    """Rebuild a pytest node id from JUnit classname/name without importing the tests."""
    name = case.get("name", "")
    # `--dist loadgroup` appends "@group" to grouped node ids.
    if name.rfind("@") > name.rfind("]"):
        name = name[: name.rfind("@")]
    parts = case.get("classname", "").split(".")
    for split in range(len(parts), 0, -1):
        path = Path(*parts[:split]).with_suffix(".py")
        if (root / path).is_file():
            return "::".join([path.as_posix(), *parts[split:], name])
    return "::".join([*parts, name])


def read_report(path: Path, root: Path) -> tuple[float, list[tuple[str, float]]]:
    tree = ET.parse(path)
    suites = [tree.getroot()] if tree.getroot().tag == "testsuite" else tree.getroot()
    session = sum(float(suite.get("time", 0)) for suite in suites)
    cases = [
        (node_id(case, root), float(case.get("time", 0)))
        for suite in suites
        for case in suite.iter("testcase")
        if case.find("skipped") is None
    ]
    return session, cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="pytest --junitxml output")
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--target-seconds", type=float, default=DEFAULT_TARGET_SECONDS)
    parser.add_argument("--show", type=int, default=15, help="slowest tests to list")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    args = parser.parse_args()
    session, cases = read_report(args.report, args.root)
    if not cases:
        parser.error(f"no executed tests found in {args.report}")
    seen = {name for name, _ in cases}
    failures = []
    for name, seconds in cases:
        limit, _ = ALLOWLIST.get(name, (args.budget_seconds, ""))
        if seconds > limit:
            failures.append((name, seconds, limit))
    stale = sorted(set(ALLOWLIST) - seen)
    slowest = sorted(cases, key=lambda case: -case[1])[: args.show]
    lines = [
        f"Portable tests: {len(cases)}, pytest session {session:.0f} s "
        f"(target {args.target_seconds:.0f} s), per-test budget {args.budget_seconds:.0f} s",
        "Slowest setup+call+teardown times:",
        *(
            f"  {seconds:7.1f} s  {name}" + ("  [allowlisted]" if name in ALLOWLIST else "")
            for name, seconds in slowest
        ),
    ]
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("### Test durations\n\n```\n" + "\n".join(lines) + "\n```\n")
    if session > args.target_seconds:
        print(
            f"::warning title=Portable test time::pytest session took {session:.0f} s, "
            f"over the {args.target_seconds:.0f} s target"
        )
    for name in stale:
        print(f"::warning title=Stale duration allowlist entry::{name} did not run")
    for name, seconds, limit in failures:
        print(
            f"::error title=Test over duration budget::{name} took {seconds:.1f} s, over its "
            f"{limit:.0f} s limit. Speed it up with representative sites, local clipped "
            "Booleans or a shared fixture, or mark exhaustive coverage slow."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
