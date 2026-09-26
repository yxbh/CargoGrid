# Writing tests

Portable CI runs every test not marked `native`, `reference` or `slow` with four workers on a shared hosted runner, where a test takes roughly five times as long as it does alone on a fast workstation. `tools/check_test_durations.py` fails the job when one test's setup, call and teardown exceed its budget, and warns when the whole pytest session runs past its target. The `slow` tier runs weekly and on a manual CI dispatch with `tier: slow`.

- Check representative sites. Keep at least one portable case for each connector sex, joint style, family variant and recorded pose, plus the largest size. Mark the other values of a sweep with `pytest.param(..., marks=pytest.mark.slow)` so their test IDs stay the same.
- Query geometry near the feature. Clip shapes to a small box around a hole before classifying points (`_local_assembly_contains` in `test_edge_variants.py`), and use `local_geometry.bounded_distance` for contact checks. Skip shapes whose bounding boxes can't reach the region. Point queries and distances against a whole tile can take seconds each.
- Avoid whole-solid witness Booleans when a clipped or local check answers the same question, and keep a small test showing that the local check agrees with the whole-solid one.
- Don't export full multi-part packages in portable tests. Export one or two designs, or mark the package check `slow`.
- Build expensive geometry once. Use a module fixture that tests only read and list it in `EARLY_FIXTURES` in `conftest.py`, so its tests start first and stay on one worker. Don't change a shared shape; move or copy it first.
- Split long integration checks by responsibility, so a failure names the broken contract and no single test becomes the tail of the run.
- A faster check must answer the same question. Keep assertions and tolerances.

Run the portable tier with `uv run pytest -q -n 4 --dist worksteal -m "not native and not reference and not slow"` and the slow tier with `uv run pytest -q -n 4 --dist loadgroup -m "slow and not native and not reference"`. Add `--durations=0 --durations-min=0` to see where time goes.
