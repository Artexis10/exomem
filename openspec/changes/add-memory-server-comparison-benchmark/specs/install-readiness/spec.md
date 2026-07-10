## ADDED Requirements

### Requirement: CLI Startup Benchmark

The repository SHALL provide `scripts/startup_benchmark.py` that measures one-shot CLI startup
cost: it parses `python -X importtime` output for importing the CLI entry module, reports the
top import-time offenders, and times both `exomem --help` and one model-free one-shot product
command end-to-end. The output MUST be a small aggregate table suitable for before/after
comparison of startup optimizations.

#### Scenario: Importtime offenders reported

- **WHEN** `scripts/startup_benchmark.py` is run
- **THEN** it reports total import time for the CLI entry module and a ranked list of the
  costliest imported packages

#### Scenario: One-shot wall times reported

- **WHEN** `scripts/startup_benchmark.py` is run
- **THEN** it reports wall-clock time for `exomem --help`
- **AND** wall-clock time for one model-free one-shot product command
