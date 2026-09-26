"""Sample combined memory of a Linux process tree while a CI command runs.

`/usr/bin/time -v` reports the largest single process, not the sum of pytest-xdist workers.
This wrapper runs the command, samples every process in its tree and the host's
MemAvailable low-water mark, then prints a short summary. The command's exit status is
returned unchanged.
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

PROC = Path("/proc")


def meminfo_kib() -> dict[str, int]:
    values = {}
    for line in (PROC / "meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields:
            values[name] = int(fields[0])
    return values


def children_by_parent() -> dict[int, list[int]]:
    tree: dict[int, list[int]] = {}
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # The command name can contain spaces or parentheses; the parent PID follows the last ")".
            parent = int((entry / "stat").read_text().rpartition(")")[2].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        tree.setdefault(parent, []).append(int(entry.name))
    return tree


def descendants(root: int) -> list[int]:
    tree = children_by_parent()
    found, pending = [], [root]
    while pending:
        pid = pending.pop()
        found.append(pid)
        pending.extend(tree.get(pid, ()))
    return found


def process_memory_kib(pid: int) -> tuple[int, int | None]:
    """Return resident and proportional set sizes; PSS is None where smaps_rollup is hidden."""
    rss = 0
    try:
        for line in (PROC / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
                break
        text = (PROC / str(pid) / "smaps_rollup").read_text()
    except PermissionError:
        return rss, None
    except OSError:
        # The process exited between listing and reading.
        return 0, 0
    for line in text.splitlines():
        if line.startswith("Pss:"):
            return rss, int(line.split()[1])
    # Exiting and zombie processes have no resident memory or PSS line.
    return rss, None if rss else 0


class Sampler:
    def __init__(self, root: int, interval: float):
        self.root = root
        self.interval = interval
        self.stop = threading.Event()
        self.samples = 0
        self.peak_rss = (0, 0.0, 0)
        self.peak_pss = (0, 0.0)
        self.pss_available = True
        self.low_available = (None, 0.0)
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sample(self) -> None:
        elapsed = time.monotonic() - self.started
        rss_total = pss_total = count = 0
        for pid in descendants(self.root):
            rss, pss = process_memory_kib(pid)
            if rss:
                count += 1
            rss_total += rss
            if pss is None:
                self.pss_available = False
            else:
                pss_total += pss
        available = meminfo_kib().get("MemAvailable")
        self.samples += 1
        if rss_total > self.peak_rss[0]:
            self.peak_rss = (rss_total, elapsed, count)
        if pss_total > self.peak_pss[0]:
            self.peak_pss = (pss_total, elapsed)
        if available is not None and (
            self.low_available[0] is None or available < self.low_available[0]
        ):
            self.low_available = (available, elapsed)

    def run(self) -> None:
        while not self.stop.is_set():
            self.sample()
            self.stop.wait(self.interval)


def gib(kib: int | None) -> str:
    return "unavailable" if kib is None else f"{kib / 1024**2:.2f} GiB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command to run after --")
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the command to run after --")
    if not (PROC / "meminfo").exists():
        parser.error("this sampler needs Linux /proc")
    total = meminfo_kib().get("MemTotal")
    process = subprocess.Popen(command)
    sampler = Sampler(process.pid, args.interval)
    sampler.thread.start()
    status = process.wait()
    sampler.stop.set()
    sampler.thread.join()
    rss, rss_at, count = sampler.peak_rss
    pss, pss_at = sampler.peak_pss
    available, available_at = sampler.low_available
    lines = [
        f"Memory samples: {sampler.samples} every {args.interval:g} s",
        f"MemTotal: {gib(total)}",
        f"Lowest MemAvailable: {gib(available)} at {available_at:.0f} s",
        f"Peak combined RSS of the command tree: {gib(rss)} at {rss_at:.0f} s, {count} processes",
        "Peak combined PSS of the command tree: "
        + (f"{gib(pss)} at {pss_at:.0f} s" if sampler.pss_available else "unavailable"),
    ]
    print("\n".join(lines), flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("### Test memory\n\n" + "\n".join(f"- {line}" for line in lines) + "\n")
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    sys.exit(main())
