# Tasks: audit-derivation-double-counting

## 1. Bounded traversal primitive (pure logic, no vault I/O)

- [x] 1.1 Red tests for `_derivation_direct_sources`, `_bounded_ancestor_walk`,
      and `_DerivationBudget`: direct-source extraction from `sources:`
      wikilinks, self-references kept as graph edges, a shared edge budget
      that terminates a walk, and a depth cap that terminates a walk —
      exercised indirectly through the category's own test fixtures (the
      diamond, the clean chain, the self-loop, the two/three-node cycles, and
      the depth-capped chain) rather than as separate unit tests of private
      helpers.
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
