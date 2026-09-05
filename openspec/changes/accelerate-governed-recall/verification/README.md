# Aggregate recall verification

Observed on 2026-09-04; final delivery is `afd5f6709b75438c0d5f14c880f82680efa0f2d9`.
This is implementation evidence, not a specification or a live-latency verdict.

Recall production targets were unchanged from `8e6cb4dc`. Earlier fixture
mutation hashes predate the warm-fixture correction; the copies were resynced
before final restored gates. Per-attempt hashes identify the actual bytes.

## Mutation results

[mutation-results.json](mutation-results.json) preserves every observed attempt:
mechanism, named test, source hashes, result excerpt, exit code and restoration.
`run=aggregate` identifies the main copy; `run=filter-completion` identifies
the independently isolated filter completion copy. No live vault was mutated.

The 68 recovered rows resolve to 58 red-covered guards, eight declared
controls, and two shared-proof duplicates. Actual execution comprised 70
attempts: 60 red runs, eight control/non-decisive survivors, and two retired
text anchors. A retired anchor is not a kill. Earlier non-decisive attempts
have current decisive equivalents in the ledger below.

Each red run failed a named behavioral test, not collection or import.
Mutated targets were restored exactly after each attempted mutation. The
final 15 source/test dependencies in the aggregate copy matched delivery
bytes; the filter copy's three production targets and eligibility test also
matched delivery.

## Restored gates

All commands ran in isolated source copies with `UV_CACHE_DIR`,
`XDG_STATE_HOME`, `LOCALAPPDATA`, `EXOMEM_STATE_ROOT`,
`EXOMEM_CONFIG_PATH`, `TMPDIR` and pytest `--basetemp` outside the checkout.
`EXOMEM_VAULT_PATH` was unset and `EXOMEM_DISABLE_FILE_WATCHER=1`.

Command shape: `uv run --frozen pytest --basetemp=<isolated-temp> -q <scope>`.

| Scope | Observed result | Exit |
|---|---|---:|
| `tests/test_recall_timing_completeness.py tests/test_recall_walk_sentinel.py` | 15 passed in 6.90s | 0 |
| `tests/test_recall_indexed_eligibility.py` (aggregate restore) | 16 passed in 14.96s | 0 |
| `tests/test_recall_indexed_eligibility.py` (filter completion restore) | 16 passed in 17.45s | 0 |
| `tests/test_recall_read_cache_custody.py` | 23 passed in 31.75s | 0 |
| `tests/test_recall_widening_opt_in.py` | 12 passed in 7.93s | 0 |

## Independent integration review

The correction review accepted the aggregate implementation after an
83-test latency/integration gate, all-field identity and refusal probes,
and a separate 23-test custody run. The warm-fixture correction was
independently reproduced and rechecked, as was the reference-lookup test
double's new reader argument. No unresolved product finding remained.

Repository-wide CI is recorded on [PR #1068](https://github.com/Artexis10/exomem/pull/1068).
Release, live-cell acceptance and OpenSpec closure remain separate gates.

## Recovered-row ledger

IDs below map to `id` in the JSON. The `filter-completion/` prefix also
selects that run. Controls name the known equivalence or non-behavioral
case; they do not claim a red test.

| lane 1 original row | disposition |
|---|---|
| M1 | red: L1-M1 |
| M2 | red: L1-M2 |
| M3 | red: L1-M3 |
| M4 | red: L1-M4 |
| M5 | red: L1-M5 |
| M6 | red: L1-M6 |
| M7 | control: declared static-source-label gap |
| M10 | red: L1-M10 |
| M10B | red: L1-M10B-current (two-file accounting bypass plus 0ms parent) |
| M11 | red: L1-M11 |
| M12 | red: L1-M12 |

| lane 2 original row | disposition |
|---|---|
| M1 | red: L2-M1-adapted (current scalar/list metadata codec) |
| M2 | red: L2-M2 |
| M3 | red: L2-M3 |
| M4 | red: filter-completion/L2-M4-current (both current refusal gates removed) |
| M5 | red: L2-M5 |
| R1 | red: L2-R1-current (current scalar canonicalizer) |
| R2 | red: filter-completion/L2-R2-current (`$in` sole member retained) |
| R3 | red: filter-completion/L2-R3-current (day `$gte` remains inclusive) |
| R4 | red: filter-completion/L2-R4-current (scene-frame children expand) |
| R5 | red: filter-completion/L2-R5-current (inexact date child not complementable) |
| R6 | control: declared benign pending-shadow variant |
| N1 | covered by red: L2-M1-adapted scalar/list identity probe |
| N2 | red: filter-completion/L2-N2-current (pending KB scope gate) |
| N3 | red: filter-completion/L2-N3-current (NULL equality guard) |
| N4 | red: filter-completion/L2-N4-current (canonical unit-tag members) |
| N5 | red: filter-completion/L2-N5-current (same-unit `EXISTS` arm) |
| N6 | covered by red: L2-M1-adapted scalar/list identity probe |
| N7 | red: L2-N7 |
| N8 | red: L2-N8 |
| N9 | red: filter-completion/L2-N9-current (complement-refusal gates) |
| N10 | red: filter-completion/L2-N10-current (`$between` remains inexact) |
| N11 | red: L2-N11-current (null predicate shape refusal) |

| lane 3 original row | disposition |
|---|---|
| M1 | red: L3-M1-current (registered seam fan-out) |
| M2 | red: L3-M2-current (post-write scope-invalidations remain empty) |
| M2b | red: L3-M2b-current (correctness eviction retains receipt-covered pages) |
| M3 | red: L3-M3-current (frontmatter custody registration) |
| M3-inert | red: L3-M3-inert-move-current (retired path is removed); same-path write comparison L3-M3-inert-current is a surviving duplicate guard |
| M3b | red: L3-M3b-current (fan-out and lexical custody degraded together) |
| M3c | control:L3-M3c-current (degraded fan-out repaired by custody seam) |
| M4 | red: L3-M4 |
| M4b | red: L3-M4b-current |
| M5 | red: L3-M5 |
| H1a | red: L3-H1a-guard-current (decline gate remains recall-only) |
| H1b | red: L3-H1b-current (serializer context reaches refs) |
| H1c | red: L3-H1c-current (missing refs return before canonical walk) |
| H1d | red: L3-H1d-current (out-of-KB misses schedule no rebuild) |
| H2 | red: L3-H2-current (absent catalogue is not drift) |
| H3 | covered by red: L3-M5 metadata-audit mutation |
| N1 | red: L3-N1-current (only named path is evicted) |
| N3 | red: L3-N3-current (final batch revalidation before custody) |
| LOW1 | red: L3-LOW1-current (idle release is uncounted) |
| LOW2 | red: L3-LOW2-current (unavailable custody invalidates scopes) |
| MEDc | control: declared bounded-diagnostics non-behavioural variant |
| MEDd | control: declared digest-threading non-behavioural variant |
| MEDb | control: declared harness-only non-behavioural variant |
| R6 | control: declared benign pending-shadow variant |

| lane 4 original row | disposition |
|---|---|
| M1 | red: L4-M1 |
| M2 | red: L4-M2 |
| M3 | red: L4-M3 |
| M4 | red: L4-M4 |
| M5 | red: L4-M5 |
| M6 | red: L4-M6 |
| M7 | red: L4-M7 |
| M8 | red: L4-M8-adapted (current exception-label branch) |
| N1 | red: L4-N1 |
| N2b | control: declared equivalent per-candidate reserve-loop variant |
| N3b | red: L4-N3b |
