# Tasks

## 1. numpy-lite vector residency (B1)

- [ ] 1.1 Drop `chunk_text` from the numpy backend's cached tuple; join metadata by rowid on
      result materialization (mirror the vec0 path)
- [ ] 1.2 Optional bf16 matrix storage behind the existing backend seam (default off unless
      measured safe)
- [ ] 1.3 Before/after `latency_curve --rss` at the 10k+ tier; golden floors + parity green

## 2. Bounded FrontmatterCache (B2)

- [ ] 2.1 LRU bound with env override (default sized for typical vaults); eviction keeps
      mtime-invalidation semantics
- [ ] 2.2 Regression test: cache stays within bound under a full-vault sweep; warm-pass hit
      behavior preserved

## 3. Lazy CLI imports (B3)

- [ ] 3.1 Defer server/embedding imports out of the CLI entry path; `--help` and model-free
      one-shots import neither
- [ ] 3.2 Before/after `startup_benchmark.py`: import time cut ≥70% on the reference host;
      behavior identical (CLI tests green)
