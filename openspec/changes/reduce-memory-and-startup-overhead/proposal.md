## Why

Three memory/startup costs are measured and pre-scoped but unfixed. First, the numpy vector
backend keeps every chunk's text inside its cached tuple: at the 200k-chunk stress tier the
numpy pass holds ~3.5 GB resident vs 1.2–1.7 GB for the vec0 backends, and the recorded
"numpy-lite" remedy (drop `chunk_text` from the cached tuple, join metadata by rowid exactly
like the vec0 path already does, optionally hold the matrix bf16) is documented in
`docs/benchmarks.md`'s ANN decision record as a ~50-line, zero-new-dependency fix. Second,
the parsed-page `FrontmatterCache` (`src/exomem/find_corpus.py`) is an unbounded dict keyed by
`Path` — a long-lived server that touches every file holds every parsed page forever; the hot
find cache next to it is deliberately bounded, this one just predates that discipline. Third,
one-shot CLI startup pays imports the command never uses (measured ~11–12 s on the Windows
reference host, ~3.1 s on WSL2); hooks fall back to the CLI ladder and onboarding feels it.

## What Changes

- Vector backend: numpy path stops caching chunk text; metadata joins by rowid; optional
  bf16 matrix storage behind the existing backend seam. Ranking MUST stay byte-identical
  (golden floors + existing parity tests are the gate).
- `FrontmatterCache` becomes size-bounded (LRU) with an env override for the bound; mtime
  invalidation semantics unchanged.
- CLI entry (`src/exomem/__main__.py`) defers heavy imports so `--help` and model-free
  one-shot commands never import server/embedding stacks.

Acceptance is benchmark-gated by the lanes added in `add-memory-server-comparison-benchmark`
(`latency_curve --rss`, `startup_benchmark.py`), plus the existing latency gate and golden
floors.

## Impact

- Affected specs: `find-recall-efficiency` (vector metadata residency, bounded parsed-page
  cache), `install-readiness` (lazy CLI imports).
- Affected code: `src/exomem/embeddings.py`, `src/exomem/embedding_index.py`,
  `src/exomem/find_corpus.py`, `src/exomem/__main__.py`. No tool-surface changes; lean CI
  stays green.
