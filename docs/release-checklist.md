# Release checklist

Use this before cutting a release. These checks cover the source and packages. They don't publish anything or prove that a printed part fits.

## Set up the checkout

1. Review the source changes, version and licence boundary.
2. Run `uv sync --group dev` with the configured package feeds.
3. Keep `uv.lock`, virtual environments, caches and generated jobs out of the commit.
4. Confirm `src/cargo_grid/_version.py` has the intended version.

## Run the portable checks

On a 12-core workstation, run the full portable suite with 12 workers:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q tests -n 12 --dist loadgroup -m "not native"
git diff --check
uv build
uv run python tools/check_distributions.py dist
```

Use fewer workers on a smaller machine. This release command includes the `slow` tier and the separately collected default contracts. CI uses four workers per selected runner with `--dist loadgroup`. Each run constructs fresh geometry; ordinary fixtures share read-only inputs within that run. Do not use xdist for native Bambu tests.

Build into a fresh directory. The distribution checker expects one wheel and one source archive and rejects extra build output. It checks package metadata, entry points, the licence and the allowlist; it isn't a general secret or licence scanner.

Install the wheel into a clean Python 3.12 environment outside the source path. Check `python -m cargo_grid --version`, `cargo-grid --help` and at least one generated part. Reimport its STEP and check that it is one valid, positive-volume solid with the expected size.

## What CI checks

CI runs for pull requests, pushes to `main` and manual dispatches. It does not run a second copy for every push to a PR branch. A concurrency group cancels older runs when a newer commit reaches the same PR. PRs select affected tests, including slow cases, using main's recorded dependencies and exact lock; eligible later pushes use that PR's last green push. Infrastructure changes and unavailable or incompatible maps force a full run. Main pushes, weekly runs and manual full dispatches retain the complete non-native suite. See [the test guide](../tests/README.md) for selection, lock artifacts and the local publish guard.

Linux runners install the CAD runtime libraries and use the planned lock. Ruff and formatting always run; source, tooling and packaging changes also build both distributions and install the wheel into a clean environment with the pinned runtime dependencies. Smoke commands generate tiles, a bracket, a normal stop, ramps, a rod and a brace, then check their manifest dimensions, orientations, support settings and recorded fit scope. Resource logs report the CPU model, available CPUs, affinity, hyperthread siblings, cgroup limits and memory where the runner exposes them. `tools/ci_memory_sampler.py` wraps the test command, samples the whole process tree once a second and reports the lowest `MemAvailable` and the peak combined RSS and PSS of pytest and its workers; GNU time's maximum RSS is only the largest single process.

The portable test command includes pytest's built-in `--durations=0 --durations-min=0` report. It lists every test's setup, call and teardown durations, slowest first, without adding a timing plugin. Add the same flags to a local run when investigating slow tests. These are per-phase elapsed times: phases on parallel workers can overlap, so their sum is not the CI job's wall time. The same run writes a JUnit report, and `tools/check_test_durations.py` fails the job if any portable test takes longer than its per-test budget, unless the test is in the checker's reviewed allowlist with its own limit. It warns, without failing, when the pytest session runs past its target, because hosted runners differ in speed from run to run. `tests/README.md` describes how to keep new tests inside the budget.

The slow marker still identifies exhaustive parameter sweeps, assembled geometry checks and complete exports; it is not a weekly-only tier. Geometry contracts have bounded owners; metadata and policy checks use stand-ins rather than repeating real construction. Recorded durations balance selected complete files across up to four runners, and `loadgroup` keeps expensive shared plans and assemblies on one worker. Weekly CI additionally builds all published variants with fresh dependencies and compares their fingerprints with the preceding approved run.

Keep option propagation, design metadata, family geometry and whole-catalogue packing as separate test responsibilities. CLI option tests capture the resolved spec at the construction boundary; each distinct physical hole configuration also goes through real CLI export. Metadata-only accessory tests use a small real solid at `make_accessory`, while family geometry tests still construct the actual bodies. Neither parser results nor manifest hole counts prove that a bore was cut.

The bracket catalogue-fit test limits enumeration to all maintained brackets, then makes independent source-pose and print-pose catalogue requests with real geometry. Catalogue metadata tests compare complete parameters against independent per-family contracts using small inputs. Geometry integrations construct one real family group at a time, keeping actual posed solids, reach, gaps, projected footprints and independent export reconstruction. Auto group exports use real bodies and a real female tile for mixed-job context. Each group's plate count is checked separately, with a small composition check and the complete publish comparison covering their combined count. Add an independent inventory and grouping contract when introducing a family; don't replace those contracts with global design totals or incidental plate numbers. The native archive's plate limit remains a separate export contract.

For a test refactor, collect test IDs before and after and map renamed, moved or split assertions to their new owners. Keep raw duration reports and measurements with the review, not in maintained documentation. Run the full portable suite after the focused checks. Keep geometry cases and tolerances intact. Share read-only inputs through ordinary fixtures within a run, copy before mutation, and keep constructor-operation tests on the real factory.

Export-lifecycle tests check fresh ownership, mutations between requests, one checked mesh shared by output formats and copies, and the actual serialized STL/3MF topology and coordinates. Tile-construction tests check independent joint-tool topology and one multi-tool socket cut; the family, hole, scaled-interface, STEP and mesh tests still establish the resulting geometry. When changing these stages, compare complete cold CLI exports as well as the suite. Record source and dependency versions, worker count, host contention, CPU time and peak memory; overlapping test or profiler phases must not be added together as wall time.

CI can't use private reference files, local slicer profiles or printers. Treat a CI failure as a source, package or test failure until the log shows otherwise.

## Optional local Bambu checks

Normal generation does not need Bambu Studio. To include the optional checks, point `CARGO_GRID_BAMBU` at the chosen Bambu Studio CLI, then run the suite serially:

```sh
uv run pytest -q tests -m native
```

Unavailable checks skip.

Native CLI import/export isn't a print test. When Bambu settings or modifiers change, slice the affected job with the intended machine, process and materials. Check material roles, nozzle mapping, support contact, X-socket clearance, model/support paths and tower reach. Keep profiles, G-code and logs in ignored output directories.

If a separate CAD workbench is needed, use its documented interpreter and launchers from this repository root. Generate a fresh project-relative STEP, inspect that same file and record the workbench revision with the local evidence. Do not copy workbench files into this repository.

## Check the physical assumptions

- Keep the original roofed tile-edge geometry unchanged.
- Confirm the default full 10 mm hole pattern or a `--no-holes` opt-out; roof support remains opt-in.
- Confirm global normal Auto support is on when requested; inspect female pocket roofs and other overhangs without assuming every object receives support.
- Recheck support, brim and tower paths after profile changes.
- Record printer, nozzle, layer height, materials, fit, roof finish, support removal and flatness for any physical trial.

One print doesn't establish fit or strength for every profile, material, climate or load.

## Prepare release source

After the release commit has been reviewed, create the release input from tracked files at that revision, for example with `git archive`. Do not zip the working directory.

Exclude local lock files, environments, caches, reference meshes, system profiles, G-code, study output and runtime diagnostics. The documentation images are the exception: seven overview images, 105 accessory thumbnails and their provenance manifest. The source archive contains exactly 112 PNGs; the runtime wheel contains none. A partial image update keeps the source and tool revisions of the images it doesn't touch and records separate provenance for the new ones.

Run `tools/render_docs.py --check` to confirm image hashes, links and one-to-one inventory coverage. Tagging, uploading packages/models and merging remain separate maintainer actions.
