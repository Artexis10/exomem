## 1. Note price

- [x] 1.1 Record the dearest time per made note on the run, and size a call's first batch on it (`test_a_continuation_starts_with_a_batch_of_notes_priced_on_the_call_before`).
- [x] 1.2 Keep the first note alone for a run with no recorded price, and keep a quick refusal from lowering the price (`test_a_quick_refusal_does_not_make_the_next_batch_of_notes_look_free`).
- [x] 1.3 Keep the per-note fallback checking the time between notes (`test_a_refused_batch_creates_its_notes_alone_only_while_the_call_has_time`).
