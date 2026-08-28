"""Trusted, ordered human rendering for canonical consolidation plans."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import NoReturn

from . import consolidation_plan

_SESSION_SCHEMA = "exomem.consolidation-render-session/v1"
_STATE_SCHEMA = "exomem.consolidation-render-state/v1"
_ACK_SCHEMA = "PlanRenderAcknowledgement/v1"
_COMPLETENESS_SCHEMA = "plan-rendering-completeness/v1"
_SESSION_DOMAIN = _SESSION_SCHEMA.encode("ascii")
_STATE_DOMAIN = _STATE_SCHEMA.encode("ascii")
_ACK_DOMAIN = b"exomem.consolidation-plan-render-acknowledgement/v1"
_COMPLETENESS_DOMAIN = b"exomem.consolidation-plan-rendering-completeness/v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z\Z"
)
_MAX_SAFE_INTEGER = (1 << 53) - 1
_SESSION_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "plan_kind",
        "plan_digest",
        "control_basis_digest",
        "rendering_definition_digest",
        "impact_summary_digest",
        "owner_binding_digest",
        "owner_principal_digest",
        "authorization_session_digest",
        "issuer",
        "surface",
        "page_count",
        "total_rows",
        "issued_at",
        "expires_at",
        "nonce",
    }
)
_STATE_FIELDS = frozenset(
    {
        "schema",
        "render_session_digest",
        "plan_digest",
        "next_page_ordinal",
        "pending",
        "pending_page_ordinal",
        "pending_page_digest",
        "acknowledged_page_digests",
        "acknowledgement_digests",
        "last_acknowledgement_digest",
        "completeness_digest",
    }
)
_ACK_FIELDS = frozenset(
    {
        "schema",
        "owner_binding_digest",
        "owner_principal_digest",
        "authorization_session_digest",
        "issuer",
        "surface",
        "render_session_digest",
        "run_id",
        "plan_kind",
        "plan_digest",
        "section_id",
        "section_page_ordinal",
        "page_ordinal",
        "page_digest",
        "impact_summary_digest",
        "issued_at",
        "nonce",
    }
)
_COMPLETENESS_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "plan_kind",
        "plan_digest",
        "render_session_digest",
        "rendering_definition_digest",
        "section_digests",
        "page_digests",
        "total_pages",
        "total_rows",
        "impact_summary_digest",
        "owner_binding_digest",
        "owner_principal_digest",
        "authorization_session_digest",
        "issuer",
        "surface",
        "issued_at",
        "expires_at",
        "nonce",
    }
)


class ConsolidationReviewUnavailable(RuntimeError):
    """Content-free refusal for invalid, incomplete, or stale review state."""

    code = "CONSOLIDATION_REVIEW_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation review is unavailable")


@dataclass(frozen=True, slots=True)
class TrustedRenderIdentity:
    owner_binding_digest: str
    owner_principal_digest: str
    authorization_session_digest: str
    issuer: str
    surface: str


@dataclass(frozen=True, slots=True)
class CanonicalReviewArtifact:
    preimage: Mapping[str, object]
    canonical_bytes: bytes
    digest: str


@dataclass(frozen=True, slots=True)
class CanonicalRenderReview:
    session: CanonicalReviewArtifact
    state: CanonicalReviewArtifact
    acknowledgements: tuple[CanonicalReviewArtifact, ...]
    completeness: CanonicalReviewArtifact | None


def _fail() -> NoReturn:
    raise ConsolidationReviewUnavailable from None


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


def _integer(value: object, *, maximum: int = _MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _fail()
    return value


def _timestamp(value: object) -> tuple[str, datetime]:
    text = _text(value)
    if _TIMESTAMP.fullmatch(text) is None:
        _fail()
    try:
        parsed = datetime.fromisoformat(text.removesuffix("Z") + "+00:00")
    except ValueError:
        _fail()
    if parsed.tzinfo != UTC:
        _fail()
    return text, parsed


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail()
    return value


def _digest_sequence(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail()
    return tuple(_digest(item) for item in value)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _artifact(value: Mapping[str, object], domain: bytes) -> CanonicalReviewArtifact:
    try:
        raw = consolidation_plan.canonical_closed_jcs(value)
    except (consolidation_plan.ConsolidationPlanUnavailable, RecursionError):
        _fail()
    framed = len(domain).to_bytes(4, "big") + domain + len(raw).to_bytes(8, "big") + raw
    frozen = _freeze(value)
    if not isinstance(frozen, Mapping):
        _fail()
    return CanonicalReviewArtifact(
        preimage=frozen,
        canonical_bytes=raw,
        digest=hashlib.sha256(framed).hexdigest(),
    )


def _validated_identity(identity: TrustedRenderIdentity) -> TrustedRenderIdentity:
    if not isinstance(identity, TrustedRenderIdentity):
        _fail()
    return TrustedRenderIdentity(
        owner_binding_digest=_digest(identity.owner_binding_digest),
        owner_principal_digest=_digest(identity.owner_principal_digest),
        authorization_session_digest=_digest(identity.authorization_session_digest),
        issuer=_identifier(identity.issuer),
        surface=_identifier(identity.surface),
    )


def _checked_plan(
    plan: consolidation_plan.CanonicalConsolidationPlan,
) -> consolidation_plan.CanonicalConsolidationPlan:
    if not isinstance(plan, consolidation_plan.CanonicalConsolidationPlan):
        _fail()
    try:
        checked = consolidation_plan.parse_canonical_plan(
            plan.canonical_bytes,
            control_basis=plan.control_basis,
        )
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    if checked != plan:
        _fail()
    return plan


def _checked_artifact(
    artifact: CanonicalReviewArtifact,
    *,
    fields: frozenset[str],
    schema: str,
    domain: bytes,
) -> Mapping[str, object]:
    if not isinstance(artifact, CanonicalReviewArtifact):
        _fail()
    preimage = _mapping(artifact.preimage, fields)
    if preimage["schema"] != schema or _artifact(preimage, domain) != artifact:
        _fail()
    return preimage


def _session_identity(session: Mapping[str, object]) -> TrustedRenderIdentity:
    return _validated_identity(
        TrustedRenderIdentity(
            owner_binding_digest=_digest(session["owner_binding_digest"]),
            owner_principal_digest=_digest(session["owner_principal_digest"]),
            authorization_session_digest=_digest(session["authorization_session_digest"]),
            issuer=_identifier(session["issuer"]),
            surface=_identifier(session["surface"]),
        )
    )


def _require_identity(
    session: Mapping[str, object], identity: TrustedRenderIdentity
) -> TrustedRenderIdentity:
    checked = _validated_identity(identity)
    if checked != _session_identity(session):
        _fail()
    return checked


def _checked_session(review: CanonicalRenderReview) -> Mapping[str, object]:
    if not isinstance(review, CanonicalRenderReview):
        _fail()
    return _checked_artifact(
        review.session,
        fields=_SESSION_FIELDS,
        schema=_SESSION_SCHEMA,
        domain=_SESSION_DOMAIN,
    )


def _checked_state(review: CanonicalRenderReview) -> Mapping[str, object]:
    state = _checked_artifact(
        review.state,
        fields=_STATE_FIELDS,
        schema=_STATE_SCHEMA,
        domain=_STATE_DOMAIN,
    )
    session = _checked_session(review)
    if (
        state["render_session_digest"] != review.session.digest
        or state["plan_digest"] != session["plan_digest"]
    ):
        _fail()
    next_page = _integer(state["next_page_ordinal"])
    if next_page > _integer(session["page_count"]):
        _fail()
    pending = state["pending"]
    if type(pending) is not bool:
        _fail()
    pending_ordinal = _integer(state["pending_page_ordinal"])
    pending_digest = state["pending_page_digest"]
    if not isinstance(pending_digest, str):
        _fail()
    if pending:
        if pending_ordinal != next_page:
            _fail()
        _digest(pending_digest)
    elif pending_digest or pending_ordinal != next_page:
        _fail()
    page_digests = state["acknowledged_page_digests"]
    acknowledgement_digests = state["acknowledgement_digests"]
    if (
        isinstance(page_digests, (str, bytes))
        or not isinstance(page_digests, Sequence)
        or isinstance(acknowledgement_digests, (str, bytes))
        or not isinstance(acknowledgement_digests, Sequence)
    ):
        _fail()
    if len(page_digests) != next_page or len(acknowledgement_digests) != next_page:
        _fail()
    if tuple(_digest(item) for item in page_digests) != tuple(page_digests):
        _fail()
    if tuple(_digest(item) for item in acknowledgement_digests) != tuple(acknowledgement_digests):
        _fail()
    last = state["last_acknowledgement_digest"]
    complete = state["completeness_digest"]
    if not isinstance(last, str) or not isinstance(complete, str):
        _fail()
    if next_page == 0:
        if last:
            _fail()
    elif _digest(last) != acknowledgement_digests[-1]:
        _fail()
    if complete:
        _digest(complete)
    if len(review.acknowledgements) != next_page:
        _fail()
    for ordinal, (expected_digest, acknowledgement) in enumerate(
        zip(
            acknowledgement_digests,
            review.acknowledgements,
            strict=True,
        )
    ):
        _checked_artifact(
            acknowledgement,
            fields=_ACK_FIELDS,
            schema=_ACK_SCHEMA,
            domain=_ACK_DOMAIN,
        )
        if acknowledgement.digest != expected_digest:
            _fail()
        fields = acknowledgement.preimage
        identity = _session_identity(session)
        if (
            fields["render_session_digest"] != review.session.digest
            or fields["run_id"] != session["run_id"]
            or fields["plan_kind"] != session["plan_kind"]
            or fields["plan_digest"] != session["plan_digest"]
            or fields["owner_binding_digest"] != identity.owner_binding_digest
            or fields["owner_principal_digest"] != identity.owner_principal_digest
            or fields["authorization_session_digest"] != identity.authorization_session_digest
            or fields["issuer"] != identity.issuer
            or fields["surface"] != identity.surface
            or fields["page_ordinal"] != ordinal
            or fields["page_digest"] != page_digests[ordinal]
            or fields["impact_summary_digest"] != session["impact_summary_digest"]
        ):
            _fail()
    if bool(review.completeness) != bool(complete):
        _fail()
    if review.completeness is not None:
        _checked_artifact(
            review.completeness,
            fields=_COMPLETENESS_FIELDS,
            schema=_COMPLETENESS_SCHEMA,
            domain=_COMPLETENESS_DOMAIN,
        )
        if review.completeness.digest != complete:
            _fail()
    return state


def _bind_plan(
    session: Mapping[str, object], plan: object
) -> consolidation_plan.CanonicalConsolidationPlan:
    checked = _checked_plan(plan)
    definition = _mapping(
        checked.preimage["rendering_definition"],
        frozenset({"schema", "page_size", "page_count", "total_rows", "sections"}),
    )
    if (
        session["run_id"] != checked.preimage["run_id"]
        or session["plan_kind"] != checked.preimage["plan_kind"]
        or session["plan_digest"] != checked.digest
        or session["control_basis_digest"] != checked.control_basis.digest
        or session["rendering_definition_digest"] != checked.rendering_definition_digest
        or session["impact_summary_digest"] != checked.impact_summary_digest
        or session["page_count"] != definition["page_count"]
        or session["total_rows"] != definition["total_rows"]
    ):
        _fail()
    return checked


def _state_artifact(
    *,
    session_digest: str,
    plan_digest: str,
    next_page_ordinal: int,
    pending: bool,
    pending_page_digest: str,
    page_digests: Sequence[str],
    acknowledgement_digests: Sequence[str],
    last_acknowledgement_digest: str,
    completeness_digest: str,
) -> CanonicalReviewArtifact:
    return _artifact(
        {
            "schema": _STATE_SCHEMA,
            "render_session_digest": session_digest,
            "plan_digest": plan_digest,
            "next_page_ordinal": next_page_ordinal,
            "pending": pending,
            "pending_page_ordinal": next_page_ordinal,
            "pending_page_digest": pending_page_digest,
            "acknowledged_page_digests": tuple(page_digests),
            "acknowledgement_digests": tuple(acknowledgement_digests),
            "last_acknowledgement_digest": last_acknowledgement_digest,
            "completeness_digest": completeness_digest,
        },
        _STATE_DOMAIN,
    )


def _within_session(session: Mapping[str, object], value: object) -> str:
    text, instant = _timestamp(value)
    _issued_text, issued = _timestamp(session["issued_at"])
    _expires_text, expires = _timestamp(session["expires_at"])
    if instant < issued or instant >= expires:
        _fail()
    return text


def begin_review(
    plan: consolidation_plan.CanonicalConsolidationPlan,
    *,
    identity: TrustedRenderIdentity,
    issued_at: str,
    expires_at: str,
    nonce: str,
) -> CanonicalRenderReview:
    plan = _checked_plan(plan)
    identity = _validated_identity(identity)
    issued_text, issued = _timestamp(issued_at)
    expires_text, expires = _timestamp(expires_at)
    _plan_deadline_text, plan_deadline = _timestamp(plan.preimage["valid_until"])
    if issued >= expires or expires > plan_deadline:
        _fail()
    definition = _mapping(
        plan.preimage["rendering_definition"],
        frozenset({"schema", "page_size", "page_count", "total_rows", "sections"}),
    )
    session = _artifact(
        {
            "schema": _SESSION_SCHEMA,
            "run_id": plan.preimage["run_id"],
            "plan_kind": plan.preimage["plan_kind"],
            "plan_digest": plan.digest,
            "control_basis_digest": plan.control_basis.digest,
            "rendering_definition_digest": plan.rendering_definition_digest,
            "impact_summary_digest": plan.impact_summary_digest,
            "owner_binding_digest": identity.owner_binding_digest,
            "owner_principal_digest": identity.owner_principal_digest,
            "authorization_session_digest": identity.authorization_session_digest,
            "issuer": identity.issuer,
            "surface": identity.surface,
            "page_count": _integer(definition["page_count"]),
            "total_rows": _integer(definition["total_rows"]),
            "issued_at": issued_text,
            "expires_at": expires_text,
            "nonce": _identifier(nonce),
        },
        _SESSION_DOMAIN,
    )
    state = _state_artifact(
        session_digest=session.digest,
        plan_digest=plan.digest,
        next_page_ordinal=0,
        pending=False,
        pending_page_digest="",
        page_digests=(),
        acknowledgement_digests=(),
        last_acknowledgement_digest="",
        completeness_digest="",
    )
    return CanonicalRenderReview(
        session=session,
        state=state,
        acknowledgements=(),
        completeness=None,
    )


def serve_page(
    review: CanonicalRenderReview,
    *,
    plan: consolidation_plan.CanonicalConsolidationPlan,
    identity: TrustedRenderIdentity,
    page_ordinal: int,
    served_at: str,
) -> tuple[CanonicalRenderReview, consolidation_plan.CanonicalPlanPage]:
    session = _checked_session(review)
    state = _checked_state(review)
    _require_identity(session, identity)
    plan = _bind_plan(session, plan)
    _within_session(session, served_at)
    ordinal = _integer(page_ordinal)
    if state["completeness_digest"] or ordinal != state["next_page_ordinal"]:
        _fail()
    page = consolidation_plan.render_plan_page(plan, page_ordinal=ordinal)
    if state["pending"]:
        if state["pending_page_digest"] != page.digest:
            _fail()
        return review, page
    next_state = _state_artifact(
        session_digest=review.session.digest,
        plan_digest=plan.digest,
        next_page_ordinal=ordinal,
        pending=True,
        pending_page_digest=page.digest,
        page_digests=_digest_sequence(state["acknowledged_page_digests"]),
        acknowledgement_digests=_digest_sequence(state["acknowledgement_digests"]),
        last_acknowledgement_digest=(
            _digest(state["last_acknowledgement_digest"])
            if state["last_acknowledgement_digest"]
            else ""
        ),
        completeness_digest="",
    )
    return (
        CanonicalRenderReview(
            session=review.session,
            state=next_state,
            acknowledgements=review.acknowledgements,
            completeness=None,
        ),
        page,
    )


def build_acknowledgement(
    review: CanonicalRenderReview,
    *,
    page: consolidation_plan.CanonicalPlanPage,
    identity: TrustedRenderIdentity,
    issued_at: str,
    nonce: str,
) -> CanonicalReviewArtifact:
    session = _checked_session(review)
    state = _checked_state(review)
    identity = _validated_identity(identity)
    if (
        not isinstance(page, consolidation_plan.CanonicalPlanPage)
        or not state["pending"]
        or page.plan_digest != session["plan_digest"]
        or page.page_ordinal != state["pending_page_ordinal"]
        or page.digest != state["pending_page_digest"]
    ):
        _fail()
    return _artifact(
        {
            "schema": _ACK_SCHEMA,
            "owner_binding_digest": identity.owner_binding_digest,
            "owner_principal_digest": identity.owner_principal_digest,
            "authorization_session_digest": identity.authorization_session_digest,
            "issuer": identity.issuer,
            "surface": identity.surface,
            "render_session_digest": review.session.digest,
            "run_id": session["run_id"],
            "plan_kind": session["plan_kind"],
            "plan_digest": session["plan_digest"],
            "section_id": _identifier(page.section_id),
            "section_page_ordinal": _integer(page.section_page_ordinal),
            "page_ordinal": _integer(page.page_ordinal),
            "page_digest": _digest(page.digest),
            "impact_summary_digest": _digest(session["impact_summary_digest"]),
            "issued_at": _within_session(session, issued_at),
            "nonce": _identifier(nonce),
        },
        _ACK_DOMAIN,
    )


def acknowledge_page(
    review: CanonicalRenderReview,
    *,
    plan: consolidation_plan.CanonicalConsolidationPlan,
    acknowledgement: CanonicalReviewArtifact,
) -> CanonicalRenderReview:
    session = _checked_session(review)
    state = _checked_state(review)
    acknowledgement_fields = _checked_artifact(
        acknowledgement,
        fields=_ACK_FIELDS,
        schema=_ACK_SCHEMA,
        domain=_ACK_DOMAIN,
    )
    identity = _session_identity(session)
    trusted_fields = {
        "owner_binding_digest": identity.owner_binding_digest,
        "owner_principal_digest": identity.owner_principal_digest,
        "authorization_session_digest": identity.authorization_session_digest,
        "issuer": identity.issuer,
        "surface": identity.surface,
        "render_session_digest": review.session.digest,
        "run_id": session["run_id"],
        "plan_kind": session["plan_kind"],
        "plan_digest": session["plan_digest"],
        "impact_summary_digest": session["impact_summary_digest"],
    }
    if any(acknowledgement_fields[field] != value for field, value in trusted_fields.items()):
        _fail()
    plan = _bind_plan(session, plan)
    if not state["pending"]:
        current_ordinal = _integer(state["next_page_ordinal"])
        if (
            current_ordinal > 0
            and acknowledgement.digest == state["last_acknowledgement_digest"]
            and acknowledgement_fields["page_ordinal"] == current_ordinal - 1
        ):
            return review
        _fail()
    pending_page = consolidation_plan.render_plan_page(
        plan,
        page_ordinal=_integer(state["pending_page_ordinal"]),
    )
    expected = {
        "owner_binding_digest": identity.owner_binding_digest,
        "owner_principal_digest": identity.owner_principal_digest,
        "authorization_session_digest": identity.authorization_session_digest,
        "issuer": identity.issuer,
        "surface": identity.surface,
        "render_session_digest": review.session.digest,
        "run_id": session["run_id"],
        "plan_kind": session["plan_kind"],
        "plan_digest": session["plan_digest"],
        "page_ordinal": state["pending_page_ordinal"],
        "page_digest": state["pending_page_digest"],
        "section_id": pending_page.section_id,
        "section_page_ordinal": pending_page.section_page_ordinal,
        "impact_summary_digest": session["impact_summary_digest"],
    }
    if any(acknowledgement_fields[field] != value for field, value in expected.items()):
        _fail()
    _ack_text = _within_session(session, acknowledgement_fields["issued_at"])
    if review.acknowledgements:
        _prior_text, prior = _timestamp(review.acknowledgements[-1].preimage["issued_at"])
        _current_text, current = _timestamp(_ack_text)
        if current < prior:
            _fail()
    next_ordinal = _integer(state["next_page_ordinal"]) + 1
    page_digests = (
        *_digest_sequence(state["acknowledged_page_digests"]),
        _digest(acknowledgement_fields["page_digest"]),
    )
    acknowledgement_digests = (
        *_digest_sequence(state["acknowledgement_digests"]),
        acknowledgement.digest,
    )
    next_state = _state_artifact(
        session_digest=review.session.digest,
        plan_digest=_digest(session["plan_digest"]),
        next_page_ordinal=next_ordinal,
        pending=False,
        pending_page_digest="",
        page_digests=page_digests,
        acknowledgement_digests=acknowledgement_digests,
        last_acknowledgement_digest=acknowledgement.digest,
        completeness_digest="",
    )
    return CanonicalRenderReview(
        session=review.session,
        state=next_state,
        acknowledgements=(*review.acknowledgements, acknowledgement),
        completeness=None,
    )


def _build_completeness(
    review: CanonicalRenderReview,
    *,
    plan: consolidation_plan.CanonicalConsolidationPlan,
    issued_at: str,
    expires_at: str,
    nonce: str,
) -> CanonicalReviewArtifact:
    session = _checked_session(review)
    state = _checked_state(review)
    plan = _bind_plan(session, plan)
    issued_text = _within_session(session, issued_at)
    expires_text, expires = _timestamp(expires_at)
    _session_expiry_text, session_expiry = _timestamp(session["expires_at"])
    _issued_again, issued = _timestamp(issued_text)
    if issued >= expires or expires > session_expiry:
        _fail()
    page_count = _integer(session["page_count"])
    if state["pending"] or state["next_page_ordinal"] != page_count:
        _fail()
    pages = tuple(
        consolidation_plan.render_plan_page(plan, page_ordinal=ordinal)
        for ordinal in range(page_count)
    )
    page_digests = tuple(page.digest for page in pages)
    if _digest_sequence(state["acknowledged_page_digests"]) != page_digests:
        _fail()
    for page, acknowledgement in zip(pages, review.acknowledgements, strict=True):
        if (
            acknowledgement.preimage["section_id"] != page.section_id
            or acknowledgement.preimage["section_page_ordinal"] != page.section_page_ordinal
            or acknowledgement.preimage["page_ordinal"] != page.page_ordinal
            or acknowledgement.preimage["page_digest"] != page.digest
        ):
            _fail()
    definition = _mapping(
        plan.preimage["rendering_definition"],
        frozenset({"schema", "page_size", "page_count", "total_rows", "sections"}),
    )
    sections = definition["sections"]
    if isinstance(sections, (str, bytes)) or not isinstance(sections, Sequence):
        _fail()
    section_digests = tuple(
        _digest(
            _mapping(
                section,
                frozenset(
                    {
                        "ordinal",
                        "section_id",
                        "row_count",
                        "first_page_ordinal",
                        "page_count",
                        "content_digest",
                    }
                ),
            )["content_digest"]
        )
        for section in sections
    )
    identity = _session_identity(session)
    return _artifact(
        {
            "schema": _COMPLETENESS_SCHEMA,
            "run_id": session["run_id"],
            "plan_kind": session["plan_kind"],
            "plan_digest": session["plan_digest"],
            "render_session_digest": review.session.digest,
            "rendering_definition_digest": session["rendering_definition_digest"],
            "section_digests": section_digests,
            "page_digests": page_digests,
            "total_pages": page_count,
            "total_rows": _integer(session["total_rows"]),
            "impact_summary_digest": session["impact_summary_digest"],
            "owner_binding_digest": identity.owner_binding_digest,
            "owner_principal_digest": identity.owner_principal_digest,
            "authorization_session_digest": identity.authorization_session_digest,
            "issuer": identity.issuer,
            "surface": identity.surface,
            "issued_at": issued_text,
            "expires_at": expires_text,
            "nonce": _identifier(nonce),
        },
        _COMPLETENESS_DOMAIN,
    )


def complete_review(
    review: CanonicalRenderReview,
    *,
    plan: consolidation_plan.CanonicalConsolidationPlan,
    identity: TrustedRenderIdentity,
    issued_at: str,
    expires_at: str,
    nonce: str,
) -> tuple[CanonicalRenderReview, CanonicalReviewArtifact]:
    _checked_state(review)
    _require_identity(_checked_session(review), identity)
    completeness = _build_completeness(
        review,
        plan=plan,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
    )
    if review.completeness is not None:
        if review.completeness != completeness:
            _fail()
        return review, completeness
    state = _checked_state(review)
    next_state = _state_artifact(
        session_digest=review.session.digest,
        plan_digest=_digest(review.session.preimage["plan_digest"]),
        next_page_ordinal=_integer(state["next_page_ordinal"]),
        pending=False,
        pending_page_digest="",
        page_digests=_digest_sequence(state["acknowledged_page_digests"]),
        acknowledgement_digests=_digest_sequence(state["acknowledgement_digests"]),
        last_acknowledgement_digest=_digest(state["last_acknowledgement_digest"]),
        completeness_digest=completeness.digest,
    )
    return (
        CanonicalRenderReview(
            session=review.session,
            state=next_state,
            acknowledgements=review.acknowledgements,
            completeness=completeness,
        ),
        completeness,
    )


__all__ = [
    "CanonicalRenderReview",
    "CanonicalReviewArtifact",
    "ConsolidationReviewUnavailable",
    "TrustedRenderIdentity",
    "acknowledge_page",
    "begin_review",
    "build_acknowledgement",
    "complete_review",
    "serve_page",
]
