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
LOOP_CLOSURE_RECEIPT = (
    ROOT
    / "benchmarks/epistemic/contracts/amendment-2026-08-loop-closure.v1.json"
)
#: The squash commit on ``main`` carrying the amended pre-registration and its
#: then-pending receipt.  The founder pinned this at acknowledgment, and it is a
#: genuine ancestor of every later run pin — a pre-merge branch sha would not be.
ACKNOWLEDGED_REVISION = "46b000e878a60dda9d4fa215c0212ade78e4eede"
#: Families the sequence-1 amendment introduced.  Withheld until 2026-08-15;
#: released by the founder acknowledgment recorded in the receipt.
AMENDED_FAMILIES = ("f15", "f16", "f17", "f18", "f19")
#: Sequence 2 (no-nudge). Registered by the same §7 path and withheld by the
#: same receipt gate, because its acknowledgment has not landed.
SEQUENCE_TWO_FAMILIES = ("f20", "f21", "f22", "f23", "f24", "f25", "f26")
#: Sequence 3 (lifecycle replay). Registered by the same §7 path, withheld until
#: the founder acknowledgment recorded in its receipt on 2026-08-30.
SEQUENCE_THREE_FAMILIES = ("f27",)
#: The squash commit on ``main`` carrying the sequence-3 amended document and its
#: then-pending receipt (#762), pinned by the founder at acknowledgment.
SEQUENCE_THREE_ACKNOWLEDGED_REVISION = "287b984418ff3a02b26e05aafeb3bcbae255b27b"
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


def _snapshot(ref: str):
    """One neutral projected state snapshot, enough to bind an expectation.

    Only needed since acknowledgment: while the amendment was pending,
    ``evaluate_scenario`` refused an f15-f19 scenario before it ever looked at
    the snapshots, so no state was required to prove the refusal.
    """

    from epistemic.snapshot import (
        EpistemicStateSnapshot,
        FieldDeclaration,
        ProjectorMeta,
        StateItem,
    )

    return EpistemicStateSnapshot(
        provider="fixture",
        phase=ref,
        taken_at="2026-08-15T00:00:00Z",
        items=(StateItem(id="claim", kind="claim", title="claim", text="text", current="yes"),),
        declarations=tuple(
            FieldDeclaration(
                field=field,
                status="declared",
                evidence=f"benchmarks/epistemic/PREREGISTRATION.md:39 ({field})",
            )
            for field in (
                "kind",
                "current",
                "external_edit",
                "locator",
                "export",
                "review_state",
            )
        ),
        projector=ProjectorMeta(
            name="fixture-projector",
            version="0.1.0",
            author="benchmark-harness",
            endpoints_used=("fixture:in-memory",),
            loc=1,
        ),
    )


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


def test_real_loop_closure_receipt_records_the_founder_acknowledgment() -> None:
    """The one decision the contract reserved for the founder, now recorded.

    Acknowledgment adds exactly four fields and changes nothing else: the
    amendment's substance was frozen when it was written, and
    ``test_receipt_git_history_refuses_later_amendment_receipt_mutation`` is what
    holds the rest of the receipt — ``effective_policy`` included — immutable
    across the transition.
    """

    from protocol.contracts import AmendmentReceipt, working_amendment_receipts

    receipt = AmendmentReceipt.model_validate_json(LOOP_CLOSURE_RECEIPT.read_bytes())
    assert receipt.parent_contract_sha256 == (
        "21aa5a8815038b82358336798b10afd8d3ffbd9739c8da597955bd14d8d962e3"
    )
    # Sequence 1's digest binds the document *as it stood at sequence 1*, which
    # stopped being the working bytes the moment sequence 2 amended it. The
    # binding that still has to hold is the chain link: the next receipt's parent
    # digest is this receipt's contract digest, and nothing may edit the document
    # between them without a receipt of its own.
    chain = working_amendment_receipts(ROOT)
    assert chain[0].contract_sha256 == receipt.contract_sha256
    assert chain[1].parent_contract_sha256 == receipt.contract_sha256
    assert receipt.ratifier == FOUNDER
    assert receipt.acknowledged_on == "2026-08-15"
    assert receipt.catastrophic_set_decision == "accept"
    assert receipt.repository_revision == ACKNOWLEDGED_REVISION
    assert receipt.acknowledgment_status == "acknowledged"
    # Unchanged from introduction, and deliberately so.  The sentence describes
    # the policy the amendment carries, not the amendment's current status; the
    # status lives in the four fields above.  Rewriting it here would put the
    # receipt outside the single allowed pending-to-acknowledged transition and
    # break the very identity chain this acknowledgment closes.
    assert receipt.effective_policy == (
        "Applies only after founder acknowledgment; until then f15-f19 cannot "
        "support comparative runs or claims."
    )


def test_manifest_schema_constrains_each_lineage_receipt_identity_to_sha256() -> None:
    import json

    schema = json.loads(
        (ROOT / "benchmarks/protocol/schema/run-manifest.v2.schema.json").read_text()
    )
    items = schema["$defs"]["PreregistrationLineage"]["properties"][
        "amendment_receipt_sha256s"
    ]["items"]
    assert items["pattern"] == "^[0-9a-f]{64}$"


def test_real_working_chain_folds_after_acknowledgment() -> None:
    """Acknowledgment changes the gate, not the digests.

    The chain folded from the ratified base to the working bytes while the
    receipt was pending, and it folds to the identical digest now: acknowledgment
    is a statement about *use*, and touches nothing the fold depends on.  That it
    is unchanged here is the evidence that the founder's four fields did not
    disturb the contract identity.
    """

    from protocol.contracts import (
        AmendmentReceipt,
        validate_working_preregistration,
        working_amendment_receipts,
    )

    receipt = AmendmentReceipt.model_validate_json(LOOP_CLOSURE_RECEIPT.read_bytes())
    assert receipt.acknowledgment_status == "acknowledged"
    # The fold culminates in the *last* receipt's document. Acknowledgment still
    # touches nothing the fold depends on: sequence 3 folded exactly the same
    # way before and after its own acknowledgment landed, which is the property
    # this test was written to hold.
    chain = working_amendment_receipts(ROOT)
    assert validate_working_preregistration(ROOT) == chain[-1].contract_sha256
    assert chain[-1].acknowledgment_status == "acknowledged"


def test_acknowledged_amendment_derives_a_complete_typed_identity() -> None:
    """Derivation now takes the amended-document revision from the founder.

    While pending, the amended-document revision was *reconstructed* from the
    receipt's unique introduction commit, because the founder could not pin one
    yet — a pre-merge branch sha does not survive a squash merge.  Acknowledgment
    supplies the real one, so the two now diverge: the contract revision is the
    merged commit that carries the amended document, while the receipt's
    introduction revision is the later commit that acknowledged it.
    """

    from protocol.contracts import derive_preregistration_identity

    identity = derive_preregistration_identity(ROOT)

    # Pinned exactly, not loosened to an inequality: a chain that silently grew
    # a fourth link would otherwise satisfy every assertion below while nobody
    # had adjudicated the new one.
    assert len(identity.amendments) == 3
    amendment = identity.amendments[0]
    assert amendment.acknowledgment_status == "acknowledged"
    assert amendment.introduced_family_ids == ("f15", "f16", "f17", "f18", "f19")
    assert amendment.contract.repository_revision == ACKNOWLEDGED_REVISION
    assert amendment.contract.repository_revision != amendment.receipt.introduction_revision
    assert amendment.sequence not in {a.sequence for a in identity.pending_amendments}

    # Sequence 2 is the *pending* half of the same chain, and its presence is
    # what proves the two shapes derive side by side: an acknowledged amendment
    # pins its amended revision from the founder, while a pending one has that
    # revision reconstructed from its unique introduction commit. Sequence 3
    # crossed from one shape to the other on 2026-08-30.
    pending_two, acknowledged_three = identity.amendments[1], identity.amendments[2]
    assert (pending_two.sequence, acknowledged_three.sequence) == (2, 3)
    assert pending_two.acknowledgment_status == "pending"
    assert pending_two.contract.repository_revision == pending_two.receipt.introduction_revision
    assert acknowledged_three.acknowledgment_status == "acknowledged"
    assert (
        acknowledged_three.contract.repository_revision
        == SEQUENCE_THREE_ACKNOWLEDGED_REVISION
    )
    assert (
        acknowledged_three.contract.repository_revision
        != acknowledged_three.receipt.introduction_revision
    )
    assert pending_two.introduced_family_ids == SEQUENCE_TWO_FAMILIES
    assert acknowledged_three.introduced_family_ids == SEQUENCE_THREE_FAMILIES
    assert identity.effective.sha256 == acknowledged_three.contract.sha256
    assert identity.pending_amendments == (pending_two,)
    assert identity.withheld_family_ids == frozenset(SEQUENCE_TWO_FAMILIES)


def test_the_acknowledged_amendment_releases_its_own_families() -> None:
    """The gate that refused f15-f19 now lets every one of them through.

    This is the whole observable effect of the acknowledgment at the contract
    layer: the same call, on the same families, that raised
    ``AmendmentAcknowledgmentPendingError`` before 2026-08-15 now returns. It
    withholds nothing *of sequence 1*; sequence 2 is a separate, still-pending
    receipt and is refused by
    :func:`test_the_loader_gate_releases_sequence_one_and_withholds_sequence_two`
    below, so the old name for this test — ``withholds_nothing`` — became a claim
    it never made.
    """

    from protocol.contracts import (
        derive_preregistration_identity,
        require_amended_families_released,
    )

    identity = derive_preregistration_identity(ROOT)

    require_amended_families_released(identity, ())
    require_amended_families_released(identity, ("f01", "f07", "f14"))
    for family_id in AMENDED_FAMILIES:
        require_amended_families_released(identity, (family_id,))
    require_amended_families_released(identity, ("f01", *AMENDED_FAMILIES))


def test_the_pending_refusal_is_still_armed_for_a_future_amendment() -> None:
    """Release is a property of *this* receipt, not a retired mechanism.

    Sequence 1 is acknowledged, so its own families are no longer withheld.  The
    refusal that withheld them must still fire for an unacknowledged amendment —
    it does, for the real sequence 2 — and it must keep firing for whatever comes
    after, which is asserted here against a synthetic identity so the property
    does not depend on there happening to be a pending receipt in the tree.
    """

    from protocol.contracts import (
        AmendmentAcknowledgmentPendingError,
        AmendmentIdentity,
        derive_preregistration_identity,
        require_amended_families_released,
    )

    identity = derive_preregistration_identity(ROOT)
    still_pending = identity.model_copy(
        update={
            "amendments": (
                identity.amendments[0].model_copy(
                    update={"acknowledgment_status": "pending"}
                ),
            )
        }
    )
    assert isinstance(still_pending.amendments[0], AmendmentIdentity)
    assert still_pending.withheld_family_ids == frozenset(AMENDED_FAMILIES)

    require_amended_families_released(still_pending, ("f01", "f07", "f14"))
    for family_id in AMENDED_FAMILIES:
        with pytest.raises(
            AmendmentAcknowledgmentPendingError,
            match=rf"amendment sequence 1 .*pending.*{family_id}",
        ):
            require_amended_families_released(still_pending, (family_id,))


def test_acknowledged_amendment_is_recorded_on_every_run_manifest(
    tmp_path,
) -> None:
    """The acknowledgment is legible in the artifact, not just in the gate."""

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
    # A reader of this manifest can see the run executed against an acknowledged
    # contract state — the same field that read "pending" before 2026-08-15.
    assert (
        manifest.preregistration_identity.amendments[0].acknowledgment_status
        == "acknowledged"
    )
    # Sequence 2's pending status rides on the same manifest. A reader can tell
    # which families backed this run and which were withheld from it without
    # reading any other artifact, which is the whole reason the field exists.
    # Sequence 3 left the withheld set when its acknowledgment landed 2026-08-30.
    assert manifest.preregistration_identity.withheld_family_ids == frozenset(
        SEQUENCE_TWO_FAMILIES
    )
    assert manifest.preregistration_lineage is not None


def test_acknowledged_amendment_admits_a_run_manifest_declaring_an_amended_family(
    tmp_path,
) -> None:
    """The released guarantee: a run may now declare f15-f19 and be recorded."""

    from protocol.manifest import start_manifest

    dataset = {
        "id": "fixture",
        "variant": "mini",
        "source": "local",
        "revision": "1",
        "sha256": "a" * 64,
        "case_count": 1,
    }

    started = start_manifest(
        tmp_path / "amended",
        run_id="amended",
        dataset=dataset,
        started_at="2026-08-15T00:00:00Z",
        family_ids=("f01", *AMENDED_FAMILIES),
    )
    assert started.status == "started"
    # The artifact this call used to refuse to create now exists on disk.
    assert (tmp_path / "amended" / "manifest.json").exists()

    ratified_only = start_manifest(
        tmp_path / "released",
        run_id="released",
        dataset=dataset,
        started_at="2026-08-15T00:00:00Z",
        family_ids=("f01", "f14"),
    )
    assert ratified_only.status == "started"


def test_acknowledged_amendment_admits_a_claim_read_back_from_a_manifest(
    tmp_path,
) -> None:
    """A recorded manifest may now be replayed into an f15-f19 claim."""

    from protocol.manifest import finalize_manifest, load_manifest, start_manifest

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
        family_ids=("f01", *AMENDED_FAMILIES),
    )
    finalize_manifest(run_dir, status="VALID", finalized_at="2026-08-15T00:01:00Z")

    assert load_manifest(run_dir).run_id == "recorded"
    assert load_manifest(run_dir, family_ids=("f01",)).run_id == "recorded"
    for family_id in AMENDED_FAMILIES:
        assert load_manifest(run_dir, family_ids=(family_id,)).run_id == "recorded"


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

    for family_id in AMENDED_FAMILIES:
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


def test_acknowledged_amendment_lets_an_amended_family_scenario_load() -> None:
    """The point of the acknowledgment: f15-f19 scenarios now exist.

    This is the same loader call, on the same payloads, that raised
    ``ScenarioLoadError: ... amendment sequence 1 ... pending`` for every one of
    these families until 2026-08-15.  Nothing downstream can run, score or claim
    a family it cannot first load, so this succeeding is what "released" means
    in practice.

    The payloads are built in-test by :func:`_scenario_payload` rather than read
    from ``benchmarks/epistemic/fixtures/``: no f15-f19 scenario fixtures exist
    yet, and authoring them is a separate change.  The loader exercised here is
    the real one.
    """

    from epistemic.schema import load_scenario_text

    for family_id in AMENDED_FAMILIES:
        scenario = load_scenario_text(
            _scenario_yaml(family_id, "refuted_retrievable_at_full_standing"),
            source=f"{family_id}.yaml",
        )
        assert scenario.family_id == family_id


def test_the_loader_gate_releases_sequences_one_and_three_and_withholds_two() -> None:
    """The loader's own view of release, asserted directly at the gate.

    ``epistemic.amendments`` answers "is amendment N acknowledged?" from the
    working receipt bytes without Git, which is the cheap path the loader takes
    per scenario.  Sequences 1 and 3 are acknowledged, so f15-f19 and f27 pass
    the gate; sequence 2 is unacknowledged, so f20-f26 are refused by the very
    same call, naming their own sequence.  Holding all of it in one test is the
    point: release is a property of each receipt, not a switch that was flipped
    once and left on.
    """

    from epistemic.amendments import (
        require_family_released,
        reset_cache,
        withheld_family_ids,
    )
    from protocol.contracts import AmendmentAcknowledgmentPendingError

    reset_cache()
    assert withheld_family_ids(ROOT) == frozenset(SEQUENCE_TWO_FAMILIES)
    for family_id in AMENDED_FAMILIES + SEQUENCE_THREE_FAMILIES:
        require_family_released(family_id, repo_root=ROOT)
    for family_id in SEQUENCE_TWO_FAMILIES:
        with pytest.raises(
            AmendmentAcknowledgmentPendingError,
            match=rf"amendment sequence 2 .*pending.*{family_id}",
        ):
            require_family_released(family_id, repo_root=ROOT)


def test_ratified_base_families_still_load() -> None:
    """The release must not disturb the families that were never withheld."""

    from epistemic.schema import load_scenario_text

    scenario = load_scenario_text(
        _scenario_yaml("f01", "evidence_path_exists"), source="f01.yaml"
    )
    assert scenario.family_id == "f01"


def test_declared_scenario_families_reach_the_manifest_gate(tmp_path) -> None:
    """The manifest still names every declared family — it now admits them all.

    The gate is unchanged and still consulted; what changed is its answer. The
    families a run declares are still enumerated to the manifest, so a future
    unacknowledged amendment is refused here exactly as sequence 1 once was.
    """

    from epistemic.manifest import (
        declared_family_ids,
        load_epistemic_manifest,
        start_epistemic_manifest,
    )
    from epistemic.schema import Scenario
    from protocol.manifest import finalize_manifest

    amended = Scenario.model_validate_json(
        _scenario_yaml("f18", "refuted_retrievable_at_full_standing")
    )
    ratified = Scenario.model_validate_json(_scenario_yaml("f01", "evidence_path_exists"))

    assert declared_family_ids((ratified, amended)) == ("f01", "f18")

    run_dir = tmp_path / "amended"
    started = start_epistemic_manifest(
        run_dir,
        run_id="amended",
        dataset=DATASET,
        started_at="2026-08-15T00:00:00Z",
        scenarios=(ratified, amended),
    )
    assert started.status == "started"
    assert (run_dir / "manifest.json").exists()
    finalize_manifest(run_dir, status="VALID", finalized_at="2026-08-15T00:01:00Z")

    assert load_epistemic_manifest(run_dir, scenarios=(ratified,)).run_id == "amended"
    assert (
        load_epistemic_manifest(run_dir, scenarios=(ratified, amended)).run_id
        == "amended"
    )


def test_every_execution_path_now_reaches_an_amended_family() -> None:
    """The release, audited across the same surfaces the withholding closed.

    Task 3.6 enumerated every way to run, score or record a family and wired the
    receipt gate into each. Those are the surfaces that must open together: a
    release that reached the loader but not the scorer would leave f15-f19
    loadable and unscoreable, which is not what the founder acknowledged.
    """

    from epistemic.runner import evaluate_scenario
    from epistemic.schema import Scenario, load_scenario_text
    from epistemic.scoring import assemble_family

    # 1. Loading a scenario — the choke point nothing downstream can skip.
    loaded = load_scenario_text(
        _scenario_yaml("f18", "evidence_path_exists"), source="f18.yaml"
    )
    assert loaded.family_id == "f18"

    # 2. Evaluating a Scenario, which the pending gate refused outright.
    hand_built = Scenario.model_validate_json(_scenario_yaml("f18", "evidence_path_exists"))
    evaluated = evaluate_scenario(hand_built, snapshots={"s1": _snapshot("s1")})
    assert [bound.assertion for bound in evaluated.assertions] == ["evidence_path_exists"]
    assert [bound.phase_id for bound in evaluated.assertions] == ["p1"]

    # 3. Scoring a family row, which takes a bare id and IS the comparative claim.
    assert assemble_family(family_id="f18", provider="fixture").family_id == "f18"

    # Ratified-base families keep working at every one of those surfaces.
    assert load_scenario_text(
        _scenario_yaml("f01", "evidence_path_exists"), source="f01.yaml"
    ).family_id == "f01"
    assert assemble_family(family_id="f01", provider="fixture").family_id == "f01"
