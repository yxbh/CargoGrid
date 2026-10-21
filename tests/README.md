# Writing tests

`uv run pytest -q` collects `tests/contracts/`: parameters, synthetic packing and serialization, printer settings, export ownership and failure paths. Put real family geometry and integration checks elsewhere under `tests/`. The full command names `tests` so it includes both directories; a marker filter alone is not the default tier.

| Selection | Command |
|---|---|
| Default contracts | `uv run pytest -q` |
| Full portable, including slow | `uv run pytest -q tests -n 12 --dist worksteal -m "not native"` |
| PR portable | `uv run pytest -q tests -n 4 --dist worksteal -m "not native and not slow"` |
| Complete slow selection | `uv run pytest -q tests -n 4 --dist loadgroup -m "slow and not native"` |
| Optional native Bambu | Supply `CARGO_GRID_BAMBU`, then `uv run pytest -q tests -m native` serially |

Use fewer workers on smaller machines. CI splits the slow selection across two runners with `--ci-partition=0/2` and `--ci-partition=1/2`. `tests/ci_partition.py` assigns complete files using measured relative costs; new files receive a default weight, not an omission. Collection checks require disjoint, complete coverage. Within each runner, `loadgroup` keeps users of an expensive shared plan together; the portable job uses `worksteal`. Native checks are excluded by default; an explicit `-m native` overrides that selection. Unavailable external checks remain skips.

The local targets are 30 seconds for default contracts and two minutes warm or six minutes cold for the full portable selection. Record actual timings, toolchain, workers and contention in the review; do not weaken a contract to meet a timing target. `tools/check_test_durations.py` also checks the PR's individual test costs from its JUnit report. `--durations=0 --durations-min=0` prints every phase, whose sum is not parallel wall time.

## Choosing an input

- Give each geometry contract one owner. Keep bounded coverage of joints, family variants, poses, largest sizes and supported custom geometry; don't repeat construction in metadata, CLI routing or settings tests.
- Use a small solid or captured resolved spec when the assertion does not inspect CAD. Inventory and printer policy do not establish geometric or physical fit.
- Share expensive read-only inputs through ordinary module fixtures within a run. Constructor-operation tests call the real factory; a test that mutates a shared input must first make its own copy.
- Shared catalogue and assembly fixtures are grouped on one worker in the slow tier. A two-worker test checks actual fixture construction counts, not just marker collection.
- Export fixtures write fresh files in a test-owned temporary directory. Every run constructs its geometry and reruns its assertions.

## Geometry checks

- Query geometry near the feature. Clip shapes to a small box around a hole before classifying points (`_local_assembly_contains` in `test_edge_variants.py`), and use `local_geometry.bounded_distance` for contact checks. Skip shapes whose bounding boxes can't reach the region. Point queries and distances against a whole tile can take seconds each.
- Avoid whole-solid witness Booleans when a clipped or local check answers the same question, and keep a small test showing that the local check agrees with the whole-solid one.
- Share an expensive input/export before splitting its assertions by responsibility. Splitting must not multiply construction or export work.
- Whole catalogue and complete-package checks belong in integration or `slow`, not in default contracts. Reuse a read-only job when an export checks that same job.
- A faster check must answer the same question. Keep assertions and tolerances.

Run default contracts and the affected geometry checks while editing, then the full portable gate before merge. Every rewrite stage independently compares all published archives with the saved baseline. Valid solids and slicer observations do not establish printed fit, support removal or strength.
