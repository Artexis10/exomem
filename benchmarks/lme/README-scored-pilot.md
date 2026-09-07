<!-- authority:non-specification -->

# Scored diagnostic replay

Check real answering, official LongMemEval grading and measured API spending
before a larger run. This command reuses a completed MemoryBench guest export
without ingesting again or changing its original checkpoint.

Install optional tooling with the project's pinned uv writer:

```sh
.uvbin/uv sync --frozen --group benchmark-judge
```

Prepare offline with a fresh output directory:

```sh
.uvbin/uv run --no-sync python -m benchmarks.lme.scored_pilot prepare \
  --export "$GUEST_RUN/memorybench-export.v1.json" \
  --run-plan "$GUEST_PLAN" --judge-home "$LONGMEMEVAL_HOME" \
  --out "$PILOT_RUN" --size 7 --budget-cap-usd 2
```

Preparation prints the plan SHA-256 and a token-based estimate. Seven questions
cover all six answerable types plus abstention, selected in source order before
answers. `--size 25` requires the entire existing 25-case source. The judge
checkout must match `benchmarks/suites/lme_v1/LOCKFILE.json`.

Configure `OPENAI_API_KEY` through the local environment or secret manager, then
execute the approved plan with the printed digest:

```sh
.uvbin/uv run --no-sync python -m benchmarks.lme.scored_pilot run \
  --out "$PILOT_RUN" --expected-plan-sha256 "$PILOT_PLAN_SHA256" \
  --metered-approval "recorded approval for this pilot and its stated cap"
```

`--api-key-env` can name another environment variable. Keys never belong in
plans, command arguments or source files. Missing credentials leave preparation
reusable. Execution creates an exclusive `execution/` directory; a failed or
completed execution is retained, not overwritten or silently resumed.

The common `ApiReader` uses the fixed `gpt-4o-2024-08-06` snapshot and a 512-token
answer limit. The judge executes the unchanged pinned upstream script using
validated input byte snapshots and the same capped OpenAI transport. Upstream
prompts, temperature, the 10-token judge output limit and verdict rules remain
unchanged. Each question receives a retrieved-context answer, a gold-evidence
control and an empty-context control. Files and directories are private on POSIX.

Inspect `execution/summary.json`, hypotheses, official labels, API usage artifacts
and `ledger.jsonl`. Each call reserves the model's full input-window price plus
maximum output before transmission. Known billing releases unused reservation;
uncertain billing retains it and writes `STOP`. Calls are never automatically
retried. Credential-bearing responses fail without propagating the credential.
Verify [official GPT-4o pricing](https://developers.openai.com/api/docs/models/gpt-4o)
before later metered runs; estimates exclude provider ingestion/search, native
compilation, retries, tax and local electricity.

Diagnostics are always non-publishable. Source-only Exomem does not test native
memory compilation or governance. The source identity preserves unrecorded
canary isolation, and replay does not assess same-product equivalence. Seven
questions check execution and spending; they cannot establish competitive
rankings. This command cannot run 500 questions, generate full-run approval
evidence or relax comparative publication gates. The older
`lme.cli run --reader openai` path does not yet enforce its displayed budget cap;
use this bounded command for these small paid diagnostics.
