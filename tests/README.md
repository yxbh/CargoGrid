# Writing tests

`uv run pytest -q` collects `tests/contracts/`: parameters, synthetic packing and serialization, printer settings, cache ownership and failure paths. Put real family geometry and integration checks elsewhere under `tests/`. The full command names `tests` so it includes both directories; a marker filter alone is not the default tier.

| Selection | Command |
|---|---|
| Default contracts | `uv run pytest -q` |
| Full portable, including slow | `uv run pytest -q tests -n 12 --dist worksteal -m "not native"` |
| PR portable | `uv run pytest -q tests -n 4 --dist worksteal -m "not native and not slow"` |
| Complete slow selection | `uv run pytest -q tests -n 4 --dist loadgroup -m "slow and not native"` |
| Fresh construction | Append `--input-cache=off` to an affected selection or the full command |
| Cold inputs | Append `--input-cache-dir .local/test-inputs-cold` with a new empty directory |
| Optional native Bambu | Supply `CARGO_GRID_BAMBU`, then `uv run pytest -q tests -m native` serially |

Use fewer workers on smaller machines. CI splits the slow selection across two runners with `--ci-partition=0/2` and `--ci-partition=1/2`. `tests/ci_partition.py` assigns complete files using measured relative costs; new files receive a default weight, not an omission. Collection checks require disjoint, complete coverage. Within each runner, `loadgroup` keeps users of an expensive shared plan together; the portable job uses `worksteal`. Native checks are excluded by default; an explicit `-m native` overrides that selection. Unavailable external checks remain skips.

The local targets are 30 seconds for default contracts and two minutes warm or six minutes cold for the full portable selection. Record actual timings, toolchain, workers and contention in the review; do not weaken a contract to meet a timing target. `tools/check_test_durations.py` also checks the PR's individual test costs from its JUnit report. `--durations=0 --durations-min=0` prints every phase, whose sum is not parallel wall time.

## Choosing an input

- Give each geometry contract one owner. Keep bounded coverage of joints, family variants, poses, largest sizes and supported custom geometry; don't repeat construction in metadata, CLI routing or settings tests.
- Use a small solid or captured resolved spec when the assertion does not inspect CAD. Inventory and printer policy do not establish geometric or physical fit.
- Opt a geometry module into `pytest.mark.geometry_inputs` to use `tests/geometry_inputs.py`'s named fixture adapters. Tests still receive independent native shapes and rerun every assertion. Constructor-operation and mesher-algorithm tests stay unmarked and construct fresh inputs.
- The test-only store is `.local/test-inputs/`. Keys include complete parameters, relevant source bytes including dirty edits, the adapter/schema and Python, OCCT and numerical-library versions, operating system, architecture and C runtime. The host OS kernel release is not a geometry dependency; hosted Linux runners can use different kernels with the same userspace. Printer profiles, nozzles and layer heights are applied after source geometry and are not geometry parameters.
- Binary BREP and the original checked export-owned arrays are separate inputs: geometry-only queries do not mesh. The first mesh request reconstructs the original constructor's owned copy rather than retaining its mutable native construction history; reloading, copying and remeshing a BREP need not reproduce the original bytes. Test-owned inputs use independent binary clones so the writer retains those arrays; ordinary public shapes keep their normal copy behavior. Mesh arrays are read-only; source mutation or a changed pose forces a fresh mesh. Each restored design also owns its metadata and assembly frames.
- Expensive exported fixtures may use the same store's `artifacts` API, keyed by the complete request, factory source and all library/writer source. Read the actual files and run assertions each time. Copy a fixture directory before a test modifies it. Fresh small-shape tests own writer lifecycle, failure and overwrite behavior.
- Hashes and native/mesh facts are checked when loading. Missing or corrupt entries fail with their path instead of silently rebuilding; inspect the entry or choose a new cache directory. Source edits during an active run also fail instead of giving already-loaded code a new key; restart pytest after editing. Failed builders and failed publication leave no usable entry. No persistent pickle or assertion result is stored.
- An intentional bad-geometry test can request `fresh_geometry` rather than asking the cache to mesh an invalid fixture. This does not change the assertions or make corrupt cache entries rebuild.

CI snapshots use the store backend, adapter and dependency fingerprint, with runner-selection prefixes and unique snapshot suffixes. Restore prefixes contain no branch or commit identity, so PRs can reuse compatible snapshots from `main`; each entry still checks its own source key and data. A manual `cache_mode: cold` dispatch skips restoration and saves its fresh inputs for a subsequent restored measurement. Scheduled slow runs also skip restoration, qualifying fresh construction weekly. Cold construction and corrupt-cache rejection are different paths: a corrupt restored entry remains a failure.

## Geometry checks

- Query geometry near the feature. Clip shapes to a small box around a hole before classifying points (`_local_assembly_contains` in `test_edge_variants.py`), and use `local_geometry.bounded_distance` for contact checks. Skip shapes whose bounding boxes can't reach the region. Point queries and distances against a whole tile can take seconds each.
- Avoid whole-solid witness Booleans when a clipped or local check answers the same question, and keep a small test showing that the local check agrees with the whole-solid one.
- Share an expensive input/export before splitting its assertions by responsibility. Splitting must not multiply construction or export work.
- Whole catalogue and complete-package checks belong in integration or `slow`, not in default contracts. Reuse a read-only job when an export checks that same job.
- A faster check must answer the same question. Keep assertions and tolerances.

Run default contracts and the affected geometry checks while editing, then the full portable gate before merge. Geometry or meshing changes also need cache-disabled affected checks. Production exports do not use this test cache; every rewrite stage independently compares all published archives with the saved baseline. Valid solids and slicer observations do not establish printed fit, support removal or strength.
