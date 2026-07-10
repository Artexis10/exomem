## ADDED Requirements

### Requirement: Like-for-like Memory-Server Comparison Harness

The repository SHALL provide a comparison harness (`scripts/compare_memory_servers.py`) that
drives exomem and basic-memory as **persistent MCP servers** over the same generated markdown
corpus and the same fixed query set, measuring per contender: first-index/cold-start wall
time, warm search-call latency distribution (median and p90), resident set size after indexing
and after the query pass, and write-tool latency. The emitted report MUST be aggregate-only
(no query text, no corpus paths) and MUST document the fairness contract, including embedding
dimensionality deltas and pinned contender versions.

#### Scenario: Both contenders measured as persistent servers

- **WHEN** `scripts/compare_memory_servers.py` is run against a generated corpus tier
- **THEN** exomem is measured through a persistent stdio MCP server process
- **AND** basic-memory is measured through a persistent stdio MCP server process pinned to a
  published release
- **AND** neither contender is measured through one-shot CLI invocations

#### Scenario: Shared corpus is never mutated

- **WHEN** the harness runs the basic-memory contender
- **THEN** basic-memory runs with its safe-read configuration in an isolated config/home dir
- **AND** the shared corpus files are byte-identical after the run

#### Scenario: Report is aggregate-only

- **WHEN** the harness emits its markdown report
- **THEN** the report contains latency percentiles, RSS values, first-index times, and corpus
  scale counts
- **AND** it contains no query text and no corpus-relative paths

### Requirement: Memory-Usage Benchmark Lane

The per-lane latency harness (`scripts/latency_curve.py`) SHALL support an `--rss` option that
records the harness process resident set size after lane warm-up and after the timed query
pass, per corpus tier and per backend selection, rendered as additional report columns. RSS
measurement MUST degrade gracefully (rendering an em-dash) when psutil is not installed, and
psutil MUST NOT become a required core dependency.

#### Scenario: RSS columns appear with --rss

- **WHEN** `scripts/latency_curve.py --rss` runs with psutil available
- **THEN** the emitted table includes RSS after warm and RSS after the query pass for each
  tier/backend row

#### Scenario: Graceful degradation without psutil

- **WHEN** `scripts/latency_curve.py --rss` runs without psutil installed
- **THEN** the run completes and RSS columns render as “—”
