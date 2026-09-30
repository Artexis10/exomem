## Context

A hosted cell is one Python process per tenant: MCP server, file watcher, derived-index drains, and an ONNX embedding model (bge-base, fp32). The vault is Markdown on an encrypted volume, with derived SQLite sidecars under the state directory. Measured on the owner's vault (about 9,300 files, 3.6 GB):

- 1.2–1.7 GiB RSS at idle;
- a first-build peak above 1.2 GiB, which was OOM-killed at 1536Mi.

The code analysis this change builds on is the knowledge-base note "Where an Exomem Cloud cell's RAM goes, and how to stop it scaling with vault size". It is inferred from code; only the model's size is measured.

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
- RSS minus tracemalloc minus the ONNX native arena is reported as unreturned allocator memory.
- It runs against a synthetic large vault in CI (sized from the 2k-synthetic latency lane, scaled up) and on demand against a real vault copy.
- Report fields are content-free: numbers and code locations only.

Alternative rejected: estimating from code. The analysis already did that, and its figures are unverified.

**D2. Allocator hygiene is runtime configuration plus two call sites.**
- The hosted image sets `MALLOC_ARENA_MAX=2`.
- `malloc_trim(0)` runs after a model reap releases anything, and after a derived-index drain batch completes.
- The call is a guarded ctypes lookup: a no-op off glibc, and it never raises.

Alternative: jemalloc via `LD_PRELOAD`. Kept as the fallback if D1 shows trimming is insufficient; it adds a native dependency.

**D3. Hosted cells use the vec0 backend.**
- `EXOMEM_VEC_BACKEND=sqlite-vec` becomes the hosted default. The existing requirement already guarantees vec0 never holds the matrix resident.
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

## Risks / Trade-offs

- **vec0 latency at about 73k chunks may exceed the in-memory scan.** Mitigation: the latency gate in D3, and the numpy backend stays available as an explicit override.
- **A byte-bounded page cache re-parses more.** Mitigation: size the budget from D1, keep the hot find cache in front, and watch recall latency.
- **Step 2 of D5 touches a correctness-critical path.** Mitigation: ship steps 1–4 and D6–D7 first, then step 2 behind its own gate.
- **`malloc_trim` costs some CPU.** It only runs after reaps and drains, never per request.

## Migration Plan

1. The harness lands first and records a baseline for a large synthetic vault and the owner vault copy.
2. The quick wins land together (D2, D3, D4, D6 and D5 step 1), each with a harness before/after in its PR.
3. D7, then D5 step 2.
4. Once the harness shows the first-build peak and idle floor with headroom, lower cellctl's default memory limit and request in the platform chart. That is its own rollout; it re-renders every cell.

Rollback: every runtime switch has an environment override, so each step can be reverted without a data migration.

## Open Questions

- The idle target in absolute terms, for example 600 MiB. It is set after D1's baseline.
- Whether the local personal runtime should also default to vec0, or keep numpy.
