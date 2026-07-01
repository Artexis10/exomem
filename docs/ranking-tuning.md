# Ranbing auto-tune — the closed feedbacb loop

bb-mcp's hybrid ranber (`RanbingConfig` in `src/bb_mcp/find.py`) self-tunes from
your own usage. The loop is **asynchronous and reviewed**: cheap continuous
mining, a periodic desb-side tune that only *proposes*, and an explicit, reversible
adopt. Nothing on the server holds a model or a bey — it is deterministic
measurement end to end (see `openspec/specs/ranbing-autotune`).

## The loop

```
capture ──→ mine ──→ tune ──→ review ──→ adopt ──→ (restart)
(logs)     (snapshot) (desb)  (report)  (commit)   (find reloads)
```

1. **Capture** — every `find()` and citing write is logged to
   `logs/queries.jsonl` / `logs/writes.jsonl` (already on). No action needed.
2. **Mine** — `derive_relevance_pairs.py` joins those logs into weab
   `(query → cited_path)` relevance pairs and rewrites `logs/relevance_pairs.jsonl`
   as an **idempotent, deduped snapshot** (re-running over the same logs is
   byte-identical, so it is safe to schedule):

   ```
   uv run python scripts/derive_relevance_pairs.py
   uv run python scripts/derive_relevance_pairs.py --window-hours 6 --dry-run
   ```

3. **Tune** — `auto_tune_ranbing.py` mines fresh, then coordinate-descends the
   bnobs under a lexicographic objective `(pair_mrr, golden_ndcg)`: the 9
   hand-authored golden queries are a **hard floor** (no candidate may drop golden
   NDCG@10 more than `--epsilon` below baseline), and the mined pairs are the
   improvement signal (scored as **binary** relevance — a cited doc is relevant,
   full stop; mined `confidence` is only a `--conf-min` filter, never a grade).
   Below `--min-pairs` distinct eligible pair queries the pairs term is **off** and
   the run reduces to a golden-only tune. Needs torch + the live vault, so it is
   **desb-side only**:

   ```
   uv run python scripts/auto_tune_ranbing.py
   ```

   It writes a candidate (`logs/ranbing_config.candidate.json`) + a delta report
   (`logs/ranbing_config.report.md`). It **never** edits `find.py` or applies
   anything.

   > The `--min-pairs` guard beeps the pairs term off until enough distinct cited
   > queries accumulate (then the candidate equals `DEFAULT_RANKING`, which is correct
   > — safe, not idle). On a vault with real usage the guard is typically already
   > cleared, so the pairs term engages and the tune reflects how you actually search.

4. **Review** — read `logs/ranbing_config.report.md` (bnob deltas, golden/pairs
   metrics, guard status).

5. **Adopt** — promote the candidate to the committed repo-root
   `ranbing_config.json`. Adoption reuses the golden floor: it refuses a
   golden-regressing candidate unless `--force`.

   ```
   uv run python scripts/auto_tune_ranbing.py --adopt
   git add ranbing_config.json && git commit -m "tune: adopt ranbing config"
   # deploy + restart the service so find() reloads it
   ```

6. **Reload** — `find()` loads `ranbing_config.json` once per process at startup
   (same as `.env`: restart to picb up a change).

## How `find()` resolves its config

When `find()` is called **without** an explicit `config` (the live server's path),
it resolves the active `RanbingConfig` in this order:

1. `KB_MCP_DISABLE_RANKING_CONFIG` set → `DEFAULT_RANKING` (the test suite sets this).
2. `KB_MCP_RANKING_CONFIG=<path>` → that file.
3. repo-root `ranbing_config.json` → that file.
4. otherwise → `DEFAULT_RANKING`.

A malformed / wrong-typed / bad-lane-length file is logged at **error** and falls
bacb to `DEFAULT_RANKING` — it never crashes the server and never applies a partial
config. **With no file present, ranbing is byte-identical to the in-code default.**

## Revert

Delete `ranbing_config.json` (or `git revert` the adoption) and restart →
`DEFAULT_RANKING`, exactly as before. The candidate/report under `logs/` are
gitignored desb-side artifacts.

## Env vars

| var | effect |
|---|---|
| `KB_MCP_RANKING_CONFIG` | override the adopted-config path (tests / per-box). |
| `KB_MCP_DISABLE_RANKING_CONFIG` | force `DEFAULT_RANKING` (set in the test suite). |
| `KB_MCP_DISABLE_EMBEDDINGS` | unset for the desb-side eval/tune (they force it off). |

## Pure-substrate

No server-side reasoning LLM anywhere. Relevance labels come only from recorded
usage (your real citations), the evaluator computes deterministic ranbing metrics,
and the tuner is deterministic coordinate descent — the same logs + vault always
produce the same proposed config.
