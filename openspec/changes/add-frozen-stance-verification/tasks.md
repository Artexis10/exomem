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

- [x] 1.3 Knob fate: `EXOMEM_CLAIM_POLARITY_NLI` default-off pin;
  `EXOMEM_CLAIM_NLI_MODEL` retired — a set value selects nothing and is
  reported on the diagnostic surface. Red-first.
  - **Evidence (red, before implementation)** — same command:
    ```
    >       status = claims.verifier_status()
    E       AttributeError: module 'exomem.claims' has no attribute 'verifier_status'
    FAILED tests/test_frozen_verifiers.py::test_retired_model_knob_is_reported_on_the_diagnostic_surface
    FAILED tests/test_frozen_verifiers.py::test_retired_model_knob_enables_nothing_on_its_own
    FAILED tests/test_frozen_verifiers.py::test_status_names_the_registry_and_the_shipped_label_maps
    3 failed, 21 passed in 0.13s
    ```
  - **Evidence (green, after implementation)** — same command: `24 passed in 0.11s`
  - Shipped: `claims.verifier_status()` — the claims-side diagnostic payload
    naming the gate (`EXOMEM_CLAIM_POLARITY_NLI`), the retired knob
    (`EXOMEM_CLAIM_NLI_MODEL`) and any ignored value sitting in it, the pinned
    model names, and the shipped label-map versions. Doctor renders it in 4.1.
  - The retired knob is proven inert two ways: it does not turn the verifier on
    (`reason="gate-off"` with only it set) and it does not become the model even
    when weights under that exact name are resident (`reason="no-pin"`,
    `model_name=None`, `pinned_models=[]`).

- [x] 1.4 Verification fixture set: golden pairs (contradiction, concordant,
  restatement, unrelated, plus known heuristic failures); admission requires
  green at the pinned (digest, label-map) pair; CI job runs it with the extra
  installed.
  - **Evidence (red, before implementation)** — same command:
    ```
    >           for pair in claims.VERIFICATION_FIXTURES["stance-v1"]
    E       AttributeError: module 'exomem.claims' has no attribute 'VERIFICATION_FIXTURES'
    FAILED tests/test_frozen_verifiers.py::test_fixture_set_covers_the_four_corpus_shapes
    FAILED tests/test_frozen_verifiers.py::test_green_fixtures_admit_the_verifier
    FAILED tests/test_frozen_verifiers.py::test_one_red_fixture_refuses_the_pair
    FAILED tests/test_frozen_verifiers.py::test_absent_extra_refuses_as_a_missing_dependency
    FAILED tests/test_frozen_verifiers.py::test_unknown_fixture_set_in_a_pin_refuses
    FAILED tests/test_frozen_verifiers.py::test_fixture_verification_runs_once_per_admitted_pair
    FAILED tests/test_frozen_verifiers.py::test_admitted_verifier_labels_through_the_label_map
    7 failed, 24 passed in 0.16s
    ```
  - **Evidence (green, after implementation)** — same command: `31 passed in 0.12s`
  - Shipped: `claims.FixturePair`, `claims.VERIFICATION_FIXTURES["stance-v1"]`
    (9 golden pairs over the four f22 corpus shapes plus 4 known heuristic
    failures), `_run_fixture_set`, `_verify_fixtures` (memoized by the exact
    `(model, digest, label-map, fixture-set)` it verified). Admission now runs
    the fixture set: `dependency-missing` when the extra cannot load,
    `fixtures-failed` on one miss — partial evidence is none.
  - The `heuristic_fails` flags are not decoration: each was checked against the
    real `_heuristic_polarity` (table in 5.1); every declared flag matches.

- [x] 1.5 Input-shape pin: the verifier accepts exactly a claim-text pair;
  structural test that no prompt/template path exists behind the seam.
  - **Evidence (mechanism-removal proof)** — the pin is an invariant that already
    held, so it is proven by removing the mechanism rather than by an absent
    symbol. Scratch mutant: `verifier_polarity` assembles a prompt
    (`f"Does this contradict? A: {claim_a} B: {claim_b}"`) and sends it in
    instruction position instead of the pair. Red:
    ```
    >           assert not (opcodes & assembly_opcodes), (function.__qualname__, opcodes)
    E           AssertionError: ('verifier_polarity', {'BUILD_LIST', 'BUILD_STRING', 'BUILD_TUPLE', 'CALL', 'FORMAT_SIMPLE', 'LOAD_ATTR', ...})
    FAILED tests/test_frozen_verifiers.py::test_admitted_verifier_labels_through_the_label_map
    FAILED tests/test_frozen_verifiers.py::test_claim_texts_reach_the_model_verbatim_as_a_classification_pair
    FAILED tests/test_frozen_verifiers.py::test_no_string_assembly_exists_behind_the_verifier_seam
    3 failed, 32 passed in 0.15s
    ```
  - **Evidence (green, mutant reverted)** — same command: `35 passed in 0.13s`
  - Three pins, not one: the signature accepts exactly `(claim_a: str, claim_b:
    str)`; the captured model input is byte-for-byte
    `[(a, b), (b, a)]` with nothing added; and structurally no string-assembly
    opcode (`FORMAT_*`, `BUILD_STRING`, `CONVERT_VALUE`) or template name
    (`format`, `join`, `Template`, `substitute`) exists on the seam's code
    objects. A future "better" verifier cannot become a prompted generative
    model behind this seam without one of the three going red.

## 2. Write path (design D3; command-surface delta)

- [x] 2.1 Remove the `_refine_contradictions` invocation and function, the
  `contradiction-band` partition, `_POLARITY_CLAUSE`, and the dead
  `DupCandidate.polarity` / `polarity_score` / `polarity_method` fields from
  `corpus_aware.py`. Red-first: gate-off byte-identity pin on warnings, then
  a gate-on pin that the advisory kind set is {near-duplicate, overlap} with
  no polarity clause; write-latency gates unchanged.
  - **Evidence (red, before implementation)** —
    `pytest tests/test_claims.py::test_detect_contradictions_attaches_no_polarity_even_when_gated tests/test_claims.py::test_write_path_carries_no_polarity_mechanism_at_all -q`:
    ```
    >       assert not hasattr(out[0], "polarity")
    E       AssertionError: assert not True
    E        +  where True = hasattr(DupCandidate(path='Knowledge Base/Notes/Insights/caching.md', title='Caching improves latency', cosine=0.85, polarity='contradict', polarity_score=0.7, polarity_method='heuristic'), 'polarity')
    >       assert not hasattr(corpus_aware, "_refine_contradictions")
    E       AssertionError: assert not True
    E        +  where True = hasattr(corpus_aware, '_refine_contradictions')
    FAILED tests/test_claims.py::test_detect_contradictions_attaches_no_polarity_even_when_gated
    FAILED tests/test_claims.py::test_write_path_carries_no_polarity_mechanism_at_all
    2 failed in 0.17s
    ```
  - **Evidence (green, after implementation)** — `pytest tests/test_claims.py
    tests/test_write_advisory_suppression.py tests/test_frozen_verifiers.py -q`
    → `93 passed, 1 skipped in 4.42s`
  - **Gate-off byte-identity pin**: `test_detect_contradictions_gate_off_stays_byte_identical`
    and `test_overlap_warning_byte_identical_without_polarity` hold the exact
    pre-feature `as_dict()` payload and warning string.
  - **Gate-on pin**: with `EXOMEM_CLAIM_LEVEL=1` the candidate has no `polarity`
    attribute at all, `as_dict()` is the baseline three keys, the warning carries
    no clause, and `detected_overlap_advisory_groups` yields the single
    `("overlap", …)` group.
  - Removed from `src/exomem/corpus_aware.py`: `_refine_contradictions` (function
    and its invocation in `detect_contradictions`), `_POLARITY_CLAUSE`, the
    `contradiction-band` partition (now one overlap branch), the
    `DupCandidate.polarity` / `polarity_score` / `polarity_method` fields and
    their `as_dict` branch, and `contradiction-band` from `_WRITE_ADVISORY_KINDS`
    (which `review_state.registered_families()` assembles from, so the retired
    name cannot linger there either).
  - Broad write-path regression (add, attention, audit contradiction order +
    corpus, authored contradictions, corpus_aware, corpus context cache, edit,
    no-nudge families, review queues, note, note suggestions, replace, review
    dispositions, review state, write advisory suppression, claims, frozen
    verifiers): `499 passed, 6 skipped in 63.16s`.
  - **Write-latency gates unchanged** — `pytest tests/test_latency_gate.py -q`
    (untouched file): `7 passed in 71.47s`. The path only got shorter.

- [x] 2.2 Update the named suites with the change, red-first:
  `tests/test_write_advisory_suppression.py` contradiction-band suppression
  cases become retired-kind cases (old dismissal does not suppress the
  overlap identity — the stated one-time resurfacing), and
  `tests/test_claims.py` gate-on write-path pins move to the queue channel.
  - **Evidence (red, before implementation)** — `pytest tests/test_claims.py
    tests/test_write_advisory_suppression.py -q` with the suites updated and the
    source untouched:
    ```
    E         Extra items in the left set:
    E         'contradiction-band'
    tests/test_write_advisory_suppression.py:514: AssertionError
    FAILED tests/test_claims.py::test_detect_contradictions_attaches_no_polarity_even_when_gated
    FAILED tests/test_claims.py::test_write_path_carries_no_polarity_mechanism_at_all
    FAILED tests/test_write_advisory_suppression.py::test_contradiction_band_kind_is_retired
    3 failed, 55 passed, 1 skipped in 4.07s
    ```
  - **Evidence (green, after implementation)** — same command:
    `56 passed, 1 skipped` within the `93 passed, 1 skipped in 4.42s` run above.
  - `tests/test_write_advisory_suppression.py`: the contradiction-band
    suppression case became two retired-kind cases —
    `test_contradiction_band_kind_is_retired` (the kind is gone from
    `_WRITE_ADVISORY_KINDS` and from `review_state.registered_families()`, and
    minting one raises) and
    `test_dismissed_contradiction_band_identity_does_not_suppress_the_overlap_advisory`,
    which records a real dismissal against the retired identity (reconstructed
    through the live formula with the kind briefly re-admitted, so it is exactly
    what a pre-retirement record is keyed by), shows the pair resurfacing once
    under the overlap identity, and then shows it suppressible there. `_candidate`
    lost its `polarity` parameter.
  - `tests/test_claims.py`: the gate-on write-path pins moved off the write path.
    The heuristic's own unit tests stay — the heuristic is retired from QUEUE
    ENRICHMENT, not deleted, and it is the comparison arm of the 5.1 table.

## 3. Queue enrichment (design D2, D3)

- [x] 3.1 Modify `audit._pair_polarity` and its rendering in place: enrichment
  only through the admitted verifier (`polarity_method: "nli"` plus digest and
  label-map version keys); the heuristic fallback never reaches queue
  metadata; bounded by the surfaced set (`EXOMEM_CONTRADICTION_TOP_N`);
  per-entry soft-fail recorded. Red-first with an injected fake verifier, plus
  a refused-verifier pin that no heuristic-method label is written.
  - **Evidence (red, before implementation)** —
    `pytest tests/test_frozen_verifiers.py -q`:
    ```
    >       audit_module._enrich_contradiction_polarity(tmp_path, [finding])
    E       AttributeError: module 'exomem.audit' has no attribute '_enrich_contradiction_polarity'
    FAILED tests/test_frozen_verifiers.py::test_admitted_verifier_writes_the_label_with_digest_and_label_map
    FAILED tests/test_frozen_verifiers.py::test_refused_verifier_writes_no_heuristic_label
    FAILED tests/test_frozen_verifiers.py::test_claim_level_gate_off_enriches_nothing
    FAILED tests/test_frozen_verifiers.py::test_enrichment_is_bounded_by_the_surfaced_set
    FAILED tests/test_frozen_verifiers.py::test_one_raising_pair_leaves_that_entry_unenriched_and_the_pass_completes
    FAILED tests/test_frozen_verifiers.py::test_a_stale_label_is_dropped_not_served
    FAILED tests/test_frozen_verifiers.py::test_a_current_label_is_attached - Att...
    FAILED tests/test_frozen_verifiers.py::test_asserted_pairs_carry_no_model_polarity_label
    FAILED tests/test_frozen_verifiers.py::test_enrichment_touches_no_competing_alternatives_stance_key
    FAILED tests/test_frozen_verifiers.py::test_labelling_changes_no_signal_version_provenance_order_or_cap
    FAILED tests/test_frozen_verifiers.py::test_a_dismissed_entry_stays_dismissed_when_a_label_arrives
    11 failed, 35 passed in 0.24s
    ```
  - **Evidence (green, after implementation)** — same command: `46 passed in 0.14s`
  - Audit regression: `pytest tests/test_audit_corpus_contradictions.py
    tests/test_audit_contradiction_order.py tests/test_authored_contradictions.py
    tests/test_epistemic_review_queues.py tests/test_attention.py
    tests/test_context_pack.py -q` → `170 passed, 1 skipped in 12.77s`.
  - `audit._pair_polarity` modified in place: it now consults
    `claims.verifier_admission()` and `claims.verifier_polarity()` only, returns
    the digest and label-map version alongside the label, and no longer swallows
    exceptions (swallowing made a soft-failed entry indistinguishable from an
    unlabelled one). `claims.classify_polarity` — the heuristic-bearing seam — is
    no longer reachable from the queue at all.
  - `audit._enrich_contradiction_polarity` is the single channel, called from
    `corpus_contradictions` over the already-ordered, already-capped `findings`
    list and BEFORE the omitted-count summary is appended, so the cap accounting
    is never in its reach. Per-entry `try/except` records a degraded count and
    continues; the injected-raise test shows the bad entry unenriched and the
    good one labelled in the same pass.
  - Refused-verifier pin: gate on, registry empty → the finding's `meta` is
    byte-identical, no key starting with `polarity` exists, and the rendered
    detail carries no claim-level clause.

- [x] 3.2 Invariance pins: `signal_version`, provenance, ordering, cap and
  omitted count unchanged by label arrival; a dismissed entry stays dismissed.
  Mechanism-removal proof for each pin on a scratch mutant.
  - **Evidence (red, before implementation)** — included in the 3.1 red run:
    `FAILED …::test_labelling_changes_no_signal_version_provenance_order_or_cap`
    and `FAILED …::test_a_dismissed_entry_stays_dismissed_when_a_label_arrives`.
  - **Evidence (green)** — `46 passed in 0.14s`.
  - **Mechanism-removal proof, one scratch mutant per pin** (each applied to
    `src/exomem/audit.py`, run, reverted):

    | Mutant | Pin that went red |
    | --- | --- |
    | labelling also writes `meta["signal_version"]` | `…no_signal_version_provenance_order_or_cap`, `…dismissed_entry_stays_dismissed`, `…writes_the_label_with_digest_and_label_map` (3 failed, 43 passed) |
    | labelling also writes `meta["provenance"] = "nli"` | `…no_signal_version_provenance_order_or_cap` (1 failed, 45 passed) |
    | labelling calls `findings.reverse()` | `…no_signal_version_provenance_order_or_cap` (1 failed, 45 passed) |
    | labelling rewrites the omitted-count summary's detail | `…no_signal_version_provenance_order_or_cap` (1 failed, 45 passed) |

    Reverted: `46 passed in 0.14s`. A first ordering mutant
    (`findings.sort(key=…polarity != "contradict")`) left the list order
    unchanged for this fixture and so proved nothing; it was replaced with
    `findings.reverse()`, which does reorder and does go red — recorded here
    because an ineffective mutant is not a passed pin.
  - The dismissal pin composes the entry's real `review_state.fingerprint` before
    and after enrichment and asserts equality, so "a dismissed entry stays
    dismissed" is checked through the machinery that actually binds a decision,
    not through a proxy.

- [x] 3.3 Staleness binding: the label records the `signal_version` it was
  computed against and is dropped, not served, on mismatch. Red-first.
  - **Evidence (red, before implementation)** — included in the 3.1 red run:
    `FAILED …::test_a_stale_label_is_dropped_not_served` and
    `FAILED …::test_a_current_label_is_attached` (`AttributeError: module
    'exomem.audit' has no attribute '_attach_polarity_label'`).
  - **Evidence (green)** — `46 passed in 0.14s`.
  - `audit._attach_polarity_label(finding, label)` is the seam: the label carries
    the `signal_version` it was computed against, a mismatch returns `False` with
    `meta` and `detail` untouched, and a match writes `polarity_signal_version`
    alongside the rest. Within one sweep the two always agree because the label
    is computed straight after the entry's signal version is read; the guard is
    what keeps that true for any label reaching an entry from anywhere else (a
    cache, a persisted record, a future asynchronous labeller).

- [x] 3.4 Asserted pairs carry no model polarity label; the recorded
  competing-alternatives stance contract is untouched by enrichment.
  Red-first.
  - **Evidence (red, before implementation)** — included in the 3.1 red run:
    `FAILED …::test_asserted_pairs_carry_no_model_polarity_label` and
    `FAILED …::test_enrichment_touches_no_competing_alternatives_stance_key`.
  - **Evidence (green)** — `46 passed in 0.14s`.
  - Enrichment skips any finding whose `meta["provenance"]` is not `"proximity"`,
    so an authored `contradicts` edge is never labelled — the author's assertion
    outranks a model's guess, and a label there would be the server forming an
    opinion about which side is right.
  - The competing-alternatives stance is untouched by construction: the second
    test seeds an unrelated `stance` key on a proximity entry and asserts the
    exact set of keys enrichment adds is the six-key polarity namespace, with the
    stance value unchanged.

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
