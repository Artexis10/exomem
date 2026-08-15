"""Immutable pre-registration receipt-chain reconstruction.

The manifest never accepts a caller-authored digest or amendment subset.  It
pins a repository revision and this module independently reads contract bytes
at each receipt's named revision plus immutable receipt bytes at the receipt's
later, uniquely reconstructed introduction commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

CONTRACT_PATH = "benchmarks/epistemic/PREREGISTRATION.md"
CONTRACTS_DIR = "benchmarks/epistemic/contracts"
RATIFICATION_RECEIPT_PATH = f"{CONTRACTS_DIR}/ratification.v1.json"
RATIFICATION_REPOSITORY_REVISION = "7cd15e6d6c67eb914e4f57bd943f98f7d1894b7f"
RATIFICATION_CONTRACT_SHA256 = "21aa5a8815038b82358336798b10afd8d3ffbd9739c8da597955bd14d8d962e3"
RATIFIER = "Hugo Ander Kivi"
RATIFIED_ON = "2026-08-11"
# Filled from the committed receipt bytes.  This root is needed only for the
# bootstrap checkout whose ratification revision predates the additive receipt.
RATIFICATION_RECEIPT_SHA256 = "31b74c6cdd69504da31af903e8464177f35fbf525f655c49e0e92e1f9862e5c6"

_SHA256 = r"^[0-9a-f]{64}$"
_REVISION = r"^[0-9a-f]{40}$"
_AMENDMENT_NAME = re.compile(
    r"^amendment-(?:(?P<sequence>[0-9]{4})|(?P<slug>[0-9]{4}-[0-9]{2}-[a-z0-9][a-z0-9-]*))\.v1\.json$"
)
#: A §1 scenario-family table row.  Used only to work out which families an
#: amendment introduced, by differencing its document against its parent's.
_FAMILY_ROW = re.compile(r"^\|\s*(f[0-9]{2})\s*\|", re.MULTILINE)
_SECTION_ONE = "## 1. Scenario families"
_SECTION_TWO = "## 2."


class ContractIdentityError(ValueError):
    """The pinned receipt chain cannot be reconstructed exactly."""


class AmendmentAcknowledgmentPendingError(ContractIdentityError):
    """An amendment exists but has not received complete founder acknowledgment."""


class AmendmentChainMissingError(ContractIdentityError):
    """Working bytes changed without an amendment receipt."""


class AmendmentChainOrderError(ContractIdentityError):
    """Amendment receipts are not a strict contiguous sequence."""


class AmendmentChainMismatchError(ContractIdentityError):
    """An amendment digest does not bind the preceding or current document."""


class PreregistrationDriftError(ContractIdentityError):
    """Working pre-registration bytes match neither the base nor a valid chain."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AmendmentReceiptContract(_StrictModel):
    artifact_type: Literal["preregistration-amendment-receipt.v1"]
    schema_version: Literal[1]
    ordering: Literal["strict_contiguous_sequence"]
    parent_binding: Literal["previous_whole_document_sha256"]
    applicability: Literal["repository_ancestry_through_contract_revision"]


class RatificationReceipt(_StrictModel):
    artifact_type: Literal["preregistration-ratification-receipt.v1"]
    schema_version: Literal[1]
    sequence: Literal[0]
    contract_path: str = Field(min_length=1)
    contract_sha256: str = Field(pattern=_SHA256)
    decision: Literal["ratified"]
    ratifier: str = Field(min_length=1)
    ratified_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    repository_revision: str = Field(pattern=_REVISION)
    amendment_contract: AmendmentReceiptContract


class AmendmentReceipt(_StrictModel):
    artifact_type: Literal["preregistration-amendment-receipt.v1"]
    schema_version: Literal[1]
    sequence: int = Field(ge=1)
    contract_path: str = Field(min_length=1)
    parent_contract_sha256: str = Field(pattern=_SHA256)
    contract_sha256: str = Field(pattern=_SHA256)
    repository_revision: str | None = Field(default=None, pattern=_REVISION)
    amended_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    affected_sections: tuple[str, ...] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    effective_policy: str = Field(min_length=1)
    ratifier: str | None = Field(default=None, min_length=1)
    acknowledged_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    catastrophic_set_decision: Literal["accept", "strike"] | None = None

    @field_validator("affected_sections")
    @classmethod
    def _sections_are_nonblank_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("affected sections must be nonblank and unique")
        return value

    @model_validator(mode="after")
    def _acknowledgment_is_complete_or_pending(self) -> AmendmentReceipt:
        if (self.ratifier is None) != (self.acknowledged_on is None):
            raise ValueError(
                "founder acknowledgment must provide both ratifier and acknowledged_on"
            )
        if self.ratifier is None and self.catastrophic_set_decision is not None:
            raise ValueError("catastrophic-set decision belongs to founder acknowledgment")
        if self.ratifier is not None and self.ratifier != RATIFIER:
            raise ValueError("ratifier differs from the pinned founder identity")
        if self.ratifier is not None and self.repository_revision is None:
            raise ValueError(
                "amended-document repository revision is required at acknowledgment"
            )
        if (
            self.ratifier is not None
            and any("§3" in section and "candidacy" in section for section in self.affected_sections)
            and self.catastrophic_set_decision is None
        ):
            raise ValueError("catastrophic-set candidacy requires an explicit decision")
        return self

    @property
    def acknowledgment_status(self) -> Literal["pending", "acknowledged"]:
        return "pending" if self.ratifier is None else "acknowledged"

    def require_acknowledged(self) -> None:
        """Assert this receipt is acknowledged, for a caller that needs all of it.

        Deliberately **not** called by chain folding or identity derivation.
        Gating those on acknowledgment turned one pending receipt into a
        repository-wide refusal, which is broader than anything the receipt
        claims.  Acknowledgment gates the use of the families the amendment
        introduced — :func:`require_amended_families_released` — and this
        remains the whole-receipt primitive for a caller whose own contract is
        "no part of this amendment applies until it is acknowledged".
        """

        if self.acknowledgment_status == "pending":
            raise AmendmentAcknowledgmentPendingError(
                f"amendment sequence {self.sequence} founder acknowledgment is pending"
            )


class ContractArtifactIdentity(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256)
    repository_revision: str = Field(pattern=_REVISION)


class ReceiptIdentity(_StrictModel):
    receipt_path: str = Field(min_length=1)
    receipt_sha256: str = Field(pattern=_SHA256)
    introduction_revision: str | None = Field(pattern=_REVISION)


class AmendmentIdentity(_StrictModel):
    sequence: int = Field(ge=1)
    receipt: ReceiptIdentity
    parent_contract_sha256: str = Field(pattern=_SHA256)
    contract: ContractArtifactIdentity
    affected_sections: tuple[str, ...]
    rationale: str
    effective_policy: str
    #: Recorded rather than refused.  A pending amendment is a legible fact about
    #: the contract a run executed against, so every manifest carries it.
    acknowledgment_status: Literal["pending", "acknowledged"]
    #: §1 family ids this amendment added to its parent document, in file order.
    #: While the amendment is pending these are exactly the families withheld
    #: from comparative runs, scores and claims.
    introduced_family_ids: tuple[str, ...]


class PreregistrationIdentity(_StrictModel):
    contract_revision: str = Field(pattern=_REVISION)
    original: ContractArtifactIdentity
    ratification: ReceiptIdentity
    amendments: tuple[AmendmentIdentity, ...] = ()
    effective: ContractArtifactIdentity

    @model_validator(mode="after")
    def _chain_is_self_consistent(self) -> PreregistrationIdentity:
        bootstrap = (
            self.contract_revision == RATIFICATION_REPOSITORY_REVISION
            and self.original.repository_revision == RATIFICATION_REPOSITORY_REVISION
            and not self.amendments
        )
        if (self.ratification.introduction_revision is None) != bootstrap:
            raise ValueError(
                "ratification receipt introduction may be null only at the exact bootstrap pin"
            )
        expected = self.original.sha256
        for ordinal, amendment in enumerate(self.amendments, 1):
            if amendment.sequence != ordinal or amendment.parent_contract_sha256 != expected:
                raise ValueError("pre-registration identity has an incomplete or out-of-order chain")
            if amendment.receipt.introduction_revision is None:
                raise ValueError("amendment receipt introduction revision is required")
            expected = amendment.contract.sha256
        if self.effective.sha256 != expected:
            raise ValueError("effective contract identity does not match the ordered chain")
        return self

    @property
    def pending_amendments(self) -> tuple[AmendmentIdentity, ...]:
        """Amendments in this identity that the founder has not yet acknowledged."""

        return tuple(
            amendment
            for amendment in self.amendments
            if amendment.acknowledgment_status == "pending"
        )

    @property
    def withheld_family_ids(self) -> frozenset[str]:
        """Families a pending amendment introduced and therefore still withholds."""

        return frozenset(
            family_id
            for amendment in self.pending_amendments
            for family_id in amendment.introduced_family_ids
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _section_one_family_ids(document: bytes, *, label: str) -> tuple[str, ...]:
    """The §1 scenario-family ids declared by one pre-registration document.

    A document with no §1 section declares no families, which is a fact and not
    an error — the derivation is differential, so an amendment that *introduces*
    the table has every one of its rows counted as introduced.  A §1 section
    that exists but yields nothing parsable is a different matter: that is a
    malformed contract, and it refuses rather than silently releasing families.
    """

    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractIdentityError(f"{label}: contract document is not UTF-8") from exc
    start = text.find(_SECTION_ONE)
    if start == -1:
        return ()
    end = text.find(_SECTION_TWO, start)
    section = text[start:] if end == -1 else text[start:end]
    rows = tuple(match.group(1) for match in _FAMILY_ROW.finditer(section))
    if not rows:
        raise ContractIdentityError(f"{label}: §1 scenario-family table has no parsable rows")
    if len(set(rows)) != len(rows):
        raise ContractIdentityError(f"{label}: §1 scenario-family table repeats a family id")
    return rows


def require_amended_families_released(
    identity: PreregistrationIdentity,
    family_ids: Iterable[str],
) -> None:
    """Refuse the *use* of a family whose amendment is still unacknowledged.

    This is the whole of what a pending amendment blocks.  Identity validation,
    chain folding, lineage recording and every consumer that does not name an
    amended family all proceed normally — a pending amendment is a fact about
    the contract, not a repository outage.  What it withholds is exact and
    narrow: the families the amendment introduced may not back a comparative
    run, a score, or a published claim until the founder acknowledges the
    receipt, which is precisely the receipt's own ``effective_policy``.
    """

    requested = tuple(dict.fromkeys(family_ids))
    if not requested:
        return
    for amendment in identity.pending_amendments:
        withheld = tuple(
            family_id
            for family_id in requested
            if family_id in set(amendment.introduced_family_ids)
        )
        if withheld:
            raise AmendmentAcknowledgmentPendingError(
                f"amendment sequence {amendment.sequence} founder acknowledgment is "
                f"pending; {', '.join(withheld)} may not back a comparative run, "
                "score or claim"
            )


def _strict_json(data: bytes, *, label: str) -> dict[str, object]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ContractIdentityError(f"{label}: duplicate JSON member {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractIdentityError(f"{label}: invalid receipt JSON") from exc
    if not isinstance(value, dict):
        raise ContractIdentityError(f"{label}: receipt must be an object")
    return value


def fold_amendment_chain(
    receipts: Iterable[AmendmentReceipt],
    *,
    base_sha256: str,
    current_sha256: str,
) -> str:
    """Fold receipt identities from a frozen base to working bytes.

    Digest identity only.  Acknowledgment is deliberately *not* checked here: a
    receipted amendment binds its parent and its own document by sha256 the
    moment it is written, and that binding is what a drift check is for.  Gating
    the fold on acknowledgment made an unacknowledged amendment indistinguishable
    from a silently edited pre-registration and turned one pending receipt into a
    repository-wide refusal.  Acknowledgment gates the *use* of the amended
    families instead — see :func:`require_amended_families_released`.
    """

    chain = tuple(receipts)
    if not chain:
        if current_sha256 == base_sha256:
            return base_sha256
        raise AmendmentChainMissingError(
            "amendment receipt missing: "
            f"expected base identity {base_sha256}; actual identity {current_sha256}"
        )

    expected_sha256 = base_sha256
    for expected_sequence, receipt in enumerate(chain, 1):
        if receipt.sequence != expected_sequence:
            raise AmendmentChainOrderError(
                "amendment chain out of order: "
                f"expected sequence {expected_sequence}; actual {receipt.sequence}"
            )
        if receipt.parent_contract_sha256 != expected_sha256:
            raise AmendmentChainMismatchError(
                f"amendment sequence {receipt.sequence} parent mismatch: "
                f"expected identity {expected_sha256}; "
                f"actual identity {receipt.parent_contract_sha256}"
            )
        expected_sha256 = receipt.contract_sha256

    if expected_sha256 != current_sha256:
        raise AmendmentChainMismatchError(
            "amendment chain does not culminate in the working document: "
            f"expected identity {expected_sha256}; actual identity {current_sha256}"
        )
    return expected_sha256


def validate_preregistration_bytes(
    data: bytes,
    receipts: Iterable[AmendmentReceipt],
    *,
    base_sha256: str = RATIFICATION_CONTRACT_SHA256,
) -> str:
    """Require working bytes to equal the frozen base or a receipted chain."""

    actual_sha256 = _sha256(data)
    try:
        return fold_amendment_chain(
            receipts,
            base_sha256=base_sha256,
            current_sha256=actual_sha256,
        )
    except (AmendmentChainMissingError, AmendmentChainOrderError, AmendmentChainMismatchError) as exc:
        raise PreregistrationDriftError(
            "working pre-registration drift: "
            f"expected base identity {base_sha256} or a receipted amendment chain; "
            f"actual identity {actual_sha256}; {exc}"
        ) from exc


def order_amendment_receipt_rows(
    rows: Iterable[tuple[str, bytes]],
) -> tuple[tuple[str, bytes], ...]:
    """Validate amendment receipt rows and order them by declared sequence."""

    parsed: list[tuple[int, str, bytes]] = []
    for name, data in rows:
        match = _AMENDMENT_NAME.fullmatch(name)
        if match is None:
            raise ContractIdentityError(f"amendment receipt name is invalid: {name}")
        try:
            _strict_json(data, label=name)
            receipt = AmendmentReceipt.model_validate_json(data)
        except ValidationError as exc:
            raise ContractIdentityError(
                f"amendment receipt schema invalid: {name}: {exc}"
            ) from exc
        filename_sequence = match.group("sequence")
        if filename_sequence is not None and receipt.sequence != int(filename_sequence):
            raise AmendmentChainOrderError("amendment filename and sequence differ")
        parsed.append((receipt.sequence, name, data))
    if len({sequence for sequence, _name, _data in parsed}) != len(parsed):
        raise AmendmentChainOrderError("amendment chain contains duplicate sequences")
    return tuple((name, data) for _sequence, name, data in sorted(parsed))


def _working_amendment_receipts(root: Path) -> tuple[AmendmentReceipt, ...]:
    contracts_dir = root / CONTRACTS_DIR
    try:
        paths = sorted(contracts_dir.glob("amendment-*.v1.json"))
    except OSError as exc:
        raise ContractIdentityError("working amendment receipts cannot be discovered") from exc
    rows: list[tuple[str, bytes]] = []
    for path in paths:
        if _AMENDMENT_NAME.fullmatch(path.name) is None:
            raise ContractIdentityError(f"working amendment receipt name is invalid: {path.name}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ContractIdentityError(
                f"working amendment receipt cannot be read: {path.name}"
            ) from exc
        rows.append((path.name, data))
    ordered = order_amendment_receipt_rows(rows)
    return tuple(AmendmentReceipt.model_validate_json(data) for _name, data in ordered)


def working_amendment_receipts(repo_root: Path | str) -> tuple[AmendmentReceipt, ...]:
    """The working-tree amendment receipts, ordered by declared sequence.

    File I/O only — no Git. A consumer that needs to know whether an amendment
    is acknowledged (rather than to reconstruct its full pinned identity) can
    answer that from the receipts alone, which keeps the acknowledgment check
    available in a checkout where Git history is unavailable.
    """

    return _working_amendment_receipts(Path(repo_root).resolve())


def validate_working_preregistration(
    repo_root: Path | str,
) -> str:
    """Validate the live pre-registration and local receipt chain before a run."""

    root = Path(repo_root).resolve()
    try:
        data = (root / CONTRACT_PATH).read_bytes()
    except OSError as exc:
        raise PreregistrationDriftError(
            f"working pre-registration is missing: expected {CONTRACT_PATH}"
        ) from exc
    return validate_preregistration_bytes(data, _working_amendment_receipts(root))


def derive_identity_from_receipt_bytes(
    receipts: Iterable[tuple[str, bytes]],
    *,
    contract_revision: str,
    read_git_artifact: Callable[[str, str], bytes],
    is_revision_applicable: Callable[[str, str], bool],
    receipt_introduction_revisions: Callable[[str, str], Iterable[str]],
    expected_ratifier: str | None = None,
    expected_repository_revision: str | None = None,
) -> PreregistrationIdentity:
    """Pure chain derivation over independently supplied receipt and Git bytes."""

    receipt_rows = tuple(receipts)
    if not receipt_rows or receipt_rows[0][0] != "ratification.v1.json":
        raise ContractIdentityError("ordered receipt chain must begin with ratification.v1.json")
    if len({name for name, _data in receipt_rows}) != len(receipt_rows):
        raise ContractIdentityError("duplicate receipt path in ordered chain")
    try:
        _strict_json(receipt_rows[0][1], label=receipt_rows[0][0])
        ratification = RatificationReceipt.model_validate_json(receipt_rows[0][1])
    except ValidationError as exc:
        raise ContractIdentityError(f"ratification receipt schema invalid: {exc}") from exc
    if expected_ratifier is not None and ratification.ratifier != expected_ratifier:
        raise ContractIdentityError("ratifier differs from the founder-ratified identity")
    if (
        expected_repository_revision is not None
        and ratification.repository_revision != expected_repository_revision
    ):
        raise ContractIdentityError("ratification repository revision differs")
    if not is_revision_applicable(ratification.repository_revision, contract_revision):
        raise ContractIdentityError("ratification repository revision is not applicable at the pin")
    try:
        original_bytes = read_git_artifact(
            ratification.repository_revision, ratification.contract_path
        )
    except Exception as exc:  # noqa: BLE001 - normalized contract refusal
        raise ContractIdentityError("ratified contract bytes are missing from the repository") from exc
    if _sha256(original_bytes) != ratification.contract_sha256:
        raise ContractIdentityError("ratification contract digest differs from Git bytes")

    original = ContractArtifactIdentity(
        path=ratification.contract_path,
        sha256=ratification.contract_sha256,
        repository_revision=ratification.repository_revision,
    )
    ratification_path = f"{CONTRACTS_DIR}/{receipt_rows[0][0]}"
    try:
        ratification_introductions = tuple(
            receipt_introduction_revisions(ratification_path, contract_revision)
        )
    except ContractIdentityError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalized contract refusal
        raise ContractIdentityError(
            "ratification receipt introduction history cannot be reconstructed"
        ) from exc
    if len(set(ratification_introductions)) != len(ratification_introductions) or any(
        re.fullmatch(_REVISION, revision) is None
        for revision in ratification_introductions
    ):
        raise ContractIdentityError("ratification receipt introduction history is invalid")
    ratification_introduction: str | None
    if contract_revision == ratification.repository_revision:
        if ratification_introductions:
            raise ContractIdentityError(
                "bootstrap ratification pin unexpectedly contains a receipt introduction"
            )
        ratification_introduction = None
    else:
        if len(ratification_introductions) != 1:
            raise ContractIdentityError(
                "ratification receipt history must have exactly one unique introduction commit"
            )
        ratification_introduction = ratification_introductions[0]
        if (
            ratification_introduction == ratification.repository_revision
            or not is_revision_applicable(
                ratification.repository_revision, ratification_introduction
            )
        ):
            raise ContractIdentityError(
                "ratification receipt introduction is not a strict descendant of the ratified revision"
            )
        if ratification_introduction != contract_revision and not is_revision_applicable(
            ratification_introduction, contract_revision
        ):
            raise ContractIdentityError(
                "ratification receipt introduction is not an ancestor of the pin"
            )
        try:
            introduced_ratification = read_git_artifact(
                ratification_introduction, ratification_path
            )
        except Exception as exc:  # noqa: BLE001 - normalized contract refusal
            raise ContractIdentityError(
                "ratification receipt is missing at its introduction commit"
            ) from exc
        if introduced_ratification != receipt_rows[0][1]:
            raise ContractIdentityError(
                "ratification receipt introduction bytes differ from bytes at the run pin"
            )

    expected_digest = original.sha256
    previous_receipt_introduction = ratification_introduction
    previous_document = original_bytes
    amendments: list[AmendmentIdentity] = []
    applicable_rows: list[tuple[str, bytes, AmendmentReceipt, str, str]] = []
    for name, data in receipt_rows[1:]:
        match = _AMENDMENT_NAME.fullmatch(name)
        if match is None:
            raise ContractIdentityError(f"ordered amendment receipt name is invalid: {name}")
        try:
            _strict_json(data, label=name)
            receipt = AmendmentReceipt.model_validate_json(data)
        except ValidationError as exc:
            raise ContractIdentityError(f"amendment receipt schema invalid: {exc}") from exc
        filename_sequence = match.group("sequence")
        if filename_sequence is not None and receipt.sequence != int(filename_sequence):
            raise ContractIdentityError("amendment filename and sequence differ")
        receipt_path = f"{CONTRACTS_DIR}/{name}"
        try:
            introductions = tuple(
                receipt_introduction_revisions(receipt_path, contract_revision)
            )
        except ContractIdentityError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized contract refusal
            raise ContractIdentityError(
                "amendment receipt introduction history cannot be reconstructed"
            ) from exc
        if (
            len(introductions) != 1
            or len(set(introductions)) != 1
            or re.fullmatch(_REVISION, introductions[0]) is None
        ):
            raise ContractIdentityError(
                "amendment receipt history must have exactly one unique introduction commit"
            )
        # A pending receipt has not pinned its amended-document revision: the
        # founder records that at acknowledgment, and cannot record it earlier
        # because a pre-merge branch sha does not survive a squash merge.  Until
        # then the receipt's uniquely reconstructed introduction commit *is* the
        # amended-document revision — the amendment and its receipt land in one
        # commit, which is exactly what the single-introduction rule proves.
        amended_revision = receipt.repository_revision or introductions[0]
        if not is_revision_applicable(amended_revision, contract_revision):
            raise ContractIdentityError(
                "amendment named amended-contract revision is not an ancestor of the pin"
            )
        applicable_rows.append((name, data, receipt, introductions[0], amended_revision))

    for expected_sequence, (name, data, receipt, introduction, amended_revision) in enumerate(
        applicable_rows, 1
    ):
        if receipt.sequence != expected_sequence:
            raise ContractIdentityError("amendment chain is not a strict contiguous ordered sequence")
        if receipt.contract_path != original.path:
            raise ContractIdentityError("amendment contract path differs from ratification")
        if receipt.parent_contract_sha256 != expected_digest:
            raise ContractIdentityError("amendment parent digest does not bind the previous document")
        if previous_receipt_introduction is None:
            raise ContractIdentityError(
                "an amendment cannot precede the ratification receipt introduction"
            )
        if (
            amended_revision == previous_receipt_introduction
            or not is_revision_applicable(
                previous_receipt_introduction, amended_revision
            )
        ):
            raise ContractIdentityError(
                "previous receipt introduction is not a strict ancestor of the "
                "amended-contract revision; ordered ancestry is broken"
            )
        receipt_path = f"{CONTRACTS_DIR}/{name}"
        # Only a receipt that *names* its amended-document revision can be
        # checked for strict descent: acknowledgment necessarily lands in a
        # later commit than the amendment.  A pending receipt's amended revision
        # is its own introduction, so descent is trivially satisfied and the
        # bytes-at-introduction check below is what binds it.
        if receipt.repository_revision is not None and (
            introduction == amended_revision
            or not is_revision_applicable(amended_revision, introduction)
        ):
            raise ContractIdentityError(
                "amendment receipt introduction is not a strict descendant of its "
                "named amended-contract revision"
            )
        if introduction != contract_revision and not is_revision_applicable(
            introduction, contract_revision
        ):
            raise ContractIdentityError(
                "amendment receipt introduction is not an ancestor of the pin"
            )
        try:
            historical_receipt = read_git_artifact(
                introduction, receipt_path
            )
        except Exception as exc:  # noqa: BLE001 - normalized contract refusal
            raise ContractIdentityError(
                "amendment receipt is missing at its introduction commit"
            ) from exc
        if historical_receipt != data:
            raise ContractIdentityError(
                "amendment receipt introduction bytes differ from bytes at the run pin"
            )
        try:
            document = read_git_artifact(amended_revision, receipt.contract_path)
        except Exception as exc:  # noqa: BLE001
            raise ContractIdentityError("amended contract bytes are missing from the repository") from exc
        if _sha256(document) != receipt.contract_sha256:
            raise ContractIdentityError("amendment contract digest differs from Git bytes")
        parent_family_ids = set(
            _section_one_family_ids(previous_document, label=f"{name} parent document")
        )
        introduced_family_ids = tuple(
            family_id
            for family_id in _section_one_family_ids(document, label=name)
            if family_id not in parent_family_ids
        )
        contract = ContractArtifactIdentity(
            path=receipt.contract_path,
            sha256=receipt.contract_sha256,
            repository_revision=amended_revision,
        )
        amendments.append(AmendmentIdentity(
            sequence=receipt.sequence,
            receipt=ReceiptIdentity(
                receipt_path=receipt_path,
                receipt_sha256=_sha256(data),
                introduction_revision=introduction,
            ),
            parent_contract_sha256=receipt.parent_contract_sha256,
            contract=contract,
            affected_sections=receipt.affected_sections,
            rationale=receipt.rationale,
            effective_policy=receipt.effective_policy,
            acknowledgment_status=receipt.acknowledgment_status,
            introduced_family_ids=introduced_family_ids,
        ))
        expected_digest = contract.sha256
        previous_receipt_introduction = introduction
        previous_document = document

    effective = amendments[-1].contract if amendments else original
    if previous_receipt_introduction is not None and (
        previous_receipt_introduction != contract_revision
        and not is_revision_applicable(previous_receipt_introduction, contract_revision)
    ):
        raise ContractIdentityError(
            "effective receipt introduction is not an ancestor of the contract pin"
        )
    try:
        pinned_document = read_git_artifact(contract_revision, original.path)
    except Exception as exc:  # noqa: BLE001 - normalized contract refusal
        raise ContractIdentityError("effective contract bytes are missing at the pin") from exc
    if _sha256(pinned_document) != effective.sha256:
        raise ContractIdentityError(
            "effective contract digest differs from the contract bytes at the pin"
        )
    return PreregistrationIdentity(
        contract_revision=contract_revision,
        original=original,
        ratification=ReceiptIdentity(
            receipt_path=ratification_path,
            receipt_sha256=_sha256(receipt_rows[0][1]),
            introduction_revision=ratification_introduction,
        ),
        amendments=tuple(amendments),
        effective=effective,
    )


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    discovered = shutil.which("git", path=os.defpath)
    if discovered is None:
        raise ContractIdentityError("trusted Git executable is unavailable")
    try:
        executable = Path(discovered).resolve(strict=True)
    except OSError as exc:
        raise ContractIdentityError("trusted Git executable cannot be resolved") from exc
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ContractIdentityError("trusted Git executable is not an executable file")
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    return subprocess.run(
        (str(executable), "-C", str(root), *args),
        check=check,
        capture_output=True,
        env=environment,
    )


def _git_show(root: Path, revision: str, path: str) -> bytes:
    try:
        return _git(root, "show", f"{revision}:{path}").stdout
    except subprocess.CalledProcessError as exc:
        raise ContractIdentityError(f"repository artifact missing at {revision}:{path}") from exc


def _git_applicable(root: Path, revision: str, pin: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", revision, pin, check=False).returncode == 0


def _git_actual_path_events(
    root: Path, commits: tuple[str, ...], path: str
) -> tuple[str, ...]:
    """Drop merge-only path artifacts while retaining events on every parent."""

    if not commits:
        return ()
    if len(set(commits)) != len(commits):
        raise ContractIdentityError("receipt history contains a duplicate Git revision")
    parent_rows = _git(
        root, "rev-list", "--parents", "--no-walk=unsorted", *commits
    ).stdout.decode().splitlines()
    parents_by_commit: dict[str, tuple[str, ...]] = {}
    for row in parent_rows:
        fields = row.split()
        if not fields or any(re.fullmatch(_REVISION, field) is None for field in fields):
            raise ContractIdentityError("receipt history contains an invalid Git revision")
        commit, *parents = fields
        if commit in parents_by_commit:
            raise ContractIdentityError("receipt history contains a duplicate Git revision")
        parents_by_commit[commit] = tuple(parents)
    if set(parents_by_commit) != set(commits):
        raise ContractIdentityError("receipt history parent graph is incomplete")
    if sum(len(parents) for parents in parents_by_commit.values()) > 256:
        raise ContractIdentityError("receipt history parent graph exceeds the audit limit")

    events: list[str] = []
    for commit in commits:
        parents = parents_by_commit[commit]
        if len(parents) <= 1:
            events.append(commit)
            continue
        for parent in parents:
            compared = _git(
                root,
                "diff",
                "--quiet",
                "--no-renames",
                parent,
                commit,
                "--",
                path,
                check=False,
            )
            if compared.returncode == 0:
                break
            if compared.returncode != 1:
                raise ContractIdentityError("receipt merge history cannot be reconstructed")
        else:
            events.append(commit)
    return tuple(events)


def _git_receipt_introduction_revisions(
    root: Path, pin: str, path: str
) -> tuple[str, ...]:
    """Return the bounded exact-path additions reachable from one run pin."""

    history_rows = tuple(
        _git(
            root,
            "log",
            "--full-history",
            "--format=%H",
            "--max-count=129",
            "--no-renames",
            pin,
            "--",
            path,
        ).stdout.decode().splitlines()
    )
    if len(history_rows) > 128:
        raise ContractIdentityError("receipt history exceeds the bounded audit limit")
    history = _git_actual_path_events(root, history_rows, path)
    addition_rows = tuple(
        line
        for line in _git(
            root,
            "log",
            "--full-history",
            "--format=%H",
            "--max-count=129",
            "--diff-filter=A",
            "--no-renames",
            pin,
            "--",
            path,
        ).stdout.decode().splitlines()
        if line
    )
    if len(addition_rows) > 128:
        raise ContractIdentityError("receipt addition history exceeds the bounded audit limit")
    additions = _git_actual_path_events(root, addition_rows, path)
    if len(additions) > 1:
        raise ContractIdentityError(
            "receipt history does not have a unique introduction commit; "
            "delete/re-add is ambiguous"
        )
    renamed = _git(
        root,
        "log",
        "--full-history",
        "--format=",
        "--max-count=128",
        "--name-status",
        "--find-renames=1%",
        "--follow",
        pin,
        "--",
        path,
    ).stdout.decode().splitlines()
    if any(line.startswith("R") for line in renamed):
        raise ContractIdentityError("receipt history contains a rename ambiguity")
    if len(history) == 1:
        return additions
    if len(history) == 2 and len(additions) == 1 and history[-1] == additions[0]:
        match = _AMENDMENT_NAME.fullmatch(Path(path).name)
        if match is not None:
            try:
                pending_bytes = _git_show(root, additions[0], path)
                acknowledged_bytes = _git_show(root, history[0], path)
                _strict_json(pending_bytes, label=f"{path}@pending")
                _strict_json(acknowledged_bytes, label=f"{path}@acknowledged")
                pending = AmendmentReceipt.model_validate_json(pending_bytes)
                acknowledged = AmendmentReceipt.model_validate_json(acknowledged_bytes)
            except (ContractIdentityError, ValidationError):
                pass
            else:
                mutable = {
                    "acknowledged_on",
                    "catastrophic_set_decision",
                    "ratifier",
                    "repository_revision",
                }
                pending_stable = pending.model_dump(exclude=mutable)
                acknowledged_stable = acknowledged.model_dump(exclude=mutable)
                if (
                    pending.acknowledgment_status == "pending"
                    and pending.repository_revision is None
                    and acknowledged.acknowledgment_status == "acknowledged"
                    and pending_stable == acknowledged_stable
                ):
                    return (history[0],)
    if len(history) > 1:
        raise ContractIdentityError(
            "receipt changed outside the single allowed "
            "pending-to-acknowledged transition"
        )
    return additions


def default_contract_revision(repo_root: Path | str) -> str:
    root = Path(repo_root).resolve()
    head = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    if _git(root, "cat-file", "-e", f"{head}:{RATIFICATION_RECEIPT_PATH}", check=False).returncode == 0:
        return head
    return RATIFICATION_REPOSITORY_REVISION


def _receipt_bytes_at_pin(root: Path, pin: str) -> tuple[tuple[str, bytes], ...]:
    listing = _git(
        root, "ls-tree", "-r", "--name-only", pin, "--", CONTRACTS_DIR, check=False
    )
    names = [
        line.removeprefix(f"{CONTRACTS_DIR}/")
        for line in listing.stdout.decode().splitlines()
        if line.startswith(f"{CONTRACTS_DIR}/")
    ]
    receipt_artifacts = tuple(name for name in names if name.endswith(".json"))
    unrecognized = tuple(
        name
        for name in receipt_artifacts
        if name != "ratification.v1.json" and _AMENDMENT_NAME.fullmatch(name) is None
    )
    if unrecognized:
        raise ContractIdentityError(
            "unrecognized receipt artifact(s) at the pin: " + ", ".join(sorted(unrecognized))
        )
    ratification_rows = tuple(
        (name, _git_show(root, pin, f"{CONTRACTS_DIR}/{name}"))
        for name in receipt_artifacts
        if name == "ratification.v1.json"
    )
    amendment_rows = tuple(
        (name, _git_show(root, pin, f"{CONTRACTS_DIR}/{name}"))
        for name in receipt_artifacts
        if _AMENDMENT_NAME.fullmatch(name)
    )
    if ratification_rows or amendment_rows:
        return ratification_rows + order_amendment_receipt_rows(amendment_rows)

    # Bootstrap: the approved document revision necessarily predates the
    # additive receipt.  Only the byte-pinned canonical receipt is accepted.
    if pin != RATIFICATION_REPOSITORY_REVISION:
        raise ContractIdentityError("no receipt chain exists at the pinned contract revision")
    path = root / RATIFICATION_RECEIPT_PATH
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ContractIdentityError("ratification receipt is missing") from exc
    if _sha256(data) != RATIFICATION_RECEIPT_SHA256:
        raise ContractIdentityError("ratification receipt digest differs from the immutable bootstrap root")
    return (("ratification.v1.json", data),)


def derive_preregistration_identity(
    repo_root: Path | str,
    *,
    contract_revision: str | None = None,
) -> PreregistrationIdentity:
    root = Path(repo_root).resolve()
    pin = contract_revision or default_contract_revision(root)
    if re.fullmatch(_REVISION, pin) is None:
        raise ContractIdentityError("contract_revision must be a full 40-hex Git revision")
    receipts = _receipt_bytes_at_pin(root, pin)
    identity = derive_identity_from_receipt_bytes(
        receipts,
        contract_revision=pin,
        read_git_artifact=lambda revision, path: _git_show(root, revision, path),
        is_revision_applicable=lambda revision, target: _git_applicable(root, revision, target),
        receipt_introduction_revisions=lambda path, target: (
            _git_receipt_introduction_revisions(root, target, path)
        ),
        expected_ratifier=RATIFIER,
        expected_repository_revision=RATIFICATION_REPOSITORY_REVISION,
    )
    if identity.original.path != CONTRACT_PATH:
        raise ContractIdentityError("ratification contract path differs")
    if identity.original.sha256 != RATIFICATION_CONTRACT_SHA256:
        raise ContractIdentityError("ratification digest differs from approved bytes")
    if identity.ratification.receipt_path != RATIFICATION_RECEIPT_PATH:
        raise ContractIdentityError("ratification receipt path differs from the immutable root")
    if identity.ratification.receipt_sha256 != RATIFICATION_RECEIPT_SHA256:
        raise ContractIdentityError("ratification receipt digest differs from the immutable root")
    return identity


def validate_preregistration_identity(
    identity: PreregistrationIdentity,
    *,
    repo_root: Path | str | None = None,
    receipts: Iterable[tuple[str, bytes]] | None = None,
    read_git_artifact: Callable[[str, str], bytes] | None = None,
    is_revision_applicable: Callable[[str, str], bool] | None = None,
    receipt_introduction_revisions: Callable[[str, str], Iterable[str]] | None = None,
) -> None:
    """Reconstruct the pin and require exact typed identity equality."""

    if repo_root is not None:
        reconstructed = derive_preregistration_identity(
            repo_root, contract_revision=identity.contract_revision
        )
    else:
        if (
            receipts is None
            or read_git_artifact is None
            or is_revision_applicable is None
            or receipt_introduction_revisions is None
        ):
            raise TypeError(
                "receipt bytes and independent Git history readers are required"
            )
        reconstructed = derive_identity_from_receipt_bytes(
            receipts,
            contract_revision=identity.contract_revision,
            read_git_artifact=read_git_artifact,
            is_revision_applicable=is_revision_applicable,
            receipt_introduction_revisions=receipt_introduction_revisions,
        )
    if reconstructed != identity:
        raise ContractIdentityError("caller-substituted or incomplete pre-registration identity")


__all__ = [
    "AmendmentAcknowledgmentPendingError",
    "AmendmentChainMismatchError",
    "AmendmentChainMissingError",
    "AmendmentChainOrderError",
    "AmendmentReceipt",
    "ContractIdentityError",
    "PreregistrationDriftError",
    "PreregistrationIdentity",
    "RatificationReceipt",
    "derive_identity_from_receipt_bytes",
    "derive_preregistration_identity",
    "default_contract_revision",
    "fold_amendment_chain",
    "order_amendment_receipt_rows",
    "require_amended_families_released",
    "validate_preregistration_bytes",
    "validate_preregistration_identity",
    "validate_working_preregistration",
    "working_amendment_receipts",
]
