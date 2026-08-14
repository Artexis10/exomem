# MemoryBench harness audit

Companion to `supermemory-provider-audit.md`; same pinned checkout
(`../LOCKFILE.json`, commit `118209a7…`). What this programme relies on,
what it must guard against, and what it re-derives instead of consuming.

## Pipeline and artifacts (what we consume)

Six phases (`src/types/checkpoint.ts:9-16`):
`ingest → indexing → search → answer → evaluate → report`. Dedicated
subcommands make an INGEST+SEARCH-only run first-class: `ingest -p … -b … -r
<runId>` (runs ingest+indexing) then `search -r <runId>`
(`src/orchestrator/index.ts:315-341`). There is no `--stages`; `--from-phase`
selects a suffix and does NOT reset completed phases (CLI cannot re-answer
from existing search data; only the UI's copyCheckpoint can).

Artifacts: `data/runs/{runId}/checkpoint.json` (whole-run state, rewritten
in full on every update — atomic tmp+rename), `results/{questionId}.json`
(per-question search payload), `report.json`. `RUNS_DIR` is a RELATIVE path
(`src/orchestrator/checkpoint.ts:25`) — run everything from the checkout
root.

`results/{qid}.json` contains: questionId, question text, questionType,
**groundTruth** (so the file is never handed to a provider), containerTag,
timestamp, durationMs, and the provider's raw `results` array VERBATIM
(SearchResult = unknown; no truncation/normalisation). Sufficient for our
re-derived ANSWER+EVALUATE. It **omits**: the query actually sent, the
SearchOptions used (limit/threshold — this is how the vendor's limit:30
stays invisible), any normalised score/rank, retries/HTTP status.
`checkpoint.json` duplicates the full results inline (write amplification:
verbose results make runs quadratically slower). Our exporter reads
`results/*.json`, treats the inline copy as redundant, marks absent protocol
fields as `missing_fields`, and our guest provider emits its own protocol
trace (including the options it was called with) because the harness will
not.

Resume gates: search requires `indexing == completed` — a provider that
never completes indexing is silently never searched ("No questions pending
search", zero rows, no error). `run()` sets checkpoint `status: "completed"`
unconditionally at the end (`src/orchestrator/index.ts:311`) even for
partial-phase runs — judge completeness from per-phase status, never from
`status`.

## Provider contract and registration

`src/types/provider.ts`: `initialize/ingest/awaitIndexing/search/clear`,
`SearchOptions {containerTag, limit?, threshold?}`. `ProviderName` is a
CLOSED union — a guest requires patching three files
(`src/types/provider.ts`, `src/providers/index.ts`,
`src/utils/config.ts`); our registration patch is hash-verified at setup.
`getProviderConfig` returns only `{apiKey, baseUrl?}`; anything else comes
from `process.env` inside the provider. `clear()` is required and called by
nothing. containerTag = `${questionId}-${dataSourceRunId}`, one container
per question, recomputed identically in three places.

## LongMemEval implementation

- Dataset: HF `xiaowu0162/longmemeval-cleaned`, file
  `longmemeval_s_cleaned.json` (`src/benchmarks/longmemeval/index.ts:14-15`)
  — fetched from `main` with **no revision pin and no checksum**. Our runs
  pin sha256 out-of-band and record it in the manifest.
- **Session-id leakage is clean**: sessionIds are synthesised
  (`${question_id}-session-${i}`), messages rebuilt as {role, content},
  `has_answer` stripped per message (`:160-184`, `:213-238`). Raw
  `answer_`-prefixed ids never reach providers. Two residual channels: (1)
  `question_id` — including the `_abs` abstention suffix — is embedded in
  every containerTag and sessionId sent to providers (an available side
  channel; nothing in-tree exploits it); (2) groundTruth is written into
  `results/{qid}.json`. The `has_answer` strip is SKIPPED on a warm
  `questions/` cache (`:100-103`) — always split from a clean data dir.
- Question selection: `readdirSync` lexicographic order, so `-l N` = "first
  N in filesystem order", not dataset order; random sampling is unseeded
  AND uses the biased `sort(() => Math.random() - 0.5)` shuffle
  (`src/orchestrator/index.ts:56`). Our runs pass explicit
  `targetQuestionIds` (the committed 25-case subset), never `-l`/sampling.
- **`questionDate` is never plumbed**: only writer is `initQuestion`'s
  optional param, which the orchestrator never passes
  (`src/orchestrator/index.ts:241-245`); every answer prompt for every
  provider renders `Question Date: Not specified`. Published
  temporal-reasoning numbers from this harness measure a degraded task. Our
  re-derived answer stage re-attaches question_date from the dataset.
- **Abstention has no judge route on LongMemEval**: the judge routes on
  questionType substring "abstention"/"adversarial"
  (`src/prompts/defaults.ts:61-81`); LongMemEval's six registered types
  match neither, and `_abs` lives in the question_id. If the cleaned
  variant retains `_abs` rows, a correctly-abstaining system is scored
  INCORRECT by the type-specific prompt. VERIFY against the fetched
  dataset before citing any abstention figure from this harness.
- Per-session date parse failures silently drop the date (no counter).
  Ingest unit is one provider call per session; provider granularity
  differs wildly by design (zep: one episode per MESSAGE).

## Judges

Vendor inferred from the model alias with a final silent fallback to
OpenAI (`src/utils/models.ts:307-315`). Temperature 0 where supported; no
seed; free-text verdicts parsed by a greedy regex with a
substring-heuristic fallback that defaults to INCORRECT (parse failures
land in the denominator as wrong answers — the opposite of the
provider-failure convention). `label` and `score` are parsed independently
(inconsistent pairs possible; accuracy uses `score`, UI uses `label`).
`maxTokens` is passed under AI SDK v5's old name and is likely inert
(unbounded outputs; medium confidence, not SDK-verified). Retrieval-metric
judging feeds each raw hit as pretty-printed JSON with no token budget —
an oversized guest result object blows the context and scores all-zero
relevance with no error: guest providers return flat `{content, score}`
objects.

## Operational hazards for a guest provider

No retries, no timeouts, no AbortController anywhere; the first error in a
batch aborts the phase and run (`src/orchestrator/concurrent.ts:86-89`);
failed runs still exit 0 (`src/index.ts` catches to console). Therefore our
guest provider carries its own bounded retry/backoff and request timeouts,
implements `awaitIndexing` with real search-readiness plus attempt and
wall-clock caps and explicit 404/5xx handling, declares honest concurrency,
and implements `clear()` properly. Any published comparison pins
`--concurrency` identically across providers. Bun-only runtime, no engines
pin upstream (we pin 1.3.14 in the LOCKFILE); `.env.example` referenced by
the README does not exist; Bun's implicit .env autoload is the only env
loading.

## The /memorybench skill: not used

The bundled adapter-generating skill seeds guests with the vendor's own
concurrency numbers, shows `limit: 30` hardcoded in its "real
implementation" example, defaults `awaitIndexing` to instant-readiness
(Option A live, correct polling commented out), and returns `[]` from its
search template — each a self-inflicted deficit for a guest. Our provider is
hand-written against `src/types/provider.ts` instead. The repo's
`framework.md` is out of date (omits the INDEXING phase; claims ROUGE and
real Recall@K; claims LongMemEval abstention support) — do not cite it.

## Upstream-report candidates (founder decision before public filing)

1. Vendor provider ignores shared `limit` and takes chunks (headline).
2. `questionDate` never plumbed (hurts every provider's temporal rows).
3. LongMemEval `_abs` abstention unroutable to the abstention judge.
4. Failed-question exclusion invisible in report totals.
5. awaitIndexing infinite loop on rejected polls; `--force` re-ingest
   contamination with no-op `clear()`.
