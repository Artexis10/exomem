## 1. Flip the default (TDD)

- [x] 1.1 Red: update `tests/test_vec_backend_fallback.py::test_backend_env_reader_defaults`
      to expect `numpy` for unset / `auto` / unrecognized, and add
      `test_default_serves_numpy_and_never_probes_extension` asserting the default serves
      the numpy scan and never probes/loads the extension. Confirm both fail on current code.
- [x] 1.2 Green: `src/exomem/vecstore.py` `backend()` resolves unset/`auto`/unrecognized →
      `numpy`; only the explicit `sqlite-vec` enables vec0. `numpy` still accepted explicitly.
- [x] 1.3 Update the module docstring and `backend()`'s docstring: numpy default, sqlite-vec
      opt-in, no probe-and-activate, drift/self-heal note.

## 2. Sweep consequences

- [x] 2.1 Verify `_vec_gate`, both `_vec_search`, and `vector_backend_active` follow from
      `backend()` with no logic change (they gate on `backend() == "numpy"`); update
      `_vec_gate`'s docstring to record the drift/self-heal consequence.
- [x] 2.2 Confirm no other site hardcodes `auto`/probe semantics (grep).

## 3. Tests

- [x] 3.1 `tests/test_vecstore.py`: the vec0-mechanics suite opts into `sqlite-vec`
      explicitly (fixture) so it exercises vec0 exactly as the old `auto` did; the legacy
      re-enable test opts in via `sqlite-vec` instead of `auto`.
- [x] 3.2 `tests/test_vec_backend_fallback.py`: the unavailable-extension fallback test opts
      into `sqlite-vec` so it still exercises the soft-fail path.
- [x] 3.3 `tests/test_retrieval_golden.py`: the binary promotion-gate case opts into
      `sqlite-vec` (else the QUANT flag is silently ignored under the numpy default); the
      `off` cases run the numpy default; comments corrected.

## 4. Docs + harness

- [x] 4.1 `scripts/latency_curve.py`: record that numpy is the product default and the
      harness names backends explicitly (no `auto`); no behavior change (already explicit).
- [x] 4.2 `docs/benchmarks.md`: dated decision record for the flip + supersession pointer.

## 5. Gates

- [x] 5.1 `uv run pytest` full suite green.
- [x] 5.2 Golden gate `uv run --extra embeddings python -m pytest tests/test_retrieval_golden.py -m embeddings` (default backend = numpy).
- [x] 5.3 `uvx ruff check` on changed files.
- [x] 5.4 OpenSpec validation: `npm exec --yes @fission-ai/openspec -- validate --specs --strict` and this change validates `--strict`.
