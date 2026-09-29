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

## Recheck 1 (fix round 6214e4c..6e4dc93)

**Verdict: REQUEST_CHANGES.** M1, M5 and M4 are done. M2's contiguous run ignores
punctuation, so the grocery-budget failure comes back across a comma or a full stop.

### Findings

| Finding | Verdict | Evidence |
|------|------|------|
| HIGH M2 | **PARTIAL** | Both review controls pass, and so does T7. But see New concern 1. |
| MEDIUM M1 | **FIXED** | `_spoken_as_name` (`working_set_resolve.py:1963`) forms the group for all-person groups, or for a word capitalised mid-sentence in a cased script. `test_working_set_bare_name_carry.py:62` is the compiled-packet test: the carry is not asked and `carried_by` is `None`. `:79` checks that the harbour turn still carries the note. |
| LOW 1 | **FIXED** | `git grep -i alex -- src/` returns nothing. The docstring now uses an invented name. |
| LOW 2 | **FIXED, bounded** | `discount_superseded_pages` (`working_set.py:1311`) checks each page with `_is_current_page`. It only covers stems counted at most `RETRIEVAL_CARRY_FETCH` (10) pages above the cap. A name with more than cap+10 revisions retired only by `superseded_by` still blocks the carry. That is acceptable, but the docstring should say it. |
| LOW 3 | **FIXED** | `test_working_set_planning_item_ref.py:89,100,114` pins all three M4 scenarios. |
| LOW 4 | **FIXED for 6e4dc93** | Merge 80b98fe took the base's v3 and v4 reports, and the real-compiler suite is green at the head (see Runs). The base has since moved to 1c6077d (A9, A10). The next base merge needs another re-record. |

### New concerns

1. **HIGH: punctuation does not break M2's run.** `_name_span`
   (`working_set_resolve.py:1150`, called at `:1128`) walks `analysis.tokens`, and
   `tokens_of` drops punctuation. So "solar array, monitoring" is one run. Probes use
   the test file's own rows with both hubs retrieved:
   - "Check the solar array, monitoring can wait." → `resolved`, Solar array monitoring hub only. Solar array hub is dropped.
   - "The solar array. Monitoring is next week." → same result.
   - "The kitchen renovation? Budget talk can wait." → budget hub only.
   - "Update the kitchen renovation, budget is fine." → budget hub only.

   This is the same confident wrong answer the review found, now from a comma. A
   coordinator also bridges a run: "the kitchen renovation and the budget for it"
   narrows. That reading is arguable, but a list ("X and Y") is two things. Fix: let
   clause punctuation (`_SENTENCE_END` plus comma, colon and dashes) end a run, and
   consider "and" and "or" as run breakers. The spec's "contiguous run of turn
   tokens" should then say that punctuation ends a run. Add the four turns above as
   controls.
2. **LOW: every person-typed group ignores casing, even in a cased turn.** With two
   `person` entities "Mark Ellison" and "Mark Fenwick", "Please mark the task done" and
   "I left a pencil mark on the draft plan" come back `ambiguous`, and the carry is
   suppressed. The ruling allows the person case, and "priya" in lower case is a
   deliberate test. Still, in a cased turn that capitalises some other word mid-sentence
   (so it is not all lower case), a lower-case person word could fall back to the
   capital test. Owner's call.
3. **LOW: headline casing.** "Notes From The Harbour Walk" → `ambiguous` between the
   two businesses. A turn where every word is capitalised carries no casing signal,
   the same as an all-caps turn.

### Attacks

- **Uncased scripts (CJK), M1.** Organisation entities "海港 面包店" and "海港 诊所": "海港 今天 很 忙" → `unresolved`, so no group, as specified. Persons 山田 ask (pinned).
- **Uncased scripts (CJK), M2.** For spaced turns: "厨房 装修 预算" with the detached word "我 的 买菜 预算 超了 厨房 装修 又 停了" → `ambiguous`, correct. For unspaced turns, `tokens_of` yields whole runs such as "厨房装修预算怎么样", so no name word matches and both hubs stay `partial` (`unresolved`). That is safe, but it means M2 never narrows in unspaced CJK. That limit is in the existing tokeniser, not in this round.
- **Withheld = absent.** `audience_view` removes a withheld row before `candidates_for`, so its `entity_type` never reaches `_bare_name_groups`. With one of two persons withheld ("Priya sent the invoice.") and one of two organisations withheld ("We ordered from Harbour again."), the result equals the twin catalogue without that row: `unresolved`, one `partial`.
- **Person name at sentence start.** "Mark called about the invoice" → `ambiguous`, which is a true positive: the person case ignores casing, so the sentence-start rule never applies to persons. Organisations at sentence start ("Harbour sent the invoice.") stay `unresolved`. That errs towards carrying, not asking, and is acceptable. After an abbreviation ("approx. Harbour lunch") the word counts as a sentence start and is not grouped, erring the same way.

### Runs

Local suite (real-compiler, continuity, `working_set_*`, `activation*`) still running; numbers follow in the next push.

`gh pr checks 1440` (read through the GitHub API) at 6e4dc93: 25 passed, 10 skipped,
0 failed, and the required CI gate passed.

### Environment
I rebuilt the tokenizer from npm `js-tiktoken`: sha256 `446a9538cb6c…`, which matches.
The CJK and M2 probe turns are resolver-level, using `_row` and `_resolve` from
`tests/test_working_set_resolve_senses.py`. The withheld probe goes through `audience_view`.
