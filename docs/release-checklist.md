# Release checklist

Use this before cutting a release. These checks cover the source and packages. They don't publish anything or prove that a printed part fits.

## Set up the checkout

1. Review the source changes, version and licence boundary.
2. Run `uv sync --group dev` with the configured package feeds.
3. Keep `uv.lock`, virtual environments, caches and generated jobs out of the commit.
4. Confirm `src/cargo_grid/_version.py` has the intended version.

## Run the portable checks

The documented workstation uses 12 process workers for the full portable suite. Use fewer on a smaller machine:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q -n 12 --dist loadfile -m "not native and not reference"
git diff --check
uv build
uv run python tools/check_distributions.py dist
```

Use fewer workers on a smaller machine. This command includes the `slow` tier. CI uses four workers with `--dist worksteal` for the portable tier, so a long geometry test does not leave other workers idle behind a whole-file assignment. `tests/conftest.py` collects the tests that share the whole-catalogue H2D plan fixture first, so they start together at the head of one worker's queue; work stealing takes tests from the end of a queue, and building that plan on a second worker would cost minutes. With many workers and a small selection, stealing can still reach those tests; `--dist loadgroup` always keeps them on one worker. `--dist loadfile` remains useful for controlled comparisons and module-local fixtures. Do not use xdist for native Bambu or local-reference tests.

Build into a fresh directory. The distribution checker expects one wheel and one source archive and rejects extra build output. It checks package metadata, entry points, the licence and the allowlist; it isn't a general secret or licence scanner.

Install the wheel into a clean Python 3.12 environment outside the source path. Check `python -m cargo_grid --version`, `cargo-grid --help` and at least one generated part. Reimport its STEP and check that it is one valid, positive-volume solid with the expected size.

## What CI checks

CI runs for pull requests, pushes to `main` and manual dispatches. It does not run a second copy for every push to a PR branch. A concurrency group cancels older runs when a newer commit reaches the same PR. A nightly schedule, or a manual dispatch with `tier: slow`, runs the slow tier instead of the portable job.

The Linux job installs the CAD runtime libraries, runs Ruff, runs the portable tests with four process workers, builds both distributions and installs the wheel into a clean environment. Its smoke commands generate a tile, bracket, normal stop and ramp, then check their manifest dimensions, orientations and support settings with named assertion messages. Resource logs report the CPU model, available CPUs, affinity, hyperthread siblings, cgroup limits and memory where the runner exposes them. `tools/ci_memory_sampler.py` wraps the test command, samples the whole process tree once a second and reports the lowest `MemAvailable` and the peak combined RSS and PSS of pytest and its workers; GNU time's maximum RSS is only the largest single process.

The portable test command includes pytest's built-in `--durations=0 --durations-min=0` report. It lists every test's setup, call and teardown durations, slowest first, without adding a timing plugin. Add the same flags to a local run when investigating slow tests. These are per-phase elapsed times: phases on parallel workers can overlap, so their sum is not the CI job's wall time. The same run writes a JUnit report, and `tools/check_test_durations.py` fails the job if any portable test takes longer than its per-test budget, unless the test is in the checker's reviewed allowlist with its own limit. It warns, without failing, when the pytest session runs past its target, because hosted runners differ in speed from run to run. `tests/README.md` describes how to keep new tests inside the budget.

The slow tier holds exhaustive parameter sweeps and review-package exports. Portable CI keeps at least one case for each connector sex, joint style, family variant and recorded pose, plus the largest size; the slow tier adds interior sizes, repeated poses and full exports. It runs with `--dist loadgroup`, which hands out one test at a time, so its few multi-minute tests start on separate workers.

Keep option propagation, design metadata, family geometry and whole-catalogue packing as separate test responsibilities. CLI option tests capture the resolved spec at the construction boundary; each distinct physical hole configuration also goes through real CLI export. Metadata-only accessory tests use a small real solid at `make_accessory`, while family geometry tests still construct the actual bodies. Neither parser results nor manifest hole counts prove that a bore was cut.

The bracket catalogue-fit test limits enumeration to all maintained brackets, then makes independent source-pose and print-pose catalogue requests with real geometry. The H2D plan tests build the whole unpatched inventory once in a read-only module fixture, compare complete parameters against independent per-family contracts, and check actual posed solids for each plate group, reach, gaps, projected footprints, the export replan, support settings and family-grouped plates as separate tests. Add an independent inventory and grouping contract when introducing a family; don't replace those contracts with global design totals or incidental plate numbers. The native archive's plate limit remains a separate export contract.

For a test refactor, collect test IDs before and after, map renamed or split assertions to their new owners, and compare equivalent selectors with the same worker count and `--dist loadfile`. Keep raw duration reports and measurements with the review, not in maintained documentation. Run the full portable suite after the focused checks. Keep geometry cases and tolerances intact; don't introduce shared mutable solids or cross-run geometry caches to improve a timing result. A module fixture that tests only read is fine.

Export-lifecycle tests check fresh ownership, mutations between requests, one checked mesh shared by output formats and copies, and the actual serialized STL/3MF topology and coordinates. Tile-construction tests check independent joint-tool topology and one multi-tool socket cut; the family, hole, scaled-interface, STEP and mesh tests still establish the resulting geometry. When changing these stages, compare complete cold CLI exports as well as the suite. Record source and dependency versions, worker count, host contention, CPU time and peak memory; overlapping test or profiler phases must not be added together as wall time.

CI can't use private reference files, local slicer profiles or printers. Treat a CI failure as a source, package or test failure until the log shows otherwise.

## Optional local Bambu and reference checks

Normal generation does not need either resource. To include the optional checks, point `CARGO_GRID_BAMBU` at the chosen Bambu Studio CLI and optionally point `CARGO_GRID_REFERENCE` at a local reference 3MF, then run the suite serially:

```sh
uv run pytest -q
```

Unavailable checks skip. `cargo-grid compare-reference --reference-file path/to/reference.3mf --output outputs/reference-report.json` runs the same local interface comparison without uploading the file.

Native CLI import/export isn't a print test. When Bambu settings or modifiers change, slice the affected job with the intended machine, process and materials. Check material roles, nozzle mapping, support contact, X-socket clearance, model/support paths and tower reach. Keep profiles, G-code and logs in ignored output directories.

If a separate CAD workbench is needed, use its documented interpreter and launchers from this repository root. Generate a fresh project-relative STEP, inspect that same file and record the workbench revision with the local evidence. Do not copy workbench files into this repository.

## Check the physical assumptions

- Keep original roofed joints and experimental open-through tile-edge joints clearly separated.
- Confirm the default full 10 mm hole pattern or a `--no-holes` opt-out; roof support remains opt-in.
- Confirm roof support appears only under retained west/south female roofs.
- Recheck support, brim and tower paths after profile changes.
- Record printer, nozzle, layer height, materials, fit, roof finish, support removal and flatness for any physical trial.

One print doesn't establish fit or strength for every profile, material, climate or load.

## Prepare release source

After the release commit has been reviewed, create the release input from tracked files at that revision, for example with `git archive`. Do not zip the working directory.

Exclude local locks, environments, caches, reference meshes, system profiles, G-code, study output and runtime diagnostics. The maintained documentation images are the exception: seven overview images, 105 accessory thumbnails and their provenance manifest. The source archive contains exactly 112 PNGs; the runtime wheel contains none. A partial image update keeps the source and tool revisions for retained images and records separate provenance for regenerated images.

Run `tools/render_docs.py --check` to confirm image hashes, links and one-to-one inventory coverage. Tagging, uploading packages/models and merging remain separate maintainer actions.
