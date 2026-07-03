## Why

The latency-vs-scale curve (shipped in the benchmark upgrade, extended by the
sqlite-vec change) identified where `find()` actually degrades at OSS scale, and it
is not the vector lane: at 50k notes the whole call costs ~25 s because **bm25
(~8.2 s), keyword (~8.7 s), and graph (~7.8 s, warm) are each O(N) per query**.
`rank-bm25` has no inverted index — `get_scores()` scores every document in Python
on every query (document tokenization is already incremental; the scoring is the
wall). The keyword lane substring-scans every page. The graph lane's warm cost
grew 226 ms → 1.1 s → 7.8 s from 2k → 10k → 50k notes, indicating a per-query
recompute proportional to corpus size that has not yet been profiled. "Sub-second
search at 100k notes" — the OSS scale story — is gated on these lanes.

## What Changes

- Add an **FTS5 lexical index** in a new per-vault sidecar (`.lexical.sqlite`):
  an inverted index serving the BM25 lane via FTS5's C-implemented `bm25()`
  ranking, so a query touches only its terms' posting lists instead of all N
  documents. Indexed text is **pre-stemmed with the existing Snowball
  `bm25.tokenize()`** (queries stemmed the same way), so stemming and token
  semantics stay byte-identical to today — FTS5 supplies only the index and the
  scorer. FTS5 ships compiled into CPython's bundled SQLite: no new dependency,
  no extension loading, available on lean installs (BM25 is a lean-install lane).
- Add a **trigram companion table** in the same sidecar to serve the keyword
  lane's strict-substring contract (FTS5 `trigram` tokenizer supports true
  substring matching, including mid-word) at index speed.
- Keep the lexical sidecar synchronized through the same three freshness seams
  as the embedding sidecars (writer hooks, file watcher, reconcile), with the
  count-mismatch sync-on-first-use from the vec0 backend as migration and drift
  healer — rebuilt from the markdown source of truth, page-level.
- **Profile the graph lane's warm O(N) growth, then fix it** by event-maintaining
  whatever per-query recompute the profile identifies (the resolver-rebuild fix
  is the precedent). Acceptance is measured: the latency gate gains a second
  corpus size with a scaling bound so a linear warm graph lane cannot return.
- Extend the latency-curve benchmark with `--lexical-backend` passes and re-run
  the 10k/50k(/100k) tiers over the cached corpora; publish before/after in
  `docs/benchmarks.md`.

Defaults and soft-fail, stated explicitly: `EXOMEM_LEXICAL_BACKEND` defaults to
`auto` — FTS5 serves the bm25/keyword lanes when the sidecar is available, and
every failure mode (FTS5 missing from the SQLite build, sidecar unreadable,
runtime error) soft-fails to the current in-process `rank-bm25`/substring-scan
paths with zero behavior change; `EXOMEM_LEXICAL_BACKEND=python` is the kill
switch restoring today's behavior wholesale.

Honest gating note (this differs from the vec0 swap): FTS5's `bm25()` is a BM25
variant with its own parameters, so **rankings are floors-gated, not
rank-identical** — the golden retrieval floors (and their per-query pins,
including the stemming pin) plus a keyword-lane contract suite are the acceptance
gate, and the substring contract IS held to exact parity. Pure-substrate note:
FTS5 is deterministic term statistics over already-owned text — measurement, no
model, no judgment.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `find-recall-efficiency`: the bm25 and keyword lanes gain a SQL-native indexed
  backend (floors-gated BM25 via FTS5; exact-parity substring via trigram; kill
  switch; soft-fail to the in-process paths).
- `live-index-freshness`: sidecar writes and watcher/reconcile seams keep the
  lexical index synchronized, with rebuild-from-markdown self-heal.

## Impact

- Code: `src/exomem/lexstore.py` (new, modeled on `vecstore.py`), `src/exomem/bm25.py`
  (backend ladder), the keyword-lane block in `src/exomem/find.py`, writer/watcher/
  reconcile seams, `src/exomem/warmup.py`, `src/exomem/doctor.py`,
  `scripts/latency_curve.py`, `tests/test_latency_gate.py`, `docs/benchmarks.md`.
- Surfaces: none — no command-registry, MCP, REST, or CLI parameter changes.
- Tests: lexstore unit suite (sync, migration, drift, substring parity), golden
  gate under the FTS5 backend, keyword-contract suite, graph-lane scaling gate.
- Dependencies: none. FTS5 and the trigram tokenizer are in CPython 3.11+'s
  bundled SQLite (trigram needs SQLite ≥ 3.34; CPython 3.11 bundles ≥ 3.37).
- Storage: the lexical sidecar adds roughly corpus-sized bytes (stemmed text +
  trigram index) as a local, per-machine dotfile — documented in design.md.
