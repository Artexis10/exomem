## 1. Measure

- [x] 1.1 Confirm the Windows failures contain no failing test: `2929 passed, 226 skipped, 0 failed in 45:02` then exit 1.
- [x] 1.2 Measure the Windows pytest sessions on this lane (2481s, 2449s, 2595s) against the 2700s cap and the 60 minute job.
- [x] 1.3 Compute the predicted busiest four-way shard from `.test_durations.json` (1385s) and derive the platform factor (1.81).
- [x] 1.4 Check the corrected model against reality: predicted 2507s versus a measured mean of 2509s.
- [x] 1.5 Confirm the pull-request tiers need no change.

## 2. Fix

- [x] 2.1 Split the lane six ways, updating the shard matrix, `--splits`, and the job name together.
- [x] 2.2 Leave `--session-timeout` at 2700s, which the corrected prediction shows already holds 1.61x at six shards.
- [x] 2.3 Put the explanation above the step, not inside the folded scalar, and confirm with `yaml.safe_load` that the rendered command contains no `#`.

## 3. Pin

- [x] 3.1 Assert the cap holds 1.5x the corrected prediction, stays inside the job deadline, and that the split count can hold the rule at all. Confirm it fails at four shards.
- [x] 3.2 Assert no folded `run:` scalar contains a `#`, reading the raw file rather than the parsed tree so literal `|` blocks are not flagged. Confirm it fails when the inline comment is reintroduced.

## 4. Verify

- [x] 4.1 Run `tests/test_ci_reliability_contract.py`.
- [x] 4.2 Run `openspec validate bound-cross-platform-session --strict` and `openspec validate --specs --strict`.
- [x] 4.3 Run lint.

## 5. Closure

- [ ] 5.1 Once merged and therefore demonstrably shipped, sync the delta into `openspec/specs/` and archive with `openspec archive`, re-running `openspec validate --all --strict` before and after.
- [ ] 5.2 Follow-up, filed separately rather than here: with the clock noise gone, triage the real Windows failures the lane reports, concentrated in `tests/test_governance_active_tuple.py` from the v4 catalog wave (#800-#818).
