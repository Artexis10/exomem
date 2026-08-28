"""Canonical one-step successor witnesses for consolidation control flow."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import NoReturn

from .consolidation_plan import ConsolidationPlanUnavailable, canonical_closed_jcs

OWNER_BINDING_SCHEMA = "exomem.consolidation-successor-owner-binding/v1"
SEED_SCHEMA = "exomem.consolidation-successor-context-seed/v1"
CONTEXT_SCHEMA = "exomem.consolidation-successor-context/v1"

_OWNER_DOMAIN = OWNER_BINDING_SCHEMA.encode("ascii")
_SEED_DOMAIN = SEED_SCHEMA.encode("ascii")
_CONTEXT_DOMAIN = CONTEXT_SCHEMA.encode("ascii")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_PAGE_ORDINAL = (1 << 31) - 1
_MAX_TTL_MS = 86_400_000
_MAX_PLAN_ENTRY_EXPIRY = "9999-12-31T23:59:59.999Z"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[0-9a-f]{64}:committed\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/?#-]{0,1023}\Z")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z\Z"
)
_PLAN_KINDS = ("cutover", "rollback", "retirement")
_SUCCESSORS = {
    "plan-materialize": ("plan", "materialize"),
    "render-begin": ("plan", "render-begin"),
    "render-page": ("plan", "render-page"),
    "render-acknowledge": ("plan", "render-acknowledge"),
    "render-complete": ("plan", "render-complete"),
    "approve": ("approve", "plan-kind"),
    "apply": ("apply", "cutover"),
    "rollback-terminal-plan": ("rollback", "terminal-plan"),
    "retire-source-clearance": ("retire-source", "clearance"),
    "rollback-nonterminal-contingency": ("rollback", "nonterminal-contingency"),
}
_FACT_FIELDS = {
    "plan-materialize": frozenset({"eligible_plan_kinds", "plan_input_basis_digest"}),
    "render-begin": frozenset({"plan_kind", "plan_digest"}),
    "render-page": frozenset({"plan_kind", "plan_digest", "render_session_digest", "page_ordinal"}),
    "render-acknowledge": frozenset(
        {
            "plan_kind",
            "plan_digest",
            "render_session_digest",
            "page_ordinal",
            "page_digest",
        }
    ),
    "render-complete": frozenset({"plan_kind", "plan_digest", "render_session_digest"}),
    "approve": frozenset({"plan_kind", "plan_digest", "rendering_completeness_digest"}),
    "apply": frozenset({"cutover_plan_digest", "approval_token_digest"}),
    "rollback-terminal-plan": frozenset({"rollback_plan_digest", "rollback_token_digest"}),
    "retire-source-clearance": frozenset({"retirement_plan_digest", "retirement_token_digest"}),
    "rollback-nonterminal-contingency": frozenset(
        {
            "original_apply_operation_id",
            "original_apply_journal_digest",
            "cutover_plan_digest",
            "rollback_contingency_digest",
            "publication_state_digest",
            "contingency_authority_ref",
            "contingency_authority_digest",
            "recovery_window_deadline",
        }
    ),
}
_OWNER_FIELDS = frozenset(
    {
        "schema",
        "vault_id",
        "installation_id",
        "generation",
        "active_fence_digest",
        "principal_digest",
        "purpose",
    }
)
_SEED_FIELDS = frozenset(
    {
        "schema",
        "context_schema",
        "context_kind",
        "run_id",
        "run_revision",
        "destination_binding_digest",
        "owner_binding_digest",
        "basis_digest",
        "successor_action",
        "successor_variant",
        "issued_at",
        "expires_at",
        "nonce",
        "facts",
    }
)
_CONTEXT_FIELDS = frozenset(
    {
        "schema",
        "context_kind",
        "run_id",
        "run_revision",
        "destination_binding_digest",
        "owner_binding_digest",
        "basis_digest",
        "context_seed_digest",
        "predecessor_event_id",
        "predecessor_payload_digest",
        "successor_action",
        "successor_variant",
        "issued_at",
        "expires_at",
        "nonce",
        "facts",
    }
)


class SuccessorContextUnavailable(RuntimeError):
    """Stable content-free refusal for an invalid or stale successor witness."""

    code = "CONSOLIDATION_SUCCESSOR_CONTEXT_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("successor context is unavailable")


@dataclass(frozen=True, slots=True)
class SuccessorOwnerIdentity:
    vault_id: str
    installation_id: str
    generation: int
    active_fence_digest: str
    principal_digest: str


@dataclass(frozen=True, slots=True)
class SuccessorSeedInput:
    context_kind: str
    run_id: str
    run_revision: int
    destination_binding_digest: str
    owner_binding_digest: str
    basis_digest: str
    successor_action: str
    successor_variant: str
    issued_at: str
    expires_at: str
    nonce: str
    facts: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ExpectedSuccessorContext:
    context_kind: str
    run_id: str
    run_revision: int
    destination_binding_digest: str
    owner_binding_digest: str
    basis_digest: str
    predecessor_event_id: str
    predecessor_payload_digest: str
    successor_action: str
    successor_variant: str
    facts: Mapping[str, object]
    verified_at: str


@dataclass(frozen=True, slots=True)
class CanonicalSuccessorObject:
    preimage: Mapping[str, object]
    canonical_bytes: bytes
    framed_bytes: bytes
    digest: str


@dataclass(frozen=True, slots=True)
class CanonicalSuccessorSeed(CanonicalSuccessorObject):
    pass


@dataclass(frozen=True, slots=True)
class CanonicalSuccessorContext(CanonicalSuccessorObject):
    seed: CanonicalSuccessorSeed


def _fail() -> NoReturn:
    raise SuccessorContextUnavailable from None


def _text(value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        _fail()
    normalized = unicodedata.normalize("NFC", value)
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError:
        _fail()
    if not normalized or size > maximum or "\x00" in normalized:
        _fail()
    return normalized


def _matching(value: object, pattern: re.Pattern[str]) -> str:
    text = _text(value, maximum=1024)
    if pattern.fullmatch(text) is None:
        _fail()
    return text


def _digest(value: object) -> str:
    return _matching(value, _DIGEST)


def _identifier(value: object) -> str:
    return _matching(value, _IDENTIFIER)


def _reference(value: object) -> str:
    return _matching(value, _REFERENCE)


def _uuid4(value: object) -> str:
    return _matching(value, _UUID4)


def _integer(value: object, *, minimum: int = 0, maximum: int = _MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
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


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _canonical(value: Mapping[str, object], domain: bytes) -> CanonicalSuccessorObject:
    try:
        raw = canonical_closed_jcs(value)
    except (ConsolidationPlanUnavailable, RecursionError):
        _fail()
    framed = len(domain).to_bytes(4, "big") + domain + len(raw).to_bytes(8, "big") + raw
    frozen = _freeze(value)
    if not isinstance(frozen, Mapping):
        _fail()
    return CanonicalSuccessorObject(
        preimage=frozen,
        canonical_bytes=raw,
        framed_bytes=framed,
        digest=hashlib.sha256(framed).hexdigest(),
    )


def _facts(kind: str, value: object) -> Mapping[str, object]:
    expected = _FACT_FIELDS.get(kind)
    if expected is None:
        _fail()
    item = _mapping(value, expected)
    normalized = dict(item)
    if kind == "plan-materialize":
        eligible_raw = item["eligible_plan_kinds"]
        if isinstance(eligible_raw, (str, bytes)) or not isinstance(eligible_raw, Sequence):
            _fail()
        eligible = tuple(_identifier(entry) for entry in eligible_raw)
        if not eligible or eligible != tuple(entry for entry in _PLAN_KINDS if entry in eligible):
            _fail()
        normalized["eligible_plan_kinds"] = eligible
        _digest(item["plan_input_basis_digest"])
    elif kind in {"render-begin", "render-page", "render-acknowledge", "render-complete"}:
        if item["plan_kind"] not in _PLAN_KINDS:
            _fail()
        _digest(item["plan_digest"])
        if "render_session_digest" in item:
            _digest(item["render_session_digest"])
        if "page_ordinal" in item:
            _integer(item["page_ordinal"], maximum=_MAX_PAGE_ORDINAL)
        if "page_digest" in item:
            _digest(item["page_digest"])
    elif kind == "approve":
        if item["plan_kind"] not in _PLAN_KINDS:
            _fail()
        _digest(item["plan_digest"])
        _digest(item["rendering_completeness_digest"])
    elif kind == "apply":
        _digest(item["cutover_plan_digest"])
        _digest(item["approval_token_digest"])
    elif kind == "rollback-terminal-plan":
        _digest(item["rollback_plan_digest"])
        _digest(item["rollback_token_digest"])
    elif kind == "retire-source-clearance":
        _digest(item["retirement_plan_digest"])
        _digest(item["retirement_token_digest"])
    else:
        _uuid4(item["original_apply_operation_id"])
        for field in (
            "original_apply_journal_digest",
            "cutover_plan_digest",
            "rollback_contingency_digest",
            "publication_state_digest",
            "contingency_authority_digest",
        ):
            _digest(item[field])
        _reference(item["contingency_authority_ref"])
        _timestamp(item["recovery_window_deadline"])
    return normalized


def _validate_seed_input(value: SuccessorSeedInput) -> dict[str, object]:
    if not isinstance(value, SuccessorSeedInput):
        _fail()
    kind = _identifier(value.context_kind)
    expected_successor = _SUCCESSORS.get(kind)
    if expected_successor is None:
        _fail()
    facts = _facts(kind, value.facts)
    action = _identifier(value.successor_action)
    variant = _identifier(value.successor_variant)
    expected_action, expected_variant = expected_successor
    if action != expected_action:
        _fail()
    if kind == "approve":
        if variant != facts["plan_kind"]:
            _fail()
    elif variant != expected_variant:
        _fail()
    issued_text, issued = _timestamp(value.issued_at)
    expires_text, expires = _timestamp(value.expires_at)
    if issued >= expires:
        _fail()
    if kind == "plan-materialize":
        if expires_text != _MAX_PLAN_ENTRY_EXPIRY:
            _fail()
    elif kind == "rollback-nonterminal-contingency":
        _deadline_text, deadline = _timestamp(facts["recovery_window_deadline"])
        if expires > deadline:
            _fail()
    basis = _digest(value.basis_digest)
    if kind == "plan-materialize" and basis != facts["plan_input_basis_digest"]:
        _fail()
    if (
        kind == "rollback-nonterminal-contingency"
        and basis != facts["original_apply_journal_digest"]
    ):
        _fail()
    return {
        "schema": SEED_SCHEMA,
        "context_schema": CONTEXT_SCHEMA,
        "context_kind": kind,
        "run_id": _uuid4(value.run_id),
        "run_revision": _integer(value.run_revision),
        "destination_binding_digest": _digest(value.destination_binding_digest),
        "owner_binding_digest": _digest(value.owner_binding_digest),
        "basis_digest": basis,
        "successor_action": action,
        "successor_variant": variant,
        "issued_at": issued_text,
        "expires_at": expires_text,
        "nonce": _identifier(value.nonce),
        "facts": facts,
    }


def build_owner_binding(identity: SuccessorOwnerIdentity) -> CanonicalSuccessorObject:
    if not isinstance(identity, SuccessorOwnerIdentity):
        _fail()
    value = {
        "schema": OWNER_BINDING_SCHEMA,
        "vault_id": _identifier(identity.vault_id),
        "installation_id": _identifier(identity.installation_id),
        "generation": _integer(identity.generation, minimum=1),
        "active_fence_digest": _digest(identity.active_fence_digest),
        "principal_digest": _digest(identity.principal_digest),
        "purpose": "vault-consolidation",
    }
    return _canonical(value, _OWNER_DOMAIN)


def build_seed(value: SuccessorSeedInput) -> CanonicalSuccessorSeed:
    artifact = _canonical(_validate_seed_input(value), _SEED_DOMAIN)
    return CanonicalSuccessorSeed(
        preimage=artifact.preimage,
        canonical_bytes=artifact.canonical_bytes,
        framed_bytes=artifact.framed_bytes,
        digest=artifact.digest,
    )


def derive_context(
    seed: CanonicalSuccessorSeed,
    *,
    predecessor_event_id: str,
    predecessor_payload_digest: str,
) -> CanonicalSuccessorContext:
    if not isinstance(seed, CanonicalSuccessorSeed) or parse_seed(seed.canonical_bytes) != seed:
        _fail()
    value = {
        key: item for key, item in seed.preimage.items() if key not in {"schema", "context_schema"}
    }
    value.update(
        {
            "schema": CONTEXT_SCHEMA,
            "context_seed_digest": seed.digest,
            "predecessor_event_id": _matching(predecessor_event_id, _EVENT_ID),
            "predecessor_payload_digest": _digest(predecessor_payload_digest),
        }
    )
    artifact = _canonical(value, _CONTEXT_DOMAIN)
    return CanonicalSuccessorContext(
        preimage=artifact.preimage,
        canonical_bytes=artifact.canonical_bytes,
        framed_bytes=artifact.framed_bytes,
        digest=artifact.digest,
        seed=seed,
    )


def _parse_integer(value: str) -> int:
    if value.startswith("-"):
        _fail()
    try:
        return _integer(int(value))
    except ValueError:
        _fail()


def _reject(_value: str) -> NoReturn:
    _fail()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for raw_key, item in pairs:
        key = unicodedata.normalize("NFC", raw_key)
        if key in value:
            _fail()
        value[key] = item
    return value


def _parse(raw: bytes, *, maximum: int) -> Mapping[str, object]:
    if not isinstance(raw, bytes) or len(raw) > maximum:
        _fail()
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_int=_parse_integer,
            parse_float=_reject,
            parse_constant=_reject,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        SuccessorContextUnavailable,
    ):
        _fail()
    if not isinstance(value, Mapping):
        _fail()
    return value


def parse_owner_binding(raw: bytes) -> CanonicalSuccessorObject:
    value = _mapping(_parse(raw, maximum=64 * 1024), _OWNER_FIELDS)
    if value["schema"] != OWNER_BINDING_SCHEMA or value["purpose"] != "vault-consolidation":
        _fail()
    identity = SuccessorOwnerIdentity(
        vault_id=_identifier(value["vault_id"]),
        installation_id=_identifier(value["installation_id"]),
        generation=_integer(value["generation"], minimum=1),
        active_fence_digest=_digest(value["active_fence_digest"]),
        principal_digest=_digest(value["principal_digest"]),
    )
    artifact = build_owner_binding(identity)
    if artifact.canonical_bytes != raw:
        _fail()
    return artifact


def parse_seed(raw: bytes) -> CanonicalSuccessorSeed:
    value = _mapping(_parse(raw, maximum=256 * 1024), _SEED_FIELDS)
    if value["schema"] != SEED_SCHEMA or value["context_schema"] != CONTEXT_SCHEMA:
        _fail()
    seed = build_seed(
        SuccessorSeedInput(
            context_kind=_identifier(value["context_kind"]),
            run_id=_uuid4(value["run_id"]),
            run_revision=_integer(value["run_revision"]),
            destination_binding_digest=_digest(value["destination_binding_digest"]),
            owner_binding_digest=_digest(value["owner_binding_digest"]),
            basis_digest=_digest(value["basis_digest"]),
            successor_action=_identifier(value["successor_action"]),
            successor_variant=_identifier(value["successor_variant"]),
            issued_at=_timestamp(value["issued_at"])[0],
            expires_at=_timestamp(value["expires_at"])[0],
            nonce=_identifier(value["nonce"]),
            facts=_mapping(
                value["facts"], _FACT_FIELDS.get(str(value["context_kind"]), frozenset())
            ),
        )
    )
    if seed.canonical_bytes != raw:
        _fail()
    return seed


def parse_context(raw: bytes) -> CanonicalSuccessorContext:
    value = _mapping(_parse(raw, maximum=256 * 1024), _CONTEXT_FIELDS)
    if value["schema"] != CONTEXT_SCHEMA:
        _fail()
    seed = build_seed(
        SuccessorSeedInput(
            context_kind=_identifier(value["context_kind"]),
            run_id=_uuid4(value["run_id"]),
            run_revision=_integer(value["run_revision"]),
            destination_binding_digest=_digest(value["destination_binding_digest"]),
            owner_binding_digest=_digest(value["owner_binding_digest"]),
            basis_digest=_digest(value["basis_digest"]),
            successor_action=_identifier(value["successor_action"]),
            successor_variant=_identifier(value["successor_variant"]),
            issued_at=_timestamp(value["issued_at"])[0],
            expires_at=_timestamp(value["expires_at"])[0],
            nonce=_identifier(value["nonce"]),
            facts=_mapping(
                value["facts"], _FACT_FIELDS.get(str(value["context_kind"]), frozenset())
            ),
        )
    )
    if value["context_seed_digest"] != seed.digest:
        _fail()
    context = derive_context(
        seed,
        predecessor_event_id=_matching(value["predecessor_event_id"], _EVENT_ID),
        predecessor_payload_digest=_digest(value["predecessor_payload_digest"]),
    )
    if context.canonical_bytes != raw:
        _fail()
    return context


def verify_context(
    context: CanonicalSuccessorContext,
    *,
    expected: ExpectedSuccessorContext,
) -> CanonicalSuccessorContext:
    if (
        not isinstance(context, CanonicalSuccessorContext)
        or not isinstance(expected, ExpectedSuccessorContext)
        or parse_context(context.canonical_bytes) != context
    ):
        _fail()
    expected_seed = _validate_seed_input(
        SuccessorSeedInput(
            context_kind=expected.context_kind,
            run_id=expected.run_id,
            run_revision=expected.run_revision,
            destination_binding_digest=expected.destination_binding_digest,
            owner_binding_digest=expected.owner_binding_digest,
            basis_digest=expected.basis_digest,
            successor_action=expected.successor_action,
            successor_variant=expected.successor_variant,
            issued_at=_timestamp(context.preimage["issued_at"])[0],
            expires_at=_timestamp(context.preimage["expires_at"])[0],
            nonce=_identifier(context.preimage["nonce"]),
            facts=expected.facts,
        )
    )
    for field in (
        "context_kind",
        "run_id",
        "run_revision",
        "destination_binding_digest",
        "owner_binding_digest",
        "basis_digest",
        "successor_action",
        "successor_variant",
        "facts",
    ):
        if canonical_closed_jcs(context.preimage[field]) != canonical_closed_jcs(
            expected_seed[field]
        ):
            _fail()
    if context.preimage["predecessor_event_id"] != _matching(
        expected.predecessor_event_id, _EVENT_ID
    ) or context.preimage["predecessor_payload_digest"] != _digest(
        expected.predecessor_payload_digest
    ):
        _fail()
    _verified_text, verified = _timestamp(expected.verified_at)
    _expires_text, expires = _timestamp(context.preimage["expires_at"])
    if verified >= expires:
        _fail()
    return context


def context_expiry(
    *,
    context_kind: str,
    issued_at: str,
    ttl_ms: int,
    deadline: str | None,
) -> str:
    kind = _identifier(context_kind)
    if kind not in _SUCCESSORS:
        _fail()
    _issued_text, issued = _timestamp(issued_at)
    ttl = _integer(ttl_ms, minimum=1, maximum=_MAX_TTL_MS)
    if kind == "plan-materialize":
        if deadline is not None:
            _fail()
        return _MAX_PLAN_ENTRY_EXPIRY
    if deadline is None:
        _fail()
    deadline_text, deadline_time = _timestamp(deadline)
    try:
        expires = issued + timedelta(milliseconds=ttl)
    except OverflowError:
        _fail()
    if deadline_time <= issued:
        _fail()
    if deadline_time < expires:
        return deadline_text
    return expires.isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "CONTEXT_SCHEMA",
    "OWNER_BINDING_SCHEMA",
    "SEED_SCHEMA",
    "CanonicalSuccessorContext",
    "CanonicalSuccessorObject",
    "CanonicalSuccessorSeed",
    "ExpectedSuccessorContext",
    "SuccessorContextUnavailable",
    "SuccessorOwnerIdentity",
    "SuccessorSeedInput",
    "build_owner_binding",
    "build_seed",
    "context_expiry",
    "derive_context",
    "parse_context",
    "parse_owner_binding",
    "parse_seed",
    "verify_context",
]
