# Tasks: add-context-activation-benchmark

## 1. Fixtures and corpus

- [x] 1.1 Red: `tests/test_context_activation_fixtures.py` — the fixture manifest
      lists nine cases and nine twins with gold, poison, roles, must-include,
      must-exclude, expected status and a reminder turn; digests are stable; editing a
      gold list changes the digest; no fixture turn appears verbatim in any corpus page.
- [x] 1.2 Author the fixture manifest (C1–C9, T1–T9) and the seeded synthetic corpus
      generator beside `benchmarks/epistemic/corpora/` (entities with aliases, hubs,
      a resource profile with a Records collection whose latest item makes it
      unavailable, a supersession chain, an ambiguous domain triple, ~200 distractor
      evidence/transcript pages for C9).
- [x] 1.3 Author the private snapshot builder (local only): digest-pinned copy of a
      vault with meta-note exclusion listed in the snapshot manifest; never committed;
      privacy gate passes on the repository.

## 2. Deterministic scorer

- [x] 2.1 Red: `tests/test_context_activation_audit.py` — per-case × anchor-kind
      recall/precision with poison; twin false activation split by status; abstention;
      supersession marking; token and latency bounds; duals present; no aggregate
      field; mechanism-removal test red when the compiler is disabled; void run when a
      manifest lacks any digest.
- [x] 2.2 Implement the scorer over packets (`activate_context` output or an oracle
      packet file) and the report writer (per case, per class, duals, no aggregate).

- [x] 2.3 Credit a superseded ancestor that the packet carries with `lifecycle:
      superseded` and its successor named as non-poison in the C8 scorer (the compiler
      contract allows marking or omission; the v0 scorer credits both paths).

## 3. Agent arms under `f32`

- [x] 3.1 Red: existing `f32` variants byte-identical (seeds, oracles, paired
      outcomes) after the tuple extension; new variants bind cases to seeded action
      worlds; five arms pair on one episode identity; harness faults → blocked.
- [x] 3.2 Implement the context-activation variants and arms (A1 control, A2 raw, A3
      compiler via injected packet or mandated call, A4 nudged, A5 oracle packet from
      the fixture's hand-written packet) reusing `f27_replay.py`; rotated arm order;
      manifest digests; cost cap and per-episode reservation.
- [x] 3.3 Implement the reminder-turn test, the blind extraction prompt with the
      model-free gold/poison intersection, and the blind rubric input; document the
      grader blinding.

## 4. Baseline runbook and measurements

- [x] 4.1 Write `docs/benchmarks/context-activation.md`: order of measurement
      (naive latency → A5 → A1 → A2/A4 → deterministic baseline), quiesced-cell and
      nonce rules, n = 1 baselines, n = 5 comparison, stopping criteria verbatim.
- [ ] 4.2 Run the pre-implementation measurements on the personal cell (A1, A2, A4,
      A5 at n = 1; deterministic baseline from `ask_memory` output; naive latency) and
      preserve the report as Evidence in the owner's knowledge base; record the
      measured latency constant in the fixture manifest.

## 5. Delivery

- [ ] 5.1 `openspec validate --all --strict`; scoped suites green; privacy gate green;
      PR with the baseline evidence pointer.
