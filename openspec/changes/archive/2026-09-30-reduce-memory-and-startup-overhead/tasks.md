# Tasks

## 1. numpy-lite vector residency (B1)

- [x] 1.1 Drop `chunk_text` from the numpy backend's cached tuple; join metadata by rowid on
      result materialization (mirror the vec0 path)
- [x] 1.2 Optional bf16 matrix storage behind the existing backend seam (default off unless
      measured safe). Declined: not pursued; hosted cells take the vec0 backend under
      `bound-cell-memory` instead, which removes the resident matrix outright.
- [x] 1.3 Before/after `latency_curve --rss` at the 10k+ tier; golden floors + parity green
      (shipped in 557dcf9b: chunk text was ~2 GB of ~3.5 GB RSS at 200k chunks; lean suite
      and `pytest -m embeddings` golden floors + parity green)

## 2. Bounded FrontmatterCache (B2)

- [x] 2.1 LRU bound with env override (default sized for typical vaults); eviction keeps
      mtime-invalidation semantics
- [x] 2.2 Regression test: cache stays within bound under a full-vault sweep; warm-pass hit
      behavior preserved (shipped in #186, 8346af14; `tests/test_frontmatter_cache_bound.py`)

## 3. Lazy CLI imports (B3)

- [x] 3.1 Defer server/embedding imports out of the CLI entry path; `--help` and model-free
      one-shots import neither
- [x] 3.2 Before/after `startup_benchmark.py`: import time cut ≥70% on the reference host;
      behavior identical (CLI tests green). Shipped in #187 (cf099871);
      `tests/test_cli_lazy_imports.py` and `tests/test_lazy_imports.py` pin the import set.
