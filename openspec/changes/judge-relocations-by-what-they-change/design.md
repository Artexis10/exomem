## Context

A governed move runs one preflight for each step. The preflight builds the corpus after the move, then finds every page whose links or link standing the move changes (`_move_evaluation_pairs`). It then judges the moved page and those dependents with `semantic_contract.evaluate`, as operation `move`. The undo of a relocation runs the same judgement through `ProjectedMoves`.

## Decisions

### A relocation passes its own flag, not `grandfathered`

`evaluate` takes `non_worsening` beside `grandfathered`. Both turn on the same subset rule: keep each error key that the before state had and refuse each new one. A satisfied relation disposition that the operation breaks still blocks. The two flags answer different questions. `grandfathered` says that the page belongs to the activation baseline. `non_worsening` says that the operation cannot change what the page says. Setting `grandfathered` for a page outside the baseline would make that flag mean two things.

`_evaluate_relocation` sets the flag for every page that it judges. `ProjectedMoves.refuse_blocked` calls the same function, so a relocation and its undo pass or refuse by one rule.

### An older page follows its lineage back to the baseline

The activation manifest matches a page without an exomem_id by its path and its source hash. A governed move changes the path and appends a `moved_from` entry, so the hash changes too. `page_lineage.remove_latest` gives back the exact bytes that the move found. The grandfathering check removes the page's lineage entries one at a time, newest first, and asks the manifest about each earlier path with the hash of the bytes at that point. It reads the page only when the page has no exomem_id, records lineage, and fails the direct check.

The edit preflight, its re-evaluation at commit and the posthoc audit use this check. The relocation does not need it, because its flag already keeps old debt. Recovery keeps the direct check.

### A move patches the corpus

`SemanticCorpusContext.with_pages` already patches a corpus by a delta. It derives again only the pages that the delta reaches: by resolver dependency keys, by changed prior claims, by facts that resolve into the delta, and by the holders of a shared exomem_id. Above 64 changed pages, or when more than half the vault is reached, it falls back to a full derivation. The move path now uses it instead of `from_states`. `_move_evaluation_pairs` still compares every page, so the set of judged pages is unchanged.

## Measurements

Measured on 2026-11-23 on a laptop with the corpus cache on, under an owner request scope, after `warm_corpus_context`. Each generated vault has insight notes in 150 projects, five typed relations and two cited Sources per note, and one Source page per ten notes. 15% of the notes have a slug for a file name. A preflight is one move of a slug note to its title with `validate_only`. The proposal is `relocate` of 50 slug notes; it seals 50 moves and the link rewrites of 98 to 107 referring pages, and refuses none in either arm. The runs alternate between the two arms.

| Notes | Preflight median, before | Preflight median, after | 50-page proposal, before | 50-page proposal, after |
|---|---|---|---|---|
| 3,000 | 1.90 s, 1.95 s | 1.25 s, 1.19 s | 106.7 s, 104.3 s | 62.4 s, 70.2 s |
| 10,000 | 7.10 s, 7.01 s | 4.94 s, 5.12 s | 386.3 s, 394.7 s | 274.8 s, 261.7 s |
| 17,000 | 13.40 s, one run | 8.62 s, one run | 936.9 s | 503.6 s |

Sampled at the start and end of each run, the one-minute load average was between 5.6 and 11.7. The 17,000-note pair ran once, the after arm first.

With the patch, `_move_evaluation_pairs` takes most of what remains: 87% of three profiled preflights at 3,000 notes. It compares the dependency signature of every page before and after each move.

## Risks

- A rename can take a page out of a name that exempts it from the semantic minimum, such as a breadth suffix. The page then gains a `missing_semantic_unit` error, and the relocation refuses it. These were the only refusals left on the vault copy. A relocation that should keep such pages needs the name rule, not this judgement, to change.
