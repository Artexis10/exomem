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


def test_real_working_chain_refuses_with_typed_pending_state() -> None:
    from protocol.contracts import (
        AmendmentAcknowledgmentPendingError,
        validate_working_preregistration,
    )

    with pytest.raises(
        AmendmentAcknowledgmentPendingError,
        match="amendment sequence 1 founder acknowledgment is pending",
    ):
        validate_working_preregistration(ROOT)
