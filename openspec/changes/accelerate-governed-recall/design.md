## Context

See proposal.md for the measurements. The shape of today's read path that the
design has to respect:

- A recall's structured-filter plan is classified by `plan_index_candidates`.
  Only exact positive `unit.category` and `unit.kind` clauses are "complete"
  and seed from the semantic-unit sidecar; every page-level field (`projects`,
  `tags`, `types`, speakers, file types) is "unsupported" and falls to the
  canonical full-scan oracle, which walks the Markdown scope and parses
  frontmatter on the reader thread. That oracle is also the definition of
  correctness: an index-backed answer must equal it for the same generation.
- `scope="kb"` auto-widens through the out-of-KB reserve, which resolves
  eligibility again with vault scope and then runs BM25 over every non-KB page,
  building a Python corpus when the maintained FTS5 catalogue is not fresh.
- Substrate caches (lexical corpus, eligibility catalogue, frontmatter cache,
  embedding matrix) key on whole-scope freshness keys. Any governed write moves
  the key, so on a busy day every recall rebuilds what the previous write
  discarded. The `accelerate-durable-write-acknowledgement` change already
  gives each governed write an exact receipt naming its paths and a
  pending-visibility row per path; nothing on the read side consumes them for
  invalidation yet.
- Timing diagnostics merge registered intervals into `unattributed_ms`; the
  stages that #983 converted are intervals now, but nothing enforces that a new
  stage is, and stages do not say whether they were answered from an index.
- The recall projection admission and the lexical repair worker already
  implement the "warm away from the reader" pattern this change extends.
- The 2026-08-31 plan's tranche 1 (#951, matrix copy) is on main; #983 and #984
  are closed. Their combined effect on the live cell has not been measured.
- The live cell runs in WSL on a box shared with test suites; the graph
  scheduler's whole-vault rebuild can livelock under a sustained writer
  (`converge-graph-incrementally`).

## Goals / Non-Goals

**Goals:**
- Zero corpus walks on the reader thread for any supported request shape,
  enforced by diagnostics and a test sentinel, not by review.
- Read-side caches that survive governed writes through exact receipt custody.
- A measured, ratcheting latency contract on the live cell.
- Result identity: every index-backed path returns the set the scan oracle would.

**Non-Goals:**
- Approximate retrieval, dropped features, ANN, or any quality-for-speed trade.
- Changing what hybrid recall computes (embed, vector, graph, fusion, rerank).
- Fixing the graph rebuild's optimistic check (owned elsewhere).
- Rust, process changes, or moving the cell off the shared box.

## Decisions

### 1. Page-level filters get a maintained page-metadata index

A page-metadata table keyed by relative path holds the filterable frontmatter
fields (`type`, `projects`, `tags`, `speakers`, file type, status) plus the
page's content hash and the generation that wrote it. It lives in the existing
lexical catalogue store, is written by the existing writer fan-out component
that already touches the catalogue on every governed write, and is rebuilt by
the same single-flight repair worker that rebuilds the catalogue.
`plan_index_candidates` learns page-level clauses, so a plan is "complete" when
every clause is either a unit clause the sidecar answers or a page clause the
metadata table answers; AND/OR/NOT composition stays exactly as today.

The oracle stays. It becomes the identity test's reference and the offline
(unmanaged) fallback, never a managed reader's path.

**Alternative rejected: derive page eligibility from FTS5 columns.** The
catalogue stores text for ranking, not typed metadata; encoding list fields
into it makes `$in` and `$exists` semantics a text-matching approximation.

### 2. Exact custody flows from the receipts to the read-side caches

Each substrate cache registers an invalidation seam keyed by relative path.
When a governed write commits, the receipt's path set is applied to those
seams: rows for those paths are evicted or refreshed; nothing else moves. The
whole-scope freshness key keeps its role for receipt-less changes only: the
reconciliation pass that detects an external edit invalidates the scope it
found drift in, as today.

The pending-visibility overlay already shadows stale rows for paths pending
custody and re-offers the committed pages; the eligibility evaluation consumes
the same overlay so a filter sees the committed frontmatter before the
catalogue row is refreshed.

**Alternative rejected: keep whole-scope keys and make rebuilds cheaper.** A
rebuild that costs 8 s at 8k pages costs 32 s at 32k; the fix has to remove
the rebuild from the request, not shave it.

### 3. Out-of-KB widening is a request option with a hard reserve

`scope="kb"` serves the KB. A new boolean request option enables widening; the
`ask_memory` product default is off. When on, widening runs one catalogue query
over the out-of-KB eligible set (from the same metadata index) and reserves at
most `limit - 1` slots, as today. If the catalogue is not live the stage
declines and says so in the diagnostics. The MCP tool surface changes, so the
hosted artifact set is regenerated in the delivery (see the surface-change
pattern in the knowledge base).

**Alternative rejected: delete the reserve.** The reserve exists for terse
out-of-KB pages whose title is the query; removing it silently changes answers
for callers that rely on it. Opt-in keeps the behaviour reachable and stops the
default from paying for it.

### 4. Timing completeness is a property test, and stages carry a source

`FindTimings.span` remains the only way a stage gets a duration; a manual write
into the stages table is rejected by construction (the table becomes
write-through from spans). Every span records a `source` in
{`index`, `cache`, `declined`, `computed`}. A test drives the real public leaf
with timing enabled and asserts the two attribution bounds; a second test
asserts that no stage on the reference corpus reports a walk. The walk sentinel
is a directory-enumeration counter installed for the duration of the request
in tests, so the assertion is structural, not a timing threshold.

### 5. The latency gate refuses to measure under load

`scripts/recall_latency_gate.py` runs the series from the proposal against the
live cell over the direct transport, novel query per sample (nonce), warm
caches, and reads the per-stage diagnostics. It checks the one-minute load
average before starting and between samples; above 2.0 it waits a bounded
time and then exits without a verdict, naming the load. It records the load it
ran under next to every percentile. Ceilings live in the script as the
contract and are not calibrated from the runner.

CI keeps a model-free structural guard (the walk sentinel and the attribution
bounds) on every PR; the live-cell numbers are produced on the operator's box
at delivery and after each release, because CI runners cannot host the 8k-page
warm cell.

### 6. Delivery order follows measurement, not ambition

Tranche order: (1) walk sentinel and timing completeness, because every later
claim is measured through them; (2) page-metadata index and index-backed
eligibility, the largest win; (3) exact custody invalidation; (4) opt-in
widening and the surface regeneration; (5) the live-cell gate and the
before/after series, including re-measuring #951/#983/#984 together on the
live cell for the first time. Each tranche is a lane with its own
author-independent reviewer and mutation proofs, after the pattern that
delivered the write-side change.

## Risks / Trade-offs

- **[Risk] The page-metadata index drifts from frontmatter.** → It is written
  by the same component and receipt that publish the catalogue; the identity
  test compares it against the scan oracle on the reference corpus, and
  reconciliation audits drift the way the graph is audited.
- **[Risk] Exact invalidation misses a path.** → Receipts name every path a
  write touched, moves included; the audit compares cache rows against
  frontmatter after a burst; an unmatched row fails closed to a scope
  invalidation, never to a stale answer.
- **[Risk] Default `scope="kb"` results change for callers that relied on the
  reserve.** → Called out as a behaviour change; the option is on the surface;
  the connector docs name it.
- **[Risk] The gate never sees a quiet box.** → It refuses rather than reports;
  the operator can pause suites, and the structural guards still run in CI.
- **[Risk] A faster read path raises the write rate a client sustains, and the
  graph rebuild livelocks more often.** → Owned by
  `converge-graph-incrementally`; this change ensures a rebuild in flight cannot
  invalidate the read-side caches.

## Migration Plan

1. Land tranches 1 to 3 behind no flag: they are result-identical by
   construction and proven by the identity tests.
2. Land tranche 4 with the widening option default off; regenerate the hosted
   artifacts, the ChatGPT pending digest and the v1 release identities in the
   same commit.
3. Release; upgrade the live cell; run the gate; record before/after in the
   change; then archive.

Rollback is a release rollback; no data migration, since the metadata index is
derived and rebuilds from the vault.

## Open Questions

- Whether `speakers` and file-type filters need the metadata table or can stay
  on the media sidecar they read today; decided by the lane that inventories
  the filter registry, without changing the specs.
