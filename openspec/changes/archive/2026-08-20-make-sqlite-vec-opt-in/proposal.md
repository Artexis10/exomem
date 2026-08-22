## Why

`add-sqlite-vec-backend` made `EXOMEM_VEC_BACKEND` default to `auto`: the vec0
full-precision backend served vector search whenever the sqlite-vec extension was
importable and loadable, with the numpy scan as fallback. That default was chosen on
*synthetic* evidence (the dense-vault latency curve), where vec0-f32 was exact and won on
memory residency. Real-vault measurement reversed the call, and the probe-and-activate
mechanism turned out to be bug-shaped:

- **Page-cache cliff on a real vault.** On the maintainer's production vault (41k chunks on
  a spinning D: drive), the vec0 f32 KNN measured 123–134ms only while its ~127MB of vec
  tables stayed warm in the OS page cache. When the cache evicted them — routine on a
  working machine — the same query cost 1–12s. The RAM-resident numpy scan with the
  generation-keyed matrix cache measures 24–65ms and is write-independent: no cliff.
- **Silent behavior flip in production.** Because `auto` probed for the extension and
  activated vec0 on success, a routine `uv sync` that installed sqlite-vec silently flipped
  production onto the vec0 backend mid-day — a retrieval-path change nobody requested.
  Probe-and-activate makes "which backend serves search" depend on package presence rather
  than an explicit choice.

Both backends are exact and rank-identical (vec0-f32's overlap@10 vs numpy is 1.00; the f32
parity unit tests and the golden retrieval floors both hold), so this is purely a
latency/robustness/operability decision — recall is unaffected.

## What Changes

- `EXOMEM_VEC_BACKEND` now defaults to `numpy`. vec0 activates ONLY on the explicit value
  `sqlite-vec`. Unset, the legacy `auto`, and any unrecognized value all resolve to `numpy`.
  There is no probe-and-activate: nothing loads or probes the extension unless a caller
  opts in, so installing sqlite-vec can never again silently change the serving backend.
- `EXOMEM_VEC_BACKEND=numpy` keeps its explicit kill-switch reading (unchanged).
- With vec0 off (the default) the sidecar writers skip their vec dual-writes, so the shadow
  tables drift from the blob tables. This is benign and self-healing: opting back into
  `sqlite-vec` re-runs the existing `ensure_synced` count-mismatch check, which rebuilds the
  vec rows from the stored blobs in pure SQL before the first opt-in search.
- Docs and the latency harness record the flip: `scripts/latency_curve.py` continues to set
  the backend explicitly per pass (it never relied on the product default) and names both
  `numpy` and `sqlite-vec` so the published comparison stays honest; `docs/benchmarks.md`
  gains a dated decision record for the reversal.

Defaults and soft-fail, stated explicitly: the default (`numpy`) needs no optional package —
the base install serves vector search unchanged. Opting into `sqlite-vec` retains every
soft-fail path from `add-sqlite-vec-backend`: a missing package, a sqlite3 built without
loadable-extension support, or a runtime vec error all fall back to the numpy scan with
identical results. Binary quantization (`EXOMEM_VEC_QUANT=binary`) is unchanged — still
default-off, still gated by the golden floors, and now (correctly) inert unless the vec0
backend is also opted into.

Pure-substrate note: unchanged from `add-sqlite-vec-backend` — sqlite-vec is a distance
calculator over vectors the server already measured; no model runs. This change only moves
which exact calculator is the default.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `find-recall-efficiency`: the SQL-Native Vector Search Backend requirement is updated so
  the default backend is the in-memory numpy scan and the vec0 backend is strictly opt-in
  via `EXOMEM_VEC_BACKEND=sqlite-vec`; installing sqlite-vec MUST NOT change the serving
  backend on its own. Exactness, soft-fail, kill-switch, and runtime-degradation behavior
  are otherwise preserved.

## Impact

- Code: `src/exomem/vecstore.py` (`backend()` resolves unset/`auto`/unrecognized → `numpy`;
  only `sqlite-vec` enables vec0; module + function docstrings). The consequence sites in
  `src/exomem/embeddings.py` (`_vec_gate`, both `_vec_search`, `vector_backend_active`) are
  unchanged in logic — they already gate on `backend() == "numpy"`, so the default flip
  flows through automatically; only `_vec_gate`'s docstring is updated to record the
  drift/self-heal consequence.
- Surfaces: none — no MCP, REST, or CLI parameter changes; `find`'s request/response shapes
  and (given exactness) its ranking are unchanged for both defaults.
- Tests: `tests/test_vec_backend_fallback.py` (env-reader default is `numpy`; a new test
  asserts the default serves numpy and NEVER probes/loads the extension; the
  unavailable-extension fallback now opts into `sqlite-vec` to stay meaningful);
  `tests/test_vecstore.py` (the vec0-mechanics suite opts into `sqlite-vec` explicitly);
  `tests/test_retrieval_golden.py` (the binary promotion-gate case opts into `sqlite-vec`,
  else the flag would be silently ignored under the numpy default).
- Docs: `scripts/latency_curve.py` comments, `docs/benchmarks.md` decision record.
- Dependencies: none. `sqlite-vec` remains in the `embeddings` extra; the base install is
  unaffected and is now the default serving path.
