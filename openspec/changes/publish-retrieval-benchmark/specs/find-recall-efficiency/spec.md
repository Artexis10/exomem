## ADDED Requirements

### Requirement: Reproducible Benchmark Report

The retrieval eval harness (`scripts/eval_retrieval.py`) SHALL provide a `--report markdown` mode
that runs the existing golden query set once per retrieval mode — keyword, hybrid, and hybrid with
reranking — and emits a single markdown artifact containing, per mode, the harness's existing
aggregate ranking-quality metrics (NDCG@5, NDCG@10, MRR, recall@10) and median/p90 `find()`
wall-clock latency measured over repeated runs of the golden set. The report generation MUST be
reproducible: re-running the command against any vault and golden set that follow the documented
harness contract (a `tests/golden/queries.yaml`-shaped golden set and a resolvable vault) MUST
produce a report in the same shape, including against the bundled `tests/fixtures` sample vault as
a deterministic smoke path.

#### Scenario: Report includes per-mode metrics and latency

- **WHEN** `scripts/eval_retrieval.py --report markdown` is run
- **THEN** the emitted markdown includes one row for each of keyword, hybrid, and hybrid-with-rerank
- **AND** each row includes NDCG@5, NDCG@10, MRR, and recall@10
- **AND** each row includes median and p90 `find()` latency measured over the run

#### Scenario: Report is reproducible against the bundled sample vault

- **WHEN** `scripts/eval_retrieval.py --report markdown` is run against the bundled
  `tests/fixtures` sample vault instead of a private vault
- **THEN** the harness produces a report in the same shape as against any other vault
- **AND** no private-vault content is required to produce a smoke-scale report

#### Scenario: Existing sweep and baseline markdown modes are unchanged

- **WHEN** `scripts/eval_retrieval.py` is run with `--sweep` or the existing baseline `--markdown`
  flag without `--report markdown`
- **THEN** the output matches the harness's existing behavior before this requirement existed

### Requirement: Aggregate-Only Publication

The markdown report produced by `--report markdown` SHALL contain only aggregate values: per-mode
mean metrics, per-mode latency percentiles, and rounded corpus counts (files, notes, media). It
MUST NOT contain per-query rows, golden query text, vault-relative paths, excerpts, or any other
content that could reveal what a private vault contains. The report-rendering logic MUST accept
only plain aggregate data as input (no vault path, no query text argument) so this constraint is
structural rather than a formatting convention.

#### Scenario: No golden query text appears in the report

- **WHEN** `--report markdown` is generated from the golden set in `tests/golden/queries.yaml`
- **THEN** none of the golden set's query strings appear anywhere in the emitted markdown

#### Scenario: No vault-relative path appears in the report

- **WHEN** `--report markdown` is generated from the golden set in `tests/golden/queries.yaml`
- **THEN** none of the golden set's `expect_any_of` or `graded` target paths appear anywhere in the
  emitted markdown

#### Scenario: Corpus stats are rounded counts only

- **WHEN** the report includes corpus statistics
- **THEN** the statistics are rounded integer counts of files, notes, and media
- **AND** no exact file name, path, or content excerpt is included

#### Scenario: Report has one row per mode, not one row per query

- **WHEN** the report is rendered for N modes over a golden set of any size
- **THEN** the report contains exactly N result rows, one per mode
- **AND** the row count does not scale with the number of golden queries
