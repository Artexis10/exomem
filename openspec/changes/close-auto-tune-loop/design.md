# Design — Close the auto-tune loop

## Context

The ranker tuning loop has five stages: **capture** (queries/writes JSONL logs,
already written by `query_log`) → **mine** (`derive_relevance_pairs.py`) →
**incorporate** (pairs enter the tuner objective) → **tune**
(`auto_tune_ranking.py` coordinate descent) → **adopt** (`find()` uses the tuned
config). Capture and tune exist and work. Incorporate is broken (pairs are loaded
but never scored), mine is non-idempotent (append), and adopt has no seam (hand-edit
`find.py`). This change fixes the three broken stages without touching capture or the
descent algorithm.

The loop is inherently **asynchronous**: mining is cheap and torch-free; tuning is
heavy and desk-side (it force-enables embeddings and runs `find()` per candidate
over the live vault, contending the GPU with the live service); adoption is a human
flip. There is no real-time component and no daemon — consistent with the
pure-substrate line (the server measures; a desk-side script or a scheduled Claude
Code routine drives tuning; nothing on the box holds a metered key).

## Goals / non-goals

- **Goal:** mined usage actually moves the tuner, without letting ~21 weak labels
  overpower 9 trusted goldens or regress retrieval.
- **Goal:** a tuned config can take effect as a reviewed, reversible file — no code
  edit, no auto-mutation.
- **Goal:** default-off by absence — with no adopted file, ranking is byte-identical
  to today.
- **Non-goal:** broadening the relevance signal (clickthrough, negatives, explicit
  `found_via`). Single note-citation channel this round.
- **Non-goal:** auto-applying a tune, hot-reloading config, or any server daemon.
- **Non-goal:** tuning the image-tag / contradiction / diarization thresholds.

## The combined objective (the subtle part)

**A mined pair is a binary fact, not a graded score.** The miner's
`confidence = (1/rank)·(0.5 + 0.5·recency)` embeds `rank_in_results` — the rank the
cited doc held under *whatever config was live when the pair was mined*. Using
`confidence` as the relevance **gain** would bake the incumbent ranking into the
optimization target: the highest-confidence (rank-1) pairs would reward keeping
things exactly where they are, and what little gradient exists would push toward the
*old* config. So:

- **Grade is binary:** a cited doc is relevant (`1`), full stop. `confidence` is used
  only as a **filter** (`CONF_MIN`, default `0.25`) to drop the weakest mined hits.
  Rank-1 pairs then act as *guardrails* (moving them down lowers the score), not as a
  free lever.
- **Dedup against golden:** drop any pair whose query matches a golden query before
  scoring, so the trusted signal isn't double-counted.

**Objective shape: lexicographic tuple with a hard floor.** `optimize()` already
selects by `>` only, and Python tuples compare lexicographically, so returning a
tuple is a drop-in (only the `:.4f` print needs a fix):

```
baseline_g = golden_ndcg10(DEFAULT_RANKING)        # measured once at start
def objective(knobs) -> tuple:
    g = golden_ndcg10(knobs)                       # trusted anchor
    if g < baseline_g - EPSILON:    return (-1.0, g)   # HARD FLOOR → infeasible
    if n_eligible_pairs < MIN_PAIRS: return (0.0, g)   # GUARD → golden-only (== today)
    return (pair_mrr(knobs), g)                    # primary: pairs; tiebreak: golden
```

- A **weighted sum** `α·golden + β·pairs` is rejected: any β large enough for ~21
  weak pairs to matter is large enough to let noise *buy* a golden regression. The
  constrained form structurally cannot sell the anchor and needs one interpretable
  threshold (`EPSILON`) instead of an unknowable `(α, β)`.
- **Pair metric is MRR** (gentle reciprocal discount, rank-sensitive without
  over-trusting exact positions on ~21 labels); `recall@10` is reported alongside
  for the human. NDCG on binary grades over so few labels compresses badly — avoid.
- `DEFAULT_RANKING` is always feasible (`baseline_g − EPSILON ≤ baseline_g`), so the
  incumbent is well-defined and `optimize()`'s "ties keep incumbent" determinism
  holds.

**Constants (all CLI-overridable):** `EPSILON = 0.01` absolute mean-NDCG@10 (≈ one
golden query's sub-position noise; blocks any real regression), `MIN_PAIRS = 8`
distinct eligible queries (≈ the golden set size — "at least as much signal as the
trusted set"; keeps pairs OFF until the signal is real), `CONF_MIN = 0.25`.

The safety stack: dedup-golden → conf-filter → `MIN_PAIRS` guard → binary MRR (no
rank baking) → hard golden floor (EPSILON-bounded). Even if the pairs term overfits,
golden cannot regress past `EPSILON`.

## The adopt seam in `find.py`

`find()` resolves config via a new memoized `_active_ranking()` whose resolution
order is:

1. `KB_MCP_DISABLE_RANKING_CONFIG` set → `DEFAULT_RANKING` (hermetic escape hatch;
   set by the test suite's autouse fixture so a committed file never pollutes tests).
2. `KB_MCP_RANKING_CONFIG=<path>` → load that path (tests / per-box override).
3. else repo-root `ranking_config.json` (`Path(__file__).resolve().parents[2]`).
4. else `DEFAULT_RANKING`.

The seam is the call-site asymmetry: the live `op_find` calls `find()` with **no**
`config=` → `config is None` → consult the adopted file; both eval harnesses pass
`config=cfg` explicitly → never consult disk → hermetic. The one-line change is
`config or DEFAULT_RANKING` → `config if config is not None else _active_ranking()`
(`is not None`, because a `RankingConfig` is always truthy and we must auto-load only
when genuinely omitted).

- **Adopted file = committed repo-root `ranking_config.json`.** Adoption *is* a git
  commit: the diff shows the knob deltas, `git revert` is the reversal, history is
  the provenance, and it survives the deploy checkout's `reset --hard origin/main`.
  The candidate + report live in gitignored `logs/`. (The file is generic numeric
  knobs, not under `src/kb_mcp/`, so the leak-guard is irrelevant; absent →
  `DEFAULT_RANKING` means a cloner can just delete it. Env-override + a gitignore
  fallback exist if a tune should stay private.)
- **(De)serialize:** `dataclasses.asdict(cfg)` writes all fields (tuples → JSON
  arrays); load is field-driven over `dataclasses.fields(RankingConfig)` — coerce
  int/float, coerce the four `intent_weights_*` to length-6 float tuples, **ignore
  unknown keys** (schema-drift-safe), **default missing keys**.
- **Fail loud → DEFAULT.** Any `JSONDecodeError`, type error, or tuple-length
  mismatch → `log.error(...)` + `DEFAULT_RANKING`. Never crash; never silently apply
  a half-parsed config. An unknown/renamed knob is the one non-fatal case (warn,
  keep going with that field defaulted).
- **Load-once + restart to reload.** A per-process memo (`reset_active_ranking_cache()`
  for tests) loads the file once; picking up a newly-adopted file is a service
  restart, identical to `.env` semantics. Hot-reload is a non-goal (a watcher is
  stateful and reopens the read race).
- **Atomic write** for the candidate and the adopted file (tmp + `os.replace`, the
  existing `vault.batch_atomic_write` pattern) so a concurrent reader at startup sees
  whole-old or whole-new, never a torn file.

## Mining — idempotent snapshot

`derive_relevance_pairs.main()` is refactored into a reusable
`mine_pairs(window_seconds, *, write, logs_dir) -> list[dict]`. The pairs file is a
pure derived artifact of `queries × writes × window`, so mining **rewrites a deduped
snapshot** (atomic `os.replace`, mode `w`) rather than appending — same logs in →
byte-identical file out, no cross-run duplicates, no unbounded growth. The source
logs remain the audit trail. The tuner imports `mine_pairs` and mines fresh as step
0 so the objective always reflects current usage. Mining stays a standalone
desk-side script (independently useful for proposing golden additions) and MAY be
run by a scheduled Claude Code routine; it is never a server daemon, and adoption is
never inside `tune`.

## Adoption gate (reuses the floor, no torch at adopt time)

`write_candidate()` embeds the measured `baseline_golden` / `candidate_golden`
(plus `pair_mrr`, `pair_recall10`, `n_eligible_pairs`, `guard_active`, `window_hours`)
in the candidate JSON's meta. `adopt(candidate, target, *, force)` validate-loads the
candidate, refuses to promote it when `candidate_golden < baseline_golden − EPSILON`
unless `--force`, then atomically copies it to the repo-root path and prints the
git-commit + restart steps. So the same floor that governs tuning gates adoption —
no torch rerun needed to adopt.

## Edge cases

- **Empty / zero eligible pairs** → guard → golden-only tune (== today); candidate
  still written (may equal `DEFAULT_RANKING`); warn.
- **All pairs already in golden** → after dedup, eligible = 0 → guard; warn "nothing
  new to learn."
- **Unknown / renamed knob in the adopted JSON** → ignored (field-driven load),
  logged; that field defaults. Non-fatal.
- **Malformed JSON / wrong type / bad tuple length** → fail loud + `DEFAULT_RANKING`.
  Atomic write means a partial file can't occur.
- **Concurrent read during a restart-coincident write** → atomic `os.replace` +
  load-once memo → old-or-new whole file.
- **Adopted config makes golden worse** → adoption blocked by the floor gate unless
  `--force`.
- **Reversibility** → delete (or `git revert`) `ranking_config.json` →
  `_active_ranking()` finds nothing → `DEFAULT_RANKING`, byte-identical. Guarded by a
  test.

## Decisions

- **Binary pairs, not confidence-graded.** Confidence embeds the incumbent rank;
  grading by it is circular and gameable. Confidence filters; it never grades.
- **Lexicographic floor, not a weighted sum.** One interpretable threshold; the
  anchor structurally cannot be sold for noise.
- **Default-off by absence.** No file → `DEFAULT_RANKING`, byte-identical. The whole
  feature is inert until a human adopts.
- **Reviewed adoption, committed file.** Visibility + reversibility via git, matching
  the project's "visibility over auto-mutation" stance — but mechanically closed (no
  hand-edit of `find.py`).
- **Snapshot, not append.** A derived artifact is regenerated, not accumulated.

## Risks

- **Tiny, weak pairs set barely moves / overfits.** Mitigated by the `MIN_PAIRS`
  guard (pairs stay OFF until real), binary grade, and the hard floor. The most
  likely near-term outcome is "the loop is a correct no-op," which is the intended
  conservative behavior, not a bug — stated plainly so the win is read as "the loop
  is now real and safe."
- **A committed `ranking_config.json` silently changes server behavior.** Mitigated
  by the conftest disable flag, the absent→DEFAULT path, and the reversibility test;
  the residual risk is a human forgetting the live box needs a restart — documented
  as `.env`-equivalent.
- **Schema drift after a future `RankingConfig` refactor.** Field-driven load
  (unknown-ignore, missing-default) + a loud log; worst case a renamed knob reverts
  to default, visible in the report/diff.
