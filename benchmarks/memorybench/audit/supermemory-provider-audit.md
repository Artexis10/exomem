# MemoryBench: Supermemory provider audit

Adversarial audit of the vendor's own provider inside `supermemoryai/memorybench`,
pinned at `118209a746d97d0d85e5a7234267f0b6962857e9` (see `../LOCKFILE.json`).
All paths are relative to that checkout. Purpose: before this programme
consumes any row from this harness, every vendor-side configuration choice is
enumerated with direction-of-favour. Findings feed the fairness matrix; none
were "fixed" by us (cross-competitor defects are filed upstream per the
fairness contract, pending founder approval for public filing).

## Headline finding: hardcoded vendor retrieval advantage

`src/providers/supermemory/index.ts:120-136` — the Supermemory provider
ignores the orchestrator's shared search budget and takes a triple one, plus
raw source chunks:

```ts
const response = await this.client.search.memories({
  q: query,
  containerTag: options.containerTag,
  limit: 30,                     // orchestrator passes limit: 10 to everyone
  threshold: options.threshold || 0.3,
  searchMode: "hybrid",
  include: { summaries: true, chunks: true }
})
```

The orchestrator calls every provider with `{limit: 10, threshold: 0.3}`
(`src/orchestrator/phases/search.ts:58-62`). Effective budgets: supermemory
**30**; mem0 10; zep 10 edges + 10 nodes; rag 10; filesystem 10. The
`include: {summaries, chunks}` flag means Supermemory alone receives raw
source transcript chunks in its results — it competes as memory + RAG over
raw text while mem0 competes as extracted memories only. Both changes landed
in one commit, `dad6d5d` ("supermemory hybrid mode change"), authored by a
Supermemory employee.

Compounding: retrieval metrics are computed over the top `k = 10`
(`src/orchestrator/phases/retrieval-eval.ts:93-95`) while the answer phase
receives all 30 (`src/orchestrator/phases/answer.ts:119`) — higher answer
accuracy, precision unpenalised for the extra 20. The SearchOptions actually
used are **not recorded in any checkpoint, report, or UI surface**, so the
asymmetry is invisible in the published artifacts.

Programme consequence: rows produced by this harness are labelled
`supermemory-hosted-memorybench` with this asymmetry on the fairness-matrix
row (`authored by: competitor; favours: supermemory`); symmetric-budget
comparisons live in our direct lane only. Our guest provider honours
`options.limit` exactly.

## Ingest and readiness

`ingest` (`src/providers/supermemory/index.ts:31-60`): one document per
session; content = formatted session date line + stringified JSON messages;
`containerTag` singular; `metadata: {sessionId, date}`. **No `customId`, no
`dreaming`, no `taskType` anywhere in the harness** (grep-verified) — so the
provider runs under default dynamic dreaming, and no extraction-mode flag is
disclosed on any published number. No `/v4/profile` use, no entityContext.

`awaitIndexing` (`:62-118`): polls `documents.get(docId)`; only on terminal
document status does it additionally poll `memories.get(docId)`, requiring
BOTH `done`. This is a genuine memories-readiness gate — Supermemory is the
only provider gated on a second downstream signal; a guest reporting
readiness at write-ack self-inflicts a deficit. Defects: a rejected poll
(404/5xx) is never handled — the id stays pending and the loop (backoff
capped 5s, no attempt/wall-clock cap) hangs forever; the ~2-minute
auto-delete on irrecoverable failures therefore hangs the run rather than
recording a failure.

`clear()` (`:138-141`) is a no-op log line (mem0/zep/rag/filesystem all
implement real deletion) — and `--force` deletes only the local checkpoint,
so a re-run on the same run id re-ingests into a still-populated container:
silent cross-run contamination for the vendor's own provider.

`initialize` (`:24-29`) drops `config.baseUrl` — `SUPERMEMORY_BASE_URL` is
dead config; the provider can only ever hit the hosted API.

Concurrency self-declaration (`:17-22`): `{default: 50, ingest: 100,
indexing: 200}` vs zep `{default: 10}` — search latency (the MemScore
latency component) is measured under provider-chosen load. Neutralisable via
the CLI `--concurrency` override; any published latency must pin it.

## Prompt asymmetries (ProviderPrompts)

Every in-tree provider overrides the answer prompt
(`src/orchestrator/phases/answer.ts:49-68`); the "shared default"
(`src/prompts/defaults.ts:3-27`) is dead code — there is no common answer
prompt in practice. Supermemory's (`src/providers/supermemory/prompts.ts`)
is ~5× the default and adds, uniquely:

1. **Chunk-priority instructions** ("This is your primary source…",
   "Prioritize information from chunks") — meaningful only because no other
   provider receives chunks.
2. **Chain-of-thought scaffolding** (Reasoning:/Answer: format) — absent
   from the default prompt entirely.
3. **Temporal coaching** on `documentDate`/`eventDate`/relative-date
   resolution — which, note, answers against a null anchor because the
   harness never plumbs `questionDate` (see harness audit §Dates).
4. **Client-side result post-processing**: `buildSupermemoryContext`
   dedupes chunks by exact content and re-sorts by `position`
   (`prompts.ts:27-81`) — harness-side retrieval improvement attributed to
   the provider; no other provider gets dedup or reordering.

Judge overrides: Supermemory does NOT override the judge (keeps per-type
prompts). **Zep does** (`src/providers/zep/prompts.ts:99-131`): one generic
rubric for every question type, containing "you should be generous with your
grading… as long as it touches on the same topic… CORRECT". Any table mixing
Zep with others grades different providers by different rubrics.

## Scoring facts that shape any consumed number

- **Failed questions are excluded from accuracy** — confirmed at
  `src/orchestrator/phases/report.ts:96-101` (skip on
  `evalPhase.status !== "completed"`) and `:228-230` (denominator =
  evaluated only). A run with 40/100 errored questions reports accuracy over
  60 and `summary.totalQuestions` hides the shortfall. Detection: diff
  `report.json summary.totalQuestions` vs `checkpoint.json` question count.
- **Judge parse failures count as WRONG** (`src/judges/base.ts:30-52`:
  greedy `/\{[\s\S]*\}/` regex; catch-branch defaults to incorrect) — the
  opposite convention from provider failures, in the same score.
- **MemScore latency = mean SEARCH latency only** (`report.ts:234`), under
  self-declared concurrency; ingest/indexing latency excluded. Percentiles
  are nearest-rank without interpolation (p95/p99 collapse to max at small
  n) plus a `|| sorted[n-1]` truthiness bug (latent).
- **Token accounting** is client-side estimation; Google runs use
  `char/4` (`src/utils/tokens.ts:14-21`) which the code itself notes
  undercounts JSON-heavy content — cross-model MemScore token comparisons
  are invalid.
- **Retrieval "recall"/NDCG are not real**: `totalRelevant = max(1,
  relevantRetrieved)` (`retrieval-eval.ts:120`) makes recall a duplicate of
  hit@k, and NDCG's ideal ranking is derived from what was retrieved. Do not
  cite them.

## Verification TODOs carried by this programme

- The `supermemory@4.0.0` SDK parameter surface was not installed/verified;
  the "not passed" list is definitive for what the harness sends only.
- Whether `longmemeval_s_cleaned.json` retains `_abs` abstention rows and
  what `question_type` they carry — verify at dataset fetch; if present,
  the harness has no abstention judge route for them (harness audit) and
  its published LongMemEval abstention behaviour is mis-scored.
