# Tasks — add frozen stance verification

Every test lands red first (verbatim failing output recorded before the
implementation, then green). Measured budgets are written into this file when
the task is ticked, not estimated.

## 1. Admission machinery (design D1, D4, D5)

- [ ] 1.1 Label map v1: promote the `_nli_polarity` threshold logic to a
  versioned in-repo artifact declaring the logit column semantics and order;
  loading an unknown or unversioned map refuses. Red-first.
- [ ] 1.2 Pin registry as repository artifact: model name, weights digest,
  label-map version, fixture set; resolve-and-hash at load (hashed once per
  process, cached) against the pinned digest through the offline model cache;
  gate-off, mismatch, or missing refuses to absence with a degradation
  record; the lexical heuristic never carries `method: "nli"`. Red-first with
  a wrong-digest fixture AND a runtime-configuration pin-injection attempt
  that must not admit.
- [ ] 1.3 Knob fate: `EXOMEM_CLAIM_POLARITY_NLI` default-off pin;
  `EXOMEM_CLAIM_NLI_MODEL` retired — a set value selects nothing and is
  reported on the diagnostic surface. Red-first.
- [ ] 1.4 Verification fixture set: golden pairs (contradiction, concordant,
  restatement, unrelated, plus known heuristic failures); admission requires
  green at the pinned (digest, label-map) pair; CI job runs it with the extra
  installed.
- [ ] 1.5 Input-shape pin: the verifier accepts exactly a claim-text pair;
  structural test that no prompt/template path exists behind the seam.

## 2. Write path (design D3; command-surface delta)

- [ ] 2.1 Remove the `_refine_contradictions` invocation and function, the
  `contradiction-band` partition, `_POLARITY_CLAUSE`, and the dead
  `DupCandidate.polarity` / `polarity_score` / `polarity_method` fields from
  `corpus_aware.py`. Red-first: gate-off byte-identity pin on warnings, then
  a gate-on pin that the advisory kind set is {near-duplicate, overlap} with
  no polarity clause; write-latency gates unchanged.
- [ ] 2.2 Update the named suites with the change, red-first:
  `tests/test_write_advisory_suppression.py` contradiction-band suppression
  cases become retired-kind cases (old dismissal does not suppress the
  overlap identity — the stated one-time resurfacing), and
  `tests/test_claims.py` gate-on write-path pins move to the queue channel.

## 3. Queue enrichment (design D2, D3)

- [ ] 3.1 Modify `audit._pair_polarity` and its rendering in place: enrichment
  only through the admitted verifier (`polarity_method: "nli"` plus digest and
  label-map version keys); the heuristic fallback never reaches queue
  metadata; bounded by the surfaced set (`EXOMEM_CONTRADICTION_TOP_N`);
  per-entry soft-fail recorded. Red-first with an injected fake verifier, plus
  a refused-verifier pin that no heuristic-method label is written.
- [ ] 3.2 Invariance pins: `signal_version`, provenance, ordering, cap and
  omitted count unchanged by label arrival; a dismissed entry stays dismissed.
  Mechanism-removal proof for each pin on a scratch mutant.
- [ ] 3.3 Staleness binding: the label records the `signal_version` it was
  computed against and is dropped, not served, on mismatch. Red-first.
- [ ] 3.4 Asserted pairs carry no model polarity label; the recorded
  competing-alternatives stance contract is untouched by enrichment.
  Red-first.

## 4. Packaging and diagnostics

- [ ] 4.1 `nli` optional extra in `pyproject.toml`; uninstalled degrades
  byte-identically; doctor reports the verifier tier's status (absent /
  admitted / refused-with-reason, including an ignored
  `EXOMEM_CLAIM_NLI_MODEL`) without failing warm.
- [ ] 4.2 Docs: README degradation-modes table gains the verifier row; the
  hosted-inference-boundary doc cross-references the admission rule.

## 5. Acceptance

- [ ] 5.1 Fixture-set precision table recorded here (heuristic vs verifier on
  the golden pairs), claimed against fixtures only; no f22 bench claim while
  sequence 2 is withheld.
- [ ] 5.2 `openspec validate --all --strict` green; scoped suites green; full
  suite at the completion boundary attributed against the origin/main baseline.
