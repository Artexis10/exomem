## 1. Measure

- [x] 1.1 Confirm the Windows failures contain no failing test: `2929 passed, 226 skipped, 0 failed in 45:02` then exit 1.
- [x] 1.2 Measure the four Windows shard durations on `main` (38:40, 38:57, 42:58, 45:44) against the 45:00 cap and the 60 minute job.
- [x] 1.3 Measure the Linux lane for contrast (12:31, 15:40, 17:16, 15:06 against a 25:00 cap) and confirm it needs no change.

## 2. Fix

- [x] 2.1 Raise the cross-platform session bound to 3,300s, keeping the lean lane's five-minute margin under the job deadline.
- [x] 2.2 Put the explanation above the step, not inside the folded scalar, and confirm with `yaml.safe_load` that the rendered command contains no `#`.

## 3. Pin

- [x] 3.1 Assert the cap is under the job deadline, leaves at most five minutes unclaimed, and sits above the slowest healthy shard measured. Confirm it fails at 2,700s.
- [x] 3.2 Assert no folded `run:` scalar contains a `#`, reading the raw file rather than the parsed tree so literal `|` blocks are not flagged. Confirm it fails when the inline comment is reintroduced.

## 4. Verify

- [x] 4.1 Run `tests/test_ci_reliability_contract.py`.
- [x] 4.2 Run `openspec validate bound-cross-platform-session --strict` and `openspec validate --specs --strict`.
- [x] 4.3 Run lint.

## 5. Closure

- [ ] 5.1 Once merged and therefore demonstrably shipped, sync the delta into `openspec/specs/` and archive with `openspec archive`, re-running `openspec validate --all --strict` before and after.
