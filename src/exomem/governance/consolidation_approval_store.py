"""Durable issuance replay and single-operation approval JTI reservation."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .. import reserved_paths
from . import (
    consolidation_approval,
    consolidation_plan,
    consolidation_review,
    consolidation_review_store,
)

_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_ISSUANCE_SCHEMA = "exomem.consolidation-approval-issuance/v1"
_JTI_SCHEMA = "exomem.consolidation-approval-jti/v1"
_RESERVATION_SCHEMA = "exomem.consolidation-approval-reservation/v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_JTI = re.compile(r"[0-9a-f]{32}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_ISSUANCE_FIELDS = frozenset(
    {
        "schema",
        "operation_id",
        "render_session_digest",
        "owner_binding_digest",
        "confirmation_digest",
        "claim",
        "claim_digest",
        "token_digest",
    }
)
_JTI_FIELDS = frozenset(
    {
        "schema",
        "jti",
        "approval_operation_id",
        "claim_digest",
        "token_digest",
    }
)
_RESERVATION_FIELDS = frozenset(
    {
        "schema",
        "jti",
        "approval_operation_id",
        "execution_operation_id",
        "request_digest",
        "claim_digest",
        "token_digest",
        "reserved_at",
    }
)
_MAX_RECORD_BYTES = 16 * 1024


class ConsolidationApprovalStoreUnavailable(RuntimeError):
    """Content-free refusal for invalid, stale, or conflicting approval state."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ConsolidationApprovalReservation:
    jti: str
    approval_operation_id: str
    execution_operation_id: str
    request_digest: str
    claim_digest: str
    token_digest: str
    reserved_at: str


def _fail(code: str) -> NoReturn:
    raise ConsolidationApprovalStoreUnavailable(code) from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("APPROVAL_STORE_INPUT_INVALID")
    return value


def _jti(value: object) -> str:
    if not isinstance(value, str) or _JTI.fullmatch(value) is None:
        _fail("APPROVAL_STORE_INPUT_INVALID")
    return value


def _operation_id(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail("APPROVAL_STORE_INPUT_INVALID")
    return value


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail("APPROVAL_STORE_CORRUPT")
    return value


def _exact_mapping(raw: bytes) -> Mapping[str, object]:
    try:
        parsed = consolidation_plan._parse_canonical_mapping(  # noqa: SLF001
            raw,
            maximum=_MAX_RECORD_BYTES,
        )
        if consolidation_plan.canonical_closed_jcs(parsed) != raw:
            _fail("APPROVAL_STORE_CORRUPT")
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail("APPROVAL_STORE_CORRUPT")
    return parsed


@contextmanager
def _authority(vault_root: Path, *, mutation: bool) -> Iterator[None]:
    try:
        with reserved_paths._subsystem_authority_scope(_OWNER):  # noqa: SLF001
            with reserved_paths._identity_coordination_scope(  # noqa: SLF001
                vault_root,
                descriptor_ids=(_DESCRIPTOR_ID,),
                identity_may_change=mutation,
            ):
                yield
    except ConsolidationApprovalStoreUnavailable:
        raise
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        consolidation_approval.ConsolidationApprovalUnavailable,
        consolidation_review.ConsolidationReviewUnavailable,
        consolidation_review_store.ConsolidationReviewStoreUnavailable,
    ):
        _fail("APPROVAL_STORE_UNAVAILABLE")


class ConsolidationApprovalStore:
    """Persist approval claims without token bytes, then reserve one execution."""

    def __init__(self, vault_root: Path | str):
        self.vault_root = Path(vault_root).absolute()
        self.review_store = consolidation_review_store.ConsolidationReviewStore(self.vault_root)

    def _approval_dir(
        self,
        plan: consolidation_plan.CanonicalConsolidationPlan,
    ) -> Path:
        return (
            self.review_store.plan_store._plan_dir(  # noqa: SLF001
                str(plan.preimage["run_id"]),
                str(plan.preimage["plan_kind"]),
                plan.digest,
            )
            / "approvals"
        )

    def _read_optional(self, path: Path) -> bytes | None:
        try:
            return reserved_paths._read_owner_bytes(  # noqa: SLF001
                self.vault_root,
                path,
                _DESCRIPTOR_ID,
                limit=_MAX_RECORD_BYTES,
            )
        except FileNotFoundError:
            return None

    def _publish_missing(self, path: Path, raw: bytes) -> None:
        reserved_paths._publish_owner_bytes(  # noqa: SLF001
            self.vault_root,
            path,
            _DESCRIPTOR_ID,
            raw,
            require_missing=True,
        )

    def _publish_jti(self, path: Path, raw: bytes) -> None:
        existing = self._read_optional(path)
        if existing is None:
            self._publish_missing(path, raw)
        elif existing != raw:
            _fail("APPROVAL_JTI_CONFLICT")

    @staticmethod
    def _new_jti() -> str:
        return secrets.token_hex(16)

    @staticmethod
    def _issuance_bytes(
        *,
        operation_id: str,
        render_session_digest: str,
        owner_binding_digest: str,
        confirmation_digest: str,
        token: consolidation_approval.ConsolidationApprovalToken,
    ) -> bytes:
        return consolidation_plan.canonical_closed_jcs(
            {
                "schema": _ISSUANCE_SCHEMA,
                "operation_id": operation_id,
                "render_session_digest": render_session_digest,
                "owner_binding_digest": owner_binding_digest,
                "confirmation_digest": confirmation_digest,
                "claim": token.claim.preimage,
                "claim_digest": token.claim.digest,
                "token_digest": token.digest,
            }
        )

    @staticmethod
    def _jti_bytes(
        *,
        jti: str,
        approval_operation_id: str,
        claim_digest: str,
        token_digest: str,
    ) -> bytes:
        return consolidation_plan.canonical_closed_jcs(
            {
                "schema": _JTI_SCHEMA,
                "jti": jti,
                "approval_operation_id": approval_operation_id,
                "claim_digest": claim_digest,
                "token_digest": token_digest,
            }
        )

    @staticmethod
    def _reservation_bytes(
        reservation: ConsolidationApprovalReservation,
    ) -> bytes:
        return consolidation_plan.canonical_closed_jcs(
            {
                "schema": _RESERVATION_SCHEMA,
                "jti": reservation.jti,
                "approval_operation_id": reservation.approval_operation_id,
                "execution_operation_id": reservation.execution_operation_id,
                "request_digest": reservation.request_digest,
                "claim_digest": reservation.claim_digest,
                "token_digest": reservation.token_digest,
                "reserved_at": reservation.reserved_at,
            }
        )

    @staticmethod
    def _checked_issuance(
        raw: bytes,
    ) -> tuple[Mapping[str, object], consolidation_approval.CanonicalApprovalClaim]:
        value = _mapping(_exact_mapping(raw), _ISSUANCE_FIELDS)
        if value["schema"] != _ISSUANCE_SCHEMA:
            _fail("APPROVAL_STORE_CORRUPT")
        _operation_id(value["operation_id"])
        _digest(value["render_session_digest"])
        _digest(value["owner_binding_digest"])
        _digest(value["confirmation_digest"])
        claim_value = value["claim"]
        if not isinstance(claim_value, Mapping):
            _fail("APPROVAL_STORE_CORRUPT")
        try:
            claim = consolidation_approval._claim(claim_value)  # noqa: SLF001
        except consolidation_approval.ConsolidationApprovalUnavailable:
            _fail("APPROVAL_STORE_CORRUPT")
        if claim.digest != value["claim_digest"]:
            _fail("APPROVAL_STORE_CORRUPT")
        _digest(value["token_digest"])
        return value, claim

    @staticmethod
    def _checked_jti(raw: bytes) -> Mapping[str, object]:
        value = _mapping(_exact_mapping(raw), _JTI_FIELDS)
        if value["schema"] != _JTI_SCHEMA:
            _fail("APPROVAL_STORE_CORRUPT")
        _jti(value["jti"])
        _operation_id(value["approval_operation_id"])
        _digest(value["claim_digest"])
        _digest(value["token_digest"])
        return value

    @staticmethod
    def _reservation(raw: bytes) -> ConsolidationApprovalReservation:
        value = _mapping(_exact_mapping(raw), _RESERVATION_FIELDS)
        if value["schema"] != _RESERVATION_SCHEMA:
            _fail("APPROVAL_STORE_CORRUPT")
        try:
            reserved_at = consolidation_approval._timestamp(  # noqa: SLF001
                value["reserved_at"]
            )[0]
        except consolidation_approval.ConsolidationApprovalUnavailable:
            _fail("APPROVAL_STORE_CORRUPT")
        return ConsolidationApprovalReservation(
            jti=_jti(value["jti"]),
            approval_operation_id=_operation_id(value["approval_operation_id"]),
            execution_operation_id=_operation_id(value["execution_operation_id"]),
            request_digest=_digest(value["request_digest"]),
            claim_digest=_digest(value["claim_digest"]),
            token_digest=_digest(value["token_digest"]),
            reserved_at=reserved_at,
        )

    def _load_review(
        self,
        plan: consolidation_plan.CanonicalConsolidationPlan,
        render_session_digest: str,
    ) -> consolidation_review.CanonicalRenderReview:
        try:
            review = self.review_store.load(
                str(plan.preimage["run_id"]),
                plan_kind=str(plan.preimage["plan_kind"]),
                plan_digest=plan.digest,
                render_session_digest=_digest(render_session_digest),
            )
            return consolidation_review.validate_review(review, plan=plan)
        except (
            KeyError,
            consolidation_review.ConsolidationReviewUnavailable,
            consolidation_review_store.ConsolidationReviewStoreUnavailable,
        ):
            _fail("APPROVAL_STORE_UNAVAILABLE")

    def issue(
        self,
        *,
        plan: consolidation_plan.CanonicalConsolidationPlan,
        render_session_digest: str,
        identity: consolidation_review.TrustedRenderIdentity,
        confirmation: consolidation_approval.TrustedOwnerConfirmation,
        operation_id: str,
        expires_at: str,
        signing_key_id: str,
        signing_key: bytes,
    ) -> consolidation_approval.ConsolidationApprovalToken:
        """Persist one issuance identity and replay the exact token after lost output."""

        operation_id = _operation_id(operation_id)
        render_session_digest = _digest(render_session_digest)
        review = self._load_review(plan, render_session_digest)
        try:
            confirmation_artifact = consolidation_approval.canonical_confirmation(confirmation)
        except consolidation_approval.ConsolidationApprovalUnavailable:
            _fail("APPROVAL_STORE_INPUT_INVALID")
        directory = self._approval_dir(plan)
        operation_path = directory / "operations" / f"{operation_id}.json"

        with _authority(self.vault_root, mutation=True):
            existing = self._read_optional(operation_path)
            if existing is None:
                jti = _jti(self._new_jti())
            else:
                issuance, claim = self._checked_issuance(existing)
                jti = _jti(claim.preimage["jti"])
            try:
                token = consolidation_approval.mint_approval(
                    plan=plan,
                    review=review,
                    identity=identity,
                    confirmation=confirmation,
                    jti=jti,
                    expires_at=expires_at,
                    signing_key_id=signing_key_id,
                    signing_key=signing_key,
                )
            except consolidation_approval.ConsolidationApprovalUnavailable:
                _fail("APPROVAL_STORE_INPUT_INVALID")
            issuance_raw = self._issuance_bytes(
                operation_id=operation_id,
                render_session_digest=render_session_digest,
                owner_binding_digest=identity.owner_binding_digest,
                confirmation_digest=confirmation_artifact.digest,
                token=token,
            )
            if existing is None:
                self._publish_missing(operation_path, issuance_raw)
            elif existing != issuance_raw:
                _fail("APPROVAL_OPERATION_CONFLICT")
            jti_raw = self._jti_bytes(
                jti=jti,
                approval_operation_id=operation_id,
                claim_digest=token.claim.digest,
                token_digest=token.digest,
            )
            self._publish_jti(directory / "jtis" / f"{jti}.json", jti_raw)
        return token

    def reserve(
        self,
        *,
        wire: str,
        plan: consolidation_plan.CanonicalConsolidationPlan,
        render_session_digest: str,
        execution_operation_id: str,
        request_digest: str,
        reserved_at: str,
        verifier_keys: Mapping[str, bytes],
    ) -> ConsolidationApprovalReservation:
        """Reserve one verified approval JTI for one exact execution request."""

        render_session_digest = _digest(render_session_digest)
        execution_operation_id = _operation_id(execution_operation_id)
        request_digest = _digest(request_digest)
        review = self._load_review(plan, render_session_digest)
        try:
            _wire_text, unverified_claim, _authentication = consolidation_approval._parse_wire(wire)  # noqa: SLF001
            current_time = consolidation_approval._timestamp(reserved_at)[0]  # noqa: SLF001
        except consolidation_approval.ConsolidationApprovalUnavailable:
            _fail("APPROVAL_STORE_INPUT_INVALID")
        jti = _jti(unverified_claim.preimage["jti"])
        directory = self._approval_dir(plan)

        with _authority(self.vault_root, mutation=True):
            reservation_path = directory / "uses" / f"{jti}.json"
            existing_reservation_raw = self._read_optional(reservation_path)
            if existing_reservation_raw is None:
                verification_time = current_time
            else:
                existing_reservation = self._reservation(existing_reservation_raw)
                verification_time = existing_reservation.reserved_at
            try:
                token = consolidation_approval.verify_approval(
                    wire,
                    plan=plan,
                    review=review,
                    now=verification_time,
                    verifier_keys=verifier_keys,
                )
            except consolidation_approval.ConsolidationApprovalUnavailable:
                _fail("APPROVAL_STORE_INPUT_INVALID")
            jti_raw = self._read_optional(directory / "jtis" / f"{jti}.json")
            if jti_raw is None:
                _fail("APPROVAL_NOT_ISSUED")
            jti_record = self._checked_jti(jti_raw)
            approval_operation_id = _operation_id(jti_record["approval_operation_id"])
            issuance_raw = self._read_optional(
                directory / "operations" / f"{approval_operation_id}.json"
            )
            if issuance_raw is None:
                _fail("APPROVAL_STORE_CORRUPT")
            issuance, claim = self._checked_issuance(issuance_raw)
            if (
                issuance["render_session_digest"] != render_session_digest
                or claim != token.claim
                or jti_record["jti"] != jti
                or jti_record["claim_digest"] != token.claim.digest
                or jti_record["token_digest"] != token.digest
                or issuance["claim_digest"] != token.claim.digest
                or issuance["token_digest"] != token.digest
            ):
                _fail("APPROVAL_STORE_CORRUPT")
            reservation = ConsolidationApprovalReservation(
                jti=jti,
                approval_operation_id=approval_operation_id,
                execution_operation_id=execution_operation_id,
                request_digest=request_digest,
                claim_digest=token.claim.digest,
                token_digest=token.digest,
                reserved_at=verification_time,
            )
            target = self._reservation_bytes(reservation)
            if existing_reservation_raw is None:
                self._publish_missing(reservation_path, target)
            elif existing_reservation_raw != target:
                _fail("APPROVAL_JTI_ALREADY_RESERVED")
            else:
                reservation = self._reservation(existing_reservation_raw)
        return reservation


__all__ = [
    "ConsolidationApprovalReservation",
    "ConsolidationApprovalStore",
    "ConsolidationApprovalStoreUnavailable",
]
