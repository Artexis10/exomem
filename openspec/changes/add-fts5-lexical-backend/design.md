# Design - FTS5 lexical backend

## Context

Measured at 50k notes (dense synthetic corpus, reference host, 2026-07-03):
bm25 8.2 s, keyword 8.7 s, graph 7.8 s **warm**, per query — the three lanes that
gate "sub-second at 100k notes". Root causes differ:

- `bm25.py` already tokenizes incrementally (per-doc mtime-keyed token cache);
  the O(N) is `rank_bm25.BM25Okapi.get_scores()`, which scores every document in
  Python per query. No inverted index exists.
- The keyword lane is a strict case-insensitive substring scan (every
  whitespace token must appear as a substring in title or body) over every page.
- The graph lane's warm cost grows linearly with corpus size (226 ms → 7.8 s,
  2k → 50k) despite the event-maintained resolver — an unprofiled per-query
  recompute. Its fix is measurement-first (see Decisions).

The sqlite-vec change established the template this design reuses wholesale:
a shadow index in a sidecar, kept in lockstep by dual-writes through the
existing freshness seams, count-mismatch sync-on-first-use as both migration
and drift healer, a per-call backend ladder with a process-global failure memo,
an env kill switch, and promotion gated by the golden floors plus the latency
harness over cached corpora.

## Goals / Non-Goals

**Goals:**

- Make bm25-lane and keyword-lane per-query cost sub-linear in corpus size
  (posting-list / trigram lookups, not full scans), preserving lane contracts.
- Keep stemming semantics byte-identical (Snowball via the existing
  `bm25.tokenize()`); keep the keyword substring contract exactly.
- Lean-install first: both lanes run on lean installs today, so the backend
  must add no dependency and no extension loading (FTS5 is in the stdlib
  SQLite build).
- Identify and eliminate the graph lane's warm O(N) per-query recompute, with
  a scaling bound in the latency gate so it cannot regress silently.
- Before/after evidence on the cached 10k/50k(/100k) benchmark corpora.

**Non-Goals:**

- No ranking-quality change beyond what the golden floors and pins permit —
  this is an indexing change, not a ranker redesign.
- No change to fusion, the vector/CLIP/rerank lanes, or any command surface.
- No removal of the in-process paths: `rank-bm25` scoring and the substring
  scan remain the fallback, the kill-switch target, and the reference
  implementation for parity tests.
- No FTS5 custom tokenizer (stdlib `sqlite3` cannot register Python
  tokenizers) — pre-stemming makes it unnecessary.

## Decisions

### Pre-stemmed external text, not FTS5 tokenizer stemming

The FTS5 table indexes text already passed through `bm25.tokenize()` (lowercase,
`[a-z0-9]+` split, Snowball-English stem, space-joined), with `tokenize =
"unicode61"` effectively splitting on the same boundaries; queries are stemmed
identically before `MATCH`. FTS5's own `porter` tokenizer is the original Porter
algorithm — close to but not identical to Snowball English — and stdlib
`sqlite3` cannot register a custom Python tokenizer. Pre-stemming keeps token
semantics byte-identical to today and reduces the ranking delta to exactly one
variable: BM25Okapi's scoring vs FTS5's `bm25()`. The golden set's stemming pin
plus the floors gate that residual delta.

Alternative considered: FTS5 `porter unicode61` on raw text. Two deltas at once
(tokenization AND scoring) with a 26-query gate is too coarse to attribute a
regression; pre-stemming removes one.

### One new sidecar: `<vault>/Knowledge Base/.lexical.sqlite`

Not the embeddings sidecar: the lexical lanes are lean-install lanes, and
`.embeddings.sqlite` is semantically the embeddings-extra artifact (absent on
lean deploys, rebuilt by embedding tooling). A separate dotfile sidecar keeps
lifecycles independent — same WAL pragmas, same Obsidian-Sync-ignored
per-machine model. Contents: one row per page (`pages(path, mtime, title,
body_stemmed, title_raw_lower, body_raw_lower)` or equivalent), an FTS5 table
over the stemmed text, and a trigram FTS5 table over raw lowercased text.
Storage is roughly corpus-sized (stemmed text + trigram postings) — acceptable
for a local dotfile; documented in the proposal.

Alternative considered: contentless/external-content FTS5 to avoid storing text
twice. Rejected: contentless tables cannot rebuild rows without the source and
complicate the count-sync heal; the storage saved is small relative to the
trigram index that is needed regardless.

### Keyword lane via trigram FTS5, held to exact parity

The keyword contract is strict case-insensitive substring, all tokens, title or
body — including mid-word matches phrase queries cannot express. FTS5's
`trigram` tokenizer supports exactly this (substring matching for terms of
length ≥ 3; shorter needles fall back to the scan path or a LIKE over the
stored raw text, decided by a parity suite that enumerates 1-, 2-, and
3+-char-needle cases). Unlike the bm25 lane this is not floors-gated — the
parity suite asserts the FTS5-served keyword lane returns the same match set as
the reference scan on the same corpus.

### Backend ladder and env surface

`EXOMEM_LEXICAL_BACKEND` = `auto` (default) | `fts5` | `python` (kill switch).
`auto` = FTS5 when the sidecar is available and healthy; every failure —
FTS5 absent from the SQLite build (probed once per process, memoized), sidecar
unreadable, runtime error — soft-fails to the in-process paths, logged once,
process-memoized, exactly the `vecstore` ladder. `bm25.BM25Index.search()` and
find.py's keyword block consult the ladder internally; callers unchanged.

### Sync via the existing seams; rebuild from markdown

Writers, the file watcher, and reconcile already funnel markdown changes
through `embeddings.upsert_after_write` / `delete_after_remove`; the lexical
sidecar hooks the same seams (a shared post-write dispatch, so a lean install
without the embeddings extra still maintains the lexical index). Page-count +
max-mtime mismatch on first use triggers a rebuild from the markdown walk —
the migration for existing vaults and the drift healer for out-of-band writers,
`_migrate_add_frame_ts`/vec0 precedent. The find-cache freshness key gains the
lexical sidecar mtime alongside the embedding/CLIP sidecars.

### Graph lane: profile first, then event-maintain; gate by scaling bound

The warm graph lane's linear growth has a cause this design refuses to guess.
Task order: (1) add intra-lane spans to the graph stage (FindTimings already
carries per-lane spans; the graph lane gains sub-spans: freshness derivation,
seed resolution, expansion, in-degree) and capture a 2k/10k/50k profile over
the cached corpora; (2) event-maintain or memoize the identified recompute —
the resolver-rebuild fix (14 s → 226 ms) is the precedent and the code seams
(freshness registry, event-maintained indexes) already exist; (3) extend
`tests/test_latency_gate.py` with a second corpus size and a ratio bound
(warm graph median at 4× the corpus MUST NOT exceed ~1.5× — exact bound set
from the post-fix measurement, not hand-tuned) so linear warm cost cannot
return unnoticed. The fix lands in this change; only its precise target awaits
the profile.

**PROFILE VERDICT (2026-07-04, sub-spans over the cached 2k/10k/50k corpora).**
The identified per-query recompute is NOT in the graph lane's own work — it is
`FreshnessSnapshot.vault()`'s O(N) stat-walk FALLBACK, billed to the
`graph.resolver` sub-span because the resolver's freshness check is the first
consumer of the vault triple when the hot-cache key doesn't compute it
(`EXOMEM_FIND_CACHE_SIZE=0`, the harness's own setting). It fired on every
query because `scripts/latency_curve.py` seeded the freshness registry and
then immediately wiped it (`find_module.clear_cache()` calls
`freshness.clear()`) — every published pass ran registry-COLD. Measured:
registry-cold 10k (python backends): graph 1130.7 ms, of which
`graph.resolver` 1126.7 ms; registry-live: graph 3.8 ms @ 2k → 8.6 ms @ 10k →
8.9 ms @ 50k (live vector lane, warm) — flat and seed-capped, exactly as the
code reads. The bm25 lane's historical ~8.2 s @ 50k was the same walk (its
scope triple) plus true O(N) python scoring; the keyword lane's ~8.7 s was its
genuinely O(N) scan (per-page stat + substring inside the span) — the lane
this change's trigram index eliminates.

The event-maintained index the reserved fix called for ALREADY EXISTS — the
freshness registry — so the fix is to stop defeating it and to pin the shape:
(a) the harness now seeds the registry AFTER each pass's cache clear (the
production shape a live watcher maintains; the ordering bug was the "wall");
(b) the graph stage exposes `graph.seeds` / `graph.resolver` / `graph.expand`
sub-spans so any scaling regression names its phase; (c) the latency gate
gains the 2k→8k warm-graph ratio bound (1.5× + a 25 ms noise floor for
millisecond-scale medians, set from the measured flat curve). Registry-cold
one-shot processes (CLI `find`) still pay one honest walk per request — the
freshness contract doing its job, not a regression; long-lived servers ride
the watcher-maintained registry.

### What "done" looks like, measured

On the cached corpora: bm25 and keyword lanes drop from ~8 s to low tens of ms
at 50k notes (posting-list cost), the graph lane's warm median stops scaling
linearly, and end-to-end `find()` at 50k lands near the sum of the remaining
lanes (vector backend + rerank + fusion) — the sub-second story at 100k becomes
a measurement, not a claim. Golden floors hold; keyword parity suite exact.

## Risks / Trade-offs

- FTS5 `bm25()` ranking differs from BM25Okapi -> floors + per-query pins gate
  promotion; the kill switch preserves today's ranking wholesale; RRF consumes
  ranks, damping small score-order shifts.
- Trigram tables don't match needles shorter than 3 chars -> parity suite
  enumerates the short-needle cases; those fall back to LIKE over stored raw
  text (indexed lookup is unaffected for the common case).
- Write amplification (markdown + embeddings + vec0 + lexical) -> per-write
  cost stays milliseconds; rebuild-from-source unchanged; measured by the
  existing write-latency tests.
- A second sidecar to keep fresh -> same seams, same heal mechanism, same
  audit surface as the sidecars that already exist; doctor gains a lexical
  sidecar probe.
- Graph-lane fix scoped before profiling -> the task explicitly reserves the
  target; if the profile reveals a cause that needs its own change (e.g. a
  find.py architectural issue), the scaling-bound gate still lands here and
  the fix spins out — surfaced at review, not silently absorbed.

## Migration Plan

No user action. First use by a new version creates and populates
`.lexical.sqlite` from the markdown walk (seconds to low minutes at 100k notes
— tokenization is the dominant cost and the per-doc token cache already
exists). Rollback: older code ignores the sidecar; `EXOMEM_LEXICAL_BACKEND=python`
disables it in place. Deleting the sidecar is always safe (rebuilt on next use).

## Open Questions

None blocking implementation. The graph-lane profile determines its fix target
(reserved in tasks); the trigram short-needle fallback shape is decided by the
parity suite's cases.
