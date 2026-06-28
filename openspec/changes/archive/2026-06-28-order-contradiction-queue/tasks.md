# Tasks — Order the contradiction review queue

## 1. Priority + ordering in audit.py
- [x] 1.1 Add env readers `_contradiction_top_n()` (default 40; `0`/negative =
      uncapped) and `_contradiction_w_dormancy()` (default 0.5), mirroring the
      `_stale_*` env-reader style (bad values log + fall back).
- [x] 1.2 Add `_contradiction_family(rel_path)` → `"Notes/Research/<X>"` or
      `None`, and use it to compute a per-pair `same_family` flag.
- [x] 1.3 In `_check_corpus_contradictions`, after collecting `pair_cos`, compute
      per-note ACT-R dormancy by reusing `_stale_access_events(today)` +
      `_stale_activation_params()` + `_activation()`; squash to `[0,1]`; combine
      per pair via `max`; compute `priority = cos + W_DORMANCY * pair_dormancy`.
- [x] 1.4 Sort findings by `(same_family, -priority, a, b)`; preserve
      `meta.cosine`; add `meta.priority`, `meta.dormancy`, `meta.same_family`.
- [x] 1.5 Cap at top-N; when capped, append ONE summary finding (no `paths`,
      `meta.truncated`); uncapped when `top_n <= 0`. Thread `today` from `audit()`.
- [x] 1.6 Keep the `KB_MCP_DISABLE_EMBEDDINGS` short-circuit and the empty/gated
      fallbacks intact (dormancy degrades to 1.0 when the access signal is gated).

## 2. Tests
- [x] 2.1 `tests/test_audit_contradiction_order.py`: patch
      `EmbeddingIndex.all_vectors` with synthetic unit vectors (numpy only, no
      torch) to control cosines; planted pairs sort by priority (cosine and
      dormancy); same-family demoted below a higher-priority cross-family pair;
      cap respected + omitted count reported in a summary finding; no mutation
      (file hashes unchanged across the audit run).

## 3. Docs
- [x] 3.1 Note the ordering/cap behavior + the new env knobs in the `op_audit`
      docstring's `corpus_contradictions` bullet (`src/kb_mcp/commands.py`).

## 4. Verify
- [x] 4.1 `PYTHONPATH=src KB_MCP_DISABLE_EMBEDDINGS=1 .venv/Scripts/python.exe -m
      pytest -q` green (full suite, no regression).
- [x] 4.2 `uvx ruff check .` clean (no new findings).
- [x] 4.3 `openspec validate order-contradiction-queue --strict` passes.
- [ ] 4.4 Live-vault smoke (Hugo, GPU/embeddings box): run
      `audit(categories=["corpus_contradictions"])` and confirm the top of the
      queue is dominated by cross-family dormant-close pairs, same-family
      architecture pairs sink, and the omitted-count line reports the remainder.
      **(Only verifies on the live vault — the suite is torch-less.)**
