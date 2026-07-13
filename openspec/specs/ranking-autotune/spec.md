# ranking-autotune Specification

## Purpose
Close the loop from real usage to better ranking without ever risking a silent
regression: the tuner mines `(query → cited_path)` pairs from usage logs as
binary relevance labels (guarded off until enough real pairs exist), scores
candidate configs against a combined pairs-plus-golden objective, and treats
the golden-set NDCG floor as a hard constraint on both selection and adoption.
The loop is deterministic and reasoning-model-free throughout, tuning only
ever writes a reviewable candidate config and report, and adoption is an
explicit, reversible step that never happens automatically.
## Requirements
### Requirement: Mined Pairs Enter the Tuning Objective as Binary Labels

The tuner SHALL incorporate mined `(query → cited_path)` relevance pairs into its
optimization objective as binary-relevance labels (a cited path is relevant with
grade 1), using each pair's mined `confidence` only as an inclusion filter and never
as the relevance grade, so that the loop tunes from real usage without baking the
incumbent ranking into the target. Pairs whose query matches a golden query SHALL be
deduplicated out before scoring, and the pairs term SHALL be measured by mean
reciprocal rank over the eligible pair queries.

#### Scenario: A cited path that ranks low is rewarded when pulled up

- **WHEN** a mined pair `(Q → D)` is eligible and a candidate config ranks `D`
  higher for `Q` than the incumbent does
- **THEN** that candidate's pairs term (pair-MRR) is higher than the incumbent's
- **AND** the reward does not depend on the pair's mined `confidence` value, only on
  `D`'s rank under the candidate

#### Scenario: A pair already covered by golden is not double-counted

- **WHEN** a mined pair's query equals a golden query (after canonicalization)
- **THEN** that pair is excluded from the eligible pair set used by the pairs term

### Requirement: MIN_PAIRS Guard Keeps Mined Signal Off Until It Is Real

The tuner SHALL drop the pairs term entirely and optimize on the golden set alone
whenever the number of distinct eligible pair queries is below `MIN_PAIRS` (default
8), so that a handful of weak labels can never drive tuning, and the loop reduces
exactly to today's golden-only behavior until usage compounds.

#### Scenario: Too few pairs reduces to golden-only tuning

- **WHEN** the eligible pair set has fewer than `MIN_PAIRS` distinct queries
- **THEN** the objective ignores the pairs term and ranks feasible candidates by
  golden NDCG@10 alone
- **AND** the run reports that the pairs guard is active

### Requirement: Golden Floor Is a Hard Constraint on Tuning and Adoption

The tuner SHALL treat any candidate whose golden NDCG@10 falls more than `EPSILON`
(default 0.01) below the `DEFAULT_RANKING` baseline as infeasible and never select
it, and adoption SHALL reuse the same floor — refusing to promote a candidate whose
recorded `candidate_golden` is more than `EPSILON` below its recorded
`baseline_golden` unless explicitly forced — so that closing the loop can never
silently regress retrieval below the trusted baseline.

#### Scenario: A golden-regressing candidate is never selected

- **WHEN** a candidate improves the pairs term but lowers golden NDCG@10 by more than
  `EPSILON` below baseline
- **THEN** the optimizer does not select it over the feasible incumbent

#### Scenario: Adoption refuses a regressing candidate

- **WHEN** `adopt` is invoked on a candidate whose `candidate_golden` is more than
  `EPSILON` below its `baseline_golden`
- **THEN** adoption is refused and the adopted `ranking_config.json` is not written
- **AND** an explicit force flag is required to override

### Requirement: Mining Produces an Idempotent Deduplicated Snapshot

The miner SHALL rewrite `logs/relevance_pairs.jsonl` as a deduplicated snapshot
written atomically (a temporary sibling file replaced into place), rather than
appending, so that re-running mining over the same source logs yields a
byte-identical, duplicate-free file and is safe to run on a schedule.

#### Scenario: Re-running mining is reproducible

- **WHEN** mining runs twice over the same `queries.jsonl` and `writes.jsonl`
- **THEN** the resulting `relevance_pairs.jsonl` is byte-identical across the two runs
- **AND** it contains no duplicate `(query, cited_path)` rows
- **AND** no temporary `.tmp` file is left behind

### Requirement: find Loads an Adopted RankingConfig From Disk

When its `config` argument is omitted, `find` SHALL resolve the active
`RankingConfig` from disk — honoring `EXOMEM_DISABLE_RANKING_CONFIG` (force default),
then `EXOMEM_RANKING_CONFIG` (explicit path), then a repo-root `ranking_config.json`,
and otherwise `DEFAULT_RANKING` — and SHALL parse the file field-by-field, ignoring
unknown keys and defaulting missing keys. A file that is malformed, wrong-typed, or
has a bad lane-weight tuple length SHALL fail loud (logged at error) and fall back to
`DEFAULT_RANKING` rather than crash or apply a partial config. When `config` is
passed explicitly, `find` SHALL use it and MUST NOT consult disk.

#### Scenario: No adopted file reproduces default ranking exactly

- **WHEN** no adopted config file is resolvable and `find` is called without a
  `config` argument
- **THEN** results are byte-identical to calling `find` with `config=DEFAULT_RANKING`

#### Scenario: A valid adopted file changes ranking

- **WHEN** a valid non-default `ranking_config.json` is resolvable
- **THEN** `find` (called without `config`) ranks according to the adopted config
- **AND** removing the file and resetting the cache restores `DEFAULT_RANKING` behavior

#### Scenario: A malformed file fails loud and falls back

- **WHEN** the resolved config file is malformed JSON or has a bad lane-weight tuple
  length
- **THEN** `find` logs an error and uses `DEFAULT_RANKING`
- **AND** the server does not crash

#### Scenario: An explicit config never consults disk

- **WHEN** `find` is called with an explicit `config` argument and an adopted file is
  also present
- **THEN** the explicit `config` is used and the adopted file is ignored

### Requirement: Tuning Is Reviewed and Reversible, Never Auto-Applied

Tuning SHALL only write a candidate config (`logs/ranking_config.candidate.json`) and
a human-readable delta report; it MUST NOT mutate `find.py` or the adopted
`ranking_config.json`. Adoption SHALL be an explicit, separate step that promotes the
candidate to the committed repo-root `ranking_config.json`, and reversal SHALL be
deleting or reverting that file (returning ranking to `DEFAULT_RANKING`).

#### Scenario: A tuning run applies nothing

- **WHEN** the tuner runs and finds a better feasible config
- **THEN** it writes the candidate file and the report only
- **AND** neither `find.py` nor `ranking_config.json` is modified by the run

### Requirement: The Loop Is Pure-Substrate

The entire loop SHALL run without any server-side reasoning LLM. Relevance labels
SHALL derive only from recorded usage (queries and citing writes), the evaluator
SHALL compute deterministic ranking metrics, and the tuner SHALL be deterministic
coordinate descent — so the same inputs always produce the same proposed config.

#### Scenario: No model decides relevance or ranking

- **WHEN** the loop mines pairs, scores candidates, and proposes a config
- **THEN** no language model is invoked at any stage
- **AND** re-running over identical logs and vault produces the same proposed config

