# Tasks — add frozen stance verification

Every test lands red first (verbatim failing output recorded before the
implementation, then green). Measured budgets are written into this file when
the task is ticked, not estimated.

## 1. Admission machinery (design D1, D4, D5)

- [x] 1.1 Label map v1: promote the `_nli_polarity` threshold logic to a
  versioned in-repo artifact declaring the logit column semantics and order;
  loading an unknown or unversioned map refuses. Red-first.
  - **Evidence (red, before implementation)** —
    `PYTHONPATH=…/src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest tests/test_frozen_verifiers.py -q`:
    ```
    E       AttributeError: module 'exomem.claims' has no attribute 'get_label_map'
    FAILED tests/test_frozen_verifiers.py::test_label_map_v1_is_the_declared_current_version
    FAILED tests/test_frozen_verifiers.py::test_label_map_declares_logit_column_semantics_and_order
    FAILED tests/test_frozen_verifiers.py::test_unknown_label_map_version_refuses
    FAILED tests/test_frozen_verifiers.py::test_unversioned_label_map_refuses - A...
    FAILED tests/test_frozen_verifiers.py::test_label_map_v1_applies_the_promoted_threshold_logic[logits0-contradict]
    FAILED tests/test_frozen_verifiers.py::test_label_map_v1_applies_the_promoted_threshold_logic[logits1-duplicate]
    FAILED tests/test_frozen_verifiers.py::test_label_map_v1_applies_the_promoted_threshold_logic[logits2-unrelated]
    FAILED tests/test_frozen_verifiers.py::test_label_map_v1_applies_the_promoted_threshold_logic[logits3-refine]
    FAILED tests/test_frozen_verifiers.py::test_label_map_v1_refuses_a_wrong_shaped_head
    9 failed in 0.09s
    ```
  - **Evidence (green, after implementation)** — same command: `9 passed in 0.08s`
  - Shipped: `claims.LabelMap` / `claims.get_label_map` / `claims._LABEL_MAPS`
    (`src/exomem/claims.py`). v1 declares `columns=("contradiction","entailment",
    "neutral")` and the bidirectional aggregation convention; a head whose width
    does not match the declaration is refused, not coerced.

- [x] 1.2 Pin registry as repository artifact: model name, weights digest,
  label-map version, fixture set; resolve-and-hash at load (hashed once per
  process, cached) against the pinned digest through the offline model cache;
  gate-off, mismatch, or missing refuses to absence with a degradation
  record; the lexical heuristic never carries `method: "nli"`. Red-first with
  a wrong-digest fixture AND a runtime-configuration pin-injection attempt
  that must not admit.
  - **Evidence (red, before implementation)** — same command, `tests/test_frozen_verifiers.py`:
    ```
    @pytest.fixture(autouse=True)
    def _reset_verifier_state():
    >       claims.reset_verifier_cache()
    E       AttributeError: module 'exomem.claims' has no attribute 'reset_verifier_cache'
    ERROR tests/test_frozen_verifiers.py::test_pin_registry_is_an_immutable_repository_artifact
    ERROR tests/test_frozen_verifiers.py::test_no_runtime_configuration_reads_a_model_name_into_a_pin
    ERROR tests/test_frozen_verifiers.py::test_gate_off_refuses_to_absence - Attr...
    ERROR tests/test_frozen_verifiers.py::test_empty_registry_refuses_even_with_the_gate_on
    ERROR tests/test_frozen_verifiers.py::test_runtime_pin_injection_attempt_does_not_admit
    ERROR tests/test_frozen_verifiers.py::test_wrong_digest_refuses_and_records_the_degradation
    ERROR tests/test_frozen_verifiers.py::test_missing_weights_refuse - Attribute...
    ERROR tests/test_frozen_verifiers.py::test_unknown_label_map_in_a_pin_refuses
    ERROR tests/test_frozen_verifiers.py::test_weights_are_hashed_once_per_process_and_cached
    ERROR tests/test_frozen_verifiers.py::test_a_second_resident_revision_is_ambiguous_and_refuses
    ERROR tests/test_frozen_verifiers.py::test_refused_verifier_never_lets_the_heuristic_wear_the_nli_name
    20 errors in 0.15s
    ```
  - **Evidence (green, after implementation)** — same command: `20 passed in 0.12s`
  - No regression on the pre-existing suites: `tests/test_claims.py
    tests/test_write_advisory_suppression.py` → `56 passed, 1 skipped in 4.21s`
    (the skip is `sentence_transformers` absent, as on the baseline).
  - Shipped in `src/exomem/claims.py`: `VerifierPin`, `VERIFIER_PINS` (the
    repository registry), `_active_pin`, `VerifierAdmission` (the degradation
    record, with `as_dict()` for the diagnostic surface),
    `VERIFIER_REFUSAL_REASONS`, `_directory_digest`, `_hash_resident_snapshot`,
    `resolve_weights_digest` (hashed once per process, cached),
    `reset_verifier_cache`, `verifier_admission`, `verifier_polarity`.
  - Pin-injection proof is two-sided: behavioural (`EXOMEM_CLAIM_NLI_MODEL` set
    to a planted, digest-resolvable model still yields `reason="no-pin"`, and
    `VERIFIER_PINS` is unchanged) and structural (`_active_pin.__code__.co_names`
    contains no `environ`/`getenv`, and its constants hold no `EXOMEM_*` literal).
  - **`VERIFIER_PINS` ships EMPTY** — see the unticked follow-up under §5.

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
