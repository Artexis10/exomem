<!-- authority:non-specification -->

# Native write-maintain-recall diagnostic

This LongMemEval row exercises Exomem through a tool-calling agent using its
shipped skill. Each historical session gets a fresh agent context; the agent
decides what to capture, compile, connect and correct in a persistent isolated
memory cell. A fresh answering agent searches and reads that memory. The pinned
official LongMemEval judge scores its final answer.

The scheduling is **session-end maintenance**, not a continuously participating
assistant. Each fresh agent receives the shipped skill and the documented
Maximal custom-instructions block from `docs/prominence.md`, both frozen before
execution. The current assignment asks it to perform memory maintenance;
archived conversation turns remain evidence. Client hooks do not run. The agent
may leave a conversation unwritten when nothing qualifies under its live policy.
This is an Exomem-only diagnostic, not a competitive ranking. Original
source-only scores remain separate and unchanged. Offline scripted tests prove
the wiring; they do not establish model accuracy or improved benchmark scores.

Prepare without API calls using the pinned project-local uv writer. The product
interpreter must already have the CPU embedding dependencies. `MODEL_CACHE`
names a local Hugging Face `models--BAAI--bge-base-en-v1.5` directory;
`CLIP_MODEL_CACHE` names `models--sentence-transformers--clip-ViT-B-32`.
Preparation copies their selected snapshots; execution remains offline for
embedding assets. The canonical retrieval profile keeps both model lanes enabled.

```sh
.uvbin/uv run --no-sync python -m benchmarks.lme.native_pilot prepare \
  --dataset "$LONGMEMEVAL_DATASET" --judge-home "$LONGMEMEVAL_HOME" \
  --product-root "$PRODUCT_ROOT" --python "$PRODUCT_PYTHON" \
  --model-cache "$MODEL_CACHE" --clip-model-cache "$CLIP_MODEL_CACHE" \
  --out "$NATIVE_RUN" \
  --size 1 --transport openrouter --budget-cap-usd 2
```

Use the exact approved spending cap for the intended run. Preparation freezes
source, shipped guidance, model assets, interpreter/distribution metadata,
implementation hashes, selection and limits. It prints the digest, session
count and minimum number of model calls. That count excludes additional tool
rounds; actual cost depends on the agent's work. Sizes 1, 7 and 25 are supported.
Selection uses a fixed seed and excludes the previously inspected 25-case pilot.
It is not stratified; inspect the frozen selection before interpreting coverage.

Inject the dedicated benchmark credential through the environment or a secret
manager, then execute the prepared plan:

```sh
.uvbin/uv run --no-sync python -m benchmarks.lme.native_pilot run \
  --out "$NATIVE_RUN" --expected-plan-sha256 "$NATIVE_PLAN_SHA256" \
  --metered-approval "recorded approval for this diagnostic and its cap" \
  --api-key-env EXOMEM_BENCHMARK_OPENROUTER_API_KEY
```

The default writer/answer model remains the dated GPT-4o model used by the
[scored replay](README-scored-pilot.md). To prepare a distinct native-agent
configuration, add `--agent-model gpt-5.6-sol --transport openrouter`.
This selects Sol with low reasoning for writing and answering; the official
judge remains `gpt-4o-2024-08-06`. Preparation records both roles and their
rates. All requests share one immutable ledger and spending cap.

The Sol profile preserves returned reasoning blocks across tool calls and uses
only parameters advertised by OpenRouter's standard OpenAI endpoint. Its frozen
promotional rates, verified on 2026-09-08, are $2/M input, $0.20/M cached input,
$2.50/M cache writes and $10/M output. Routing rejects higher input/output
prices. Reservations cover the more expensive cache-write rate. The diagnostic
keeps a 128k accounting envelope. Context preflight uses `o200k_base` with chat
framing, pinned to OpenAI's tokenizer mapping at commit
`212b893ba940cba53476851103d2e5c1d0020c6e`; the explicit encoding avoids older
library versions' incomplete GPT-5 point-release name lookup.
The 4096-token output limit includes reasoning. A model change requires a new
preparation and must be disclosed when comparing results.

Tool arguments and results remain real public MCP payloads. Function definitions
set `strict: false` to preserve optional-field omission across provider APIs;
public MCP validation still enforces the supplied arguments. The answer phase has recall-only access; writers cannot
see future questions, gold answers or answer-session labels. Every question
gets a separate vault, configuration, state root, lease, logs and model cache.
No personal Exomem service is used. Text-only ingestion excludes remote files,
transfer tools and arbitrary filesystem access. Guidance reads are limited to
the frozen shipped skill files. These restrictions are part of this row.

Default per-phase limits are 24 model calls, 64 tool calls and 600 seconds.
The run shares limits of 256 model calls, 768 tool calls, 8 million tokens and
7,200 seconds, plus the approved currency cap. Every call reserves tokens and
currency before transmission. Additional limits bound context, output, IPC,
tool results and stored memory. Large cohorts can exhaust this diagnostic
envelope; incomplete runs never receive a cohort accuracy. There is no automatic
retry, truncation, cap increase or resume of a stopped execution.

Inspect `execution/summary.json` and each case's `row.json`. They preserve
phase inputs, complete model/tool events, committed write receipts, memory
snapshot hashes, worker outcomes, semantic readiness and observed fallback.
No-write and refusal outcomes remain visible. `ledger.jsonl` separates actual
charges from held reservations; uncertainty writes `STOP`. Prepared and execution
artifacts are private and may contain benchmark source text and evaluator data.

The offline regression suite includes a real MCP compiled write, a later
superseding correction, and fresh-agent recall of the updated compiled note.
It also covers blinded inputs, unchanged source text, phase isolation, denied
routes, token/call budgets, cancellation, model-cache confinement and metering.
Use those tests during development. Run the next paid diagnostic after a material
reviewed correction, preserving previous run artifacts and reporting the actual
agent operations alongside accuracy and cost.
