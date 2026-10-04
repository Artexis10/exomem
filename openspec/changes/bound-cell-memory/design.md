## Context

A cell is one Python process per tenant: MCP server, file watcher, derived-index drains, and an ONNX embedding model. At proposal time the hosted runtime used bge-base fp32; Cloud cells now use bge-m3 int8 through the separate ONNX change. The vault is Markdown on an encrypted volume, with derived SQLite sidecars under the state directory. Historical measurements on the owner's vault (about 9,300 files, 3.6 GB), before these optimisations:

- 1.2–1.7 GiB RSS at idle;
- a first-build peak above 1.2 GiB, which was OOM-killed at 1536Mi.

The code analysis this change builds on is the knowledge-base note "Where an Exomem Cloud cell's RAM goes, and how to stop it scaling with vault size". Its original ranking was inferred from code, with only the model measured. The harness now supplies per-phase measurements; the native owner-copy baseline remains a separate open gate.

**Constraint: tenant isolation.** No process may hold one tenant's plaintext for another. Sharing read-only model weights is allowed but out of scope here.

## Goals / Non-Goals

**Goals:**

- A large vault's cell idles well below 1 GiB.
- The first index build peaks below the 3Gi limit with headroom.
- Resident memory stops growing linearly with vault Markdown bytes.
- Every step is proven by measurement.

**Non-Goals:**

- Model size: int8 is a separate decision.
- Node density through shared weights: that is ONNX task 5.4.
- Canonical storage format.
- The local personal runtime's defaults, beyond what shared code changes imply.

## Decisions

**D1. Measure before and after every step with one harness.**
- The harness drives a cell process through five fixed points: import, model load, first hybrid find, first governed write, and a reaper tick.
- At each point it records tracemalloc (top allocation sites and total), `smaps_rollup` (Rss, Pss, Private_Dirty, Anonymous) and the product's cache-status counters.
- Report estimated unattributed anonymous memory alongside traced allocations and a model-load estimate. This is an attribution heuristic, not a measurement of allocator-free memory. RSS and peaks include tracing overhead; native/container sizing requires its own measurement.
- It runs against a synthetic large vault in CI (sized from the 2k-synthetic latency lane, scaled up) and on demand against a real vault copy.
- Report fields are content-free: numbers and code locations only.

Alternative rejected: estimating from code. The analysis already did that, and its figures are unverified.

**D2. Allocator hygiene is runtime configuration plus two call sites.**
- The hosted image sets `MALLOC_ARENA_MAX=2`.
- `malloc_trim(0)` runs after a model reap releases anything, and after a derived-index drain batch completes.
- The call is a guarded ctypes lookup: a no-op off glibc, and it never raises.

Alternative: jemalloc via `LD_PRELOAD`. Kept as the fallback if D1 shows trimming is insufficient; it adds a native dependency.

**D3. Hosted cells use the vec0 backend.**
- `EXOMEM_VEC_BACKEND=sqlite-vec` becomes the Cloud-cell default only after the normal governed recall and write paths prove the matrix stays cold. The 2026-09-30 owner-copy measurement found that the setting avoids the startup matrix but the first governed search loads it; permitted-path filtering and published-generation reuse still reach the numpy matrix. The setting alone does not meet this gate.
- Gates: the golden retrieval floors, backend parity, and the latency lane at the owner-vault chunk count.
- The image must be verified to load the extension. If it cannot, the cell refuses at start with a stable code rather than silently falling back to numpy.

**D4. The page cache is bounded by bytes.**
- `FrontmatterCache` tracks each entry's body and frontmatter size and evicts least-recently-used entries past a byte budget, as well as past the entry count.
- The budget has an environment override. The hosted default is set from D1's measurement.

**D5. Corpus projections are released, then replaced.**
- Step 1: the semantic corpus context registers as a reapable idle cache slot, released when the model reaper releases RAM caches.
- Step 2: page states and inbound and outbound link maps are served from the lexical store's page and semantic-unit tables. The Python context becomes a bounded, per-request view.
- Step 2 is the largest item and is gated separately: correctness of governed writes and link resolution comes first, by the existing semantic-contract suites.
- Step 1 waits for step 2. While the Python context is still what writers read, reclaiming it moves its cold whole-vault build onto the next governed write after every idle window, and a concurrent writer that waits past the two-second join fails closed.

**D6. No whole-corpus fallbacks on a transient.**
- `bm25.warm` declines when the FTS5 sidecar exists but is busy or unsynced. The next pass retries through FTS5.
- The entity-registry cache holds the current and previous checkpoints only (2), not 16.

**D7. Builds stream.**
- The matrix load preallocates from the row count and fills from the cursor.
- A bulk drain drops the matrix cache and reloads once, rather than splicing per file.
- `rebuild_all` embeds in batches.
- The identity census streams page text.

### D7 implementation boundaries

Matrix reads take the serving token, stored vector-space identity, row count and ordered cursor from one explicit SQLite read transaction. Allocate one float32 matrix at the stored width, copy each row directly into it, and validate every blob and the final count before publishing the cache. Keep the historical empty result and zero-argument load seam. This reduces transient copies; it does not remove the resident matrix.

Bulk embedding publication commits each bounded group of prepared parent replacements in one transaction, including its chunk rows, supplied semantic-unit rows, vec0 mirror, path log and generation advancement. It performs no per-file matrix splice. After commit, invalidate older matrix and eligibility-mask caches without discarding a cache already loaded at the committed token or a later one. Never hold the Python cache lock across SQLite writes. In-memory serving identity and cache invalidation are fenced together by the full observed token, including semantic-unit generation: a delayed older completion cannot relabel newer rows, even when the matrix is cold. One lazy reload belongs to each committed batch; concurrent readers can legitimately observe different completed batches.

Prepared vectors carry the actual producing encoder's `SpaceIdentity`, validated against the stored identity inside publication even when both spaces have the same width. Rebuild staging fixes one producing identity for the run and checks every encoding batch against it. Refuse incompatible publication; do not infer a replacement identity from mutable process state at the end. Encoding and producer capture use one pinned encoder reference, including the previous-resident encoder path.

Full rebuilds stage bounded encoding batches in ordinary run-keyed tables inside the existing owner-authorized embedding sidecar. Capture the projected source/policy snapshot and the serving `(epoch, generation, instance, semantic_unit_generation)` token before staging. Staging commits do not alter serving rows, vector-space identity or tokens. The final SQLite write transaction compares the captured token and source/policy snapshot, replaces both serving row families from this run, updates vec0 and identity, advances all serving generations together, and rechecks source/policy before committing. An intervening serving write refuses publication instead of being overwritten. Encoding, SQL or source-validation failure leaves the previous serving generation intact. Cleanup names only this run; a crash leaves invisible identifiable staging rows, with no age-based deletion of other builders. Preserve existing missing-KB and empty-input no-op behaviour. External Markdown edits retain the existing optimistic validation contract; this change does not claim filesystem-wide atomicity.

Publication begins with explicit `BEGIN IMMEDIATE` before reading the comparison token. Admission and vec0 preparation inside it must be transaction-neutral: existing helpers that own `with conn` transactions cannot release the lock or commit identity, mirroring or serving rows prematurely. Failure after any identity or vec0 change rolls back the whole publication, including width redeclaration. A failed multi-batch replay retains its captured receipts even if an earlier batch committed.

The identity census yields one validated page and immediately consumes its raw text. Identity-only callers retain only stable identity entries. Corpus-context construction consumes each eligible KB page through the existing page-state builder, and reads outside-KB pages once. Keep alias refusal, enumeration failures, malformed-page handling and the one-read/parse-per-page invariant. Existing page states remain resident: this is not D5's replacement of corpus authority.

Verification covers snapshot width and generation consistency, immutable retained matrices, bounded batch cache invalidation races, unchanged per-path receipt retirement, vec0 parity, rebuild failure and concurrent-publication refusal for warm and cold readers, census safety and one-read/parse-per-page behaviour. The same tracing-inclusive owner-copy and synthetic harness fixtures provide before/after evidence; native/container sizing remains a separate gate.

## Risks / Trade-offs

- **vec0 latency at about 73k chunks may exceed the in-memory scan.** Mitigation: the latency gate in D3, and the numpy backend stays available as an explicit override.
- **A byte-bounded page cache re-parses more.** Mitigation: size the budget from D1, keep the hot find cache in front, and watch recall latency.
- **Step 2 of D5 touches a correctness-critical path.** Mitigation: ship steps 1–4 and D6–D7 first, then step 2 behind its own gate.
- **`malloc_trim` costs some CPU.** It only runs after reaps and drains, never per request.

## Migration Plan

1. The harness lands first and records a baseline for a large synthetic vault and the owner vault copy.
2. The quick wins land together (D2, D3, D4 and D6), each with a harness before/after in its PR.
3. D7, then D5 step 2.
4. Once the harness shows the first-build peak and idle floor with headroom, lower cellctl's default memory limit and request in the platform chart. That is its own rollout; it re-renders every cell.

Rollback: every runtime switch has an environment override, so each step can be reverted without a data migration.

## Open Questions

- The idle target in absolute terms, for example 600 MiB. It is set after D1's baseline.
- Whether the local personal runtime should also default to vec0, or keep numpy.
