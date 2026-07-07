# Ranking auto-tune - the closed feedback loop

Exomem's hybrid ranker (`RankingConfig` in `src/exomem/ranking_config.py`) self-tunes from
your own usage. The loop is asynchronous and reviewed: cheap continuous
mining, a periodic desk-side tune that only proposes, and an explicit, reversible
adopt. Nothing on the server holds a model or a key for this loop. It is
deterministic measurement end to end (see `openspec/specs/ranking-autotune`).

## The loop

```text
capture -> mine -> tune -> review -> adopt -> restart
(logs)    (snapshot) (desk) (report) (commit)  (find reloads)
```

1. **Capture** - every `find()` and citing write is logged to
   `logs/queries.jsonl` / `logs/writes.jsonl` (already on). No action needed.
2. **Mine** - `derive_relevance_pairs.py` joins those logs into weak
   `(query -> cited_path)` relevance pairs and rewrites
   `logs/relevance_pairs.jsonl` as an idempotent, deduped snapshot. Re-running
   over the same logs is byte-identical, so it is safe to schedule:

   ```bash
   uv run python scripts/derive_relevance_pairs.py
   uv run python scripts/derive_relevance_pairs.py --window-hours 6 --dry-run
   ```

3. **Tune** - `auto_tune_ranking.py` mines fresh, then coordinate-descends the
   knobs under a lexicographic objective `(pair_mrr, golden_ndcg)`: the
   hand-authored golden queries are a hard floor (no candidate may drop golden
   NDCG@10 more than `--epsilon` below baseline), and the mined pairs are the
   improvement signal (scored as binary relevance - a cited doc is relevant,
   full stop; mined `confidence` is only a `--conf-min` filter, never a grade).
   Below `--min-pairs` distinct eligible pair queries the pairs term is off and
   the run reduces to a golden-only tune. Needs torch plus the live vault, so it
   is desk-side only:

   ```bash
   uv run python scripts/auto_tune_ranking.py
   ```

   It writes a candidate (`logs/ranking_config.candidate.json`) and a delta
   report (`logs/ranking_config.report.md`). It never edits `find.py` or applies
   anything.

   > The `--min-pairs` guard keeps the pairs term off until enough distinct
   > cited queries accumulate. On a vault with real usage the guard is typically
   > already cleared, so the pairs term engages and the tune reflects how you
   > actually search.

4. **Review** - read `logs/ranking_config.report.md` (knob deltas,
   golden/pairs metrics, guard status).

5. **Adopt** - promote the candidate to the committed repo-root
   `ranking_config.json`. Adoption reuses the golden floor: it refuses a
   golden-regressing candidate unless `--force`.

   ```bash
   uv run python scripts/auto_tune_ranking.py --adopt
   git add ranking_config.json && git commit -m "tune: adopt ranking config"
   # deploy + restart the service so find() reloads it
   ```

6. **Reload** - `find()` loads `ranking_config.json` once per process at startup
   (same as `.env`: restart to pick up a change).

## How `find()` resolves its config

When `find()` is called without an explicit `config` (the live server path), it
resolves the active `RankingConfig` in this order:

1. `EXOMEM_DISABLE_RANKING_CONFIG` set -> `DEFAULT_RANKING` (the test suite sets this).
2. `EXOMEM_RANKING_CONFIG=<path>` -> that file.
3. repo-root `ranking_config.json` -> that file.
4. otherwise -> `DEFAULT_RANKING`.

A malformed, wrong-typed, or bad-lane-length file is logged at error and falls
back to `DEFAULT_RANKING`. It never crashes the server and never applies a
partial config. With no file present, ranking is byte-identical to the in-code
default.

## Revert

Delete `ranking_config.json` (or `git revert` the adoption) and restart:
`DEFAULT_RANKING`, exactly as before. The candidate/report under `logs/` are
gitignored desk-side artifacts.

## Env vars

| var | effect |
|---|---|
| `EXOMEM_RANKING_CONFIG` | Override the adopted-config path (tests / per-box). |
| `EXOMEM_DISABLE_RANKING_CONFIG` | Force `DEFAULT_RANKING` (set in the test suite). |
| `EXOMEM_DISABLE_EMBEDDINGS` | Unset for the desk-side eval/tune (tests force it off). |

## Pure substrate

No server-side reasoning LLM is involved. Relevance labels come only from
recorded usage (your real citations), the evaluator computes deterministic
ranking metrics, and the tuner is deterministic coordinate descent. The same
logs and vault always produce the same proposed config.
