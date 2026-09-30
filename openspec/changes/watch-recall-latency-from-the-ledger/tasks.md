## 1. Latency watch ring

- [ ] 1.1 Red first: `tests/test_latency_watch.py` asserts that `observe` records content-free entries only (no argument value, path or excerpt can be present by construction), evicts beyond the bound, classifies `deep`, ignores samples inside the startup grace for the verdict, reports `p50_ms`/`p90_ms`/`samples` per (tool, client, deep) over the window, needs `MIN_SAMPLES` before any breach, and names the five dominant spans among the calls over the ceiling.
- [ ] 1.2 Add `src/exomem/latency_watch.py`: the PROVISIONAL constants (D2), the bounded ring, `observe(...)`, `verdicts(now, client=None)`, and the once-per-hour `latency_ceiling_exceeded` log event (D5), every entry point wrapped so it can neither raise into nor slow the call.
- [ ] 1.3 Call `observe` beside `call_ledger.record_call` in `command_surface` with the tool name, client, the `deep` boolean read from the real arguments, `total_ms` and the spans; red-first test that a failing watch leaves the ledger row and the response untouched.

## 2. Bootstrap surfaces a breach

- [ ] 2.1 Red first: `tests/test_bootstrap_latency.py` asserts that a healthy watch leaves every bootstrap profile's response without a `latency` key (shape identical to today's), that a breaching (tool, client) adds the `latency` block with `tool`, `deep`, `samples`, `p50_ms`, `p90_ms`, `ceiling_ms`, `dominant_spans`, and that another client's breach is not reported to this one.
- [ ] 2.2 Implement in `op_bootstrap` from the ring only; one sentence in the scaffold skill reference and its plugin copy explaining the block.

## 3. Doctor reads the ledger

- [ ] 3.1 Red first: `tests/test_doctor_latency.py` builds a ledger file (and one archive generation) with rows inside and outside the window and asserts per-(tool, client) figures, `warn` above the ceiling with the dominant spans named, `pass` below, and `pass` with a sample-count note when rows are too few; a missing or unparseable ledger yields `pass` with a note, never a crash.
- [ ] 3.2 Add `_check_latency()` to `doctor.py`, registered beside `observability`; document it in `docs/observability.md` (Doctor section and a short "Latency watch" section naming the ceilings and the bootstrap block).

## 4. Delivery verification

- [ ] 4.1 Scoped: the three new test files plus `tests/test_call_ledger.py`, `tests/test_command_path_spans.py`, `tests/test_bootstrap*.py`, `tests/test_tool_surface_fingerprint.py`, `tests/test_plugin_sync.py`, `tests/test_scaffold_no_leak.py`; privacy gate; `openspec validate --all --strict`.
- [ ] 4.2 After deploy: `exomem doctor` on the personal cell shows the `latency` check with the trailing figures; the ledger p90 for `openai-mcp` plain recalls is read from the same check the next day.
