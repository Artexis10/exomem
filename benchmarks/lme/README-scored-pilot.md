<!-- authority:non-specification -->

# Scored diagnostic replay

Check real answering, official LongMemEval grading and measured API spending
before a larger run. This command reuses a completed MemoryBench guest export
without ingesting again or changing its original checkpoint.

LongMemEval supplies the dataset and official judge. This repository supplies
the retrieval adapter, context packing and common answer reader: an external
test still measures the system configuration submitted to it. The upstream
[Testing Your System](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc#testing-your-system)
instructions accept a system's own hypotheses for official grading.

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

To use OpenRouter, prepare a fresh run with `--transport openrouter` and inject
`OPENROUTER_API_KEY` at execution. The route is bound to the prepared plan;
execution cannot change it. It requests `openai/gpt-4o-2024-08-06` from OpenAI
through OpenRouter, with provider fallbacks and prompt transforms disabled.
Maximum provider prices match the reservation. OpenRouter's reported usage
cost settles the ledger; token-derived pricing remains an estimate. Model or
provider drift, missing charge evidence and external BYOK charges stop the run.
An upstream cost breakdown on an explicitly non-BYOK chat response is
informational: it is not added to the reported account charge.
Credit-purchase fees and taxes are outside reported inference charges. Preserve
any held reservation from an earlier failed run when budgeting another attempt.
See [OpenRouter usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
and [provider selection](https://openrouter.ai/docs/guides/routing/provider-selection).

The common `ApiReader` uses the fixed `gpt-4o-2024-08-06` snapshot and a 512-token
answer limit. The judge executes the unchanged pinned upstream script using
validated input byte snapshots and the same selected, capped transport. Upstream
prompts, temperature, the 10-token judge output limit and verdict rules remain
unchanged. Each question receives a retrieved-context answer, a gold-evidence
control and an empty-context control. Files and directories are private on POSIX.

The common reader identifies retrieved histories as earlier conversations with
the user, treats archived turns as evidence, and places the current date,
question and answer cue after the complete supplied context. These choices
follow the framing and ordering in the pinned
[reference reader](https://github.com/xiaowu0162/LongMemEval/blob/9e0b455f4ef0e2ab8f2e582289761153549043fc/src/generation/run_generation.py#L46),
without reproducing all its generation options. The same prompt applies to all
three controls. Source text and order remain intact; evaluation-only gold labels
are excluded. Preparation binds the reader source digest, so changes require a
fresh plan and estimate. Completed runs retain their original prompts and scores.

Use saved artifacts and offline tests while developing. Run another paid
diagnostic after a material correction or to resolve a specific measurement,
with configuration and selection frozen before scoring. A single-question
probe or passing prompt tests does not establish improved answer accuracy;
validate generalization on a fresh cohort before a full scored run.

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

The write-and-recall loop needs a separate native-lifecycle evaluation under
[the programme's contract](../../openspec/changes/add-competitive-benchmark-programme/specs/native-lifecycle-bench/spec.md).
Replay timestamped history to a writing agent using the shipped product skill
and documented interfaces, then let a fresh answering agent recall the resulting
memory. The writer must not see future questions, gold answers or answer-session
labels. Record its writes, maintenance operations and cost, including work that
creates compiled notes and relations. Keep this row separate from the raw-history
baseline. This replay command does not run that loop, and a reader-prompt
correction alone does not make it a native product evaluation.
The source adapter sends `compile_guidance=false`; setting it to `true` would
only return a compilation proposal. An agent must still act through the
governed writing interfaces to create or maintain compiled knowledge.
