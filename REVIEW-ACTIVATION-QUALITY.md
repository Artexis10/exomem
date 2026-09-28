# Review: activation quality (PR #1440, `feat/activation-quality` at cc8d745)

**Verdict: REQUEST_CHANGES.** The gain (raw 5/18 → 9/18) is real and `src/` has no
corpus constants. But two of the five mechanisms fit the benchmark's wording rather
than a real vault.

I reviewed the three-dot diff from `origin/test/activation-benchmark-corpus`. Probes
ran on the PR head and on a local, unpushed merge with the base head (0e09560,
continuity v3).

## Findings

### HIGH: M2 narrows on a qualifier the turn never attached to the name
`src/exomem/working_set_resolve.py:1799-1833`. `name_contact` is a set of words
(`:996`, `:1027`), so any word of the wider name, anywhere in the turn, narrows. The
narrower sense is then dropped from every list.

- **Detached word.** Hubs "Kitchen renovation hub" and "Kitchen renovation budget
  hub", both retrieved. "I blew my grocery budget this week, and the kitchen
  renovation is stalled again" → `resolved` to the budget hub alone. Without
  "budget" the turn is honestly `ambiguous`.
- **Both named.** "Is the monitoring wired up yet, and did the solar array pass
  inspection?" → "Solar array monitoring hub" alone. "Solar array hub" is dropped.
- **Reproduction.** Rows from `_resolve`/`_row` in
  `tests/test_working_set_resolve_senses.py`, with both paths retrieved.
- **Impact.** An honest question becomes a confident wrong answer with nothing left
  to pick. Nested hub names ("X", "X budget", "X notes") are common on a large
  vault.
- **Fix.** Derive the contact from the longest contiguous turn span of one anchor's
  name words (positions from `term_positions`). Narrow only when the wider anchor's
  span strictly contains the narrower one's. T7 ("AI search feature", one span)
  still passes. Add both cases as controls.

### MEDIUM: M1 reads an ordinary noun in two entity names as a name
`_bare_name_groups`, `:1860-1890`. Any word two entity titles share counts, if it
is rare among anchor names. The comment at `:963-970` already says anchor-name
rarity is not word rarity. M1 makes that a turn-level decision and suppresses the
carry.

- **Scenario.** Entities "Harbour Bakery" and "Harbour Clinic". "the harbour was
  busy this morning, ferries everywhere" → `ambiguous` between the two businesses,
  carry not asked. At base it was `unresolved` and carry-eligible.
- **Fix.** Form the group only when the shared token is capitalised in the raw turn
  (cased scripts), or only for person entities. T4 ("Alex mentioned…") still passes.
- **Test gap.** The requirement's "the retrieval carry SHALL NOT be asked" has no
  compiled-packet test; the new file is pure resolver (`:16`). Add a vault-level
  test with a carryable note.

### LOW
1. **Fixture name in `src/`.** `working_set_resolve.py:1869-1870` says "Alex", T4's
   fixture name, which is new to `src/`. Use an invented name.
2. **M5 ignores `superseded_by`.** `_excluded_rows_clause` in `lexstore.py` reads
   only `p.status`, but `_is_current_page` also retires pages with `superseded_by`.
   Probe: three `status: active` revisions with `superseded_by` still block the
   carry. The spec says "status", so either count `superseded_by` or narrow the
   rationale. `status: Superseded` (capitalised) is excluded correctly.
3. **M4 scenarios unpinned.** No test covers "an agent choice naming the collection
   selects every item", "items filed together stay complementary" or "an unreadable
   item page reports its home". I probed the first two and they hold.
4. **Stacking debt.** After merging base head 0e09560 (amendment A7), the v4 report
   is stale: 2 real-compiler failures. A fresh run gives raw 9/18 and A7 9/18.
   Re-record after the next base merge.

## Hypotheses cleared

- **Constants.** I grepped every word of the corpus against the added `src/` lines.
  The only hit is "alex" (Low 1). No constant is tuned to v4.
- **M3.** Ledes are capped at 240 characters and units at 360, so a dropped repeat
  is never lost to truncation. A repeat on another page is kept.
- **M5.** Four current namesakes still block the carry.
- **Withheld = absent.** I withheld one Planning item page (ceiling 0, external
  audience) and compared against a twin vault without that item. Three turns, plus
  a carryable released note, gave identical packets: `audience_view` decides the
  item ref before resolution. M1 and M2 run after that gate on audience term counts.
- **Regressions.**

  | Run | Result |
  |------|------|
  | Real-compiler and continuity suites, PR head | 68 passed |
  | Continuity v3, merged head | green |
  | 72 modules (`working_set_*`, `activation*`, `context_activation_*`, `recall*`, `records_recall*`, `resolver*`, `retrieve_nudge*`, `lexstore*`), merged head | 2,366 passed, 3 failed, 28 skipped |

  Two failures are Low 4. The third
  (`test_activation_lexical_term_budget::…stops_at_k`, SQLite query plan) fails the
  same on base. "continue", a bare name, a plan-item question, a scoped question
  and an item-page choice behave as specified.
- **Latency.** Harness path, cold caches, 90 activations per run, alternating pairs:

  | Pair | Base ws p95 (ms) | Head ws p95 (ms) | Delta (ms) |
  |------|------|------|------|
  | 1 | 166.9 | 170.2 | +3.3 |
  | 2 | 153.6 | 135.9 | −17.7 |
  | 3 | 141.4 | 166.4 | +25.1 |

  Inside base's own 25 ms spread, so no regression.
- **Spec.** M3, M4 and M5 state general rules. M2 must say "contiguous", and M1
  must say what makes a word a name.

## Environment
The tiktoken and huggingface hosts are blocked here. I rebuilt `o200k_base` from npm
`js-tiktoken`; its sha256 matches tiktoken's pinned `446a9538…`. There is no xdist,
so the suites ran serially. Nothing was left to CI.
