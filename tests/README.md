# Writing tests

`uv run pytest -q` collects `tests/contracts/`: parameters, synthetic packing and serialization, printer settings, export ownership and failure paths. Put real family geometry and integration checks elsewhere under `tests/`. The full command names `tests` so it includes both directories; a marker filter alone is not the default tier.

| Selection | Command |
|---|---|
| Default contracts | `uv run pytest -q` |
| Seed or refresh a local dependency map | `uv run python tools/test_selection.py run --full` |
| Affected cases, including selected slow cases | `uv run python tools/test_selection.py run` |
| Inspect selection without running it | `uv run python tools/test_selection.py select` |
| Full portable, including slow | `uv run pytest -q tests -n 12 --dist loadgroup -m "not native"` |
| Complete slow selection | `uv run pytest -q tests -n 4 --dist loadgroup -m "slow and not native"` |
| Optional native Bambu | Supply `CARGO_GRID_BAMBU`, then `uv run pytest -q tests -m native` serially |

Use fewer workers on smaller machines (`--jobs N` for the local selector). Selection commands collect the complete non-native suite before deselecting. Native checks are excluded by default; an explicit `-m native` overrides that selection. Unavailable external checks remain skips.

The local targets are 30 seconds for default contracts and two minutes for the full portable selection. Record actual timings, toolchain, workers and contention in the review; do not weaken a contract to meet a timing target. `tools/check_test_durations.py` also checks the PR's individual test costs from its JUnit report. `--durations=0 --durations-min=0` prints every phase, whose sum is not parallel wall time.

## Change-based selection

The recorder uses coverage.py function contexts, project-owned shared-fixture contexts and opened data files. While recording it clears project `lru_cache` wrappers before each case; every fixture peer inherits setup and lazy work performed by any peer. The selector compares function ASTs and follows constants, imports, defaults, decorators and class fields. Changes that can reach import-time initializers, defaults or decorators request a full run because those calls do not have a per-test context. Comments and formatting alone do not change a function. Contracts, unknown/new cases and previously failed or skipped cases always run. Process-starting cases run conservatively whenever a tracked input changes, since child dependencies are not recorded per test.

The local map and reports live under ignored `.local/test-map/`. A missing, invalid or environment-mismatched map requests a full run with a reason; there is no geometry or exported-file cache. A successful or completed failing local run refreshes the cases it executed, preserving failures for the next selection. An interrupted run cannot replace the map with incomplete coverage. A Linux CI map is not a substitute for recording the local Python, package versions and platform.

PR CI restores main's most recent successful map and installs its exact `uv.lock` and Python patch version. Same-repository PRs can select incrementally against their own last green map when its lock and complete environment match main's. GitHub's branch-scoped caches keep other PRs' maps out; the selector compares file contents, so moving the base or rewriting a branch does not require an ancestry check. A dependency declaration change resolves fresh and runs everything. Missing or expired caches also fall back to a full run, not an empty selection.

Any `conftest.py`, selection/CI tooling, workflow, local CI action, source package data or the dependency/build/pytest sections of `pyproject.toml` forces a full run. Ruff and Hatch settings alone do not. Adding the `full-tests` label requests one too. Every push to main, weekly run and manual full dispatch runs every non-native case, including slow cases, and records a new map. No tests change tiers. Selected complete files are balanced by recorded durations over up to four runners; `loadgroup` keeps expensive shared plans on one worker inside each runner. Unknown cases get a cost rather than an omission.

CI caches dependency wheels for the lock and platform, not geometry, meshes or exported files; every geometry check constructs its inputs again. Linux CAD jobs install their system libraries with apt. Main and backstop runs still resolve fresh dependencies. Planning collects the whole suite without installing CAD system libraries; only selected CAD jobs and installed-wheel CAD checks request them. A contracts-only selection runs in the checks job instead of starting test shards, with the same recording and per-case budget. The aggregate merges those parts with the exact pinned Python but needs no CAD packages.

Only a successful aggregate `CI` check saves a reusable map and lock. Main keys start with `test-map-main-`; PR keys start with `test-map-pr-N-`. GitHub evicts idle caches, so a map is an optimization, not a requirement for coverage. Main's branch ruleset requires the check and an up-to-date branch. To request the full backstop on the current branch:

```sh
gh workflow run ci.yml --ref BRANCH -f mode=full
```

## Local publish guard

Any change under `src/` or to `tools/makerworld_exports.py` selects the publish guard, as does a full-run fallback. The selection report says whether it is required. Before merging a selected change, fetch main and run:

```sh
git fetch origin main
uv run python tools/publish_guard.py compare --base origin/main --output .local/publish-guard
```

Commit source changes first. This freshly builds all 24 archive/manifest pairs from the merge base and committed `HEAD` with the same interpreter and dependencies, without touching another checkout. Both archived sources use their own `src` on `PYTHONPATH`, not the current editable install or mutable working files. The report names the commits captured at the start; rerun it after another commit. A dependency change during the builds rejects the comparison. `.local/publish-guard/report.json` contains both commit IDs, environment, SHA-256 fingerprints, changed ZIP members, named objects/placements and every changed manifest field with before/after values. A refactor requires no differences. For an intentional change, include the report and explain every affected design, placement, setting and manifest field in the PR; the maintainer approves those differences before merge. The command still exits nonzero when files differ. There is no blanket acceptance switch; after merge, main becomes the next baseline. This checks generated files, not slicing or printed fit.

Weekly CI builds the same inventory with fresh dependencies in eight Apple Silicon `macos-15` jobs: two printers crossed with the catalogue, Zeekr, pull-handle and adjustable-stop sets. Each uses the public `--printer` and `--set` routes to build both supported nozzle sizes. Published bytes are reproducible on Apple Silicon macOS with matching generator and dependencies, not across platforms. The test suite runs on x86_64 Linux. Publication parts must agree on commit and environment and cover all 24 archive/manifest pairs exactly once.

A changed weekly fingerprint fails the aggregate check and saves a report containing the exact fingerprint-set digest. A missing publication baseline also requires approval; cache loss must not silently accept different files. After reviewing the commit, environment and changed files, the maintainer can approve that exact set with `gh workflow run ci.yml --ref main -f mode=weekly -f approve_fingerprints=DIGEST`. The map is saved only after that check succeeds. Weekly fingerprint reports do not authorize uploads or printing.

## Choosing an input

- Give each geometry contract one owner. Keep bounded coverage of joints, family variants, poses, largest sizes and supported custom geometry; don't repeat construction in metadata, CLI routing or settings tests.
- Use a small solid or captured resolved spec when the assertion does not inspect CAD. Inventory and printer policy do not establish geometric or physical fit.
- Share expensive read-only inputs through ordinary module fixtures within a run. Constructor-operation tests call the real factory; a test that mutates a shared input must first make its own copy.
- Keep source STEP/STL/core 3MF versus Bambu orientation checks together at the export boundary. One real bracket owns the generic writing path; other families use small solids for metadata, with their real poses and contact areas checked in the family tests. Rail ends keep every section height and variant, with one core/Bambu mesh comparison.
- A bed-contact check needs a lower bound as well as agreement between the CAD face and mesh areas. Original brackets require at least 65% of their posed XY bounding area; shallow brackets retain their exact contact-area checks. This is a geometric contact check, not evidence of adhesion or printed stability.
- Catalogue integrations use real construction for one independent family group per case and synthetic solids for unrelated groups. Every family retains actual posed-bound, packing and independent export checks; the selected constructors must run exactly once per candidate. Whole-inventory metadata and plate-number composition have separate small-input checks.
- Auto group exports contain only the real group and, where needed, a small real female tile for mixed-job roof checks. Unrelated stand-ins do not go through geometry validation. Per-group plate counts compose the complete catalogue count; the publish comparison independently exercises every real group together.
- Shared perimeter and assembly fixtures are grouped on one worker in the slow tier. A two-worker test checks actual fixture construction counts, not just marker collection.
- Export fixtures write fresh files in a test-owned temporary directory. Every run constructs its geometry and reruns its assertions.

The CLI resolves a `Tile`, `Accessory`, `Rod` or `RodBrace` before calling a geometry factory. `placements.py` checks measured sizes and footprints without constructing CAD, and `bambu.py` owns material/profile settings. `export.py` owns independent geometry preparation and writing; its existing settings imports remain available to callers.

## Geometry checks

- Query geometry near the feature. Clip shapes to a small box around a hole before classifying points (`_local_assembly_contains` in `test_edge_variants.py`), and use `local_geometry.bounded_distance` for contact checks. Skip shapes whose bounding boxes can't reach the region. Prepare exact bounds and classifiers once after placing the shapes; never retain them across a move or geometry change. Point queries and distances against a whole tile can take seconds each.
- Reuse a clipped pair for distance and intersection-volume checks: its region retains the whole intersection and every pair of points within the chosen distance margin. Keep the comparison against whole-solid queries. Reuse unchanged bounds, reference sections and STEP source measurements within a case without dropping sample points, section heights or exports.
- Avoid whole-solid witness Booleans when a clipped or local check answers the same question, and keep a small test showing that the local check agrees with the whole-solid one.
- Share an expensive input/export before splitting its assertions by responsibility. Splitting must not multiply construction or export work.
- Whole catalogue and complete-package checks belong in integration or `slow`, not in default contracts. Reuse a read-only job when an export checks that same job.
- A faster check must answer the same question. Keep assertions and tolerances.

Run default contracts and affected geometry checks while editing, then the complete selected gate and selected local publish guard before merge. Use a full run whenever selection requests it; main, weekly and manual backstops retain complete coverage. Every rewrite stage independently runs the full portable suite and compares all published archives with the saved baseline. Valid solids and slicer observations do not establish printed fit, support removal or strength.
