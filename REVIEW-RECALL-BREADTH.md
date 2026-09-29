# Review: feat/activation-recall-breadth (PR #1449) at fead38a9

Diff: `merge-base(origin/feat/activation-quality, head)..head` (6e4dc933), 31 branch-owned files.

## Verification

- Tokenizer: rebuilt from npm `js-tiktoken`. sha256 `446a9538…` matches.
- Real-compiler, continuity, `working_set_*`, `activation*`, request-path and roles suites: **1515 passed, 21 skipped, 1 failed**. The failure (`test_activation_lexical_term_budget…stops_at_k`, a SQLite query-plan assertion) also fails on base 6e4dc933.
- `gh pr checks 1449`: 36 runs. 27 passed, including the required gate, all core and harness shards, and OpenSpec. 9 were path-skipped. None failed.
- Packet size (injected text measured live on base and head builds, o200k tokens):

| set | base median / p95 | head median / p95 |
|--|--|--|
| corpus bytes | 210.5 / 391 | 94 / 410 |
| corpus tokens | 47.5 / 118 | 18.5 / 115 |
| continuity bytes | 178.5 / 309 | 155 / 220 |
| continuity tokens | 40 / 67 | 34.5 / 48 |

  Caveat: the corpus has no standing page and no unit over 360 chars, so the two paths that grow packets never run (M2).
- Latency, 72 interleaved cold activations per side: p95 252.3 → 259.6 ms (**+2.9%**, within bound); median +10.2%.
- Withheld = absent: a probe withheld the standing page and the conclusion page, and `guard_working_set` removed both. The PR has no test for this.
- Qualifier: units are never cut (`working_set.py:483`), and the hook renders whole lines only. Safe.

## Findings

**High**

H1. **The K2 cause returns through the precedent reach.** The recency-only gate works on roles (`context_roles.py:765`). `reach_precedents` then walks every resolved anchor without checking evidence (`working_set.py:822`, `:834`). If `precedents` is selected any other way (the cue "before", or a named project or hub), a recency-supplied entity still gets its conclusion pages and its project's standing page. Probe: a `("recency",)` entity with the turn "before we ship, where were we" returned both. The gate also leaves out `project` (`context_roles.py:734`), so a recency-only project serves its methodology page on every "where were we" turn. Fix: skip prior-only anchors inside `reach_precedents`, and add the recency tests.

**Medium**

M1. **"Newest first" is really "alphabetically first".** `_conclusion_pages` keeps the first 24 holders by path before it sorts by date (`working_set.py:786`). Probe: 30 older `ab-old-*` decisions pushed out the newest (`zz-…`), and all six served were from 2020. Fix: date-sort every holder (they are already bounded).

M2. **Standing units jump the queue at up to 900 chars.** They sort first (`:437`), skip the role cap, and may be 900 chars each. Two can take about 1,800 of 4,000 chars (~450 tokens) on every turn that resolves a project, relevant or not. The hook stops at the first line that doesn't fit, so one long line can truncate everything after it. That is within the ruling, but it runs against the byte cut. Cap standing units at `MAX_UNIT_CHARS`, or point to them, and add a project fixture to the size report.

M3. **The named anchor wins `anchors[]` but not its material.** Beside items share the pool (`:2785`), and `_sort_key` ranks only by role, lifecycle and date. A newer carried page can take the resolved anchor's slots under `MAX_ITEMS_PER_ROLE`. The ruling test checks anchor order only. (Found by reading the code; not reproduced.)

**Low**

L1. `thread_overlap` accepts 3 shared body words out of 6,000 chars (`:3122`), which is loose for common vocabulary.

L2. The carry label is only in the packet. The hook renders carried units as plain `unit` lines, so the agent never sees the label.

## Verdict

**REQUEST_CHANGES.** H1 reopens K2, and M1 defeats the link-overflow reach. Both are small, local fixes. M2 and M3 need a test or an explicit ruling.
