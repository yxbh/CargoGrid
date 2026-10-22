# Writing tests

`uv run pytest -q` collects `tests/contracts/`: parameters, synthetic packing and serialization, printer settings, export ownership and failure paths. Put real family geometry and integration checks elsewhere under `tests/`. The full command names `tests` so it includes both directories; a marker filter alone is not the default tier.

| Selection | Command |
|---|---|
| Default contracts | `uv run pytest -q` |
| Full portable, including slow | `uv run pytest -q tests -n 12 --dist loadgroup -m "not native"` |
| PR portable | `uv run pytest -q tests -n 4 --dist worksteal -m "not native and not slow"` |
| Complete slow selection | `uv run pytest -q tests -n 4 --dist loadgroup -m "slow and not native"` |
| Optional native Bambu | Supply `CARGO_GRID_BAMBU`, then `uv run pytest -q tests -m native` serially |

Use fewer workers on smaller machines. CI splits the slow selection across three runners with `--ci-partition=0/3`, `--ci-partition=1/3` and `--ci-partition=2/3`. `tests/ci_partition.py` assigns complete files using measured relative costs; new files receive a default weight, not an omission. Collection checks require disjoint, complete coverage. Within each runner, `loadgroup` keeps users of an expensive shared plan together; the portable job uses `worksteal`. Native checks are excluded by default; an explicit `-m native` overrides that selection. Unavailable external checks remain skips.

The local targets are 30 seconds for default contracts and two minutes for the full portable selection. Record actual timings, toolchain, workers and contention in the review; do not weaken a contract to meet a timing target. `tools/check_test_durations.py` also checks the PR's individual test costs from its JUnit report. `--durations=0 --durations-min=0` prints every phase, whose sum is not parallel wall time.

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

- Query geometry near the feature. Clip shapes to a small box around a hole before classifying points (`_local_assembly_contains` in `test_edge_variants.py`), and use `local_geometry.bounded_distance` for contact checks. Skip shapes whose bounding boxes can't reach the region. Point queries and distances against a whole tile can take seconds each.
- Avoid whole-solid witness Booleans when a clipped or local check answers the same question, and keep a small test showing that the local check agrees with the whole-solid one.
- Share an expensive input/export before splitting its assertions by responsibility. Splitting must not multiply construction or export work.
- Whole catalogue and complete-package checks belong in integration or `slow`, not in default contracts. Reuse a read-only job when an export checks that same job.
- A faster check must answer the same question. Keep assertions and tolerances.

Run default contracts and the affected geometry checks while editing, then the full portable gate before merge. Every rewrite stage independently compares all published archives with the saved baseline. Valid solids and slicer observations do not establish printed fit, support removal or strength.
