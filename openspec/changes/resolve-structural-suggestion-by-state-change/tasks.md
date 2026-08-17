## 1. Red-first acceptance

- [x] 1.1 Add the restructure-by-addition regression: the origin page keeps every durable unit, destinations declaring the cluster vocabulary exist, and the origin page goes quiet. Assert against structural behaviour, never fixture prose.
- [x] 1.2 Prove the existing control was the gap: keep `test_advice_stops_once_the_material_is_routed_into_matching_scope` and add the un-cleaned origin alongside it, so subtraction and addition are both covered.
- [x] 1.3 Add the reversal control: delete the destinations, confirm the suggestion returns with its existing shape, and confirm no state was stored between the two outcomes.
- [x] 1.4 Add the incidental-single-term control: several pages each declaring exactly one cluster term resolve nothing.
- [x] 1.5 Add the partial-routing control: routing part of a cluster leaves a suggestion for the remainder.
- [x] 1.6 Add the eligibility control: a destination outside the caller's recall eligibility does not resolve.
- [x] 1.7 Add the fail-open control: absent corpus emits exactly today's suggestion.
- [x] 1.8 Confirm every pre-existing detector test still passes unchanged, proving the no-corpus path is byte-identical to today.

## 2. Detector

- [x] 2.1 Add the destination index built from eligible compiled corpus pages, excluding the written page, reusing the existing identity normaliser.
- [x] 2.2 Add `MIN_DESTINATION_COVERAGE` and require it per contributing destination.
- [x] 2.3 Remove routed terms and re-apply the existing mass requirement rather than introducing a second threshold.
- [x] 2.4 Keep `detect` working with no destination index, returning exactly today's result.
- [x] 2.5 Keep the emitted payload byte-identical: no new key, reason code, or numeric quantity.

## 3. Write path

- [x] 3.1 Retain the corpus context already built during creation preflight on `CreationPreflight` instead of discarding it.
- [x] 3.2 Pass the corpus from both commit functions into the advisory analysis, inside the existing failure-isolating guard.
- [x] 3.3 Confirm no second corpus build, no new walk, and no new I/O on the write path.

## 4. Verification

- [x] 4.1 Replay the real dogfooded page shape against its actual destinations. Outcome, recorded as measured rather than as hoped: 16 of the cluster's 21 recurring terms resolve, and the page still speaks about the 5 that genuinely have no home (`countryside, income, ptz, rita, savings`). The stale farm signal is gone; the remaining one is true. It does not go silent, and no threshold was added to make it.
- [x] 4.2 Run the focused detector, mutation-terminal, note, observe, creation, and replace test modules.
- [x] 4.3 Run the governance egress and postfilter tests.
- [x] 4.4 Run the semantic write-latency gate and report the delta against the absolute ceilings. Measured: the destination pass costs 9.2 ms at 2k pages and 33.4 ms at 8k, paid only on a write that would otherwise emit a suggestion; ordinary writes are untouched.
- [x] 4.5 Validate the change in strict mode and run the repository lint and lean suite.
- [x] 4.6 Record `f25` status honestly: the family also asserts absence across the due-state counters surface, which S1 has not built, so it stays red for that reason and the detector claim is reported separately.
