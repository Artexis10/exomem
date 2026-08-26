# Tasks: audit-derivation-double-counting

## 1. Bounded traversal primitive (pure logic, no vault I/O)

- [x] 1.1 Red tests for `_derivation_direct_sources` and `_bounded_ancestor_walk`:
      direct-source extraction from `sources:` wikilinks, self-references kept
      as graph edges, and a depth cap that terminates a walk — exercised
      through the category's own fixtures (the diamond, the clean chain, the
      self-loop, the two/three-node cycles, the depth-capped chain). This
      task originally *also* claimed the shared edge budget and the `seen`
      cycle-terminator were exercised this way; mutation testing (correction
      round 1) proved that false — replacing `_DerivationBudget.take` with
      `lambda self: True`, and deleting the `seen` re-enqueue guard, both left
      the suite fully green. Corrected by 1.1a below; this line now only
      claims what the original fixtures actually cover.
- [x] 1.1a (correction round 1) Red tests that isolate each mechanism from
      the fixtures that accidentally didn't need it: an edge-budget-exhaustion
      test on a chain well under the depth cap (so a `truncated` finding can
      *only* come from the edge budget, not depth), and an `X -> A -> B -> A`
      cycle fixture where `X` is not part of the `A<->B` cycle (2- and 3-node
      self-closing cycles terminate via the `target == start` shortcut
      regardless of `seen`; only a cycle reached *from* but not *containing*
      the walk's start exercises `seen`'s necessity). Acceptance: both
      mutations above now fail the suite — confirmed and reproduced in the
      correction-round report.
- [x] 1.2 Implement `_derivation_direct_sources`, `_DerivationBudget`,
      `_DerivationWalk`, and `_bounded_ancestor_walk` in `src/exomem/audit.py`.

## 2. `derivation_double_counting` category

- [x] 2.1 Red tests for support collapse: the diamond (one source, two
      derived notes, a third citing both) is flagged; a clean chain with
      genuinely independent sources is NOT flagged (false-positive guard); a
      single-source page is not a collapse candidate; a page whose two direct
      sources overlap only because one directly cites the other still
      collapses.
- [x] 2.2 Red tests for the origination gate: only `research-note` /
      `insight` / `failure` / `pattern` originate a `support_collapse`
      finding; inactive status (`archived`) does not originate one.
- [x] 2.3 Red tests for circular derivation: a direct self-reference (`A`
      cites `A`) is a 2-node cycle; a 2-node cycle (`A` <-> `B`) is detected
      and reported exactly once (deduplicated across both discovery
      directions); a 3-node cycle (`A -> B -> C -> A`) terminates and is
      reported exactly once.
- [x] 2.4 Red tests for cap visibility: a chain longer than a lowered
      `EXOMEM_DERIVATION_MAX_DEPTH` produces exactly one `truncated` finding
      naming the cap; a small graph under the cap produces none.
- [x] 2.5 Red tests for the hard constraints: every finding's severity is in
      `{info, warn}`, never `error`; running the check (twice) never modifies
      any file's mtime or content.
- [x] 2.6 Implement `_check_derivation_double_counting`, register
      `derivation_double_counting` in `OPTIONAL_CATEGORIES` (absent from
      `ALL_CATEGORIES`), and wire it into `audit()`'s category dispatch.
- [x] 2.7 Green: `tests/test_audit_derivation_double_counting.py` passes.

## 3. Contract and gates

- [x] 3.1 Confirm no public tool/contract surface changed (the category is
      reachable only through the existing `audit(categories=...)` parameter,
      already an open string list) — no schema/contract regeneration needed.
- [x] 3.2 Run targeted gates: `tests/test_audit_derivation_double_counting.py`,
      `tests/test_audit*.py`, `tests/test_epistemic_graph*.py`,
      `tests/test_relation_registry*.py`, `tests/test_attention.py`,
      `tests/test_memory_schema.py`; `uvx ruff check` on changed files;
      `openspec validate audit-derivation-double-counting --strict`.

## 4. Correction round 1 (independent mutation-testing review)

- [x] 4.1 Red test proving a page can be reported as its own shared ancestor
      (`C` cites `A`/`B`, both cite `C` back); fix by excluding the citing
      page's own key from `collapse_roots` (design.md D6).
- [x] 4.2 Red test proving one converging tail (`C <- D <- E`, both `A`/`B`
      tracing through it) fans out to one finding per tail node instead of
      one finding for the situation; fix via `_nearest_shared_roots`
      (order-independent pairwise domination, with a deterministic
      single-survivor fallback for a mutual-cycle-among-candidates edge case)
      (design.md D6).
- [x] 4.3 Re-derive `EXOMEM_DERIVATION_MAX_EDGES`'s default from a measured
      edges-per-sourced-page rate, scaled to a ~5,000-file vault with margin,
      then validate empirically against a synthetic 5,000-file vault built
      with a realistic (shallow, tiered) citation shape — confirm the old
      default truncates it and the new default does not, and confirm a
      deliberately pathological deep/braided-chain vault still terminates
      promptly with truncation correctly attributed to `depth` (design.md D2).
- [x] 4.4 Replace `_DerivationWalk.truncated: bool` with `truncated_reasons:
      frozenset[str]` (`{"depth", "edges"}`) so an edge-budget truncation is
      never misreported as depth-capped; thread the reason set into both the
      per-page `support_collapse` finding's meta and the aggregate
      `truncated` finding's meta and detail text.
- [x] 4.5 Track the original raw `sources:` wikilink text per canon key so an
      unresolved `shared_ancestor`/`via_sources` target renders as a
      reconstructed vault-relative path instead of the internal lowercase
      canon key (design.md D7).
- [x] 4.6 Adopt `_STALE_SKIP_SLUG_SUFFIXES`/`_STALE_SKIP_TAGS` in the
      support-collapse origination gate (previously only claimed in a
      comment, not in code) and correct the comment to describe the complete,
      now-accurate exclusion set (design.md D4).
- [x] 4.7 Widen the read-only test from `rglob("*.md")` to `rglob("*")` and
      assert findings are non-empty, so the check cannot pass vacuously
      against a `return []` no-op.
- [x] 4.8 Mutation-test acceptance: with `_DerivationBudget.take` replaced by
      `lambda self: True`, the suite fails (isolated to the new 4.1a-adjacent
      edge-budget test). With the `seen` re-enqueue guard deleted, the suite
      fails (isolated to the new `X -> A -> B -> A` test). Both mutations
      reverted; suite green afterward; restored file confirmed byte-identical
      to the pre-mutation state via `diff`.
- [x] 4.9 Re-run targeted gates from 3.2 plus `openspec validate
      audit-derivation-double-counting --strict` after all fixes.
