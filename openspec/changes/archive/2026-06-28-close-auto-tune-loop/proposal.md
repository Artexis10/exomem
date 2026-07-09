## Why

exomem ships the pieces of a self-tuning retrieval ranker — a golden eval set
(`tests/golden/queries.yaml`), an offline NDCG harness (`scripts/eval_retrieval.py`),
a coordinate-descent tuner (`scripts/auto_tune_ranking.py`) over `RankingConfig`,
and a usage miner (`scripts/derive_relevance_pairs.py`) that turns ordinary
"search-then-compile" usage into weak `(query → cited_path)` relevance labels. On
paper that's an uncopyable edge: relevance signal mined for free from a single
user's real work, plus an eval harness to act on it. In practice **the loop is
open — it never feeds back.** Three concrete breaks:

1. **The mined pairs never reach the tuner's objective.** `auto_tune_ranking.py`
   loads `logs/relevance_pairs.jsonl` and prints its row count, but its objective
   (`build_evaluate_fn`) closes over the 9 hand-authored golden queries **only**.
   Even when pairs exist, they do not move tuning. This is the central bug.
2. **Mining is manual and non-idempotent.** `derive_relevance_pairs.py` works
   (~21 pairs on the live vault) but is a hand-run script that **appends**, piling
   cross-run duplicates — unsafe to run on any schedule.
3. **A tuned config can't take effect without editing code.** `find()` reads
   `RankingConfig` only as an in-process param (default `DEFAULT_RANKING`); there
   is no disk-load seam. The tuner deliberately *prints* the winner and instructs
   the operator to hand-edit `find.py`.

This change closes the loop and makes it **safe**: mined usage actually moves the
tuner; a tuned config is adopted as a reviewed, reversible file (no code edit); and
a trusted golden floor guarantees adoption can never regress retrieval.

A `MIN_PAIRS` guard keeps the mined signal OFF until there is enough of it, so the
loop is safe when usage is thin. A real-vault smoke (2026-06-28) confirmed the
mechanism end to end AND showed the guard is **already cleared on the live vault**:
the current usage logs mine to 10 distinct eligible queries (≥ `MIN_PAIRS=8`), so the
pairs term engages today — the loop tunes from real signal now, not just in the
future. The deliverable is the closed, guarded mechanism (and it is already active).

## What Changes

- **Wire mined pairs into the tuning objective (binary relevance).** A mined pair
  is a binary fact — "for query Q the user cited D, so D is relevant" — graded `1`,
  never graded by its mined `confidence` (which embeds the *incumbent* rank and
  would bake the old ranking into the target). `confidence` is used only as a
  filter (`CONF_MIN`). The objective becomes a lexicographic tuple
  `(pair_mrr, golden_ndcg)` under a **hard golden floor**: any candidate whose
  golden NDCG@10 falls more than `EPSILON` below the `DEFAULT_RANKING` baseline is
  infeasible. A `MIN_PAIRS` guard drops the pairs term entirely (reducing to
  today's golden-only tune) until enough distinct, golden-deduped pairs exist.
- **Idempotent mining snapshot.** `derive_relevance_pairs.py` rewrites a
  deduped snapshot atomically (tmp + `os.replace`) instead of appending, so
  re-running over the same logs is reproducible and safe to schedule. The tuner
  mines fresh first so the objective always reflects current usage.
- **Adopted-config load seam in `find()`.** When `config` is omitted (the live
  server's path), `find()` loads an adopted `RankingConfig` from disk if present
  (resolution: `KB_MCP_DISABLE_RANKING_CONFIG` → `KB_MCP_RANKING_CONFIG` path →
  repo-root `ranking_config.json` → `DEFAULT_RANKING`). A malformed/wrong-type/
  bad-length file **fails loud and falls back to `DEFAULT_RANKING`** — never
  crashes the server, never silently applies a half-parsed config. With no file
  present, behavior is byte-identical to today. Eval harnesses pass `config=`
  explicitly and stay hermetic.
- **Reviewed, reversible adoption.** Tuning only writes a candidate
  (`logs/ranking_config.candidate.json`) + a human-readable delta report; it never
  auto-applies. Adoption is an explicit `--adopt` flip that promotes the candidate
  to the committed repo-root `ranking_config.json`, gated by the same golden floor
  (refuses a golden-regressing candidate without `--force`). Reversal is deleting
  or `git revert`-ing the file.

Out of scope (separate changes): broadening the relevance signal (clickthrough /
negatives / an explicit `found_via` threaded through the skill); tuning the
image-tag (`0.22`/`K=5`) and contradiction (`W_DORMANCY`/`TOP_N`) thresholds against
the live vault; anything touching diarization.

## Capabilities

### New Capabilities
- `ranking-autotune`: a closed, guarded self-tuning loop for the hybrid ranker —
  mined note-citation pairs enter the tuner objective as binary labels behind a
  `MIN_PAIRS` guard and a hard golden floor; mining is an idempotent snapshot;
  tuning writes a reviewed candidate + report; `find()` loads an adopted, reversible
  `RankingConfig` from disk (absent → `DEFAULT_RANKING`, byte-identical to today).

## Impact

- Code: `src/kb_mcp/find.py` (config (de)serialization + a memoized disk-load seam,
  one-line change at the `config or DEFAULT_RANKING` call site);
  `scripts/derive_relevance_pairs.py` (reusable `mine_pairs()` + atomic snapshot);
  `scripts/auto_tune_ranking.py` (combined lexicographic objective, candidate/report
  writers, `--adopt` with the golden-floor gate, mine-fresh); a small
  `rank_queries()` helper reused from `scripts/eval_retrieval.py`.
- Tests: `tests/test_ranking_config_load.py` (the load seam), extended
  `tests/test_ranking_config.py` (serialize roundtrip + reversibility), extended
  `tests/test_auto_tune.py` (pairs→eval, binary pair-MRR, floor, `MIN_PAIRS` guard,
  candidate/report shape), `tests/test_derive_relevance_pairs.py` (snapshot
  idempotency). `tests/conftest.py` gains `KB_MCP_DISABLE_RANKING_CONFIG=1` so a
  committed `ranking_config.json` never pollutes the suite. All torch-free; the real
  combined tune stays manual/desk-side (needs torch + the live vault), as today.
- Behavior: **default-off by absence** — with no `ranking_config.json` present,
  ranking is byte-identical to today. The server-code change is behavior-neutral
  until a config is adopted, so deploying is safe pre-adoption (restart-to-reload,
  same as `.env`).
- Env knobs: `KB_MCP_RANKING_CONFIG` (override the adopted-file path),
  `KB_MCP_DISABLE_RANKING_CONFIG` (force `DEFAULT_RANKING`; set in the test suite).
- Pure-substrate: no server-side reasoning LLM anywhere; relevance labels derive
  only from recorded usage (Hugo's real citations); the evaluator is deterministic
  metrics and the tuner is deterministic coordinate descent.
