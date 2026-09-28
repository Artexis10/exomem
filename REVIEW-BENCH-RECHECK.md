# Recheck: benchmark-integrity corrections (PR #1429)

Reviewed `test/activation-benchmark-corpus` at `7dc2c2b`.

**Tests:** 202 passed in the three requested modules. The proxy blocks
`o200k_base.tiktoken` and no PyPI wheel carries it, so the run used a
review-only shim that swaps in cl100k ranks from `tiktoken-offline`. The
recorded-report equality tests still passed.

## Findings

- **F1: FIXED.** `hedged` is B3-only (`context_activation.py:710-719`). A4 only
  drops `is_poison_hit`. I ran 3,570 probes on the 18 cases, raw and under A4:
  a wrong page or each poison added to 7 base shapes, as a resolved or partial
  anchor, a unit, a pointer or an ambiguity candidate. A4 never removed a
  non-poison reason.
- **F2: FIXED.** K3 counts partial anchors and ambiguity
  (`context_activation_continuity.py:267-270`, `:324`).
- **F3: PARTIAL.** An extra anchor of any status, or an extra ambiguity
  candidate, now fails (`:314-321`). Units, pointers and `current_state` are
  unchecked: 72 of 180 continuity probes still pass after a wrong page was
  added through them. That includes K1 serving a unit of its own
  `must_not_resolve` page.
- **F4: FIXED.** The digest covers the module source's sha256. Imported
  helpers are outside it, which is acceptable because the scoring logic is
  in-module.
- **F5: FIXED.** The runbook says where the T6 distractor came from, that it
  is hard on purpose, and that it is kept by ruling.

## Probes

1. **Can an added page remove a failure reason?** Not through A4 or
   continuity: continuity scoring is monotone. In the 18 cases, yes, through
   `turn_status`, both raw and amended. This predates these commits. A wrong
   resolved anchor or ambiguity candidate can turn a status mismatch into a
   match. That happened in 256 probes, but precision, recall or poison
   usually took the status reason's place. The exception is C7, below.
2. **Was the pin made before the run?** Yes. `eaba0cf` touches only the module
   and the test. It pins `b208a986…` and has no JSON and no run tests. Both
   arrive in `07be52c`, six minutes later. The module's CRLF-normalised
   sha256 is `660bfe9c…` at `eaba0cf`, `07be52c`, `7dc2c2b` and the head,
   matching the recorded `scorer_source_sha256`. Git proves the order of the
   commits, not that nothing ran locally first.
3. **Is `served_subset` too strict?** Not for a unit of the right page: units
   are ignored. Not for the right page as `retrieval_carried`: it is allowed,
   though `referents` still needs resolved plus recency. One mild risk: the
   match is on one exact ref spelling, and `_hot_ambiguity` names a non-row
   page by path (`src/exomem/working_set.py:2571-2579`).
4. **Are raw `per_case` results unchanged?** Yes. The v4 and historical product
   JSON and the v1 continuity JSON are untouched since `e9b4fad`, and
   `recorded == live` passes.

## New concerns

- **C7 passes when a wrong page is added**
  (`benchmarks/membench/utility/context_activation.py:318-319`, `:731`). A
  packet with C7's gold as partial anchors and its facts in `ambiguity_text`
  fails on status. Add `ambiguity=("zz_wrong_page",)` and it passes, raw and
  under A4: nothing penalises ambiguity candidates outside a non-twin's gold.
  This predates the PR. Pre-registration forbids changing the raw scorer, so
  it needs disclosing, or an opt-in amendment scored beside the raw result.
- **Continuity v2 checks only anchors and ambiguity for extra pages**
  (`context_activation_continuity.py:314-329`). The runbook's wording is
  accurate, but readers may take it as covering all served material.

## Verdict

**REQUEST_CHANGES.** F1, F2, F4 and F5 are sound. The pin order holds and the
raw results are unchanged. Two changes are needed:

- Extend `served_subset` to cover units, pointers and `current_state` under a
  new v3 digest pinned before its first run, or state the limit plainly.
- Disclose the C7 ambiguity-shape pass in the runbook.

## Recheck 3 (head `0e09560`)

Commits: `7db1f3d` (A7), `932c6f5` (v3 pin), `2158e84` (v3 run) and `0e09560`
(docs). The run commit is `2158e84`; `5b1d6ea` is not an object in this
repository. **Tests:** 214 passed in the same three modules, with the same
cl100k tokenizer shim.

- **F3: FIXED.** v3 maps every anchor, ambiguity candidate, unit, pointer and
  `current_state` entry to a canonical page
  (`context_activation_continuity.py:300-320`). Spellings are read back from
  the vault's frontmatter before the fresh turn: a memory ref, a path, a path
  without the prefix, or any of these with a `#fragment`. All 180 recheck-2
  probes now fail, and none removes an existing reason (it was 72 passing).
  Spelling probes on K2: the right page as a path, a prefix-less path, or a
  `#unit` of its path or memory ref passes in all five channels. A wrong page
  in those spellings fails. A unit whose own ref names another known page is
  judged on that page, not on its provenance.
- **Pin and run order: honest.** `932c6f5` touches only the module and the
  test. It pins `8c6eb814…` and has no JSON and no run tests. The v3 JSON and
  the run tests arrive four minutes later in `2158e84`. The module's
  CRLF-normalised sha256 is `1086854d…` at the pin, the run and `0e09560`,
  matching the recorded `scorer_source_sha256`.
- **C7 disclosure and A7: FIXED.** The runbook discloses the raw gap
  (`docs/benchmarks/context-activation.md:260-269`), and so does
  `design.md:153`. Under A7:
  - with C7's gold hubs as partial anchors, a wrong candidate fails:
    precision 0.00 alone, 0.75 beside all three gold candidates, 0.67 beside
    two;
  - two or three gold candidates still pass, at precision 1.0;
  - twins are unchanged.

  I also tried hiding the wrong candidate behind a credited superseded unit of
  the same ref. That removes it from the precision denominator, but the
  ambiguous-status rule forbids units, so the packet still fails. A7 changes
  no verdict on v4 (5/18).
- **Raw `per_case`: byte-identical.** The v4 JSON diff since `7dc2c2b` adds
  lines only: a new `amended_a7` block. `per_case` and `amended` are equal as
  objects. The historical product JSON and the v1 and v2 continuity JSON are
  unchanged.

**Minor, non-blocking:** `context_activation_continuity.py:310` skips a unit
that is not a mapping. A malformed packet whose units are bare strings would
therefore escape `served_subset`, and on K3 `serves_nothing` too; v2 counted
any units. Real packets emit mappings. A one-line fallback to
`identities.page(str(item))` would close it.

**APPROVE**
