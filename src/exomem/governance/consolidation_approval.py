"""Purpose-bound owner confirmation and authenticated consolidation approval."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import NoReturn

from . import consolidation_plan, consolidation_review

_CLAIM_SCHEMA = "exomem.consolidation-approval-token/v1"
_WIRE_VERSION = "cap1"
_DOMAIN = _CLAIM_SCHEMA.encode("ascii")
_CLAIM_FIELDS = frozenset(
    {
        "schema",
        "plan_kind",
        "run_id",
        "plan_digest",
        "rendering_completeness_digest",
        "jti",
        "expires_at",
        "signing_key_id",
    }
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_JTI = re.compile(r"[0-9a-f]{32}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z\Z"
)
_PLAN_KINDS = frozenset({"cutover", "retirement", "rollback"})
_MAX_WIRE_BYTES = 16 * 1024


class ConsolidationApprovalUnavailable(RuntimeError):
    """Content-free refusal for invalid confirmation or approval authority."""

    code = "CONSOLIDATION_APPROVAL_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation approval is unavailable")


@dataclass(frozen=True, slots=True)
class TrustedOwnerConfirmation:
    owner_binding_digest: str
    owner_principal_digest: str
    authorization_session_digest: str
    issuer: str
    surface: str
    action: str
    run_id: str
    plan_kind: str
    plan_digest: str
    rendering_completeness_digest: str
    confirmed_at: str
    nonce: str


@dataclass(frozen=True, slots=True)
class CanonicalApprovalClaim:
    preimage: Mapping[str, object]
    canonical_bytes: bytes
    framed_bytes: bytes
    digest: str


@dataclass(frozen=True, slots=True)
class ConsolidationApprovalToken:
    claim: CanonicalApprovalClaim
    wire: str
    digest: str


def _fail() -> NoReturn:
    raise ConsolidationApprovalUnavailable from None


def _text(value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        _fail()
    normalized = unicodedata.normalize("NFC", value)
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError:
        _fail()
    if not normalized or "\x00" in normalized or size > maximum:
        _fail()
    return normalized


def _digest(value: object) -> str:
    text = _text(value)
    if _DIGEST.fullmatch(text) is None:
        _fail()
    return text


def _identifier(value: object) -> str:
    text = _text(value)
    if _IDENTIFIER.fullmatch(text) is None:
        _fail()
    return text


def _timestamp(value: object) -> tuple[str, datetime]:
    text = _text(value)
    if _TIMESTAMP.fullmatch(text) is None:
        _fail()
    try:
        instant = datetime.fromisoformat(text.removesuffix("Z") + "+00:00")
    except ValueError:
        _fail()
    if instant.tzinfo != UTC:
        _fail()
    return text, instant


def _key(value: object) -> bytes:
    if not isinstance(value, bytes) or not 32 <= len(value) <= 64:
        _fail()
    return value


def _framed(raw: bytes) -> bytes:
    return len(_DOMAIN).to_bytes(4, "big") + _DOMAIN + len(raw).to_bytes(8, "big") + raw


def _claim(value: Mapping[str, object]) -> CanonicalApprovalClaim:
    if frozenset(value) != _CLAIM_FIELDS or value["schema"] != _CLAIM_SCHEMA:
        _fail()
    kind = _text(value["plan_kind"])
    run_id = _text(value["run_id"])
    jti = _text(value["jti"])
    if kind not in _PLAN_KINDS or _UUID4.fullmatch(run_id) is None or _JTI.fullmatch(jti) is None:
        _fail()
    normalized = {
        "schema": _CLAIM_SCHEMA,
        "plan_kind": kind,
        "run_id": run_id,
        "plan_digest": _digest(value["plan_digest"]),
        "rendering_completeness_digest": _digest(value["rendering_completeness_digest"]),
        "jti": jti,
        "expires_at": _timestamp(value["expires_at"])[0],
        "signing_key_id": _identifier(value["signing_key_id"]),
    }
    try:
        raw = consolidation_plan.canonical_closed_jcs(normalized)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    framed = _framed(raw)
    return CanonicalApprovalClaim(
        preimage=MappingProxyType(normalized),
        canonical_bytes=raw,
        framed_bytes=framed,
        digest=hashlib.sha256(framed).hexdigest(),
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: object) -> bytes:
    text = _text(value, maximum=_MAX_WIRE_BYTES)
    if "=" in text:
        _fail()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, UnicodeEncodeError, binascii.Error):
        _fail()
    if _encode(raw) != text:
        _fail()
    return raw


def _token(claim: CanonicalApprovalClaim, signing_key: bytes) -> ConsolidationApprovalToken:
    authentication = hmac.new(_key(signing_key), claim.framed_bytes, hashlib.sha256).digest()
    wire = f"{_WIRE_VERSION}.{_encode(claim.canonical_bytes)}.{_encode(authentication)}"
    return ConsolidationApprovalToken(
        claim=claim,
        wire=wire,
        digest=hashlib.sha256(wire.encode("ascii")).hexdigest(),
    )


def _review_identity(
    review: consolidation_review.CanonicalRenderReview,
) -> consolidation_review.TrustedRenderIdentity:
    session = review.session.preimage
    try:
        return consolidation_review.TrustedRenderIdentity(
            owner_binding_digest=_digest(session["owner_binding_digest"]),
            owner_principal_digest=_digest(session["owner_principal_digest"]),
            authorization_session_digest=_digest(session["authorization_session_digest"]),
            issuer=_identifier(session["issuer"]),
            surface=_identifier(session["surface"]),
        )
    except KeyError:
        _fail()


def _checked_confirmation(
    confirmation: TrustedOwnerConfirmation,
) -> TrustedOwnerConfirmation:
    if not isinstance(confirmation, TrustedOwnerConfirmation):
        _fail()
    kind = _text(confirmation.plan_kind)
    run_id = _text(confirmation.run_id)
    if (
        confirmation.action != "approve"
        or kind not in _PLAN_KINDS
        or _UUID4.fullmatch(run_id) is None
    ):
        _fail()
    return TrustedOwnerConfirmation(
        owner_binding_digest=_digest(confirmation.owner_binding_digest),
        owner_principal_digest=_digest(confirmation.owner_principal_digest),
        authorization_session_digest=_digest(confirmation.authorization_session_digest),
        issuer=_identifier(confirmation.issuer),
        surface=_identifier(confirmation.surface),
        action="approve",
        run_id=run_id,
        plan_kind=kind,
        plan_digest=_digest(confirmation.plan_digest),
        rendering_completeness_digest=_digest(confirmation.rendering_completeness_digest),
        confirmed_at=_timestamp(confirmation.confirmed_at)[0],
        nonce=_identifier(confirmation.nonce),
    )


def mint_approval(
    *,
    plan: consolidation_plan.CanonicalConsolidationPlan,
    review: consolidation_review.CanonicalRenderReview,
    identity: consolidation_review.TrustedRenderIdentity,
    confirmation: TrustedOwnerConfirmation,
    jti: str,
    expires_at: str,
    signing_key_id: str,
    signing_key: bytes,
) -> ConsolidationApprovalToken:
    """Mint one token only from exact complete trusted rendering and confirmation."""

    try:
        review = consolidation_review.validate_review(review, plan=plan)
    except consolidation_review.ConsolidationReviewUnavailable:
        _fail()
    if review.completeness is None:
        _fail()
    if not isinstance(identity, consolidation_review.TrustedRenderIdentity):
        _fail()
    checked_identity = consolidation_review.TrustedRenderIdentity(
        owner_binding_digest=_digest(identity.owner_binding_digest),
        owner_principal_digest=_digest(identity.owner_principal_digest),
        authorization_session_digest=_digest(identity.authorization_session_digest),
        issuer=_identifier(identity.issuer),
        surface=_identifier(identity.surface),
    )
    if checked_identity != _review_identity(review):
        _fail()
    checked_confirmation = _checked_confirmation(confirmation)
    completeness = review.completeness.preimage
    expected_confirmation = TrustedOwnerConfirmation(
        owner_binding_digest=checked_identity.owner_binding_digest,
        owner_principal_digest=checked_identity.owner_principal_digest,
        authorization_session_digest=checked_identity.authorization_session_digest,
        issuer=checked_identity.issuer,
        surface=checked_identity.surface,
        action="approve",
        run_id=_text(plan.preimage["run_id"]),
        plan_kind=_text(plan.preimage["plan_kind"]),
        plan_digest=plan.digest,
        rendering_completeness_digest=review.completeness.digest,
        confirmed_at=checked_confirmation.confirmed_at,
        nonce=checked_confirmation.nonce,
    )
    if checked_confirmation != expected_confirmation:
        _fail()
    _confirmed_text, confirmed = _timestamp(checked_confirmation.confirmed_at)
    expiry_text, expiry = _timestamp(expires_at)
    _completeness_issued_text, completeness_issued = _timestamp(completeness["issued_at"])
    deadlines = tuple(
        _timestamp(value)[1]
        for value in (
            plan.preimage["valid_until"],
            review.session.preimage["expires_at"],
            completeness["expires_at"],
        )
    )
    if confirmed < completeness_issued or confirmed >= expiry or expiry > min(deadlines):
        _fail()
    jti = _text(jti)
    if _JTI.fullmatch(jti) is None:
        _fail()
    claim = _claim(
        {
            "schema": _CLAIM_SCHEMA,
            "plan_kind": plan.preimage["plan_kind"],
            "run_id": plan.preimage["run_id"],
            "plan_digest": plan.digest,
            "rendering_completeness_digest": review.completeness.digest,
            "jti": jti,
            "expires_at": expiry_text,
            "signing_key_id": _identifier(signing_key_id),
        }
    )
    return _token(claim, signing_key)


def verify_approval(
    wire: str,
    *,
    plan: consolidation_plan.CanonicalConsolidationPlan,
    review: consolidation_review.CanonicalRenderReview,
    now: str,
    verifier_keys: Mapping[str, bytes],
) -> ConsolidationApprovalToken:
    """Verify one canonical token against its exact plan and completeness proof."""

    text = _text(wire, maximum=_MAX_WIRE_BYTES)
    parts = text.split(".")
    if len(parts) != 3 or parts[0] != _WIRE_VERSION:
        _fail()
    raw = _decode(parts[1])
    authentication = _decode(parts[2])
    if len(authentication) != hashlib.sha256().digest_size:
        _fail()
    try:
        parsed = consolidation_plan._parse_canonical_mapping(  # noqa: SLF001
            raw,
            maximum=4 * 1024,
        )
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    claim = _claim(parsed)
    if claim.canonical_bytes != raw:
        _fail()
    key_id = _identifier(claim.preimage["signing_key_id"])
    key = verifier_keys.get(key_id) if isinstance(verifier_keys, Mapping) else None
    if key is None:
        _fail()
    expected = hmac.new(_key(key), claim.framed_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(authentication, expected):
        _fail()
    try:
        review = consolidation_review.validate_review(review, plan=plan)
    except consolidation_review.ConsolidationReviewUnavailable:
        _fail()
    if review.completeness is None:
        _fail()
    if (
        claim.preimage["plan_kind"] != plan.preimage["plan_kind"]
        or claim.preimage["run_id"] != plan.preimage["run_id"]
        or claim.preimage["plan_digest"] != plan.digest
        or claim.preimage["rendering_completeness_digest"] != review.completeness.digest
    ):
        _fail()
    _now_text, current = _timestamp(now)
    _expiry_text, expiry = _timestamp(claim.preimage["expires_at"])
    if current >= expiry:
        _fail()
    return ConsolidationApprovalToken(
        claim=claim,
        wire=text,
        digest=hashlib.sha256(text.encode("ascii")).hexdigest(),
    )


__all__ = [
    "CanonicalApprovalClaim",
    "ConsolidationApprovalToken",
    "ConsolidationApprovalUnavailable",
    "TrustedOwnerConfirmation",
    "mint_approval",
    "verify_approval",
]
