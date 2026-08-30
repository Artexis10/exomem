# Tasks — add frozen stance verification

Every test lands red first (verbatim failing output recorded before the
implementation, then green). Measured budgets are written into this file when
the task is ticked, not estimated.

## 1. Admission machinery (design D1, D4, D5)

- [ ] 1.1 Label map v1: promote the `_nli_polarity` threshold logic to a
  versioned in-repo artifact; loading an unknown or unversioned map refuses.
  Red-first.
- [ ] 1.2 Weights pinning: resolve-and-hash at load against the pinned digest
  through the offline model cache; mismatch/missing refuses to absence with a
  degradation record; the lexical heuristic never carries `method: "nli"`.
  Red-first with a wrong-digest fixture.
- [ ] 1.3 Verification fixture set: golden pairs (contradiction, concordant,
  restatement, unrelated, plus known heuristic failures); admission requires
  green at the pinned (digest, label-map) pair; CI job runs it with the extra
  installed.
- [ ] 1.4 Input-shape pin: the verifier accepts exactly a claim-text pair;
  structural test that no prompt/template path exists behind the seam.

## 2. Write path (design D3)

- [ ] 2.1 Remove the `_refine_contradictions` invocation from write-time
  warning generation; delete the write-time polarity clause. Red-first:
  gate-off byte-identity pin on warnings, then a gate-on test asserting no
  stance clause; write-latency gates unchanged.

## 3. Queue enrichment (design D2, D3)

- [ ] 3.1 Audit/sweep contradiction pass enriches proximity entries with
  stance via the admitted verifier; bounded per pass; per-entry soft-fail
  recorded. Red-first with an injected fake verifier.
- [ ] 3.2 Invariance pins: `signal_version`, provenance, ordering, cap and
  omitted count unchanged by stance arrival; a dismissed entry stays dismissed.
  Mechanism-removal proof for each pin on a scratch mutant.
- [ ] 3.3 Asserted pairs are never stance-labelled. Red-first.

## 4. Packaging and diagnostics

- [ ] 4.1 `nli` optional extra in `pyproject.toml`; uninstalled degrades
  byte-identically; doctor reports the verifier tier's status (absent /
  admitted / refused-with-reason) without failing warm.
- [ ] 4.2 Docs: README degradation-modes table gains the verifier row; the
  hosted-inference-boundary doc cross-references the admission rule.

## 5. Acceptance

- [ ] 5.1 Fixture-set precision table recorded here (heuristic vs verifier on
  the golden pairs), claimed against fixtures only; no f22 bench claim while
  sequence 2 is withheld.
- [ ] 5.2 `openspec validate --all --strict` green; scoped suites green; full
  suite at the completion boundary attributed against the origin/main baseline.
