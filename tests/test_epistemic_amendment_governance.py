from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

BASE_BYTES = b"ratified preregistration\n"
BASE_SHA256 = hashlib.sha256(BASE_BYTES).hexdigest()
FOUNDER = "Hugo Ander Kivi"
ROOT = Path(__file__).resolve().parents[1]
PENDING_RECEIPT = (
    ROOT
    / "benchmarks/epistemic/contracts/amendment-2026-08-loop-closure.v1.json"
)
#: Families the sequence-1 amendment introduced, and therefore withholds while
#: its receipt is pending.
WITHHELD = ("f15", "f16", "f17", "f18", "f19")
DATASET = {
    "id": "fixture",
    "variant": "mini",
    "source": "local",
    "revision": "1",
    "sha256": "a" * 64,
    "case_count": 1,
}


def _scenario_payload(family_id: str, assertion: str) -> dict[str, Any]:
    """A structurally complete scenario for one family, as the loader wants it."""

    return {
        "scenario_id": f"probe-{family_id}",
        "family_id": family_id,
        "kind": "corpus",
        "public_coverage": "none",
        "phases": [
            {
                "phase_id": "p1",
                "ops": [
                    {"op": "ingest_source", "ref": "src-1"},
                    {"op": "snapshot", "ref": "s1"},
                ],
                "expect": [{"assert": assertion}],
            }
        ],
        "fairness": {
            "why_neutral": "The property is independent of any product's storage shape.",
            "public_coverage_subtraction": "No public suite covers this family.",
            "mechanisms": [
                {
                    "provider_role": "subject",
                    "mechanism": "documented query over neutral projected state",
                    "verdict": "satisfiable",
                    "evidence": "benchmarks/epistemic/PREREGISTRATION.md:1",
                }
            ],
            "privileged_endpoint_matrix": [
                {
                    "driver_surface_id": "state.read",
                    "provider": "fixture",
                    "variant": "native",
                    "disposition": "equivalent",
                    "audit_scope": "read-only projected state",
                    "evidence": "benchmarks/epistemic/PREREGISTRATION.md:1",
                    "reason": "The fixture projects only documented neutral state.",
                    "competitor_surface": "documented read endpoint",
                }
            ],
            "acceptance_predicate": "PREREGISTRATION.md section 4.",
        },
    }


def _scenario_yaml(family_id: str, assertion: str) -> str:
    import json

    return json.dumps(_scenario_payload(family_id, assertion))


def _receipt(
    sequence: int,
    parent_sha256: str,
    amended_bytes: bytes,
    *,
    ratifier: str | None = FOUNDER,
    acknowledged_on: str | None = "2026-08-15",
) -> dict[str, Any]:
    return {
        "artifact_type": "preregistration-amendment-receipt.v1",
        "schema_version": 1,
        "sequence": sequence,
        "contract_path": "benchmarks/epistemic/PREREGISTRATION.md",
        "parent_contract_sha256": parent_sha256,
        "contract_sha256": hashlib.sha256(amended_bytes).hexdigest(),
        "repository_revision": "a" * 40,
        "amended_on": "2026-08-15",
        "affected_sections": ("Status", "§1", "§2", "§4", "§7"),
        "rationale": "Add the ratified loop-closure benchmark families.",
        "effective_policy": "Applies only after founder acknowledgment.",
        "ratifier": ratifier,
        "acknowledged_on": acknowledged_on,
    }


def test_amendment_receipt_distinguishes_pending_from_acknowledged() -> None:
    from protocol.contracts import (
        AmendmentAcknowledgmentPendingError,
        AmendmentReceipt,
    )

    pending = AmendmentReceipt.model_validate(
        _receipt(
            1,
            BASE_SHA256,
            BASE_BYTES + b"amendment\n",
            ratifier=None,
            acknowledged_on=None,
        )
    )
    assert pending.acknowledgment_status == "pending"
    with pytest.raises(AmendmentAcknowledgmentPendingError, match="pending"):
        pending.require_acknowledged()

    acknowledged = AmendmentReceipt.model_validate(
        _receipt(1, BASE_SHA256, BASE_BYTES + b"amendment\n")
    )
    assert acknowledged.acknowledgment_status == "acknowledged"
    acknowledged.require_acknowledged()


def test_pending_receipt_may_defer_repository_revision_to_acknowledgment() -> None:
    from protocol.contracts import AmendmentReceipt

    pending_payload = _receipt(
        1,
        BASE_SHA256,
        BASE_BYTES + b"amendment\n",
        ratifier=None,
        acknowledged_on=None,
    )
    pending_payload["repository_revision"] = None
    assert AmendmentReceipt.model_validate(pending_payload).repository_revision is None

    with pytest.raises(ValidationError, match="repository revision.*acknowledg"):
        AmendmentReceipt.model_validate(
            {
                **pending_payload,
                "ratifier": FOUNDER,
                "acknowledged_on": "2026-08-15",
            }
        )


@pytest.mark.parametrize(
    ("ratifier", "acknowledged_on"),
    [(FOUNDER, None), (None, "2026-08-15")],
)
def test_amendment_receipt_refuses_partial_acknowledgment(
    ratifier: str | None, acknowledged_on: str | None
) -> None:
    from protocol.contracts import AmendmentReceipt

    with pytest.raises(ValidationError, match="acknowledg"):
        AmendmentReceipt.model_validate(
            _receipt(
                1,
                BASE_SHA256,
                BASE_BYTES + b"amendment\n",
                ratifier=ratifier,
                acknowledged_on=acknowledged_on,
            )
        )


def test_f18_catastrophic_candidacy_is_decided_at_acknowledgment() -> None:
    from protocol.contracts import AmendmentReceipt

    payload = _receipt(1, BASE_SHA256, BASE_BYTES + b"amendment\n")
    payload["affected_sections"] = (*payload["affected_sections"], "§3 candidacy")
    with pytest.raises(ValidationError, match="catastrophic.*decision"):
        AmendmentReceipt.model_validate(payload)

    acknowledged = AmendmentReceipt.model_validate(
        {**payload, "catastrophic_set_decision": "accept"}
    )
    assert acknowledged.catastrophic_set_decision == "accept"


def test_acknowledged_amendment_requires_the_pinned_founder_identity() -> None:
    from protocol.contracts import AmendmentReceipt

    with pytest.raises(ValidationError, match="founder.*identity"):
        AmendmentReceipt.model_validate(
            {
                **_receipt(1, BASE_SHA256, BASE_BYTES + b"amendment\n"),
                "ratifier": "Not the founder",
            }
        )


def test_chain_fold_accepts_ordered_acknowledged_receipts() -> None:
    from protocol.contracts import AmendmentReceipt, fold_amendment_chain

    amended_once = BASE_BYTES + b"one\n"
    amended_twice = amended_once + b"two\n"
    receipts = (
        AmendmentReceipt.model_validate(_receipt(1, BASE_SHA256, amended_once)),
        AmendmentReceipt.model_validate(
            _receipt(2, hashlib.sha256(amended_once).hexdigest(), amended_twice)
        ),
    )

    assert fold_amendment_chain(
        receipts,
        base_sha256=BASE_SHA256,
        current_sha256=hashlib.sha256(amended_twice).hexdigest(),
    ) == hashlib.sha256(amended_twice).hexdigest()


def test_chain_fold_missing_receipt_has_typed_refusal() -> None:
    from protocol.contracts import AmendmentChainMissingError, fold_amendment_chain

    actual = hashlib.sha256(BASE_BYTES + b"silent edit\n").hexdigest()
    with pytest.raises(
        AmendmentChainMissingError,
        match=rf"expected.*{BASE_SHA256}.*actual.*{actual}",
    ):
        fold_amendment_chain((), base_sha256=BASE_SHA256, current_sha256=actual)


def test_chain_fold_out_of_order_receipt_has_typed_refusal() -> None:
    from protocol.contracts import (
        AmendmentChainOrderError,
        AmendmentReceipt,
        fold_amendment_chain,
    )

    amended = BASE_BYTES + b"one\n"
    receipt = AmendmentReceipt.model_validate(_receipt(2, BASE_SHA256, amended))
    with pytest.raises(AmendmentChainOrderError, match="expected sequence 1.*actual 2"):
        fold_amendment_chain(
            (receipt,),
            base_sha256=BASE_SHA256,
            current_sha256=hashlib.sha256(amended).hexdigest(),
        )


def test_chain_fold_mismatched_parent_has_typed_refusal() -> None:
    from protocol.contracts import (
        AmendmentChainMismatchError,
        AmendmentReceipt,
        fold_amendment_chain,
    )

    amended = BASE_BYTES + b"one\n"
    receipt = AmendmentReceipt.model_validate(_receipt(1, "f" * 64, amended))
    with pytest.raises(
        AmendmentChainMismatchError,
        match=rf"expected.*{BASE_SHA256}.*actual.*{'f' * 64}",
    ):
        fold_amendment_chain(
            (receipt,),
            base_sha256=BASE_SHA256,
            current_sha256=hashlib.sha256(amended).hexdigest(),
        )


def test_ratified_identity_drift_check_accepts_base_or_receipted_chain() -> None:
    from protocol.contracts import AmendmentReceipt, validate_preregistration_bytes

    assert (
        validate_preregistration_bytes(BASE_BYTES, (), base_sha256=BASE_SHA256)
        == BASE_SHA256
    )

    amended = BASE_BYTES + b"one\n"
    receipt = AmendmentReceipt.model_validate(_receipt(1, BASE_SHA256, amended))
    assert validate_preregistration_bytes(
        amended, (receipt,), base_sha256=BASE_SHA256
    ) == hashlib.sha256(amended).hexdigest()


def test_ratified_identity_drift_names_expected_and_actual_identities() -> None:
    from protocol.contracts import PreregistrationDriftError, validate_preregistration_bytes

    edited = BASE_BYTES + b"silent edit\n"
    actual = hashlib.sha256(edited).hexdigest()
    with pytest.raises(
        PreregistrationDriftError,
        match=rf"expected.*{BASE_SHA256}.*actual.*{actual}",
    ):
        validate_preregistration_bytes(edited, (), base_sha256=BASE_SHA256)


def test_manifest_lineage_is_optional_but_must_match_typed_identity() -> None:
    from protocol.contracts import (
        AmendmentIdentity,
        ContractArtifactIdentity,
        PreregistrationIdentity,
        ReceiptIdentity,
    )
    from protocol.models import PreregistrationLineage, RunManifest

    revision = "a" * 40
    receipt_revision = "b" * 40
    amended_sha256 = "c" * 64
    receipt_sha256 = "d" * 64
    original = ContractArtifactIdentity(
        path="benchmarks/epistemic/PREREGISTRATION.md",
        sha256=BASE_SHA256,
        repository_revision=revision,
    )
    ratification = ReceiptIdentity(
        receipt_path="benchmarks/epistemic/contracts/ratification.v1.json",
        receipt_sha256="e" * 64,
        introduction_revision=receipt_revision,
    )
    amended = ContractArtifactIdentity(
        path=original.path,
        sha256=amended_sha256,
        repository_revision="c" * 40,
    )
    amendment = AmendmentIdentity(
        sequence=1,
        receipt=ReceiptIdentity(
            receipt_path="benchmarks/epistemic/contracts/amendment-0001.v1.json",
            receipt_sha256=receipt_sha256,
            introduction_revision="d" * 40,
        ),
        parent_contract_sha256=BASE_SHA256,
        contract=amended,
        affected_sections=("§7",),
        rationale="reason",
        effective_policy="after acknowledgment",
        acknowledgment_status="acknowledged",
        introduced_family_ids=(),
    )
    base_identity = PreregistrationIdentity(
        contract_revision=receipt_revision,
        original=original,
        ratification=ratification,
        amendments=(),
        effective=original,
    )
    identity = PreregistrationIdentity(
        contract_revision="d" * 40,
        original=original,
        ratification=ratification,
        amendments=(amendment,),
        effective=amended,
    )
    payload = {
        "run_id": "run",
        "dataset": {
            "id": "fixture",
            "variant": "mini",
            "source": "local",
            "revision": "1",
            "sha256": "f" * 64,
            "case_count": 1,
        },
        "status": "started",
        "started_at": "2026-08-15T00:00:00Z",
        "preregistration_identity": base_identity,
    }

    without_lineage = RunManifest.model_validate(payload)
    assert without_lineage.preregistration_lineage is None

    amended_payload = {**payload, "preregistration_identity": identity}
    with pytest.raises(ValidationError, match="amended.*lineage"):
        RunManifest.model_validate(amended_payload)

    lineage = PreregistrationLineage.from_identity(identity)
    assert lineage.amendment_receipt_sha256s == (receipt_sha256,)
    assert RunManifest.model_validate(
        {**amended_payload, "preregistration_lineage": lineage}
    ).preregistration_lineage == lineage

    with pytest.raises(ValidationError, match="lineage.*effective"):
        RunManifest.model_validate(
            {
                **amended_payload,
                "preregistration_lineage": lineage.model_copy(
                    update={"effective_sha256": "0" * 64}
                ),
            }
        )


def test_manifest_construction_refuses_unreceipted_working_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import protocol.manifest as manifest
    from protocol.contracts import PreregistrationDriftError

    def refuse(_root):
        raise PreregistrationDriftError("working pre-registration drift: expected base; actual edit")

    monkeypatch.setattr(manifest, "validate_working_preregistration", refuse)
    with pytest.raises(manifest.ManifestError, match="working pre-registration drift"):
        manifest.start_manifest(
            tmp_path,
            run_id="run",
            dataset={
                "id": "fixture",
                "variant": "mini",
                "source": "local",
                "revision": "1",
                "sha256": "a" * 64,
                "case_count": 1,
            },
            started_at="2026-08-15T00:00:00Z",
        )


def test_manifest_construction_binds_working_sha_to_derived_identity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import protocol.manifest as manifest
    from protocol.contracts import derive_preregistration_identity

    identity = derive_preregistration_identity(
        ROOT, contract_revision="7cd15e6d6c67eb914e4f57bd943f98f7d1894b7f"
    )
    monkeypatch.setattr(manifest, "validate_working_preregistration", lambda _root: "f" * 64)
    monkeypatch.setattr(manifest, "derive_preregistration_identity", lambda *_args, **_kwargs: identity)

    with pytest.raises(manifest.ManifestError, match="working.*derived.*identity"):
        manifest.start_manifest(
            tmp_path,
            run_id="run",
            dataset={
                "id": "fixture",
                "variant": "mini",
                "source": "local",
                "revision": "1",
                "sha256": "a" * 64,
                "case_count": 1,
            },
            started_at="2026-08-15T00:00:00Z",
        )


def test_dated_receipts_order_by_declared_sequence_not_filename() -> None:
    import json

    from protocol.contracts import AmendmentReceipt, order_amendment_receipt_rows

    amended_once = BASE_BYTES + b"one\n"
    amended_twice = amended_once + b"two\n"
    first = AmendmentReceipt.model_validate(_receipt(1, BASE_SHA256, amended_once))
    second = AmendmentReceipt.model_validate(
        _receipt(2, hashlib.sha256(amended_once).hexdigest(), amended_twice)
    )
    rows = (
        ("amendment-2026-08-aardvark.v1.json", second.model_dump_json().encode()),
        ("amendment-2026-08-loop-closure.v1.json", first.model_dump_json().encode()),
    )

    ordered = order_amendment_receipt_rows(rows)

    assert [json.loads(data)["sequence"] for _name, data in ordered] == [1, 2]


def test_frozen_amendment_text_is_acknowledgment_lifecycle_neutral() -> None:
    text = (ROOT / "benchmarks/epistemic/PREREGISTRATION.md").read_text(encoding="utf-8")
    assert "PENDING AMENDMENT" not in text
    assert "pending founder acknowledgment" not in text.lower()
    assert "awaiting founder acknowledgment" not in text.lower()


def test_real_loop_closure_receipt_is_explicitly_pending_founder_acknowledgment() -> None:
    from protocol.contracts import AmendmentReceipt

    receipt = AmendmentReceipt.model_validate_json(PENDING_RECEIPT.read_bytes())
    assert receipt.parent_contract_sha256 == (
        "21aa5a8815038b82358336798b10afd8d3ffbd9739c8da597955bd14d8d962e3"
    )
    assert receipt.contract_sha256 == hashlib.sha256(
        (ROOT / receipt.contract_path).read_bytes()
    ).hexdigest()
    assert receipt.ratifier is None
    assert receipt.acknowledged_on is None
    assert receipt.catastrophic_set_decision is None
    assert receipt.repository_revision is None
    assert receipt.acknowledgment_status == "pending"


def test_manifest_schema_constrains_each_lineage_receipt_identity_to_sha256() -> None:
    import json

    schema = json.loads(
        (ROOT / "benchmarks/protocol/schema/run-manifest.v2.schema.json").read_text()
    )
    items = schema["$defs"]["PreregistrationLineage"]["properties"][
        "amendment_receipt_sha256s"
    ]["items"]
    assert items["pattern"] == "^[0-9a-f]{64}$"


def test_real_working_chain_folds_while_acknowledgment_is_pending() -> None:
    """A pending amendment is a fact about the contract, not a repository outage.

    The chain still folds from the ratified base to the working bytes: the
    digests bind exactly as they will after acknowledgment.  What acknowledgment
    gates is the *use* of the families the amendment introduced, which is
    asserted separately below.
    """

    from protocol.contracts import AmendmentReceipt, validate_working_preregistration

    receipt = AmendmentReceipt.model_validate_json(PENDING_RECEIPT.read_bytes())
    assert receipt.acknowledgment_status == "pending"
    assert validate_working_preregistration(ROOT) == receipt.contract_sha256


def test_pending_amendment_still_derives_a_complete_typed_identity() -> None:
    """Identity derivation must reconstruct a pending amendment, not refuse it.

    Every digest and ancestry binding still holds.  The one field the founder
    has not yet supplied — the amended-document revision — is taken from the
    receipt's uniquely reconstructed introduction commit, which is where the
    amendment actually landed.
    """

    from protocol.contracts import derive_preregistration_identity

    identity = derive_preregistration_identity(ROOT)

    assert len(identity.amendments) == 1
    amendment = identity.amendments[0]
    assert amendment.acknowledgment_status == "pending"
    assert amendment.introduced_family_ids == ("f15", "f16", "f17", "f18", "f19")
    assert amendment.contract.repository_revision == amendment.receipt.introduction_revision
    assert identity.effective.sha256 == amendment.contract.sha256
    assert identity.withheld_family_ids == frozenset(
        {"f15", "f16", "f17", "f18", "f19"}
    )


def test_pending_amendment_withholds_only_the_families_it_introduced() -> None:
    from protocol.contracts import (
        AmendmentAcknowledgmentPendingError,
        derive_preregistration_identity,
        require_amended_families_released,
    )

    identity = derive_preregistration_identity(ROOT)

    # Ratified-base families are untouched by a pending amendment.
    require_amended_families_released(identity, ("f01", "f07", "f14"))
    require_amended_families_released(identity, ())

    for family_id in ("f15", "f16", "f17", "f18", "f19"):
        with pytest.raises(
            AmendmentAcknowledgmentPendingError,
            match=rf"amendment sequence 1 .*pending.*{family_id}",
        ):
            require_amended_families_released(identity, (family_id,))

    with pytest.raises(AmendmentAcknowledgmentPendingError, match="f18"):
        require_amended_families_released(identity, ("f01", "f18"))


def test_pending_amendment_does_not_block_an_unrelated_run_manifest(
    tmp_path,
) -> None:
    """The narrowing that matters: unrelated work is not collateral damage."""

    from protocol.manifest import start_manifest

    manifest = start_manifest(
        tmp_path / "unrelated",
        run_id="unrelated",
        dataset={
            "id": "fixture",
            "variant": "mini",
            "source": "local",
            "revision": "1",
            "sha256": "a" * 64,
            "case_count": 1,
        },
        started_at="2026-08-15T00:00:00Z",
    )

    assert manifest.status == "started"
    # The pending amendment is recorded, not hidden: a reader of this manifest
    # can see the run executed against an unacknowledged contract state.
    assert manifest.preregistration_identity.amendments[0].acknowledgment_status == "pending"
    assert manifest.preregistration_lineage is not None


def test_pending_amendment_refuses_a_run_manifest_declaring_an_amended_family(
    tmp_path,
) -> None:
    """The fail-closed guarantee, aimed: no run may declare f15-f19 while pending."""

    from protocol.manifest import ManifestError, start_manifest

    dataset = {
        "id": "fixture",
        "variant": "mini",
        "source": "local",
        "revision": "1",
        "sha256": "a" * 64,
        "case_count": 1,
    }

    with pytest.raises(ManifestError, match="amendment pending.*f18"):
        start_manifest(
            tmp_path / "amended",
            run_id="amended",
            dataset=dataset,
            started_at="2026-08-15T00:00:00Z",
            family_ids=("f01", "f18"),
        )
    # Refused before any artifact exists: nothing was written to disk.
    assert not (tmp_path / "amended" / "manifest.json").exists()

    started = start_manifest(
        tmp_path / "released",
        run_id="released",
        dataset=dataset,
        started_at="2026-08-15T00:00:00Z",
        family_ids=("f01", "f14"),
    )
    assert started.status == "started"


def test_pending_amendment_refuses_a_claim_read_back_from_a_recorded_manifest(
    tmp_path,
) -> None:
    """A manifest recorded while pending may not be replayed into an f15-f19 claim."""

    from protocol.manifest import ManifestError, finalize_manifest, load_manifest, start_manifest

    run_dir = tmp_path / "recorded"
    start_manifest(
        run_dir,
        run_id="recorded",
        dataset={
            "id": "fixture",
            "variant": "mini",
            "source": "local",
            "revision": "1",
            "sha256": "a" * 64,
            "case_count": 1,
        },
        started_at="2026-08-15T00:00:00Z",
    )
    finalize_manifest(run_dir, status="VALID", finalized_at="2026-08-15T00:01:00Z")

    assert load_manifest(run_dir).run_id == "recorded"
    assert load_manifest(run_dir, family_ids=("f01",)).run_id == "recorded"
    with pytest.raises(ManifestError, match="amendment pending.*f15"):
        load_manifest(run_dir, family_ids=("f15",))


# --------------------------------------------------------------------------
# The pairing: registering f15-f19 removes the registry's incidental refusal,
# so the receipt-governed gate must take over in the same change.
# --------------------------------------------------------------------------


def test_amended_families_are_registered_against_the_amended_document() -> None:
    """The frozen registry mirrors §1 and §2 of the document, amendment included."""

    from epistemic.registry import (
        AMENDMENT_INTRODUCED_FAMILIES,
        ASSERTION_REGISTRY,
        PREREGISTERED_FAMILY_IDS,
    )

    for family_id in WITHHELD:
        assert family_id in PREREGISTERED_FAMILY_IDS
        assert AMENDMENT_INTRODUCED_FAMILIES[family_id] == 1
    for assertion in (
        "due_prediction_surfaced",
        "verdict_state_retrievable",
        "divergence_surfaced_without_mutation",
        "support_collapse_inspectable",
        "refuted_retrievable_at_full_standing",
        "loop_journey_state_coherent",
    ):
        assert assertion in ASSERTION_REGISTRY


def test_registry_mirror_of_introduced_families_matches_the_receipt_chain() -> None:
    """The hand-mirrored constant cannot drift from what the receipts prove."""

    from epistemic.registry import AMENDMENT_INTRODUCED_FAMILIES
    from protocol.contracts import derive_preregistration_identity

    identity = derive_preregistration_identity(ROOT)
    derived = {
        family_id: amendment.sequence
        for amendment in identity.amendments
        for family_id in amendment.introduced_family_ids
    }
    assert dict(AMENDMENT_INTRODUCED_FAMILIES) == derived


def test_pending_amendment_refuses_to_load_a_scenario_for_an_amended_family() -> None:
    """The non-bypassable gate: an f15-f19 scenario cannot be constructed at all.

    Before f15-f19 were registered, the frozen §1 table refused these scenarios
    incidentally — the family simply was not known. Registering them removes
    that accident, so the receipt has to refuse them on purpose, at the same
    load-time choke point.
    """

    from epistemic.schema import ScenarioLoadError, load_scenario_text

    for family_id in WITHHELD:
        with pytest.raises(
            ScenarioLoadError,
            match=rf"amendment sequence 1 .*pending.*{family_id}",
        ):
            load_scenario_text(
                _scenario_yaml(family_id, "refuted_retrievable_at_full_standing"),
                source=f"{family_id}.yaml",
            )


def test_ratified_base_families_still_load_while_the_amendment_is_pending() -> None:
    """The narrowing must not become a blanket refusal of scenario loading."""

    from epistemic.schema import load_scenario_text

    scenario = load_scenario_text(
        _scenario_yaml("f01", "evidence_path_exists"), source="f01.yaml"
    )
    assert scenario.family_id == "f01"


def test_declared_scenario_families_reach_the_manifest_gate(tmp_path) -> None:
    """Defence in depth: a Scenario built without the loader is still refused.

    The loader is the choke point, but a run manifest is the protocol's
    mandatory pre-provider artifact, so the families a run declares must also
    be named to it. This is the wiring that made the gate stop being latent.
    """

    from epistemic.manifest import (
        declared_family_ids,
        load_epistemic_manifest,
        start_epistemic_manifest,
    )
    from epistemic.schema import Scenario
    from protocol.manifest import ManifestError, finalize_manifest

    # Built straight from JSON, deliberately bypassing load_scenario_text: the
    # loader would refuse f18 outright, and the point here is that the manifest
    # gate holds even for a Scenario that never went through it.
    amended = Scenario.model_validate_json(
        _scenario_yaml("f18", "refuted_retrievable_at_full_standing")
    )
    released = Scenario.model_validate_json(_scenario_yaml("f01", "evidence_path_exists"))

    assert declared_family_ids((released, amended)) == ("f01", "f18")

    with pytest.raises(ManifestError, match="amendment pending.*f18"):
        start_epistemic_manifest(
            tmp_path / "amended",
            run_id="amended",
            dataset=DATASET,
            started_at="2026-08-15T00:00:00Z",
            scenarios=(released, amended),
        )
    assert not (tmp_path / "amended" / "manifest.json").exists()

    run_dir = tmp_path / "released"
    started = start_epistemic_manifest(
        run_dir,
        run_id="released",
        dataset=DATASET,
        started_at="2026-08-15T00:00:00Z",
        scenarios=(released,),
    )
    assert started.status == "started"
    finalize_manifest(run_dir, status="VALID", finalized_at="2026-08-15T00:01:00Z")

    assert load_epistemic_manifest(run_dir, scenarios=(released,)).run_id == "released"
    with pytest.raises(ManifestError, match="amendment pending.*f18"):
        load_epistemic_manifest(run_dir, scenarios=(released, amended))


def test_no_execution_path_reaches_an_amended_family_while_pending() -> None:
    """The audit the narrowing turns on: enumerate every way in, and close it.

    Registering f15-f19 removed the frozen registry's incidental refusal. If any
    surface that runs, scores, or records a family still accepted one of them,
    the protection would have evaporated between the registry and the gate
    rather than transferred. These are those surfaces.
    """

    from epistemic.runner import evaluate_scenario
    from epistemic.schema import Scenario, ScenarioLoadError, load_scenario_text
    from epistemic.scoring import assemble_family
    from protocol.contracts import AmendmentAcknowledgmentPendingError

    payload = _scenario_payload("f18", "refuted_retrievable_at_full_standing")

    # 1. Loading a scenario — the choke point nothing downstream can skip.
    with pytest.raises(ScenarioLoadError, match="pending"):
        load_scenario_text(_scenario_yaml("f18", "evidence_path_exists"), source="f18.yaml")

    # 2. Evaluating a Scenario built without the loader.
    hand_built = Scenario.model_validate_json(_scenario_yaml("f18", "evidence_path_exists"))
    with pytest.raises(AmendmentAcknowledgmentPendingError, match="f18"):
        evaluate_scenario(hand_built, snapshots={})

    # 3. Scoring a family row, which takes a bare id and IS the comparative claim.
    with pytest.raises(AmendmentAcknowledgmentPendingError, match="f18"):
        assemble_family(family_id="f18", provider="fixture")

    # Ratified-base families keep working at every one of those surfaces.
    assert load_scenario_text(
        _scenario_yaml("f01", "evidence_path_exists"), source="f01.yaml"
    ).family_id == "f01"
    assert assemble_family(family_id="f01", provider="fixture").family_id == "f01"
    assert Scenario.model_validate_json(_scenario_yaml("f01", "evidence_path_exists"))
    assert payload["family_id"] == "f18"
