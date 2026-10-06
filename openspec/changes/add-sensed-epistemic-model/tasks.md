## 1. Specification and rulings (slice 1)

- [x] 1.1 Write this change: the proposal, the design with the owner's rulings R1–R7, the delta specs (`sensed-epistemic-model`, `frozen-verifiers`, `contradiction-queue`) and these tasks. Validate with `openspec validate --all --strict`.
- [x] 1.2 Amend `close-memory-loop` in place:
  - `design.md:208`: the Cloud reversal points here.
  - `design.md:212`: drop condition 4.
  - Replace the scenario "Verifier labels do not reach upkeep" with the R2 rule.
  - Close task 8.4 as "admitted, re-laned to add-sensed-epistemic-model".
- [x] 1.3 Update the pure-substrate paragraph of `openspec/config.yaml` for R1, R2 and R7. Rewrite the thresholds of `docs/hosted-inference-boundary.md` as acceptance measures (R4), keeping the recorded quantized-candidate evidence.

## 2. Question registry and reading record (slice 1)

- [x] 2.1 Add `sensing.py`, pure code with no model import:
  - the setting (`EXOMEM_SENSING`, then the config key `sensing`, default `off`);
  - the `pair.relation` registry entry: template `nli-pair-v1`, label map `relation-v1`, unit scope `units-v1`, fixture set `relation-v1-multilingual`;
  - `InstrumentIdentity` and `instrument_id`;
  - `reading_id` and `input_key` over the ordered text hashes;
  - the `unit-text-v1` extractor.

  Unit tests come first: label-map rules in order (including one-way contradiction → `abstain: directional_asymmetry`, and the refining side's direction), id stability, and label-map and fixture versions outside `instrument_id`.
- [x] 2.2 Add `sensing_ledger.py`: `<vault state dir>/sensing/readings.sqlite`, append-only by `BEFORE UPDATE/DELETE` triggers, idempotent append by reading id, and lookup by `input_key`. Test the refused update and delete, the idempotent re-append, lookup by content across instruments, the state-root placement, and that no vault text is stored.

## 3. Invalidation and migration (slice 1)

- [x] 3.1 Consume by current text hash (`current`, `stale`, `pending`, `migrating`). Re-derive verdicts under the active label map from stored vectors. Keep but never consume an old instrument's readings. Report `evidence_complete: false` until the page's pairs drain. Order the re-sense with previously served pairs first. Test an unrelated edit that re-senses nothing, the stale unit, a label-map change with no sensing, and pin migration.

## 4. Sensor worker (slice 1)

- [x] 4.1 Add `sensor_worker.py` (supervisor, child loop, spend accounting) and the `sensor_worker_child.py` entry point: vault lock, lowest priority, CPU only, one thread, 60-second idle exit.
  - The supervisor inherits `dreamer_policy.decide` and adds 300 CPU-s and 600 judgements per rolling hour.
  - It terminates the child when any gate closes and kills it in quiet mode, under pressure and in standby.
  - It charges an unreported death the whole allotment.
  - It suspends relaunch after a named refusal until the setting or pin changes.
- [x] 4.2 Drive the supervisor from the dreamer loop, re-evaluating at least every 2 seconds while a child lives. Keep `tests/test_dreamer_no_side_effects.py` green: the dreamer thread imports no model runtime.
- [x] 4.3 Tests with a fake launcher and a real no-weights child cover: launch only with every gate open; kill on quiet, pressure, standby, foreground and setting off; budgets across launches; the refusal backoff; and the child's exit when its parent dies.

## 5. The `pair.relation` instrument on the admitted NLI pin (slice 1)

- [x] 5.1 Add `sensing_nli.py`: admission through the stance verifier's pin, resident digest and loader (`claims`), forced to CPU. The `nli-pair-v1` template scores both orders in one batch. `input_too_long` abstains instead of truncating. Non-finite logits refuse. Vectors are rounded to six decimals.
- [x] 5.2 Fixture set `relation-v1-multilingual`: the stance set's pairs under `relation-v1` labels with direction, plus a negative twin per label. Run it through `_verify_fixtures`-style memoised admission. Add a real-pin test under `pytest.mark.nli` and `EXOMEM_RUN_REAL_NLI=1`, and extend `.github/workflows/frozen-verifier.yml` to run it.

## 6. Pair proposers and projections (slice 1)

- [x] 6.1 A disposable projection file, `<vault state dir>/sensing/projection.sqlite`, kept apart from `dreamer.sqlite` so sensed rows never count against its size cap. It holds:
  - `pages`: the seen signature, knowledge date, lifecycle and supersession partners (the candidate count and capped flag went with the per-page cap in 8.3);
  - `units`: each page's in-scope units with text, hash and stored vector;
  - `links`: each page's normalised authored link targets;
  - `pairs`: every proposed pair with proposer, order key, state, verdict, priority, selection and queue flags.

  The projection follows the dreamer's `seen` map, and each page replaces its own rows.
- [x] 6.2 Proposers:
  - structural (a graph edge either way);
  - temporal same-subject (at least two shared authored link targets, and different knowledge dates);
  - cosine (stored vectors of the ranked encoder whose source hash matches, θ = 0.72 keyed by the exact encoder fingerprint, 16,384 vectors held in memory; 8.3 replaced the per-tick matrix bound with one shared pass per tick).

  Pairs are cross-page only, with no identical texts, and capped in a fixed order. Test per-pair monotonicity: a third page never adds or removes a pair. Task 8.3 replaced the cap of 128 per page, which marked a binding page capped, with 128 per page pair, and replaced the cosine matrix bound with a memory bound that never changes what is proposed.
- [x] 6.3 Projection per pair: consumed readings become edges (state, verdict, direction, `p`, instrument, reading id, fingerprint over the inputs and verdict); `instruments_disagree` across active instruments; stale and migrating pairs are queued. Readings the worker appended between ticks are ingested at tick start.
- [x] 6.4 Per-request projections over released edges: contradiction components (bounded at 32 pages) and refinement and supersession chains in time order (bounded at 8).
- [x] 6.5 Replay test: rebuilding a fresh projection from the same ledger yields byte-identical edges, fingerprints and served statuses, independent of append order and `sensed_at`.

## 7. Point-of-use status line (slice 1)

- [x] 7.1 `read_memory` attaches `epistemic_status` for a released page with a non-zero count:
  - a line such as `refined by 2 later notes; 1 open contradiction`;
  - `refined_by_later`, `open_contradictions` and their released items (verdict, `p`, instrument, fixture precision, reading id);
  - the chain, the component size and `evidence_complete`.

  Nothing else in the read changes, and the field is absent when there is nothing to say.
- [x] 7.2 `activate_context` attaches the same status to resolved anchor pages after the packet is built, outside the packet cache, charged to the packet budget, and never raising.
- [ ] 7.3 Describe `epistemic_status` in the `read_memory` and `activate_context` tool text, and regenerate the derived artifacts (tool schemas and plugin contract, packaged skills, hosted renders, capabilities). Deferred to slice 3: slice 1 changes no tool text, the field exists only when the owner turns sensing on, and its line is self-describing.

## 8. Egress (slice 1)

- [x] 8.1 Twin tests under a governed policy with a restricted principal. A withheld contradicting page, a withheld refining page, a withheld page bridging a contradiction component and a withheld chain member each give byte-identical read and activation output to the absent twin.
- [x] 8.2 Slice 1 served sensed items to owner-bound principals only under a non-empty governed policy, because the per-page cap and the cosine bound counted withheld pages. Task 8.3 superseded that: items are served per caller, and the review twin is a per-caller test. At request time, an edge whose page signature or instrument key is not live is dropped with `evidence_complete: false` (with zero counts when nothing survives), a supersession partner that is not live leaves the chain, and a read of a snapshot the projection did not model carries no status.
- [x] 8.3 Before slice 5: replace the per-page cap with a per-page-pair cap, whose selection depends only on the pair's own two pages, and bound the cosine proposer without counting withheld units. That restores D5's two-page property for selection. Then serve sensed items per caller again, and turn the owner-only twins into per-caller twins.
  - Built: `PAIR_CAP = 128`, ranked within each page pair. The cosine bound now limits memory only: over 16,384 stored vectors, one block pass per tick is shared by that tick's pages, and hits are judged on the exact cosine. The owner-only gate is removed, and requests still route through `egress.release_walk_filter`. Projection schema 3 is rebuilt from the ledger, and a projection rebuilt from empty serves nothing until it is whole.
  - Tests: `test_a_third_page_never_moves_a_pairs_selection`, `test_over_the_cache_bound_the_same_cosine_pairs_are_proposed`, `test_one_pass_over_the_vectors_serves_every_page_of_a_tick`, `test_a_withheld_page_cannot_decide_a_visible_pages_selection`, `test_a_rebuilt_projection_serves_nothing_until_every_page_is_projected`, `test_a_budget_spent_by_the_pass_still_applies_the_pages_it_was_read_for`, `test_pages_in_one_tick_find_the_cosine_pairs_separate_ticks_find`, and the contradicting, refining and bridge twins served per caller.
- [ ] 8.4 Slice 3: close-memory-loop `design.md:212` condition (a). Build a fixture on the sensed family's real pairs showing the label improves disposition at a false-positive rate no worse than the family's structural evidence.
- [x] 8.5 Close-memory-loop `design.md:212` condition (b): no read regression in the write-burst probes. `tests/test_sensed_write_burst_probe.py` measures `read_memory` and `activate_context` on a synthetic vault under a write burst, with sensing off and on, interleaved. The numbers are recorded under "Measured" in `design.md`.
- [ ] 8.6 Before sensing is on by default, or slice 3 surfaces status widely: bound a hub's served-status cost ("Open items" in `design.md`). Filter neutral verdicts in the query and keep each partner's dropped count. Make any truncation count released edges only and report `evidence_complete: false`.
- [ ] 8.7 Before sensing is enabled on any install, and before slice 5's ledger restore: close the three rebuild windows under "Open items" in `design.md`. Hold the sensed tick and its view while the dreamer reseeds. Write the `seeding` flag atomically with the schema row. Gate the view during a genesis-triggered reprojection.

## 9. Later slices (specified here, built later)

- [ ] 9.1 Slice 2: `mention.same_referent` (same, different, abstain) on an open-weight 3–8B instruct model chosen by a fixture spike. The generative template runtime has delimited slots and label-token logits, never sampled text (R1). Local GPU placement comes only after `detect-co-tenant-gpu-pressure` ships (R5).
- [ ] 9.2 Slice 3:
  - Tensions on active or recent work become sensed upkeep families (`upkeep_sensed_*`) under the existing carrier caps and S6. They never touch structural families.
  - The relation writer cites the reading id (R2).
  - The audit's in-request NLI enrichment (`audit.py:6218-6268`) moves onto the ledger with extractor `page-claim-v1`, per the `contradiction-queue` delta.
- [ ] 9.3 Slice 5:
  - The Cloud plane: cells run the dreamer, an in-cluster shared sensing plane, and no third-party API by default. It is measured against the hosted-inference acceptance measures.
  - The per-vault and per-tenant API placement opt-in, with `unpinned_weights` identity and retirement migration (R7).
  - The ledger registered as a portable-derived external-state family for hosted export and restore (R3).
  - Update `tests/test_dreamer_hosted_boundary.py` and `tests/test_frozen_verifier_hosted_boundary.py` to the new contract.
- [ ] 9.4 Slice 4: `recap.covered_by` (entailed, partial, absent); supersession direction, relation typing and `term.same_meaning`; convergence across independent origins and emerging connections to recent work.

## 10. Verification and closure

- [x] 10.1 Scoped suites green: dreamer, upkeep, claims, audit, egress twins and the new sensing tests.
- [x] 10.2 `uvx ruff check --select F src tests`, `validate-public-artifacts --repository` and `openspec validate --all --strict` pass.
- [x] 10.3 An independent review of the slice-1 diff. The first round's findings (H1, M2, M3, L4–L9) are corrected on this branch, and its informational items are recorded under "Open items" in `design.md`. A re-review closes this task.
  - Closed by the independent re-review of task 8.3 (two rounds, final verdict APPROVE). Round 1 verified each first-round finding in code and by test. The recheck verified the 8.3 corrections and recorded three latent rebuild windows as Open items (task 8.7).
- [x] 10.4 Record known misses in `design.md` from the real-pin fixture run.
- [x] 10.5 Record measured sensor cost (CPU-s per judgement, child peak RSS, kill-to-exit time) in `design.md` from the real-pin probe.
