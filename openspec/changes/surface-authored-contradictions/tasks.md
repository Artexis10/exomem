## 1. Delta specifications

- [x] 1.1 Add the `contradiction-queue` delta: asserted entries, provenance labelling, the competing-alternatives pair stance, the structural-pair exemption, and the two modified ordering/measurement requirements.
- [x] 1.2 Add the `attention-queue` delta: asserted-before-proximity preservation, provenance on reasons, and `competing` as a review state honored by every queue.
- [x] 1.3 Add the `context-packs` delta: tension-pair provenance and asserted pairs without embeddings.

## 2. Pure-logic units (red first)

- [x] 2.1 Test the pair-stance identity helpers: order-independent pair key, namespaced pair id, and a pair fingerprint that changes when either endpoint's signal version changes.
- [x] 2.2 Test that `review_state` accepts `competing` as an action and a view, resolves it to a `competing` effective state, refuses `until`, and clears on `reopen`.
- [x] 2.3 Test the asserted-pair graph reader: both endpoints resolved, symmetric edges deduped to one unordered pair, and an unavailable or warming graph yielding an explicit empty result.
- [x] 2.4 Test the structural-pair predicate: an authored `contradicts` edge and two `answers` edges into one question both qualify; an unrelated pair does not.

## 3. Asserted contradiction entries

- [x] 3.1 Test that an authored `contradicts` edge surfaces a `corpus_contradictions` finding with `meta.provenance == "asserted"` while embeddings are disabled.
- [x] 3.2 Test that asserted findings are emitted before every proximity finding and that proximity findings carry `meta.provenance == "proximity"`.
- [x] 3.3 Test that a pair surfaced as asserted does not also surface as a proximity pair and is not counted in the cap's omitted total.
- [x] 3.4 Test endpoint eligibility: an authored edge whose other endpoint is archived, a raw source, or read-only does not surface.
- [x] 3.5 Implement the asserted lane in `_check_corpus_contradictions` ahead of the embeddings short-circuit, tag both lanes with `provenance`, and suppress the proximity duplicate.

## 4. Competing-alternatives stance

- [x] 4.1 Test that `triage_memory(action="competing")` on a contradiction item records a pair-keyed decision and removes the item from the open attention view.
- [x] 4.2 Test that editing either rival resurfaces the pair as open.
- [x] 4.3 Test that `competing` is refused for a review item that carries no pair.
- [x] 4.4 Test that `reopen` on a stanced pair clears both the item record and the pair record.
- [x] 4.5 Implement `competing` in `review_state`, the pair-stance consultation in `attention._apply_review_state`, the `state_summary` key, and the triage leaf routing.

## 5. Write-time warnings

- [x] 5.1 Test that a competing-stanced pair is suppressed from both `detect_duplicates` and `detect_contradictions` candidates.
- [x] 5.2 Test that a structurally paired pair is suppressed from both, with no stance recorded.
- [x] 5.3 Test that an unrelated, unstanced pair still warns exactly as today.
- [x] 5.4 Implement the shared suppression filter in `corpus_aware`, keyed on `self_path` and each candidate path, no-op when `self_path` is absent.

## 6. Deep-pack tension provenance

- [x] 6.1 Test that every tension pair carries `provenance`, that authored pairs are labelled `asserted` and emitted first, and that proximity pairs stay `proximity`.
- [x] 6.2 Test that an embeddings-off pack still carries asserted tension pairs while `embeddings_available` stays `false`.
- [x] 6.3 Implement asserted pairs and provenance labelling in `context_pack._tension_pairs`.

## 7. Verification

- [x] 7.1 Run the new test module plus the attention, contradiction-queue, corpus-aware, context-pack, relation-queue, review-context, and graph relation-filter suites.
- [x] 7.2 Run `uvx ruff check` on every changed file.
- [x] 7.3 Confirm the guarded goldens and the MCP schema fixture are untouched by the diff.
- [x] 7.4 Validate the OpenSpec change in strict mode.

## 8. Correction round

- [x] 8.1 Return every contradiction pair from `pairs_from_reasons`; record and clear the stance on each, and resolve an item to `competing` only when all its pairs are stanced.
- [x] 8.2 Cover an anchor carrying two conflicts: record, honour, reopen-clears-all, and the drift-reopen transition that previously stranded a stance.
- [x] 8.3 Parametrize the resurfacing test over both endpoints so the both-sides fingerprint binding is pinned.
- [x] 8.4 Replace the per-anchor asserted-pair fan-out with one indexed `EpistemicGraphIndex.relation_edges` query, and cover the new method directly.
- [x] 8.5 Build the write-time declared-pair snapshot once per call instead of per candidate.
- [x] 8.6 Report the item's own identity from a `competing` triage, with pair identities under `pairs`.
- [x] 8.7 Filter declared rivals inside each detect loop so an exempt pair cannot consume a `top_n` slot.
- [x] 8.8 Pin the proximity `signal_version` formula, the inverted-band asserted path, and the warming-graph proximity path.
- [x] 8.9 Correct the design.md cost claim and re-validate the change in strict mode.
