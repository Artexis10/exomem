"""The release plane: disclosure ladder, per-level projector, decision annotation.

This module is the *only* path from a retrieval candidate (hit, page, pack
element, semantic unit) to a wire dict. Design decisions D2/D3/D4.

Three states, checked in this order at every entry point — the contract the
kernel's `decide_paths` documents and this module must mirror at each call
site, because `membership.evaluate` + `decisions.decide` are invoked directly
here (the `decide_paths` facade takes no grants parameter and cannot carry the
per-request decision memo):

1. `policy.empty` — no `_Governance/` configured. The open fast path: hand the
   candidates back untouched, parse nothing, open no sidecar. This is what
   keeps the latency gate flat for ungoverned vaults.
2. `policy.blocked` — a cold-start compile refusal (a conflicted-copy sibling,
   or a compile error) with no prior good policy to fall back on. The
   fail-closed floor: EVERYTHING withholdable is withheld at `DISCLOSURE_MIN`,
   which is L0 and therefore silent. Never, ever fall through to (1): a
   refused compile is not "no governance at all".
3. Otherwise, decide per item normally.

An unresolved-but-expected principal (`RequestPrincipal.resolved is False`) is
treated exactly like (2) for the same reason: identity that should have
resolved and did not must not reach the open path.
"""

from __future__ import annotations

import ast
import copy
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import sqlite3
import textwrap
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from .. import find_corpus, memory_refs, reserved_paths, vault
from ..find_types import Hit, SemanticUnitHit
from ..kbdir import kb_dirname
from . import (
    authorization_custody,
    authorization_session_authority,
    authorization_session_lifecycle,
    bridges,
    lifecycle,
    receipts,
    scrubber,
    store,
    tokens,
)
from . import membership as membership_module
from . import policy as policy_module
from .decisions import Decision, decide
from .policy import DISCLOSURE_MAX, DISCLOSURE_MIN, Policy
from .principal import OWNER_AUDIENCE, RequestPrincipal, effective_principal

log = logging.getLogger(__name__)


class SelectorCoverageError(RuntimeError):
    """An invocation selector has no release/tombstone adapter.

    The direct classifier keeps raising this hard coverage failure. Dispatch
    seams may recognize the type only to acquire mutation authority first and
    then translate it into a content-free refusal before the leaf executes.
    """


#: Sentinel for a memo that legitimately caches .
_UNSET = object()


class ReceiptUnavailableError(RuntimeError):
    """Publicly safe failure when a governed representation lacks evidence."""

    def __init__(self) -> None:
        super().__init__("GOVERNANCE_RECEIPT_UNAVAILABLE: retry the request")


class AuthorizationSessionDecisionUnavailable(RuntimeError):
    """Content-free refusal when verified session state cannot be rechecked."""

    def __init__(self) -> None:
        super().__init__("AUTHORIZATION_SESSION_UNAVAILABLE")


@dataclass
class DisclosureOutcome:
    """A content-free decision made while shaping one boundary response."""

    value: dict[str, Any]


@dataclass
class DisclosureCollector:
    vault_root: Path
    boundary_id: str
    command_name: str
    outcomes: list[DisclosureOutcome] = field(default_factory=list)
    path_outcomes: set[tuple[str, str, int | None]] = field(default_factory=set)
    credential_redactions: int = 0
    credential_principal: str | None = None
    credential_purpose: str | None = None


_DISCLOSURE_COLLECTOR: ContextVar[DisclosureCollector | None] = ContextVar(
    "exomem_disclosure_collector", default=None
)
_DISCLOSURE_BOUNDARY_OWNERS: ContextVar[tuple[bool, ...]] = ContextVar(
    "exomem_disclosure_boundary_owners", default=()
)


@contextmanager
def disclosure_boundary(vault_root: Path, command_name: str, *, join_existing: bool = False):
    """Collect one command's decisions and emit only on its success.

    Nested commands own separate receipts unless their caller explicitly opts
    into the active command's same-vault collector.
    """
    existing = _collector()
    root = Path(vault_root)
    if join_existing and existing is not None and existing.vault_root != root:
        raise RuntimeError("nested disclosure boundary cannot use a different vault")
    owns_collector = not (join_existing and existing is not None)
    collector = (
        existing
        if not owns_collector
        else DisclosureCollector(root, uuid.uuid4().hex, command_name)
    )
    token = _DISCLOSURE_COLLECTOR.set(collector) if owns_collector else None
    owners = _DISCLOSURE_BOUNDARY_OWNERS.set((*_DISCLOSURE_BOUNDARY_OWNERS.get(), owns_collector))
    try:
        yield collector
    finally:
        _DISCLOSURE_BOUNDARY_OWNERS.reset(owners)
        if token is not None:
            _DISCLOSURE_COLLECTOR.reset(token)


def _collector() -> DisclosureCollector | None:
    return _DISCLOSURE_COLLECTOR.get()


def _record_outcome(value: Mapping[str, Any]) -> None:
    collector = _collector()
    if collector is None:
        return
    collector.outcomes.append(DisclosureOutcome(dict(value)))


def record_direct_text_release(
    text: str,
    *,
    stable_ref: str,
    representation: str,
    principal: RequestPrincipal | None = None,
    authorization: Mapping[str, Any] | None = None,
) -> None:
    """Record the exact bounded text that crossed a direct-read boundary."""
    if representation not in {"page_body", "semantic_unit_span"}:
        raise ValueError("invalid direct text representation")
    who = principal if principal is not None else effective_principal()
    raw = text.encode("utf-8")
    value = {
        key: item
        for key, item in (authorization or {}).items()
        if key
        in {
            "level",
            "purpose",
            "policy_fingerprint",
            "confirmation",
            "scope_ids",
            "scope_label_digests",
            "release_grant_id",
            "release_dependency_digest",
        }
    }
    collector = _collector()
    if collector is not None:
        value["command"] = collector.command_name
    value.update(
        {
            "decision": "released",
            "ref": stable_ref,
            "content_hash": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "representation": representation,
            "principal": who.audience_id,
            "audience": who.audience_id,
        }
    )
    _record_outcome(value)


def _record_credential_block(count: int = 1) -> None:
    collector = _collector()
    if collector is not None:
        collector.credential_redactions += count
        principal = effective_principal().audience_id
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", principal):
            collector.credential_principal = principal
        purpose = effective_principal().purpose
        if purpose and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", purpose):
            collector.credential_purpose = purpose


def _record_blocked_outcome(audience: str) -> None:
    value: dict[str, Any] = {"decision": "blocked"}
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", audience):
        value["audience"] = audience
        value["principal"] = audience
    collector = _collector()
    if collector is not None:
        value["command"] = collector.command_name
    purpose = effective_principal().purpose
    if purpose and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", purpose):
        value["purpose"] = purpose
    _record_outcome(value)


def _outcome_for_decision(
    vault_root: Path,
    rel_path: str,
    *,
    decision: Decision | None,
    policy: Policy,
    audience: str,
    outcome: str,
    purpose: str | None = None,
    content_hash: str | None = None,
    size: int | None = None,
    ref: str | None = None,
    purpose_is_bound: bool = False,
) -> None:
    """Project a decision into the receipt union without carrying a path/title."""
    value: dict[str, Any] = {"decision": outcome}
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", audience):
        value["audience"] = audience
        value["principal"] = audience
    collector = _collector()
    if collector is not None:
        value["command"] = collector.command_name
    who = effective_principal()
    declared_purpose = purpose if purpose_is_bound else _declared_purpose(vault_root, who, purpose)
    if declared_purpose and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", declared_purpose):
        value["purpose"] = declared_purpose
    if decision is not None:
        value["level"] = decision.level
        if policy.fingerprint != "blocked":
            value["policy_fingerprint"] = policy.fingerprint
        if decision.scope_ids:
            value["scope_ids"] = list(decision.scope_ids)
            labels = [
                policy.scopes[scope_id].name
                for scope_id in decision.scope_ids
                if scope_id in policy.scopes and policy.scopes[scope_id].name
            ]
            if labels:
                value["scope_label_digests"] = [
                    receipts.label_digest(vault_root, label) for label in labels
                ]
        value["confirmation"] = "none"
        if decision.release_grant_id is not None:
            value["release_grant_id"] = decision.release_grant_id
        if decision.release_dependency_digest is not None:
            value["release_dependency_digest"] = decision.release_dependency_digest
    if content_hash is not None:
        value["content_hash"] = content_hash
        if size is not None:
            value["size"] = size
        if ref is not None:
            value["ref"] = ref
    else:
        # Defence in depth: `rel_path` is expected to already be a decided,
        # vault-relative candidate, but this hash is the last thing that
        # touches the filesystem before the receipt is written. Confining it
        # here too means an unconfined candidate that reaches this far still
        # cannot make the receipt read (and hash the size of) an arbitrary
        # server file — it just loses its content hash.
        try:
            target = Path(vault_root) / rel_path
            resolved = target.resolve()
            resolved.relative_to(Path(vault_root).resolve())
        except (OSError, ValueError):
            pass
        else:
            if resolved.is_file():
                try:
                    raw = resolved.read_bytes()
                except OSError:
                    pass
                else:
                    value["content_hash"] = hashlib.sha256(raw).hexdigest()
                    value["size"] = len(raw)
    collector = _collector()
    outcome_key = (
        rel_path,
        outcome,
        decision.level if decision is not None else None,
    )
    if collector is not None:
        if outcome_key in collector.path_outcomes:
            return
        collector.path_outcomes.add(outcome_key)
    _record_outcome(value)


def emit_boundary_receipt(collector: DisclosureCollector) -> None:
    """Synchronously append evidence after the final representation is fixed."""
    owners = _DISCLOSURE_BOUNDARY_OWNERS.get()
    if not owners or not owners[-1]:
        return
    try:
        if collector.credential_redactions:
            receipts.append_event(
                collector.vault_root,
                event_type="credential_block",
                event_id=uuid.uuid5(uuid.NAMESPACE_URL, f"credential:{collector.boundary_id}").hex,
                payload={
                    "count": collector.credential_redactions,
                    "redaction_count": collector.credential_redactions,
                    "command": collector.command_name,
                    **(
                        {
                            "principal": collector.credential_principal,
                            "audience": collector.credential_principal,
                        }
                        if collector.credential_principal
                        else {}
                    ),
                    **(
                        {"purpose": collector.credential_purpose}
                        if collector.credential_purpose
                        else {}
                    ),
                },
            )
        if collector.outcomes:
            receipts.append_event(
                collector.vault_root,
                event_type="disclosure",
                event_id=collector.boundary_id,
                payload={"outcomes": _bounded_outcomes(collector.outcomes)},
            )
    except (receipts.ReceiptError, OSError, sqlite3.Error) as exc:
        raise ReceiptUnavailableError() from exc


def _bounded_outcomes(outcomes: Sequence[DisclosureOutcome]) -> list[dict[str, Any]]:
    """Keep receipt schemas bounded without making a large reduction fail closed."""
    values = [outcome.value for outcome in outcomes]
    raw_size = len(
        json.dumps(
            {"outcomes": values},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    if len(values) <= receipts.MAX_OUTCOMES and raw_size <= receipts.MAX_RECORD_BYTES // 2:
        return values

    # At most 4 decisions x 7 disclosure levels (including a missing level).
    # Higher-cardinality typed identities become deterministic set/manifest
    # digests inside those audit-useful buckets instead of one row per
    # principal/scope/purpose, which could itself exceed MAX_OUTCOMES.
    buckets: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        typed = {key: value[key] for key in ("decision", "level") if key in value}
        key = json.dumps(typed, sort_keys=True, separators=(",", ":"))
        buckets.setdefault(key, []).append(value)

    def _digest(items: Iterable[Any], *, unique: bool = False) -> str:
        encoded = [
            json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            for item in items
        ]
        manifest = sorted(set(encoded) if unique else encoded)
        return hashlib.sha256(json.dumps(manifest, separators=(",", ":")).encode()).hexdigest()

    identity_keys = (
        "command",
        "principal",
        "audience",
        "purpose",
        "policy_fingerprint",
        "confirmation",
        "scope_ids",
        "scope_label_digests",
        "release_grant_id",
        "release_dependency_digest",
    )
    set_dimensions = {
        "principal_set_digest": "principal",
        "audience_set_digest": "audience",
        "purpose_set_digest": "purpose",
        "policy_set_digest": "policy_fingerprint",
        "confirmation_set_digest": "confirmation",
        "boundary_set_digest": "command",
    }
    result: list[dict[str, Any]] = []
    optional_identity: list[tuple[int, str, Any]] = []
    for key, members in sorted(buckets.items()):
        summary = json.loads(key)
        identities = [
            {identity_key: member.get(identity_key) for identity_key in identity_keys}
            for member in members
        ]
        summary.update(
            {
                "count": len(members),
                "membership_digest": _digest(
                    [member.get("content_hash") or member.get("ref") or "" for member in members]
                ),
                "identity_manifest_digest": _digest(identities),
                "scope_set_digest": _digest(
                    [
                        {
                            "scope_ids": member.get("scope_ids"),
                            "scope_label_digests": member.get("scope_label_digests"),
                        }
                        for member in members
                    ],
                    unique=True,
                ),
                **{
                    digest_field: _digest(
                        [member.get(source_field) for member in members], unique=True
                    )
                    for digest_field, source_field in set_dimensions.items()
                },
            }
        )
        result.append(summary)
        # Preserve compact singleton dimensions when the complete aggregate
        # still fits a conservative fraction of the receipt record window.
        # The set/manifest digests above remain the truthful representation
        # when a 128-element scope identity would make raw retention unsafe.
        result_index = len(result) - 1
        for identity_key in identity_keys:
            present = [member[identity_key] for member in members if identity_key in member]
            if (
                present
                and len(present) == len(members)
                and _digest(present, unique=True) == _digest([present[0]], unique=True)
            ):
                optional_identity.append((result_index, identity_key, present[0]))
    if len(result) > receipts.MAX_OUTCOMES:  # defensive if decision schema expands
        raise ReceiptUnavailableError()
    optional_budget = receipts.MAX_RECORD_BYTES // 2
    for result_index, identity_key, identity_value in optional_identity:
        result[result_index][identity_key] = identity_value
        encoded_size = len(
            json.dumps(
                {"outcomes": result},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        if encoded_size > optional_budget:
            result[result_index].pop(identity_key)
    return result


# ---------------------------------------------------------------------------
# The disclosure ladder
# ---------------------------------------------------------------------------

LEVEL_NONE = 0  # L0 nothing — the item is omitted, silently
LEVEL_NOTICE = 1  # L1 rule id + scope label
LEVEL_CONSTRAINT = 2  # L2 + the constraint string
LEVEL_ABSTRACT = 3  # L3 + an approved abstraction
LEVEL_EXCERPT_REDACTED = 4  # L4 — exact approved bridge abstraction only
LEVEL_EXCERPT = 5  # L5 bounded excerpt + ranking signals
LEVEL_FULL = 6  # L6 full disclosure

#: The lowest level at which an item may carry a path, title, or excerpt.
#: Below it the item is represented only by a notice (or nothing at all).
RELEASE_FLOOR = LEVEL_EXCERPT

assert LEVEL_NONE == DISCLOSURE_MIN and LEVEL_FULL == DISCLOSURE_MAX

# ---------------------------------------------------------------------------
# Per-level field allow-lists (D3)
# ---------------------------------------------------------------------------

# A find hit is inherently an excerpt shape, so L5 and L6 share a field set;
# the ladder distinction between them bites on `get`/`read_memory`, where L5 is
# a bounded excerpt of a body L6 returns whole (see `annotate_page`).
#
# This is an ALLOW-list, applied over whatever the underlying serializer
# produced: a field added to `Hit.as_dict` later and not enumerated here is
# dropped at every level rather than silently released. That fail-closed
# direction is the entire point of routing every surface through `project`.
_HIT_FIELDS: frozenset[str] = frozenset(
    {
        "path",
        "type",
        "scope",
        "title",
        "updated",
        "order_indeterminate",
        "excerpt",
        "graph",
        "relation_match",
        "media_type",
        "media_file",
        "clip_match_at",
        "scene_frame",
        "scene_match_at",
        "transcript_match_at",
        "outside_kb",
        "status",
        "superseded_by",
        "matched_units",
        "matched_units_truncated",
        "result_type",
        "mixed_units_truncated",
        "signals",
    }
)

_UNIT_FIELDS: frozenset[str] = frozenset(
    {
        "result_type",
        "unit_ref",
        "form",
        "category_raw",
        "category_key",
        "category",
        "kind",
        "content",
        "excerpt",
        "tags",
        "context",
        "verdict",
        "check_by",
        "relations",
        "source_anchor",
        "source_span",
        "source_hash",
        "parent_path",
        "parent_ref",
        "parent_title",
        "parent_type",
        "parent_status",
        "parent_updated",
        "parent_superseded_by",
        "relation_match",
        "mixed_units_truncated",
        "signals",
    }
)

# Fields whose value may name one or more vault paths, and how to read them.
_PATH_LIST_FIELDS = ("superseded_by", "parent_superseded_by")
_PATH_DICT_LIST_FIELDS = ("matched_units",)


# ---------------------------------------------------------------------------
# Projector registry
# ---------------------------------------------------------------------------

#: `annotate_page` renders a whole page rather than a hit; these are the fields
#: it may emit at L5-L6. A non-empty entry here is load-bearing:
#: `registered_kinds()` filters empty sets, so the previous `frozenset()`
#: placeholder made the coverage check permanently unsatisfiable — the
#: subset test could never be True, so the assertion could never pass and was
#: therefore never wired in.
_PAGE_FIELDS: frozenset[str] = frozenset(
    {
        "path",
        "frontmatter",
        "body",
        "body_truncated",
        "body_chars",
        "content",
        "content_hash",
        "mtime",
        "has_frontmatter",
        "history",
        "links",
        "ref",
        "release_level",
    }
)

_PROJECTORS: dict[str, frozenset[str]] = {
    "hit": _HIT_FIELDS,
    "semantic_unit": _UNIT_FIELDS,
    #  renders a whole page rather than a hit; these are the
    # fields it may emit at L5-L6. A non-empty entry is load-bearing:
    # `registered_kinds()` filters empty sets, so `frozenset()` here made the
    # coverage check permanently unsatisfiable.
    "page": _PAGE_FIELDS,
}
_PROJECTOR_VALIDATORS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any] | None]] = {}
_PROJECTOR_VALIDATOR_UNSET = object()

_FULL_ONLY_PROJECTORS: frozenset[str] = frozenset(
    {
        "record_query",
        "record_inspection",
        "record_manifest",
        "record_template",
        "record_mutation",
        "planning_query",
        "planning_inspection",
        "planning_mutation",
    }
)


def _kind_for(payload: Any) -> str | None:
    if isinstance(payload, Hit):
        return "hit"
    if isinstance(payload, SemanticUnitHit):
        return "semantic_unit"
    return None


def _serialize(payload: Any, *, compact: bool) -> dict[str, Any]:
    if compact and hasattr(payload, "as_compact_dict"):
        return dict(payload.as_compact_dict())
    return dict(payload.as_dict())


# ---------------------------------------------------------------------------
# Withheld-path recognition (M14)
# ---------------------------------------------------------------------------
#
# A raw string comparison made this check trivially bypassable: the SAME page
# is written four different ways across the structured fields this module
# filters — `path.md`, `path.md#Heading`, `[[stem]]`, and
# `exomem://source/path` — so a withheld page survived in a released payload
# under any form but the one the decision produced. A permitted page then
# stands as an existence oracle for its withheld neighbour, which is exactly
# the disclosure the ceiling was set to prevent.
#
# Scope is deliberately narrow: STRUCTURED FIELDS ONLY (pointer fields, refs,
# link lists). Scanning released page bodies for mentions is a different
# problem and explicitly out of scope here.

_WIKILINK_ANYWHERE = re.compile(r"\[\[([^\[\]]+)\]\]")
_EXOMEM_PATH_PREFIXES = ("exomem://vault/", "exomem://source/")
MAX_DIRECT_TEXT_REFERENCES = 64


def _unwrap_reference(raw: str, *, is_wikilink_target: bool = False) -> tuple[str, bool]:
    """`(path-ish text, explicitly-a-reference)` for one reference string.

    Shared by the withheld-key comparison and by the reference COLLECTION in
    `annotate_page`, so both read a wikilink, an `exomem://` ref and a plain
    path exactly the same way.

    `is_wikilink_target` marks `raw` as content `_WIKILINK_ANYWHERE` already
    extracted from inside a `[[...]]` pair — the regex's capture group
    excludes the brackets themselves (`[^\\[\\]]+`), so a target pulled from
    running prose NEVER starts with `[[`, and the `text.startswith("[[")`
    detection below cannot recognise it as wikilink syntax by inspecting the
    text alone. Passing this flag applies the SAME display-alias
    (`target|label`) / heading-anchor (`target#Section`) split the bracketed
    branch applies, without requiring brackets this text will never carry.
    Every caller that iterates `_WIKILINK_ANYWHERE.findall(...)` and unwraps
    each match must pass it. The bracket-gated split below has looked like
    this since round 1 (moved there to fix the decode-order bug for
    `exomem://` refs), and an extracted, bracket-less target has ALWAYS
    fallen to the `else` branch, never that one — round 1's own tests still
    passed because the `else` branch's own `_strip_trailing_marker`
    (deleted by this round's R3 rewrite) split on the first `|`/`#` for any
    text with no `.md` found before it, which correctly recovered a bare
    stem's alias/heading as a side effect, by coincidence rather than
    design. Deleting it with no direct replacement for a wikilink target is
    what actually broke this — a round-3 regression, found while
    investigating reviewer follow-up (i), not a round-1 one: omitting this
    flag stopped stripping the alias/heading off an extracted wikilink
    target, so `[[withheld-page|Read more]]` inside otherwise permitted
    prose stopped being recognised as naming `withheld-page`.

    An `exomem://` reference's path component is percent-encoded by
    `context_refs._encode`, which leaves `.`/`/` unescaped but DOES encode a
    literal `#` or `|` (`%23`/`%7C`) — a real filename may contain either. The
    structural fragment delimiter the pipeline appends (`#unit-<hash>`,
    `#current`) is always the one UNENCODED `#` in the raw text. Splitting
    only ever happens on that raw, still-encoded text, BEFORE decoding: an
    encoded `%23`/`%7C` inside the path is inert to a split that has already
    happened, so it survives decoding as the literal character it names
    instead of truncating the path or being mistaken for an alias separator.

    A PLAIN string is never percent-encoded, so a `#`/`|` in it cannot be
    told apart from a genuine trailing marker (`#current`, `path.md#Heading`)
    by inspecting the string alone: `notes.md#draft.md` is exactly as
    consistent with "the file named notes.md#draft.md" as with "notes.md,
    fragment draft.md" — picking one by a positional guess (a former version
    of this function cut at the first `.md`) decided a DIFFERENT, wrong file
    than the one a governed packet's content actually came from. This
    function therefore does not guess for a plain string: it keeps the text
    exactly as given, and a caller with the vault available to check for
    itself — `_working_set_paths`'s candidate/interpretation-set handling —
    resolves the ambiguity by deciding every reading that exists rather than
    picking one. The wikilink form is unencoded too, but IS split as
    written: a wikilink target is a page TITLE, which this module has never
    had to reconcile against a `.md` filename the way a plain path or a
    decoded URI does.
    """
    text = raw.strip()
    if not text:
        return "", False
    explicit = False
    if is_wikilink_target or (text.startswith("[[") and text.endswith("]]")):
        if not is_wikilink_target:
            text = text[2:-2].strip()
        explicit = True
        # Wikilink display alias (`[[target|label]]`) and heading anchor
        # (`[[target#Section]]`) are presentation, not identity. Unencoded
        # text, so splitting after the fact is exact.
        text = text.split("|", 1)[0]
        text = text.split("#", 1)[0]
    else:
        lowered = text.lower()
        matched_prefix = next(
            (prefix for prefix in _EXOMEM_PATH_PREFIXES if lowered.startswith(prefix)),
            None,
        )
        if matched_prefix is not None:
            remainder = text[len(matched_prefix) :]
            remainder = remainder.split("#", 1)[0]
            text = unquote(remainder)
            explicit = True
        # A plain path or an unrecognised scheme: kept exactly as given,
        # never percent-decoded and never split on '#'/'|' -- see the
        # docstring above.
    text = text.replace("\\", "/").strip().strip("/")
    return text, explicit


def direct_text_references_visible(
    vault_root: Path,
    text: str,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> bool:
    """Prove every wikilink in returned direct-read text remains releasable.

    A governed body can name a page by title or alias, neither of which is a
    filesystem path.  The maintained working-set catalogue is the only bounded
    authority for that mapping.  Target checks never record a disclosure of
    content this direct read does not return.
    """
    root = Path(vault_root)
    policy = policy_module.load(root)
    who = principal if principal is not None else effective_principal()
    if policy.empty:
        return True
    if policy.blocked or not who.resolved:
        _record_blocked_outcome(who.audience_id)
        return False

    paths: set[str] = set()
    names: set[str] = set()
    for raw in _WIKILINK_ANYWHERE.findall(text):
        # `raw` is already bracket-stripped by the regex capture, so the
        # alias/heading split needs `is_wikilink_target=True` -- see
        # `_unwrap_reference`'s docstring.
        target, _explicit = _unwrap_reference(raw, is_wikilink_target=True)
        if not target:
            return False
        if target.endswith(".md"):
            paths.add(target)
        else:
            names.add(target)
    if len(paths) + len(names) > MAX_DIRECT_TEXT_REFERENCES:
        return False

    checkpoint = None
    if names:
        from .. import freshness, working_set_index, working_set_runtime

        checkpoint = freshness.live_recall_checkpoint(root, "kb")
        if checkpoint is None:
            return False
        stamp = working_set_runtime._key_text(
            (checkpoint.triple, checkpoint.policy_version, checkpoint.access_policy_fingerprint)
        )
        index = working_set_index.WorkingSetIndex(root)
        if not index.available() or index.freshness_stamp() != stamp:
            return False
        try:
            resolved = _resolved_prose_names(root, names)
        except WorkingSetResolutionUnavailable:
            return False
        if any(name not in resolved for name in names):
            return False
        resolved_paths = {path for name in names for path in resolved[name]}
        if not resolved_paths or len(paths) + len(resolved_paths) > MAX_DIRECT_TEXT_REFERENCES:
            return False
        paths |= resolved_paths

    grants_hash = _grants_hash(policy)
    declared_purpose = _declared_purpose(root, who, purpose)
    for path in paths:
        if lifecycle.is_tombstoned(root, path):
            return False
        decision = _decide_path(
            root,
            path,
            policy=policy,
            audience=who.audience_id,
            purpose=declared_purpose,
            grants_hash=grants_hash,
            authorization_session=who.authorization_session_id,
            authorization_context=who.verified_authorization_session,
        )
        if decision is None or decision.level < RELEASE_FLOOR:
            return False
    if checkpoint is not None:
        from .. import freshness

        if not freshness.recall_checkpoint_is_current(root, "kb", checkpoint):
            return False
    return True


def _is_plain_reference(raw: str, *, is_wikilink_target: bool) -> bool:
    """True when `raw` is a PLAIN reference whose `#`/`|` is genuinely
    ambiguous (R3) — not a bracket-wrapped wikilink form, not an
    `exomem://vault|source/`-scheme'd URI, and not already known to be an
    (unbracketed) wikilink target via `is_wikilink_target`. Both of those
    other forms are unambiguous once unwrapped, by construction. Shared by
    `_interpretations_for` and `_canonical_references` so the same
    classification is never restated.
    """
    if is_wikilink_target:
        return False
    text = raw.strip()
    if text.startswith("[[") and text.endswith("]]"):
        return False
    lowered = text.lower()
    return not any(lowered.startswith(prefix) for prefix in _EXOMEM_PATH_PREFIXES)


def _plain_reference_readings(text: str) -> tuple[str, ...]:
    """Every positional reading a PLAIN reference's UNWRAPPED text could
    denote (R3): the literal text itself, and every prefix ending exactly
    where a `#`/`|` immediately follows a markdown suffix
    (`_is_markdown_path`, case-insensitive — the SAME predicate
    `_decide_path` uses). Shared by `_interpretations_for` (which filters
    these to safety-valid vault paths before deciding any of them) and
    `_canonical_references` (which turns each into a comparison key,
    matching if ANY of them names a withheld page).
    """
    readings = [text]
    for index, char in enumerate(text):
        if char in ("#", "|") and _is_markdown_path(text[:index]):
            readings.append(text[:index])
    return tuple(readings)


def _canonical_references(
    raw: str, *, is_wikilink_target: bool = False
) -> tuple[tuple[str, bool], ...]:
    """Every `(canonical key, compare-against-stems)` reading `raw` could
    denote (R3's ambiguity, applied to the withheld-key comparison rather
    than to a release decision).

    The second element of each pair marks a reference that carries no
    directory of its own (a wikilink target, a lone `foo.md`) but IS
    unambiguously a reference — wikilink-wrapped, `exomem://`-prefixed, or
    `.md`-suffixed. Only those may be compared against filename stems. Two
    exclusions keep the normalization from degenerating into a blocklist:

    - a reference that carries a directory is compared against FULL paths
      only, so a permitted `Sources/index.md` is not stripped because some
      withheld `Patterns/index.md` shares a filename;
    - a bare word that is not marked as a reference is not compared at all,
      so an ordinary title (`Overview`) is not stripped because a withheld
      page happens to be named `overview.md`.

    A wikilink target, an `exomem://` URI, and a call that already knows
    `raw` is a wikilink target (`is_wikilink_target=True`) are all
    unambiguous once unwrapped and return exactly one reading, same as
    always. A PLAIN string containing `#`/`|` is ambiguous the same way a
    path-bearing field's candidate is (`_interpretations_for`): every
    reading `_plain_reference_readings` can produce is returned, so a
    caller matching against a KNOWN withheld set
    (`_string_names_withheld`) can match on ANY one of them — the opposite
    of a decision's unanimous-admission requirement, and the correct
    direction here: recognising that a value names a withheld page needs
    only one TRUE reading, not every syntactically possible one to agree.
    """
    text, explicit = _unwrap_reference(raw, is_wikilink_target=is_wikilink_target)
    if not text:
        return ()
    readings = (
        _plain_reference_readings(text)
        if _is_plain_reference(raw, is_wikilink_target=is_wikilink_target)
        else (text,)
    )
    out: list[tuple[str, bool]] = []
    for reading in readings:
        key = reading.casefold()
        reading_explicit = explicit
        if key.endswith(".md"):
            key = key[: -len(".md")]
            reading_explicit = True
        out.append((key, reading_explicit and "/" not in key))
    return tuple(out)


def _canonical_reference(
    raw: str, *, is_wikilink_target: bool = False
) -> tuple[str, bool] | None:
    """`(canonical key, compare-against-stems)` for `raw`'s single reading.

    A convenience wrapper over `_canonical_references` for the callers that
    only ever compare an UNAMBIGUOUS reference — `_withheld_keys`, over an
    already-decided real vault path, which by construction carries no
    `#`/`|` marker still left to resolve. `_string_names_withheld` is the
    one caller comparing a possibly-ambiguous CANDIDATE value, and calls
    `_canonical_references` directly to check every reading.

    `is_wikilink_target` forwards to `_unwrap_reference` — see its docstring;
    a caller comparing an already-bracket-stripped wikilink capture must pass
    it so the alias/heading split still applies.
    """
    readings = _canonical_references(raw, is_wikilink_target=is_wikilink_target)
    return readings[0] if readings else None


def _kb_stripped(key: str) -> str:
    """The same key without a leading Knowledge-Base directory component."""
    prefix = f"{kb_dirname().casefold()}/"
    return key[len(prefix) :] if key.startswith(prefix) else key


@functools.lru_cache(maxsize=256)
def _withheld_keys(withheld_paths: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
    """`(full-path keys, filename-stem keys)` for a withheld set.

    Cached on the frozenset because this runs per projected item on a
    governed vault; an ungoverned vault never reaches here at all.
    """
    full: set[str] = set()
    stems: set[str] = set()
    for path in withheld_paths:
        canonical = _canonical_reference(path)
        if canonical is None:
            continue
        key = canonical[0]
        full.add(key)
        full.add(_kb_stripped(key))
        stems.add(key.rsplit("/", 1)[-1])
    return frozenset(full), frozenset(stems)


def _string_names_withheld(
    value: str, withheld_paths: frozenset[str], *, reference_field: bool = False
) -> bool:
    full, stems = _withheld_keys(withheld_paths)

    def _hit(candidate: str, *, is_wikilink_target: bool = False) -> bool:
        # `_canonical_references` (plural): a PLAIN candidate containing
        # `#`/`|` is ambiguous (R3), and matching against a KNOWN withheld
        # set only needs ONE reading to be true -- unlike a release
        # decision, which needs every EXISTING reading admitted to serve.
        for key, compare_stems in _canonical_references(
            candidate, is_wikilink_target=is_wikilink_target
        ):
            # Inside a reference field a bare name needs no `[[…]]` or `.md`
            # to count as a reference -- that is what the field means.
            if compare_stems or (reference_field and "/" not in key):
                if key in stems:
                    return True
            elif key in full or _kb_stripped(key) in full:
                return True
        return False

    if _hit(value):
        return True
    # A wikilink is an unambiguous reference wherever it appears, so a
    # structured field carrying one inside a longer label still names its
    # target. `_WIKILINK_ANYWHERE`'s capture already excludes the brackets
    # (`is_wikilink_target=True`), so a `[[withheld|Read more]]` alias or
    # `[[withheld#Section]]` heading anchor still canonicalises to the
    # withheld stem instead of the literal, never-matching `withheld|read
    # more`.
    return any(
        _hit(target, is_wikilink_target=True) for target in _WIKILINK_ANYWHERE.findall(value)
    )


def _names_withheld(
    value: Any, withheld_paths: frozenset[str], *, reference_field: bool = False
) -> bool:
    """True when `value` mentions any withheld path, in any reference form,
    at any nesting depth.

    `reference_field` marks a container whose entries are DEFINITIONALLY
    references — `links.outbound`, `relations`, `sources`. The bare-word
    asymmetry that protects prose (a plain title is not a reference) is
    exactly wrong there: a wikilink field stores bare stems, so `outbound:
    ["kill-switch-for-risky-releases"]` named a withheld page in the clear.
    Inside such a field a bare stem IS a reference and is compared as one.
    """
    if not withheld_paths:
        return False
    if isinstance(value, str):
        return _string_names_withheld(value, withheld_paths, reference_field=reference_field)
    if isinstance(value, Mapping):
        return any(
            _names_withheld(v, withheld_paths, reference_field=reference_field)
            for v in value.values()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _names_withheld(v, withheld_paths, reference_field=reference_field) for v in value
        )
    return False


def _strip_withheld_provenance(
    out: dict[str, Any], withheld_paths: frozenset[str]
) -> dict[str, Any]:
    """Remove every annotation that names a sub-notice item (D3).

    List-shaped pointers are filtered entry-by-entry and dropped when the
    filter empties them; scalar/dict-shaped ones are removed outright.
    """
    if not withheld_paths:
        return out
    for name in _PATH_LIST_FIELDS:
        values = out.get(name)
        if isinstance(values, list):
            kept = [v for v in values if not _names_withheld(v, withheld_paths)]
            if kept:
                out[name] = kept
            else:
                out.pop(name, None)
    for name in _PATH_DICT_LIST_FIELDS:
        values = out.get(name)
        if isinstance(values, list):
            kept = [v for v in values if not _names_withheld(v, withheld_paths)]
            if kept:
                out[name] = kept
            else:
                out.pop(name, None)
                out.pop(f"{name}_truncated", None)
    for name in ("graph", "relation_match", "parent_ref", "relations"):
        if name in out and _names_withheld(out[name], withheld_paths):
            out.pop(name, None)
    return out


def _fail_closed_notice(reason: str) -> dict[str, Any]:
    """The shape a surface gets when no projector claims its payload.

    No path, no title, no excerpt — a missed surface degrades to silence
    rather than to a leak, and the `reason` marker makes it loud for the
    startup assertion and its test.
    """
    return {"withheld": True, "reason": reason}


def _notice(
    level: int,
    *,
    rule_ids: Sequence[str] = (),
    scope_label: str | None = None,
    options: Mapping[str, Any] | None = None,
    bridge_abstraction: str | None = None,
) -> dict[str, Any]:
    """L1–L4 rendering: low-level notices or one approved L4 abstraction.

    Deliberately carries no path, title, excerpt, score, or provenance at any
    of these levels — see the `release-gate` spec's "Low levels strip metadata
    oracles" scenario.
    """
    options = options or {}
    if level == LEVEL_EXCERPT_REDACTED and bridge_abstraction:
        return {
            "withheld": True,
            "level": LEVEL_EXCERPT_REDACTED,
            "bridge": bridge_abstraction,
        }
    out: dict[str, Any] = {"withheld": True, "level": level}
    if level == LEVEL_CONSTRAINT and options.get("constraint_source") == "scope":
        constraint = options.get("constraint")
        if constraint:
            out["constraint"] = str(constraint)
        return out
    if rule_ids:
        out["rule_ids"] = sorted(rule_ids)
    if scope_label:
        out["scope_label"] = scope_label
    if level >= LEVEL_CONSTRAINT:
        constraint = options.get("constraint")
        if constraint:
            out["constraint"] = str(constraint)
    if level >= LEVEL_ABSTRACT:
        abstract = options.get("abstract")
        if abstract:
            out["abstract"] = str(abstract)
    notice_text = options.get("notice")
    if notice_text:
        out["notice"] = str(notice_text)
    return out


def project(
    payload: Any,
    level: int,
    *,
    kind: str | None = None,
    decision: Decision | None = None,
    rule_ids: Sequence[str] = (),
    scope_label: str | None = None,
    options: Mapping[str, Any] | None = None,
    withheld_paths: frozenset[str] = frozenset(),
    compact: bool = False,
) -> dict[str, Any] | None:
    """The single serializer: render `payload` at `level`, or `None` for L0.

    `decision` supplies `rule_ids`/`options` when present; the explicit
    keywords exist so the projector can be exercised without constructing a
    whole `Decision`.
    """
    if decision is not None:
        rule_ids = rule_ids or decision.rule_ids
        options = options if options is not None else decision.options
    bridge_abstraction = decision.bridge_abstraction if decision is not None else None

    if level <= LEVEL_NONE:
        return None
    if level == LEVEL_EXCERPT_REDACTED and not bridge_abstraction:
        # L4 without exact approved bridge content lowers to L3 rather than
        # borrowing any source text or reviving the retired redaction lane.
        level = LEVEL_ABSTRACT
    if level < RELEASE_FLOOR:
        return _notice(
            level,
            rule_ids=rule_ids,
            scope_label=scope_label,
            options=options,
            bridge_abstraction=bridge_abstraction,
        )

    resolved_kind = kind or _kind_for(payload)
    if resolved_kind in _FULL_ONLY_PROJECTORS and level < LEVEL_FULL:
        return _fail_closed_notice("records_requires_full_release")
    allowed = _PROJECTORS.get(resolved_kind or "")
    if not allowed:
        log.warning(
            "governance.egress: no projector registered for payload kind %r; failing closed",
            resolved_kind or type(payload).__name__,
        )
        return _fail_closed_notice("no_projector")

    try:
        raw = _serialize(payload, compact=compact)
    except AttributeError:
        log.warning(
            "governance.egress: payload kind %r has no serializer; failing closed",
            resolved_kind,
        )
        return _fail_closed_notice("no_projector")

    validator = _PROJECTOR_VALIDATORS.get(resolved_kind or "")
    if validator is not None:
        try:
            validated = validator(raw)
        except Exception:  # noqa: BLE001 - a projector is an untrusted extension boundary.
            label = (
                resolved_kind
                if type(resolved_kind) is str and len(resolved_kind) <= 128
                else type(resolved_kind).__name__
            )
            log.warning("governance.egress: projector validator failed for kind %s", label)
            return _fail_closed_notice("invalid_projector_payload")
        if validated is None:
            return _fail_closed_notice("invalid_projector_payload")
        raw = validated

    # Only L5–L6 reach this line: every lower level returned a notice above,
    # which is how "scores, graph seeds, relation matches, matched units,
    # supersession pointers, and parent refs appear only at L5–L6" (D3) is
    # enforced — by those levels never touching a serializer at all, not by
    # subtracting fields afterwards.
    out = {key: value for key, value in raw.items() if key in allowed}
    out = _strip_withheld_provenance(out, withheld_paths)
    if decision is not None and decision.release_strip:
        out = bridges.strip_provenance(out, decision.release_strip)
    return out


def project_hits(
    hits: Sequence[Any],
    *,
    compact: bool = False,
    withheld_paths: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Serialize a released candidate list — the only path out of `op_find`.

    An unannotated candidate (`decision is None`) came off the empty-policy
    fast path and renders at L6; anything the release plane touched renders at
    its own decision's level.
    """
    out: list[dict[str, Any]] = []
    for hit in hits:
        decision = getattr(hit, "decision", None)
        level = decision.level if decision is not None else LEVEL_FULL
        projected = project(
            hit,
            level,
            decision=decision,
            compact=compact,
            withheld_paths=withheld_paths,
        )
        if projected is not None:
            out.append(projected)
    return out


def annotate_pack(pack: dict[str, Any] | None, release: AnnotatedHits) -> dict[str, Any] | None:
    """Carry governance context in the pack header, never sub-notice content.

    `assemble_pack` already runs over released hits only, so `packed_paths`
    is clean by construction. What still needs scrubbing is everything the
    pack derives from the graph — neighbours, contradictions, claims — since
    those walk edges out of permitted pages and can land on a withheld one.
    """
    if pack is None:
        return None
    withheld = release.withheld_paths
    if withheld:
        for section in (
            "packed_paths",
            "claims",
            "neighborhood",
            "contradictions",
            "semantic_units",
            "semantic_blocks",
        ):
            values = pack.get(section)
            if isinstance(values, list):
                pack[section] = [v for v in values if not _names_withheld(v, withheld)]
    # Emit NOTHING when nothing was withheld. A `governance` block on every
    # governed-vault pack tells any audience that governance is active, and
    # the policy fingerprint is a SHA-256 over the policy bytes — poll it and
    # you learn exactly when the owner retuned their rules. Same reasoning
    # that keeps `release_level` off a fully-disclosed page.
    # N4: key on NOTICES, never on the withheld set. At L0 the item is dropped
    # silently and no notice is emitted (D4) — so a `governance` block whose
    # notices list is empty communicates exactly one fact, "something was
    # hidden from you", which is the existence oracle the silent L0 path was
    # designed to prevent. An empty block is a louder oracle than a notice.
    if not release.notices:
        return pack
    governance: dict[str, Any] = {"notices": list(release.notices)}
    # The fingerprint is an owner-facing diagnostic, never a third-party one.
    if release.audience_is_owner:
        governance["fingerprint"] = release.fingerprint
    pack["governance"] = governance
    return pack


def register_projector(
    kind: str,
    allowed_fields: Iterable[str],
    *,
    validator: Callable[[Mapping[str, Any]], dict[str, Any] | None]
    | None
    | object = _PROJECTOR_VALIDATOR_UNSET,
) -> None:
    """Register the wire allow-list for one payload kind."""
    _PROJECTORS[kind] = frozenset(allowed_fields)
    if validator is _PROJECTOR_VALIDATOR_UNSET:
        return
    if validator is None:
        _PROJECTOR_VALIDATORS.pop(kind, None)
    else:
        _PROJECTOR_VALIDATORS[kind] = validator  # type: ignore[assignment]


def registered_kinds() -> frozenset[str]:
    return frozenset(k for k, v in _PROJECTORS.items() if v)


# ---------------------------------------------------------------------------
# Per-request decision memo (D2)
# ---------------------------------------------------------------------------

_DECISION_MEMO_MAX = 4096
_DECISION_MEMO: OrderedDict[tuple[Any, ...], Decision] = OrderedDict()


def clear_decision_memo() -> None:
    _DECISION_MEMO.clear()


def decision_memo_size() -> int:
    return len(_DECISION_MEMO)


def _grants_hash(policy: Policy) -> str:
    """Stable digest of the grants participating in this decision.

    Its own key component (not folded into the policy fingerprint) because a
    later change narrows `active_grants` to a live session — at which point
    this value moves per request while the fingerprint does not.
    """
    digest = hashlib.sha256()
    for grant in policy.grants:
        digest.update(f"{grant.id}\0{grant.audience}\0{grant.ceiling}\0".encode())
        digest.update("\0".join(grant.scope_ids).encode())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _declared_purpose(
    vault_root: Path,
    who: RequestPrincipal,
    explicit: str | None,
) -> str | None:
    if explicit is not None:
        return explicit
    if who.purpose is not None:
        return who.purpose
    context = who.verified_authorization_session
    if isinstance(context, authorization_session_lifecycle.AuthorizationSessionContext):
        connection: sqlite3.Connection | None = None
        try:
            connection = store.open_authorization_session_connection(vault_root)
            return authorization_session_authority.active_session_purpose(
                connection,
                context=context,
                audience=who.audience_id,
                now=int(__import__("time").time()),
            )
        except (
            authorization_session_lifecycle.AuthorizationSessionUnavailable,
            FileNotFoundError,
            OSError,
            sqlite3.Error,
            store.UnsupportedGovernanceSchema,
        ):
            raise AuthorizationSessionDecisionUnavailable from None
        finally:
            if connection is not None:
                connection.close()
    return store.active_session_purpose(
        vault_root,
        audience=who.audience_id,
        authorization_session=who.authorization_session_id,
    )


def _resolve_l4_bridge(
    vault_root: Path,
    decision: Decision,
    *,
    policy: Policy,
    audience: str,
) -> Decision:
    """Bind an L4 decision to live approved content, never to its opaque id.

    The pure policy meet deliberately carries only a release-grant id.  Bridge
    bytes and dependency state are mutable inputs, so this resolution happens
    after (and outside) the decision memo on every request.  A missing or stale
    approval lowers to the already-authorized L3 abstract without borrowing
    source text.
    """
    if decision.level != LEVEL_EXCERPT_REDACTED:
        return decision
    bridge_id = decision.bridge
    projection = (
        bridges.resolve_approved_abstraction(
            vault_root,
            bridge_id,
            policy=policy,
            audience=audience,
        )
        if bridge_id
        else None
    )
    if projection is None or not projection.allowed:
        return replace(
            decision,
            level=LEVEL_ABSTRACT,
            options={key: value for key, value in decision.options.items() if key != "bridge"},
            bridge=None,
            bridge_abstraction=None,
            release_reason=(
                projection.reason if projection is not None else bridges.RELEASE_UNAPPROVED
            ),
            release_grant_id=None,
            release_strip=(),
            release_dependency_digest=None,
        )
    return replace(
        decision,
        bridge_abstraction=projection.abstraction,
        release_grant_id=projection.grant.id if projection.grant else None,
        release_strip=projection.strip_identities,
        release_dependency_digest=projection.dependency_digest,
    )


def _applicable_org_ceiling(policy: Policy, decision: Decision) -> int:
    participating = set(decision.rule_ids)
    return min(
        (
            rule.ceiling
            for rule in policy.rules
            if rule.kind == "org_cap" and rule.id in participating
        ),
        default=DISCLOSURE_MAX,
    )


def _mint_escalation_quietly(
    vault_root: Path,
    *,
    rel_path: str,
    who: RequestPrincipal,
    purpose: str | None,
    decision: Decision,
    requested_level: int,
    org_ceiling: int,
    expected_content_hash: str | None = None,
) -> str | None:
    context = who.verified_authorization_session
    if not isinstance(
        context,
        authorization_session_lifecycle.AuthorizationSessionContext,
    ):
        return tokens.mint_quietly(
            vault_root,
            paths=[rel_path],
            audience=who.audience_id,
            max_level=requested_level,
            authorization_session=who.authorization_session_id,
            purpose=purpose,
            org_ceiling=org_ceiling,
        )
    connection: sqlite3.Connection | None = None
    try:
        now = int(__import__("time").time())
        custody = authorization_custody.load_authorization_custody(vault_root, now=now)
        if (
            custody.keyring.cell_id != context.cell_id
            or custody.keyring.logical_vault_id != context.logical_vault_id
            or custody.keyring.keyring_id != context.keyring_id
        ):
            return None
        connection = store.open_authorization_session_connection(vault_root)
        if expected_content_hash is None:
            fingerprint = hashlib.sha256((vault_root / rel_path).read_bytes()).hexdigest()
        elif re.fullmatch(r"[0-9a-f]{64}", expected_content_hash):
            fingerprint = expected_content_hash
        else:
            return None
        expires_at = min(
            context.expires_at,
            now + tokens.DEFAULT_TTL_SECONDS,
        )
        if expires_at <= now:
            return None
        return authorization_session_authority.mint_escalation_token(
            connection=connection,
            context=context,
            signing_key=custody.keyring.active_key.key,
            audience=who.audience_id,
            purpose=purpose,
            max_level=requested_level,
            org_ceiling=org_ceiling,
            paths=(rel_path,),
            fingerprints=(fingerprint,),
            scope_ids=tuple(sorted(decision.scope_ids)),
            now=now,
            expires_at=expires_at,
        )
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        authorization_session_lifecycle.AuthorizationSessionUnavailable,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        store.UnsupportedGovernanceSchema,
    ):
        return None
    finally:
        if connection is not None:
            connection.close()


def _active_grants_for_snapshot(
    vault_root: Path,
    *,
    policy: Policy,
    audience: str,
    purpose: str | None,
    rel_path: str,
    content_hash: str | None,
    scope_ids: Iterable[str],
    authorization_context: authorization_session_lifecycle.AuthorizationSessionContext | None,
) -> tuple[list[policy_module.StandingGrant], str]:
    """Resolve request-local grants against one exact content/membership snapshot."""

    active_grants = list(policy.grants)
    session_identity = (
        "v3-session-grants-unscoped" if authorization_context is None else "no-session-grants"
    )
    if authorization_context is None or content_hash is None:
        return active_grants, session_identity

    connection: sqlite3.Connection | None = None
    try:
        connection = store.open_authorization_session_connection(vault_root)
        session_grants, session_identity = authorization_session_authority.active_session_grants(
                connection=connection,
                context=authorization_context,
                audience=audience,
                purpose=purpose,
                path=rel_path,
                fingerprint=content_hash,
                scope_ids=tuple(sorted(scope_ids)),
                policy_fingerprint=policy.fingerprint,
                now=int(__import__("time").time()),
            )
    except (
        authorization_session_lifecycle.AuthorizationSessionUnavailable,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        store.UnsupportedGovernanceSchema,
    ):
        session_grants = ()
        session_identity = "session-authority-unavailable"
    finally:
        if connection is not None:
            connection.close()
    active_grants.extend(
        policy_module.StandingGrant(
            id=grant.grant_id,
            source="authorization-session",
            scope_ids=grant.scope_ids,
            audience=grant.audience,
            ceiling=grant.ceiling,
        )
        for grant in session_grants
    )
    return active_grants, session_identity


def _is_markdown_path(rel_path: str) -> bool:
    """The ONE markdown-suffix predicate, case-insensitive.

    Every other place in this release plane that needs to know whether a
    path names a markdown page reuses this — `_decide_path` itself, and the
    packet-reference candidate/interpretation logic below — so the test is
    never restated (and never case-sensitively, which `Secret.MD` needed).
    """
    return rel_path.lower().endswith(".md")


def _decide_path(
    vault_root: Path,
    rel_path: str,
    *,
    policy: Policy,
    audience: str,
    purpose: str | None,
    grants_hash: str,
    authorization_session: str | None = None,
    authorization_context: authorization_session_lifecycle.AuthorizationSessionContext
    | None = None,
    expected_content_hash: str | None = None,
) -> Decision | None:
    """Decide one path, memoized per request identity AND page identity.

    The key must close over BOTH ends of the decision:

    - **Page identity** (`st_mtime_ns`, `st_size`). Without it, retagging a
      note into a restricted scope is a no-op for any principal already
      served: the policy fingerprint has not moved (the policy did not
      change — the *page* did), so a stale permissive decision is replayed
      for the process lifetime and revocation never takes effect. The
      kernel's own `membership._MEMO` already keys on `mtime_ns` for exactly
      this reason; this matches that precedent.
    - **`vault_root`**. `policy._content_fingerprint` hashes only each
      document's relative path and bytes, so two vaults sharing a
      `_Governance/` tree produce the SAME fingerprint. Without the root in
      the key, whichever vault is decided first wins, and vault A's
      restricted page is served at vault B's permissive level (or the
      reverse).

    The `stat()` is taken BEFORE the memo lookup — it is the cache-validity
    probe, not an afterthought — and a stat failure fails closed with `None`
    rather than falling through to a decision.
    """
    if lifecycle.is_tombstoned(vault_root, rel_path):
        return None
    full_path = vault_root / rel_path
    try:
        st = full_path.stat()
    except OSError:
        return None

    raw: bytes | None = None
    live_content_hash: str | None = None
    if _is_markdown_path(rel_path):
        try:
            raw = full_path.read_bytes()
        except OSError:
            return None
        live_content_hash = hashlib.sha256(raw).hexdigest()
        if expected_content_hash is not None and expected_content_hash != live_content_hash:
            return None
    mtime = st.st_mtime
    if not _is_markdown_path(rel_path):
        # NON-MARKDOWN. Never hand a binary to the markdown parser: it cannot
        # decode one, and its failure used to arrive here as `None` — a value
        # meaning BOTH "unreadable" and "not permitted". That single
        # conflation broke both directions at once (withheld media stayed
        # enumerated in the walk; permitted media stopped downloading for
        # everyone, owner included) and logged a `utf-8 codec` warning per
        # decision on the way. Path/ref selectors decide a binary with no
        # parse at all; semantic selectors remain explicitly unresolved until
        # companion descriptors are implemented.
        try:
            scope_ids = membership_module.evaluate_path_only(
                vault_root, rel_path, policy
            ).require_classified()
        except membership_module.MembershipUnresolved:
            return None
    else:
        page = find_corpus.parse_page(full_path, mtime, vault_root, content=raw)
        if page is None:
            # A `.md` that will not decode IS a genuine read failure, which is
            # the one meaning `None` still carries.
            return None
        try:
            scope_ids = membership_module.evaluate_snapshot(
                page, policy, content_hash=live_content_hash or ""
            )
        except membership_module.MembershipUnresolved:
            # Same fail-closed signal as the stat failure above: no decision,
            # so every consumer withholds. Reached on a TOCTOU race — the page
            # was stattable one line ago and is not now — which is exactly
            # when guessing is least defensible.
            return None
    if live_content_hash is None and authorization_context is not None:
        try:
            live_content_hash = hashlib.sha256(full_path.read_bytes()).hexdigest()
        except OSError:
            return None

    active_grants, session_identity = _active_grants_for_snapshot(
        vault_root,
        policy=policy,
        audience=audience,
        purpose=purpose,
        rel_path=rel_path,
        content_hash=live_content_hash,
        scope_ids=scope_ids,
        authorization_context=authorization_context,
    )

    key = (
        str(vault_root),
        policy.fingerprint,
        rel_path,
        audience,
        purpose,
        grants_hash,
        session_identity,
        st.st_mtime_ns,
        st.st_size,
        live_content_hash,
    )
    cached = _DECISION_MEMO.get(key)
    if cached is not None and (raw is None or not bridges.maybe_bridge(raw)):
        _DECISION_MEMO.move_to_end(key)
        return _resolve_l4_bridge(
            vault_root,
            cached,
            policy=policy,
            audience=audience,
        )
    decision = decide(
        scope_ids,
        audience=audience,
        purpose=purpose,
        policy=policy,
        active_grants=active_grants,
    )
    if raw is not None:
        admission = bridges.admit(
            vault_root,
            rel_path,
            raw,
            policy=policy,
            audience=audience,
        )
        if admission.is_bridge:
            if not admission.allowed:
                decision = replace(
                    decision,
                    level=LEVEL_NONE,
                    options={},
                    notice=None,
                    bridge=None,
                    release_reason=admission.reason,
                )
            else:
                decision = replace(
                    decision,
                    release_grant_id=admission.grant.id if admission.grant else None,
                    release_strip=admission.strip_identities,
                    release_dependency_digest=admission.dependency_digest,
                )

    _DECISION_MEMO[key] = decision
    _DECISION_MEMO.move_to_end(key)
    while len(_DECISION_MEMO) > _DECISION_MEMO_MAX:
        _DECISION_MEMO.popitem(last=False)
    return _resolve_l4_bridge(
        vault_root,
        decision,
        policy=policy,
        audience=audience,
    )


def resolve_visible_identifier(
    vault_root: Path,
    value: str,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> str:
    """Resolve a memory ref after removing candidates invisible to the caller.

    Raw reference resolution decides ambiguity from every matching page. That
    leaks both the presence and count of L0 pages and lets one hidden duplicate
    shadow an otherwise unique visible page. A governed content route must
    instead behave as if those candidates were physically absent.
    """

    vault_root = Path(vault_root)
    raw = str(value or "").strip()
    memory_id = memory_refs.parse_memory_ref(raw)
    if not raw.lower().startswith(memory_refs.REF_PREFIX):
        return memory_refs.resolve_identifier(vault_root, raw)
    if memory_id is None:
        raise memory_refs.ReferenceError(
            "INVALID_REFERENCE", f"invalid memory reference: {raw!r}"
        )

    visible = _visible_candidates(
        vault_root,
        memory_refs.paths_for_ids_read_only(vault_root, (memory_id,)).get(memory_id, ()),
        principal=principal,
        purpose=purpose,
    )
    if len(visible) > 1:
        raise memory_refs.ReferenceError(
            "AMBIGUOUS_REFERENCE",
            f"memory id {memory_id} appears in {len(visible)} pages",
        )
    if not visible:
        raise memory_refs.ReferenceError(
            "REFERENCE_NOT_FOUND", f"memory id not found: {memory_id}"
        )
    return visible[0]


def _visible_candidates(
    vault_root: Path,
    paths: Iterable[str],
    *,
    principal: RequestPrincipal | None,
    purpose: str | None,
) -> tuple[str, ...]:
    """The pages holding one id that the caller may see, as if the rest were absent."""
    candidates = tuple(
        rel_path
        for rel_path in paths
        if not reserved_paths.classify_logical(rel_path).blocked
        if not lifecycle.is_tombstoned(vault_root, rel_path)
    )
    policy = policy_module.load(vault_root)
    who = principal if principal is not None else effective_principal()
    if policy.empty:
        return candidates
    if policy.blocked or not who.resolved:
        return ()
    declared_purpose = _declared_purpose(vault_root, who, purpose)
    grants_hash = _grants_hash(policy)
    return tuple(
        rel_path
        for rel_path in candidates
        if (
            decision := _decide_path(
                vault_root,
                rel_path,
                policy=policy,
                audience=who.audience_id,
                purpose=declared_purpose,
                grants_hash=grants_hash,
                authorization_session=who.authorization_session_id,
                authorization_context=who.verified_authorization_session,
            )
        )
        is not None
        and decision.level > LEVEL_NONE
    )


def visible_memory_refs(
    vault_root: Path,
    values: Iterable[str],
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> frozenset[str]:
    """The memory refs among `values` that name exactly one page the caller may see.

    `resolve_visible_identifier` for a batch: one corpus scan for all of them,
    never one per ref, and the scan runs whatever the refs are, so the work
    says nothing about which of them exist. An unknown, withheld or ambiguous
    ref is simply absent from the answer, and the three are indistinguishable.
    """
    return frozenset(
        visible_memory_ref_paths(vault_root, values, principal=principal, purpose=purpose)
    )


def visible_memory_ref_paths(
    vault_root: Path,
    values: Iterable[str],
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> dict[str, str]:
    """`visible_memory_refs` with the one visible page each ref names, from the
    same single scan: `{ref: vault-relative path}`."""
    wanted = {
        value: memory_id
        for value in dict.fromkeys(str(item or "").strip() for item in values)
        if (memory_id := memory_refs.parse_memory_ref(value)) is not None
    }
    if not wanted:
        return {}
    found = memory_refs.paths_for_ids_read_only(Path(vault_root), wanted.values())
    out: dict[str, str] = {}
    for value, memory_id in wanted.items():
        visible = _visible_candidates(
            Path(vault_root),
            found.get(memory_id, ()),
            principal=principal,
            purpose=purpose,
        )
        if len(visible) == 1:
            out[value] = visible[0]
    return out


def _scope_label(policy: Policy, decision: Decision) -> str | None:
    labels = [policy.scopes[sid].name or sid for sid in decision.scope_ids if sid in policy.scopes]
    return ", ".join(sorted(labels)) if labels else None


# ---------------------------------------------------------------------------
# Decision annotation (D2 / D4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnnotatedHits:
    """Released candidates, their notices, and the paths that were withheld."""

    hits: list[Any]
    notices: list[dict[str, Any]] = field(default_factory=list)
    withheld_paths: frozenset[str] = frozenset()
    active: bool = False
    blocked: bool = False
    fingerprint: str = "missing"
    #: Whether the deciding audience is the vault owner. Gates owner-facing
    #: diagnostics (the policy fingerprint) out of third-party responses.
    audience_is_owner: bool = False


def annotate_projected_hits(
    vault_root: Path,
    hits: list[Any],
    *,
    policy: Policy,
    principal: RequestPrincipal,
    purpose: str | None,
    withheld_paths: frozenset[str],
) -> AnnotatedHits:
    """Finalize already-selected projection hits without reopening source bytes."""

    released: list[Any] = []
    notices: list[dict[str, Any]] = []
    for hit in hits:
        decision = getattr(hit, "decision", None)
        content_hash = getattr(hit, "snapshot_hash", None)
        if not isinstance(decision, Decision) or not (
            isinstance(content_hash, str) and re.fullmatch(r"[0-9a-f]{64}", content_hash)
        ):
            raise ValueError("projected release snapshot is invalid")
        rel_path = _hit_path(hit)
        if not rel_path:
            raise ValueError("projected release snapshot is invalid")
        if decision.level >= RELEASE_FLOOR:
            released.append(hit)
            _outcome_for_decision(
                vault_root,
                rel_path,
                decision=decision,
                policy=policy,
                audience=principal.audience_id,
                outcome="released",
                purpose=purpose,
                content_hash=content_hash,
                purpose_is_bound=True,
            )
            continue
        notice = _notice(
            decision.level,
            rule_ids=decision.rule_ids,
            scope_label=_scope_label(policy, decision),
            options=decision.options,
            bridge_abstraction=decision.bridge_abstraction,
        )
        requested_level = (
            RELEASE_FLOOR
            if (
                principal.verified_authorization_session is not None
                or principal.authorization_session_id is not None
            )
            else decision.level
        )
        token = (
            _mint_escalation_quietly(
                Path(vault_root),
                rel_path=rel_path,
                who=principal,
                purpose=purpose,
                decision=decision,
                requested_level=requested_level,
                org_ceiling=_applicable_org_ceiling(policy, decision),
                expected_content_hash=content_hash,
            )
            if isinstance(
                principal.verified_authorization_session,
                authorization_session_lifecycle.AuthorizationSessionContext,
            )
            else None
        )
        if isinstance(token, str):
            notice["escalation_token"] = token
        notices.append(notice)
        _outcome_for_decision(
            vault_root,
            rel_path,
            decision=decision,
            policy=policy,
            audience=principal.audience_id,
            outcome="withheld",
            purpose=purpose,
            content_hash=content_hash,
            purpose_is_bound=True,
        )
    if withheld_paths:
        _record_outcome({"decision": "withheld", "count": len(withheld_paths)})
    return AnnotatedHits(
        hits=released,
        notices=notices,
        withheld_paths=withheld_paths,
        active=True,
        fingerprint=policy.fingerprint,
        audience_is_owner=(principal.resolved and principal.audience_id == OWNER_AUDIENCE),
    )


#: Pre-committed over-fetch: the pool size is a function of the REQUEST alone
#: (D4), so the shown count can be backfilled without revealing how many
#: candidates were withheld. Never applied on the empty-policy fast path.
_OVERFETCH_CAP = 30


def pool_limit(limit: int) -> int:
    """The over-fetch pool size for a request asking for `limit` items."""
    return limit + min(max(limit, 1), _OVERFETCH_CAP)


def gate_state(vault_root: Path) -> tuple[Policy, bool]:
    """`(policy, needs_overfetch)` — the cheap pre-`find()` probe.

    On an ungoverned vault this performs only a bounded set of policy-marker,
    sidecar, and lifecycle probes, independent of corpus size, keeping the
    empty-policy fast path genuinely fast.
    """
    policy = policy_module.load(Path(vault_root))
    return policy, (not policy.empty or bool(lifecycle.tombstoned_paths(vault_root)))


def _hit_path(hit: Any) -> str:
    return str(getattr(hit, "path", None) or getattr(hit, "parent_path", "") or "")


def annotate_hits(
    vault_root: Path,
    hits: list[Any],
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
    limit: int | None = None,
) -> AnnotatedHits:
    """Attach release decisions to `hits` and drop what may not be released.

    Runs strictly AFTER `find()` returns (`commands.py:901→902`) and before
    `assemble_pack`/serialize, so nothing principal-dependent can reach the
    shared `_FIND_CACHE`.
    """
    vault_root = Path(vault_root)
    policy = policy_module.load(vault_root)
    who = principal if principal is not None else effective_principal()
    declared_purpose = _declared_purpose(vault_root, who, purpose)
    effective_limit = len(hits) if limit is None else limit

    tombstoned = frozenset(
        path
        for hit in hits
        if (path := _hit_path(hit)) and lifecycle.is_tombstoned(vault_root, path)
    )
    if tombstoned:
        hits = [hit for hit in hits if _hit_path(hit) not in tombstoned]

    # (1) Open fast path — no governance configured.
    if policy.empty:
        return AnnotatedHits(
            hits=hits,
            withheld_paths=tombstoned,
            active=bool(tombstoned),
            fingerprint=policy.fingerprint,
        )

    # (2) Fail-closed floor — a refused cold-start compile, or an identity that
    #     should have resolved and did not. Both are DISCLOSURE_MIN for every
    #     item, and L0 is silent: no notices, no count, no marker.
    if policy.blocked or not who.resolved:
        for _hit in hits:
            _record_blocked_outcome(who.audience_id)
        return AnnotatedHits(
            hits=[],
            notices=[],
            withheld_paths=frozenset(_hit_path(h) for h in hits if _hit_path(h)),
            active=True,
            blocked=True,
            fingerprint=policy.fingerprint,
            audience_is_owner=(who.resolved and who.audience_id == OWNER_AUDIENCE),
        )

    # (3) Normal per-item decision, in two passes: every candidate is decided
    #     first, so the graph guard in pass 2 can see the COMPLETE withheld
    #     set — a neighbour may be seeded from a page decided later in rank
    #     order, and a one-pass loop would let it through.
    grants_hash = _grants_hash(policy)
    permitted: list[Any] = []
    pending_notices: list[tuple[str, Decision, dict[str, Any]]] = []
    withheld: set[str] = set()

    for hit in hits:
        rel_path = _hit_path(hit)
        if not rel_path:
            permitted.append(hit)
            continue
        decision = _decide_path(
            vault_root,
            rel_path,
            policy=policy,
            audience=who.audience_id,
            purpose=declared_purpose,
            grants_hash=grants_hash,
            authorization_session=who.authorization_session_id,
            authorization_context=who.verified_authorization_session,
            expected_content_hash=getattr(hit, "snapshot_hash", None),
        )
        if decision is None:
            # The page vanished or would not parse: it cannot be shown to
            # have been permitted, so it is withheld rather than released.
            withheld.add(rel_path)
            continue
        if decision.level >= RELEASE_FLOOR:
            hit.decision = decision
            permitted.append(hit)
            continue
        withheld.add(rel_path)
        if decision.level >= LEVEL_NOTICE:
            # Built now, but NOT minted yet — see the truncation below.
            pending_notices.append(
                (
                    rel_path,
                    decision,
                    _notice(
                        decision.level,
                        rule_ids=decision.rule_ids,
                        scope_label=_scope_label(policy, decision),
                        options=decision.options,
                    ),
                )
            )

    frozen_withheld = frozenset(withheld)
    permitted = [h for h in permitted if not _seeded_only_by_withheld(h, frozen_withheld)]
    released = permitted[:effective_limit]
    # D4: notices occupy a slot only once the over-fetch pool is exhausted —
    # until then the withheld slot is backfilled by the next permitted
    # candidate, so the count does not reveal that anything was withheld.
    spare = max(0, effective_limit - len(released))
    notices: list[dict[str, Any]] = []
    for rel_path, decision, notice in pending_notices[:spare]:
        # Mint only for notices that are actually returned.  Session-aware
        # clients get an approval capability for the requested releasable
        # representation, capped by the applicable organization ceiling;
        # legacy clients retain their historical non-escalating notice token.
        requested_level = (
            RELEASE_FLOOR
            if (
                who.verified_authorization_session is not None
                or who.authorization_session_id is not None
            )
            else decision.level
        )
        org_ceiling = _applicable_org_ceiling(policy, decision)
        token = _mint_escalation_quietly(
            vault_root,
            rel_path=rel_path,
            who=who,
            purpose=declared_purpose,
            decision=decision,
            requested_level=requested_level,
            org_ceiling=org_ceiling,
        )
        if token is not None:
            notice["escalation_token"] = token
        notices.append(notice)
        _outcome_for_decision(
            vault_root,
            rel_path,
            decision=decision,
            policy=policy,
            audience=who.audience_id,
            outcome="withheld",
            purpose=declared_purpose,
        )
    for hit in released:
        rel_path = _hit_path(hit)
        decision = getattr(hit, "decision", None)
        if rel_path and decision is not None:
            _outcome_for_decision(
                vault_root,
                rel_path,
                decision=decision,
                policy=policy,
                audience=who.audience_id,
                outcome="released",
                purpose=declared_purpose,
            )
    hidden_count = len(withheld) - len(notices)
    if hidden_count:
        _record_outcome({"decision": "withheld", "count": hidden_count})
    return AnnotatedHits(
        hits=released,
        notices=notices,
        withheld_paths=frozen_withheld,
        active=True,
        blocked=False,
        fingerprint=policy.fingerprint,
        audience_is_owner=(who.resolved and who.audience_id == OWNER_AUDIENCE),
    )


# ---------------------------------------------------------------------------
# Graph lane (D4) — a withheld seed must not smuggle its neighbours out
# ---------------------------------------------------------------------------

#: Lanes a hit can match on its own. A graph-expanded hit with none of these
#: entered results *only* by hopping from its seed.
_OWN_LANE_FIELDS = (
    "bm25_rank",
    "vector_rank",
    "vector_score",
    "keyword_rank",
    "clip_rank",
    "clip_score",
    "rerank_input_rank",
)


def _seeded_only_by_withheld(hit: Any, withheld_paths: frozenset[str]) -> bool:
    """True when this hit's ONLY provenance is expansion from a withheld seed."""
    if not withheld_paths:
        return False
    provenance = getattr(hit, "graph_provenance", None)
    if provenance is None or provenance.seed not in withheld_paths:
        return False
    return not any(getattr(hit, name, None) is not None for name in _OWN_LANE_FIELDS)


def guard_seed(payload: dict[str, Any], withheld_paths: frozenset[str]) -> dict[str, Any]:
    """Drop graph seeds, nodes, and edge endpoints that name a sub-notice item.

    Pure over an already-built `graph_context` payload: an edge survives only
    when BOTH endpoints survived, so a withheld node cannot leave a dangling
    reference behind as an existence oracle.
    """
    if not withheld_paths:
        return payload
    dropped_keys: set[str] = set()
    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        kept_nodes = []
        for node in nodes:
            if isinstance(node, Mapping) and str(node.get("path") or "") in withheld_paths:
                dropped_keys.add(str(node.get("node_key") or ""))
                continue
            kept_nodes.append(node)
        payload["nodes"] = kept_nodes
    seeds = payload.get("seeds")
    if isinstance(seeds, list):
        payload["seeds"] = [
            seed
            for seed in seeds
            if not (
                isinstance(seed, Mapping)
                and (
                    str(seed.get("path") or "") in withheld_paths
                    or str(seed.get("node_key") or "") in dropped_keys
                )
            )
            and not _names_withheld(seed, withheld_paths)
        ]
    edges = payload.get("edges")
    if isinstance(edges, list):
        payload["edges"] = [
            edge
            for edge in edges
            if not (
                isinstance(edge, Mapping)
                and (
                    str(edge.get("src_key") or "") in dropped_keys
                    or str(edge.get("dst_key") or "") in dropped_keys
                )
            )
            and not _names_withheld(edge, withheld_paths)
        ]
    return payload


def guard_graph_context(
    vault_root: Path,
    payload: dict[str, Any],
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Apply the release decision to a `graph_context` result.

    Same three-state contract as `annotate_hits`: empty -> untouched; blocked
    (or an unresolved-but-expected principal) -> an empty, available-shaped
    neighborhood, since L0 must not even reveal that a neighborhood exists.
    """
    vault_root = Path(vault_root)
    policy = policy_module.load(vault_root)
    who = principal if principal is not None else effective_principal()
    tombstoned = frozenset(
        str(node.get("path") or "")
        for section in ("nodes", "seeds")
        for node in (payload.get(section) or [])
        if isinstance(node, Mapping)
        and node.get("path")
        and lifecycle.is_tombstoned(vault_root, str(node.get("path")))
    )
    if tombstoned:
        payload = guard_seed(payload, tombstoned)
    if policy.empty:
        return payload
    if policy.blocked or not who.resolved:
        _record_blocked_outcome(who.audience_id)
        payload["seeds"] = []
        payload["nodes"] = []
        payload["edges"] = []
        return payload

    grants_hash = _grants_hash(policy)
    declared_purpose = _declared_purpose(vault_root, who, purpose)
    candidate_paths = {
        str(node.get("path") or "")
        for section in ("nodes", "seeds")
        for node in (payload.get(section) or [])
        if isinstance(node, Mapping) and node.get("path")
    }
    withheld = {
        rel_path
        for rel_path in candidate_paths
        if (
            decision := _decide_path(
                vault_root,
                rel_path,
                policy=policy,
                audience=who.audience_id,
                purpose=declared_purpose,
                grants_hash=grants_hash,
                authorization_session=who.authorization_session_id,
                authorization_context=who.verified_authorization_session,
            )
        )
        is None
        or decision.level < RELEASE_FLOOR
    }
    for rel_path in candidate_paths:
        _outcome_for_decision(
            vault_root,
            rel_path,
            decision=_decide_path(
                vault_root,
                rel_path,
                policy=policy,
                audience=who.audience_id,
                purpose=declared_purpose,
                grants_hash=grants_hash,
                authorization_session=who.authorization_session_id,
                authorization_context=who.verified_authorization_session,
            ),
            policy=policy,
            audience=who.audience_id,
            outcome="withheld" if rel_path in withheld else "released",
            purpose=declared_purpose,
        )
    payload = guard_seed(payload, frozenset(withheld))
    for rel_path in sorted(candidate_paths):
        decision = _decide_path(
            vault_root,
            rel_path,
            policy=policy,
            audience=who.audience_id,
            purpose=declared_purpose,
            grants_hash=grants_hash,
            authorization_session=who.authorization_session_id,
            authorization_context=who.verified_authorization_session,
        )
        if decision is not None and decision.release_strip:
            payload = bridges.strip_provenance(payload, decision.release_strip)
    return payload


def guard_referents(
    vault_root: Path,
    payload: dict[str, Any],
    release: AnnotatedHits,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> dict[str, Any] | None:
    """Apply release decisions to entity candidates and their evidence paths."""
    if release.blocked:
        return None
    vault_root = Path(vault_root)
    guarded = copy.deepcopy(payload)
    policy, release_gate_active = gate_state(vault_root)
    who = principal if principal is not None else effective_principal()
    if policy.blocked or (not policy.empty and not who.resolved):
        _record_blocked_outcome(who.audience_id)
        return None
    if release_gate_active:
        guarded.pop("reasons", None)
        guarded.pop("omitted_candidate_count", None)

    tombstoned: set[str] = set()
    for section in ("resolved", "candidates"):
        for item in guarded.get(section, []):
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("path") or "")
            if path and lifecycle.is_tombstoned(vault_root, path):
                tombstoned.add(path)
            for evidence in item.get("evidence") or []:
                if not isinstance(evidence, Mapping):
                    continue
                for field_name in ("seed", "anchor", "path"):
                    evidence_path = evidence.get(field_name)
                    if isinstance(evidence_path, str) and lifecycle.is_tombstoned(
                        vault_root, evidence_path
                    ):
                        tombstoned.add(evidence_path)

    withheld = set(release.withheld_paths) | tombstoned
    decisions: dict[str, Decision | None] = {}
    if not policy.empty:
        grants_hash = _grants_hash(policy)
        declared_purpose = _declared_purpose(vault_root, who, purpose)
        candidate_paths = {
            str(item.get("path") or "")
            for section in ("resolved", "candidates")
            for item in guarded.get(section, [])
            if isinstance(item, Mapping) and item.get("path")
        }
        for rel_path in sorted(candidate_paths):
            decision = _decide_path(
                vault_root,
                rel_path,
                policy=policy,
                audience=who.audience_id,
                purpose=declared_purpose,
                grants_hash=grants_hash,
                authorization_session=who.authorization_session_id,
            )
            decisions[rel_path] = decision
            if decision is None or decision.level < RELEASE_FLOOR:
                withheld.add(rel_path)
            _outcome_for_decision(
                vault_root,
                rel_path,
                decision=decision,
                policy=policy,
                audience=who.audience_id,
                outcome="withheld" if rel_path in withheld else "released",
                purpose=declared_purpose,
            )

    frozen_withheld = frozenset(withheld)
    for section in ("resolved", "candidates"):
        kept: list[dict[str, Any]] = []
        for raw_item in guarded.get(section, []):
            if not isinstance(raw_item, Mapping):
                continue
            item = dict(raw_item)
            if _names_withheld(item.get("path"), frozen_withheld):
                continue
            evidence = item.get("evidence")
            if isinstance(evidence, list):
                item["evidence"] = [
                    value
                    for value in evidence
                    if not _names_withheld(value, frozen_withheld, reference_field=True)
                ]
            decision = decisions.get(str(item.get("path") or ""))
            if decision is not None and decision.release_strip:
                protected = {
                    key: item[key] for key in ("path", "title", "entity_type") if key in item
                }
                detail = {key: value for key, value in item.items() if key not in protected}
                stripped = bridges.strip_provenance(detail, decision.release_strip)
                item = dict(protected)
                if isinstance(stripped, Mapping):
                    item.update(stripped)
            kept.append(item)
        guarded[section] = kept

    expected = guarded.get("expected_count")
    resolved_count = len(guarded.get("resolved") or [])
    if isinstance(expected, int):
        if resolved_count > expected:
            guarded["status"] = "ambiguous"
            guarded.pop("unresolved_count", None)
        elif resolved_count == expected:
            guarded["status"] = "resolved"
            guarded.pop("unresolved_count", None)
        else:
            guarded["status"] = "partial" if resolved_count else "unresolved"
            guarded["unresolved_count"] = expected - resolved_count
    else:
        guarded["status"] = "resolved" if resolved_count else "unresolved"
        guarded.pop("unresolved_count", None)
    return guarded


def quick_page_visible(
    vault_root: Path,
    rel_path: str,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> bool:
    """Is `rel_path` visible to the CURRENT principal, decided on the release
    plane alone — no packet, no compile.

    For an activation `anchor` naming a page rather than an index row
    (design close-memory-loop, agent-picked page), the expensive part —
    `_carried_packet`'s full unit lane, current-state lookup and budget
    assembly — buys NOTHING when the answer was always going to be the
    refusal a withheld page gets: measured at 80 ms against 15 ms for an
    unknown ref, because the compile ran to completion before the release
    plane was ever consulted. This is that consultation, moved first.

    Reuses `_decide_path` — the SAME per-path decision `guard_working_set`
    makes after compiling, memoized per request identity AND page identity —
    so calling it again from `guard_working_set` for the identical path is
    the memo hit, never a second stat or a second parse.

    Errs towards compiling on anything this function does not itself fully
    resolve: an ungoverned vault (`policy.empty`) is visible outright, and a
    principal or policy state this function cannot decide returns `True` and
    leaves the actual call to `guard_working_set`, which already owns it and
    runs regardless. This can only ever produce an EARLY refusal matching
    what the guard would decide anyway, or a no-op that falls through to the
    unchanged compile-then-guard path — never a decision the guard would not
    also have made.
    """
    if lifecycle.is_tombstoned(vault_root, rel_path):
        return False
    policy, _release_gate_active = gate_state(vault_root)
    if policy.empty:
        return True
    if policy.blocked:
        return False
    who = principal if principal is not None else effective_principal()
    if not who.resolved:
        return False
    grants_hash = _grants_hash(policy)
    declared_purpose = _declared_purpose(vault_root, who, purpose)
    decision = _decide_path(
        vault_root,
        rel_path,
        policy=policy,
        audience=who.audience_id,
        purpose=declared_purpose,
        grants_hash=grants_hash,
        authorization_session=who.authorization_session_id,
        authorization_context=who.verified_authorization_session,
    )
    if decision is None:
        # Undecidable is not admissible: `guard_working_set` withholds an
        # existing-but-undecided path the same way. The caller has already
        # proven the path exists (`_eligible_agent_page`), so `None` here
        # means genuinely undecidable, never merely absent.
        return False
    return decision.level >= RELEASE_FLOOR


def page_release_filter(
    vault_root: Path,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> Callable[[str], bool] | None:
    """`quick_page_visible` for many pages in one request, or `None` when
    every page is released (an ungoverned vault with no tombstone).

    The policy, the tombstones, the principal, the grants hash and the
    declared purpose are resolved once, and each page costs only its own
    memoized decision (`_decide_path`). Answers exactly what
    `quick_page_visible` answers per page, so the per-page policy reload that
    re-signs the governance tree on every call is paid once, not per page."""
    root = Path(vault_root)
    policy, _release_gate_active = gate_state(root)
    who = principal if principal is not None else effective_principal()
    memo: dict[str, bool] = {}
    if policy.empty:
        tombstones = lifecycle.tombstoned_paths(root)
        if not tombstones:
            return None
        if lifecycle.FAIL_CLOSED_TOMBSTONE in tombstones:
            return lambda _rel_path: False
        return lambda rel_path: lifecycle._normalize_rel(rel_path) not in tombstones
    if policy.blocked or not who.resolved:
        return lambda _rel_path: False
    grants_hash = _grants_hash(policy)
    declared_purpose = _declared_purpose(root, who, purpose)

    def released(rel_path: str) -> bool:
        if rel_path in memo:
            return memo[rel_path]
        decision = None
        if not lifecycle.is_tombstoned(root, rel_path):
            decision = _decide_path(
                root,
                rel_path,
                policy=policy,
                audience=who.audience_id,
                purpose=declared_purpose,
                grants_hash=grants_hash,
                authorization_session=who.authorization_session_id,
                authorization_context=who.verified_authorization_session,
            )
        memo[rel_path] = decision is not None and decision.level >= RELEASE_FLOOR
        return memo[rel_path]

    return released


def guard_working_set(
    vault_root: Path,
    packet: dict[str, Any],
    release: AnnotatedHits,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> dict[str, Any] | None:
    """Apply release decisions to a working-memory packet (design D7).

    Sits beside `guard_referents` and takes the SAME release object hit
    projection gets, because the packet is assembled from the same pages and
    walks further: a typed neighbourhood, a Records collection's newest item, a
    supersession pointer. Each of those is a way for a permitted page to
    enumerate a withheld one, which is the disclosure the release ceiling exists
    to prevent.

    Three states, the same three every other consumer has: `empty` policy and no
    tombstones -> untouched; `blocked` or an unresolved-but-expected principal ->
    no packet at all; otherwise every named path is decided and every field that
    names a withheld one is dropped.

    `neighbourhood` is removed from every anchor unconditionally. It is private
    resolution state that exists so the compiler can bound its lanes and detect
    ambiguity; publishing it would hand an audience a page list it never asked
    for, and filtering it entry-by-entry would still disclose its SIZE.
    """
    if release.blocked:
        return None
    vault_root = Path(vault_root)
    guarded = copy.deepcopy(packet)
    policy, release_gate_active = gate_state(vault_root)
    who = principal if principal is not None else effective_principal()
    if policy.blocked or (not policy.empty and not who.resolved):
        _record_blocked_outcome(who.audience_id)
        return None

    named_paths, prose_names, interpretations, unresolvable = _working_set_paths(guarded)
    tombstoned = {
        path for path in named_paths if path and lifecycle.is_tombstoned(vault_root, path)
    }
    withheld = set(release.withheld_paths) | tombstoned
    if not release_gate_active and policy.empty and not withheld:
        # Nothing to decide, so nothing to resolve. A vault that has opted into no
        # governance must not depend on a DERIVED index for its reads: resolving
        # above this line made a sidecar hiccup abstain a request that had no
        # release decision to take. Governed vaults fall through and keep failing
        # closed.
        #
        # `unresolvable`/`interpretations` are deliberately NOT part of this
        # condition: an ungoverned vault has no release decision to withhold
        # from in the first place, so an ambiguous or malformed reference
        # here changes nothing.
        return guarded

    # Prose resolution happens only now, when the decision loop below (or the
    # already-withheld set) will actually use it.
    prose_resolved = _resolved_prose_names(vault_root, prose_names)
    resolved_paths = {path for paths in prose_resolved.values() for path in paths}
    named_paths |= resolved_paths
    withheld |= {
        path
        for path in resolved_paths
        if path and lifecycle.is_tombstoned(vault_root, path)
    }

    decisions: dict[str, Decision | None] = {}
    if not policy.empty:
        grants_hash = _grants_hash(policy)
        declared_purpose = _declared_purpose(vault_root, who, purpose)
        for rel_path in sorted(path for path in named_paths if path):
            decision = _decide_path(
                vault_root,
                rel_path,
                policy=policy,
                audience=who.audience_id,
                purpose=declared_purpose,
                grants_hash=grants_hash,
                authorization_session=who.authorization_session_id,
                authorization_context=who.verified_authorization_session,
            )
            decisions[rel_path] = decision
            if decision is not None:
                if decision.level < RELEASE_FLOOR:
                    withheld.add(rel_path)
            elif rel_path in tombstoned or (vault_root / rel_path).exists():
                # `_decide_path` returns `None` for BOTH a genuinely
                # tombstoned/unreadable/unclassifiable EXISTING path and a
                # path that simply does not exist. The latter is expected
                # for a PHANTOM interpretation reading (R3): `named_paths`
                # is the union of every candidate's readings
                # (`_interpretations_for`), and an ambiguous candidate's
                # non-real readings are validated as safe relative paths
                # (`_is_safe_relative_path`) but never claimed to exist.
                # Adding a phantom reading to `withheld` corrupts
                # `frozen`'s canonical-key comparisons (`_names_withheld`)
                # against every OTHER field in the packet -- and a phantom
                # reading is frequently IDENTICAL to the candidate's own
                # original text (`path.md#current`'s literal-reading IS
                # `ref` itself), so it falsely matched its own item, as
                # though a real withheld page shared that exact spelling --
                # dropping a unit under a policy scoped to an entirely
                # different folder. Only an existing-but-undecidable path is
                # withheld here; the invalid_refs computation below makes
                # the identical existence check for the phantom-vs-denied
                # distinction, against `decisions`/`tombstoned`/the
                # filesystem.
                withheld.add(rel_path)
            _outcome_for_decision(
                vault_root,
                rel_path,
                decision=decision,
                policy=policy,
                audience=who.audience_id,
                outcome="withheld" if rel_path in withheld else "released",
                purpose=declared_purpose,
            )

    # A candidate the guard could not resolve to a single real page has
    # a SET of interpretations instead (R3): a plain string containing `#`
    # or `|` is genuinely ambiguous between "a filename with that
    # character" and "a path plus a fragment/alias", so every reading is a
    # hypothesis, not a guess to make. `invalid_refs` starts from
    # `unresolvable` -- a candidate with no safety-valid interpretation at
    # all, a pure syntax fact independent of policy -- and, only when an
    # actual policy exists to decide against, ALSO gains any candidate
    # whose readings are not every-one-admitted: none of them existed, or
    # at least one that did was not released. An interpretation the decide
    # loop above already decided is read from `decisions`; one it never
    # reached (unresolved names, or simply undecided under an empty
    # policy) is checked for existence directly -- never `stat()` on one
    # that failed `_is_safe_relative_path`, since `interpretations` never
    # contains one. Withheld by exact text match on the ORIGINAL candidate
    # (`_value_names_an_invalid_reference`, applied per item below), not
    # through `frozen`/`_names_withheld`: that matcher compares CANONICAL
    # keys, and `_canonical_reference` returns `None` for a candidate that
    # unwraps to an empty string, which can never equal any canonical key,
    # including its own.
    invalid_refs: set[str] = set(unresolvable)
    if not policy.empty:
        for candidate, readings in interpretations.items():
            existing_decisions: list[Decision | None] = []
            for reading in readings:
                decision = decisions.get(reading)
                if decision is not None:
                    existing_decisions.append(decision)
                elif reading in tombstoned or (vault_root / reading).exists():
                    existing_decisions.append(None)
            if not existing_decisions or any(
                d is None or d.level < RELEASE_FLOOR for d in existing_decisions
            ):
                invalid_refs.add(candidate)

    # The match set is wider than the withheld PATH set on purpose. `_withheld_keys`
    # derives its comparison keys from filenames, so a prose link spelled as the
    # page's TITLE — `[[Kill switch for risky releases]]` — canonicalises to
    # something that is no filename stem and matched nothing, even though the path
    # it resolves to was decided and withheld. Every name that resolved to a
    # withheld path is therefore added as its own match key, in BOTH the spelling
    # the prose contained and its normalised form: the matcher casefolds without
    # normalising, so a normalised key alone misses an NBSP or full-width spelling.
    #
    # Deliberately local to this guard. The same gap exists in the shared
    # `_withheld_keys` that `guard_referents` and hit projection use, and fixing it
    # there changes what every consumer strips; that root cause gets its own change.
    frozen = frozenset(
        withheld
        | {
            name
            for name, paths in prose_resolved.items()
            if any(path in withheld for path in paths)
        }
    )
    #: Sections whose removals are reported. `ambiguity` and `missing` are
    #: excluded: the first is a diagnostic about resolution rather than material,
    #: and the second is where the markers themselves live.
    removed: dict[str, int] = {}

    def _note_removal(section: str, before: int, after: int) -> None:
        if after < before:
            removed[section] = before - after

    original_anchors = [
        item for item in guarded.get("anchors") or () if isinstance(item, Mapping)
    ]
    guarded["anchors"] = [
        anchor
        for anchor in (
            _guarded_anchor(item, frozen, decisions, invalid_refs)
            for item in original_anchors
        )
        if anchor is not None
    ]
    _note_removal("anchors", len(original_anchors), len(guarded["anchors"]))

    original_recent = [
        item for item in guarded.get("recent_context") or () if isinstance(item, Mapping)
    ]
    guarded["recent_context"] = [
        entry
        for entry in (
            _guarded_recent(item, frozen, invalid_refs) for item in original_recent
        )
        if entry is not None
    ]
    _note_removal("recent_context", len(original_recent), len(guarded["recent_context"]))
    # `used_chars` is the caller's account of what it was charged for, and the
    # compiler budgeted these entries before this guard saw them. What was
    # removed is subtracted — a subtraction, never a recount: every other
    # block's characters are in that number too and are not this guard's to
    # re-derive.
    _charge_back_removed_recent(
        guarded, original_recent, guarded["recent_context"]
    )

    original_units = [
        item for item in guarded.get("units") or () if isinstance(item, Mapping)
    ]
    guarded["units"] = [
        unit
        for unit in (
            _guarded_unit(item, frozen, decisions, invalid_refs) for item in original_units
        )
        if unit is not None
    ]
    _note_removal("units", len(original_units), len(guarded["units"]))

    for section in ("pointers", "ambiguity", "current_state", "missing"):
        values = guarded.get(section)
        if isinstance(values, list):
            kept = [
                dict(item)
                for item in values
                if not _names_withheld(item, frozen, reference_field=True)
                and not _value_names_an_invalid_reference(item, invalid_refs)
            ]
            if section in ("pointers", "current_state"):
                _note_removal(section, len(values), len(kept))
            guarded[section] = kept

    # Appended AFTER the `missing` filter runs, never before: `missing[]` entries
    # are compared as reference fields, so a bare word matches a withheld page's
    # filename stem, and a vault holding `anchors.md` would otherwise delete the
    # very marker explaining why its anchors vanished.
    #
    # Fail-closed is right here — a title a withheld page also bears cannot be told
    # apart at this layer — but a silent removal reads exactly like a vault with
    # nothing to say, which is what `lane_truncated` and `budget` already refuse to
    # do. The marker names no path and no name: it says a section lost something,
    # which is what the caller needs to know and the most it may be told.
    if removed and isinstance(guarded.get("missing"), list):
        guarded["missing"].extend(
            {"role": section, "reason": "withheld"} for section in sorted(removed)
        )
    # A packet whose every anchor was withheld is not a resolved packet with a
    # short answer — it is an abstention. Serving it with `abstained: false` and
    # empty blocks would state that the turn resolved and the vault had nothing,
    # which is a different and false claim. Everything downstream of an anchor
    # goes with it, since a unit's only warrant was the anchor it hung from.
    if packet.get("anchors") and not guarded["anchors"] and not guarded.get("abstained"):
        guarded["abstained"] = True
        guarded["abstention"] = {"reason": "withheld"}
        for section in ("units", "pointers", "current_state", "roles"):
            guarded[section] = []
        # `missing` is deliberately NOT cleared: its markers are the only thing
        # left saying the packet is empty because the guard emptied it, rather
        # than because the compiler found nothing. `recent_context` is not
        # cleared either: it hangs from no anchor — it is what the vault has
        # been working on, decided on its own paths above — and it is precisely
        # what a turn with no anchors left still has to say.
        budget = guarded.get("budget")
        if isinstance(budget, Mapping):
            guarded["budget"] = {
                **dict(budget),
                "used_chars": sum(
                    _recent_entry_chars(entry)
                    for entry in guarded.get("recent_context") or ()
                    if isinstance(entry, Mapping)
                ),
            }
    return guarded


#: TYPED PAGE FIELDS (correction round 4, T1): `path` and `anchor`, wherever
#: they appear (on the item itself, or under its `provenance`). Every
#: non-empty value here IS a page reference regardless of shape -- decided
#: under every existing reading, and withholding its item if none exists.
#: `_is_page_shaped` plays no part here; that classifier is for `ref` alone,
#: and only on an item that ALSO carries one of these (T2).
_WORKING_SET_STRICT_PATH_FIELDS = ("path", "anchor")
#: Packet fields carrying authored PROSE that may name a page in wikilink syntax.
#: Harvested so a page mentioned only inside a sentence still gets a release
#: decision: `release.withheld_paths` carries what hit projection happened to
#: touch, and a unit's text can name a page recall never surfaced.
_WORKING_SET_PROSE_FIELDS = ("text", "statement", "why", "title")
#: List-shaped fields whose entries are vault paths. Reuses `_PATH_LIST_FIELDS`
#: (`superseded_by`/`parent_superseded_by`, hit projection's own path-list
#: fields) and adds an anchor's own neighbourhood fields -- private
#: resolution state `_guarded_anchor` already promises to strip
#: (`neighbourhood`) or that shares its shape (`anchor_neighbourhood`).
#: Correction round 3's BLOCKER: `neighbourhood` is a LIST, not a Mapping, so
#: nothing in `_collect` reached it once the round-2 rewrite scoped candidate
#: collection to named fields -- a withheld page named ONLY there was never
#: decided, so `_guarded_anchor`'s own `_names_withheld(neighbourhood, ...)`
#: check silently never fired and a corroboration claim that leaned on a
#: withheld neighbour survived. (Checked, per the reviewer's request: as of
#: this commit neither key is actually serialized into a real compiled
#: packet's anchor dict -- `ResolvedAnchor.as_dict()`,
#: `working_set_resolve.py:194-201`, emits neither, confirmed against both a
#: hand-seeded vault and the standard fixture vault's real
#: `graph_corroboration` turn. Collected anyway: `_guarded_anchor` already
#: commits to stripping `neighbourhood` regardless, and a silently-ungoverned
#: field reaching a FUTURE anchor shape is exactly the failure mode
#: field-name scoping risks.)
_WORKING_SET_PATH_LIST_FIELDS = (*_PATH_LIST_FIELDS, "neighbourhood", "anchor_neighbourhood")


def _is_safe_relative_path(path: str) -> bool:
    """True when `path` is a genuine vault-relative path.

    Applied to every interpretation `_interpretations_for` can produce, so
    nothing that fails this is ever handed to `_decide_path` — and
    therefore never `stat()`'d. A percent-encoded traversal or absolute
    path reaching here from a scheme'd reference is already neutralised by
    `_unwrap_reference`'s decode step and its own trailing `strip("/")`,
    which turns a leading `/` into a relative segment before this function
    ever sees it — the `PurePosixPath(...).is_absolute()` check below is
    kept as defence in depth against a future change to that stripping, not
    as this function's actual protection against that specific shape. What
    this function alone catches is a Windows-style drive-letter path (a
    single letter, a colon, then a separator) and a `.`/`..` segment.
    """
    if not path or "\0" in path or "://" in path:
        return False
    if len(path) >= 2 and path[0].isalpha() and path[1] == ":":
        return False
    if PurePosixPath(path).is_absolute():
        return False
    return not any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)


def _matches_project_anchor_shape(text: str) -> bool:
    """`project:<key>` — `working_set_index.py`'s project-anchor `anchor_id`
    (`f"project:{key}"`, line ~1108, used verbatim as `ref` with `path=""`).

    A real project key never contains a path separator or ends in a
    markdown suffix, so `project:` followed by something that DOES look
    like a path is a near miss, not this shape, and falls through to
    ordinary candidate handling instead of being exempted.
    """
    prefix = "project:"
    if not text.startswith(prefix):
        return False
    key = text[len(prefix) :]
    return bool(key) and "/" not in key and not _is_markdown_path(key)


def _matches_plan_anchor_shape(text: str) -> bool:
    """`plan:<manifest-path>#<title>` — `working_set_index.py`'s plan-
    candidate `anchor_id` (line ~1013). In practice `anchor_ref()`
    (`working_set_resolve.py`) prefers a plan candidate's always-truthy
    `path` over this id, so it should never actually reach a packet's `ref`
    field — kept as an explicit shape anyway, in case that fallback chain
    ever changes. A real plan anchor id always has a `#` separating the
    manifest path from the title; `plan:` followed by anything else is a
    near miss and falls through to ordinary candidate handling.
    """
    prefix = "plan:"
    if not text.startswith(prefix):
        return False
    remainder = text[len(prefix) :]
    return "#" in remainder and bool(remainder.split("#", 1)[0])


def _matches_explicit_non_page_shape(text: str) -> bool:
    """True for a reference shape the compiler's own lanes and index
    (`working_set.py`, `working_set_index.py`) can legitimately produce
    that is NOT a page reference at all — exhaustively enumerated, never
    guessed at by how the string looks: a memory-id reference, the
    synthetic project-anchor id, the synthetic plan-anchor id. R1's default
    is that every OTHER non-empty path-bearing-field string is a candidate
    that must be decided or withheld — nothing else is exempted.
    """
    fragment_stripped = text.split("#", 1)[0]
    if memory_refs.parse_memory_ref(fragment_stripped) is not None:
        return True
    if _matches_project_anchor_shape(text):
        return True
    return _matches_plan_anchor_shape(text)


def _is_page_shaped(text: str) -> bool:
    """True when `text` looks like it is meant to name a page AT ALL: a
    markdown suffix after fragment-stripping, an `exomem://vault|source/`
    scheme, a wikilink bracket pair, or a path separator. Correction round
    3's LANDMINE fix: an opaque, hand-authored id (`unit-open`) has none of
    these -- it names no page and is not a "reference" for the item
    invariant `guard_working_set` enforces (see there) to count at all: it
    is neither decided nor invalid, so it can never make an otherwise-fine
    item's OTHER references insufficient, and it never by itself supplies
    the "at least one admitted page reference" half of that invariant
    either. Every real vault path the compiler emits carries a directory
    (`Knowledge Base/...`), so this only ever excludes a genuinely opaque
    string, never a real page reference.

    Correction round 4, T2: this classifier now gates exactly ONE thing --
    a `ref` on an item that ALSO carries its own `path`/`anchor` (a unit, an
    ordinary anchor), where a non-page-shaped value really is just an
    opaque id and must be ignored. A TYPED page field (`path`/`anchor`
    themselves, a path-list entry, or `ref` on an item with no `path`/
    `anchor` of its own) is never checked against this at all: `secret` and
    `secret.markdown` are not page-shaped either, but in a typed field they
    ARE the page reference, bare or oddly-suffixed or not -- the round-4
    BLOCKER this round closed was exactly `_add` applying this gate to
    every field alike.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if "/" in stripped:
        return True
    if stripped.startswith("[[") and stripped.endswith("]]"):
        return True
    lowered = stripped.lower()
    if any(lowered.startswith(prefix) for prefix in _EXOMEM_PATH_PREFIXES):
        return True
    return any(_is_markdown_path(reading) for reading in _plain_reference_readings(stripped))


def _interpretations_for(candidate: str) -> frozenset[str]:
    """Every distinct real-path interpretation `candidate` could denote,
    filtered to the ones that are at least a safe in-vault relative path
    (`_is_safe_relative_path`) — nothing unsafe is ever returned, so a
    caller that only ever decides what this returns never `stat()`s a `..`
    segment or an absolute path.

    A scheme'd `exomem://vault/`/`exomem://source/` reference has exactly
    one interpretation: `_unwrap_reference`'s raw-split-then-decode already
    resolves it unambiguously (`context_refs._encode` percent-escapes a
    literal `#`/`|` inside the real filename, so the one UNENCODED `#` in
    the raw text is unambiguously the pipeline's own fragment delimiter).

    A PLAIN string containing `#` or `|` is genuinely ambiguous: nothing in
    an unencoded string says whether it is "a filename containing that
    character" or "a path plus a fragment/alias" — there is no encoding
    here to supply the signal the scheme'd form has. Guessing one reading
    by position (a former version of this function cut at the first `.md`)
    decided a DIFFERENT, wrong file than the one a governed packet's
    content actually came from. This returns every plausible reading
    instead: the literal string itself, and every prefix that ends exactly
    where a `#`/`|` immediately follows a markdown suffix (`_is_markdown_path`,
    case-insensitive — the SAME predicate `_decide_path` uses). The caller
    decides each reading that exists rather than picking one.
    """
    text = candidate.strip()
    if not text:
        return frozenset()
    if text.startswith("[[") and text.endswith("]]"):
        # Wikilink form: unencoded, unambiguously split as written by
        # `_unwrap_reference` — out of THIS function's scope (a wikilink
        # target is a page TITLE, not a filename with a fragment to guess
        # at). See PROGRESS.md for what this does and does not cover.
        unwrapped, _explicit = _unwrap_reference(text)
        if unwrapped and _is_safe_relative_path(unwrapped):
            return frozenset({unwrapped})
        return frozenset()

    lowered = text.lower()
    matched_prefix = next(
        (prefix for prefix in _EXOMEM_PATH_PREFIXES if lowered.startswith(prefix)), None
    )
    if matched_prefix is not None:
        remainder = text[len(matched_prefix) :]
        remainder = remainder.split("#", 1)[0]
        unwrapped = unquote(remainder)
        unwrapped = unwrapped.replace("\\", "/").strip().strip("/")
        if unwrapped and _is_safe_relative_path(unwrapped):
            return frozenset({unwrapped})
        return frozenset()

    # A plain path, or an unrecognised scheme kept literal (never decoded):
    # both are handled identically, on the text exactly as written.
    # `_plain_reference_readings` is the SAME reading generation
    # `_canonical_references` uses for the withheld-key comparison, shared
    # rather than restated.
    normalized = text.replace("\\", "/").strip().strip("/")
    if not normalized:
        return frozenset()
    readings = _plain_reference_readings(normalized)
    return frozenset(reading for reading in readings if _is_safe_relative_path(reading))


def _item_has_own_page_field(item: Mapping[str, Any]) -> bool:
    """True when `item` carries a non-empty `path` or `anchor` of its own —
    at the item's own top level (an anchor, a current_state entry), or
    under its `provenance` (a unit) — correction round 4's T1 test for
    whether this item's `ref` is a TYPED page field (T1, no item has one of
    these AND lacks its own path/anchor) or merely a TOLERATED one (T2,
    page-shape-gated, alongside a real `path`/`anchor`).

    A pointer and an ambiguity entry carry neither field at all
    (`working_set.py::_pointer`, `working_set_resolve.py::_ambiguity`), so
    their `ref` is always typed. A unit's `provenance.path`/`.anchor` are
    unconditional for every real lane (`working_set.py::_provenance`, line
    ~307), so a unit's `ref` is usually merely tolerated -- except the one
    packet shape the test suite carries forward from an earlier round
    (`_ref_only_packet`, `guard_working_set`'s own docstring: "the
    compiler walks further" than hit projection), where a unit is named
    ONLY through its `ref`; this function reports that unit as having no
    page field of its own too, so its `ref` is typed there as well,
    exactly like a pointer's. An ordinary anchor's own `path` is real, so
    its `ref` is tolerated too (and in practice always equals `path`, so
    this changes nothing for one); a project/plan anchor's `path` is empty
    by construction (`path=""`), so THIS function alone would call its
    `ref` typed — but its `ref` is `project:<key>`/`plan:<rel>#<title>`, an
    explicit non-page shape (`_matches_explicit_non_page_shape`) that
    `_add` exempts from candidate-hood before strict/tolerant is even
    consulted, and the item invariant exempts it again at
    `_guarded_anchor`'s own call site (it legitimately has no page of its
    own at all, typed or not).
    """
    for key in _WORKING_SET_STRICT_PATH_FIELDS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return True
    provenance = item.get("provenance")
    if isinstance(provenance, Mapping):
        for key in _WORKING_SET_STRICT_PATH_FIELDS:
            value = provenance.get(key)
            if isinstance(value, str) and value.strip():
                return True
    return False


def _working_set_paths(
    packet: Mapping[str, Any],
) -> tuple[set[str], set[str], dict[str, frozenset[str]], set[str]]:
    """`(vault paths, wikilink names, interpretation sets, unresolvable
    candidates)` the packet names.

    A path field holds a vault-relative reference the release plane can
    decide directly; a wikilink inside authored prose holds a NAME, and a
    name is not a path — the caller resolves names to paths first (via the
    activation index), and only real paths are ever decided.

    R1: the default for a non-empty string in a PATH-BEARING field is never
    to skip it — only an explicit non-page shape does
    (`_matches_explicit_non_page_shape`). Everything else is a candidate:
    `interpretations` maps it to every safety-valid reading
    (`_interpretations_for`, R3) whose union populates `paths` — the flat
    set the decide loop below actually decides — or, when NO reading is
    even safety-valid, it goes straight into `unresolvable` instead
    (nothing here is ever handed to `_decide_path`, so nothing here ever
    reaches the filesystem). The caller (`guard_working_set`) decides every
    reading that exists on disk and withholds the item carrying a
    candidate whose readings are not every-one-admitted.

    "Path-bearing field" is scoped by FIELD NAME, never by string shape: the
    TYPED page fields (`_WORKING_SET_STRICT_PATH_FIELDS` — `path`/`anchor`,
    T1), authored prose's wikilinks (`_WORKING_SET_PROSE_FIELDS`), a known
    reference-LIST field's entries (`_PATH_LIST_FIELDS` — `superseded_by`
    and its like, the same fields hit projection's
    `_strip_withheld_provenance` treats as path lists — also typed, T1), and
    `ref`, which is typed (T1) on an item that carries no `path`/`anchor` of
    its own (a pointer, an ambiguity entry) and merely TOLERATED (T2,
    `_is_page_shaped`-gated) on one that does (a unit, an ordinary anchor) —
    see correction round 4's item invariant below. An ordinary scalar or
    list value under any OTHER key — `kind`, `status`, `role`, `reason`,
    `lifecycle`, `updated`, `as_of`, an `evidence` tag list, `category`,
    `source` — is never a candidate. A blanket catch-all that treated EVERY
    string reachable anywhere in the packet as a candidate turned those
    ordinary tag values into bogus "unresolvable" entries, which then
    wrongly withheld a sibling pointer/anchor/current_state item whose OWN
    field happened to share that exact word
    (`_value_names_an_invalid_reference` compares a whole item's every field
    against `invalid_refs`) — an over-restriction bug, caught by the very
    first end-to-end test run of this rewrite, not a defect a reviewer
    reported.

    Correction round 4's T1/T2 split, after the reviewer found `_is_page_shaped`
    gating EVERY field the same way left a bare or oddly-suffixed value
    (`secret`, `secret.markdown`) simply invisible in a TYPED field: not
    decided, not invalid, so a unit whose real source was named only that
    way was served in full once ANY other admitted reference (its own
    tolerated `ref`) satisfied the item invariant's (b) half, and a
    pointer/current_state/ambiguity entry whose ONLY reference was such a
    value was never checked against the withheld/invalid sets at all.
    `_is_page_shaped` now gates only the one place T2 needs it — a `ref`
    beside a real `path`/`anchor`, where an opaque legacy id
    (`unit-open`) must still be ignored rather than decided.
    """
    paths: set[str] = set()
    names: set[str] = set()
    interpretations: dict[str, frozenset[str]] = {}
    unresolvable: set[str] = set()

    def _add(candidate: str, *, strict: bool) -> None:
        text = candidate.strip()
        if not text or _matches_explicit_non_page_shape(text):
            return
        if not strict and not _is_page_shaped(text):
            # T2: a `ref` beside a real `path`/`anchor` names no page at all
            # when it is not even page-shaped (the LANDMINE fix) -- an
            # opaque, hand-authored id (`unit-open`) is neither decided nor
            # invalid, so it can never make an item's OTHER, genuine page
            # references insufficient. `strict` fields (T1) never take this
            # branch: EVERY non-empty, non-exempt value in one is a
            # candidate, page-shaped or not.
            return
        readings = _interpretations_for(candidate)
        if not readings:
            unresolvable.add(candidate)
            return
        interpretations[candidate] = readings
        paths.update(readings)

    def _collect(value: Any, *, ref_strict: bool) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "ref" and isinstance(item, str):
                    # T1/T2: typed (strict) when this item carries no
                    # `path`/`anchor` of its own; merely tolerated
                    # (page-shape-gated) when it does. `ref_strict` is
                    # computed once per ITEM, below, from that item's own
                    # (and its `provenance`'s) fields.
                    _add(item, strict=ref_strict)
                elif key in _WORKING_SET_STRICT_PATH_FIELDS and isinstance(item, str):
                    # T1: `path`/`anchor` are typed page fields wherever
                    # they appear -- on the item itself, or (a unit) under
                    # its `provenance` -- never gated by shape.
                    _add(item, strict=True)
                elif key in _WORKING_SET_PROSE_FIELDS and isinstance(item, str):
                    for raw_target in _WIKILINK_ANYWHERE.findall(item):
                        # `_unwrap_reference` is the SAME helper the matcher uses,
                        # deliberately: a display alias (`[[x|label]]`) and a
                        # heading anchor (`[[x#Section]]`) are presentation, not
                        # identity, and two independent unwrappings would drift.
                        # `is_wikilink_target=True` because the regex capture is
                        # already bracket-stripped -- omitting it left `x|label`
                        # unsplit (never resolving to `x`), so a page mentioned
                        # only via an aliased or heading-anchored wikilink was
                        # never decided, and if withheld, never matched either.
                        target, _explicit = _unwrap_reference(
                            str(raw_target), is_wikilink_target=True
                        )
                        if not target:
                            continue
                        if _is_markdown_path(target):
                            _add(target, strict=True)
                        else:
                            names.add(target)
                elif key in _WORKING_SET_PATH_LIST_FIELDS and isinstance(item, list):
                    # A list-shaped reference field -- `superseded_by` and its
                    # like, the SAME fields `_strip_withheld_provenance` treats
                    # as path lists for hit projection, PLUS an anchor's own
                    # `neighbourhood`/`anchor_neighbourhood` (the BLOCKER: a
                    # LIST is not a Mapping, so nothing else in `_collect`
                    # ever reached it). T1: typed like `path`/`anchor`, never
                    # gated by shape -- not a restated `.endswith(".md")`
                    # pre-filter that would miss a bare-stem or scheme'd
                    # reference here.
                    for entry in item:
                        if isinstance(entry, str):
                            _add(entry, strict=True)
                elif isinstance(item, Mapping):
                    # Only a nested MAPPING (`provenance`, ...) can hold
                    # another path/prose/path-list field of its own. An
                    # ordinary scalar or list value under any OTHER key --
                    # `kind`, `status`, `role`, `reason`, `lifecycle`,
                    # `updated`, `as_of`, an `evidence` tag list, `category`,
                    # `source` -- is not a reference and must never become a
                    # path candidate: a sibling item's every field is checked
                    # against `invalid_refs` by EXACT TEXT
                    # (`_value_names_an_invalid_reference`), so a stray
                    # "resources"/"budget"/"hub" value turning into an
                    # unresolvable candidate here wrongly dropped every
                    # unrelated pointer/anchor/current_state entry that
                    # happened to share that word in some field of its own --
                    # an over-restriction a blanket catch-all here
                    # reintroduced; scoping by FIELD NAME instead of by
                    # string shape is what the round-2 baseline's
                    # `.endswith(".md")` filter was accidentally also doing.
                    # `ref_strict` carries over unchanged: `ref` never
                    # appears inside a nested mapping like `provenance` in
                    # any packet shape the compiler emits, but the flag is
                    # this ITEM's own regardless of nesting depth.
                    _collect(item, ref_strict=ref_strict)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _collect(item, ref_strict=ref_strict)

    for section in (
        "recent_context",
        "anchors",
        "units",
        "pointers",
        "current_state",
        "ambiguity",
        "missing",
    ):
        for item in packet.get(section) or ():
            if isinstance(item, Mapping):
                _collect(item, ref_strict=not _item_has_own_page_field(item))
    return paths, names, interpretations, unresolvable


def _value_names_an_invalid_reference(value: Any, invalid_refs: frozenset[str]) -> bool:
    """True when `value` (a field, a list of them, or a whole item) contains
    one of `_working_set_paths`'s `invalid` candidates, by EXACT text match.

    `_names_withheld` cannot stand in for this: it compares CANONICAL keys,
    and `_canonical_reference` returns `None` for a candidate that unwraps to
    an empty string (`exomem://vault/` alone, with nothing after it) — which
    can never equal any canonical key, including its own. `invalid_refs`
    holds the exact candidate text `_working_set_paths` classified, so
    matching it exactly, the same way it was found, is the reliable
    comparison, not a re-derived key that a degenerate candidate cannot
    produce.
    """
    if not invalid_refs:
        return False
    if isinstance(value, str):
        return value in invalid_refs
    if isinstance(value, Mapping):
        return any(_value_names_an_invalid_reference(v, invalid_refs) for v in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_value_names_an_invalid_reference(v, invalid_refs) for v in value)
    return False


class WorkingSetResolutionUnavailable(RuntimeError):
    """The prose-name resolver could not answer.

    Deliberately NOT an empty result. "This name matches no page" and "I could
    not look up this name" are different facts, and a guard that returns the
    first when it means the second removes its own filter at the moment that is
    least safe. The caller abstains on this; it never serves.
    """


def _resolved_prose_names(vault_root: Path, names: set[str]) -> dict[str, tuple[str, ...]]:
    """Resolve wikilink names to every vault path bearing them, via the index.

    The index already performs exactly this resolution at build time to turn a
    page's wikilinks into typed edges (`working_set_index._resolve_links`), over a
    name map covering every walked knowledge-base page — the vault's page set, not
    only its anchors. Persisting that map means the guard answers a stem with one
    indexed lookup: no corpus walk, no per-stem filesystem work, and no dependency
    on a warm semantic snapshot it could not guarantee.

    Returns the mapping, not a flattened path set, because the NAMES matter after
    the decision: a page withheld by its path is matched in prose through
    `_withheld_keys`, which derives comparison keys from filenames only — so
    `[[Kill switch for risky releases]]` found no match and was served. The names
    that resolved to a withheld path are added as extra match keys for this
    guard's own comparisons.

    Every resolved path is keyed under BOTH the spelling the prose contained and
    its normalised form. The resolver normalises NFKC + casefold; the matcher
    casefolds only. Keying on the normalised form alone therefore missed a title
    carrying a non-breaking space or a full-width letter — resolved, decided,
    withheld, and still matched nothing. The match has to be available on the text
    that is actually written, not only on a canonical form of it.

    An unknown name is absent from the result and decides nothing. A resolver that
    cannot run raises: the packet reaching this guard was COMPILED from that index,
    so an index that is now unavailable is a contradiction about the release plane,
    not a vault with nothing in it.
    """
    if not names:
        return {}
    from .. import working_set_index

    index = working_set_index.WorkingSetIndex(vault_root)
    if not index.available():
        raise WorkingSetResolutionUnavailable(
            "the activation index is unavailable while guarding a packet built from it"
        )
    try:
        resolved = index.resolve_names(names)
    except working_set_index.WorkingSetIndexUnavailable as error:
        raise WorkingSetResolutionUnavailable(str(error)) from error
    except sqlite3.Error as error:
        raise WorkingSetResolutionUnavailable(
            "the activation index could not resolve prose references"
        ) from error
    out: dict[str, tuple[str, ...]] = {}
    for raw in names:
        key = working_set_index.normalize(raw)
        paths = resolved.get(key)
        if not paths:
            continue
        out[raw] = paths
        out[key] = paths
    return out


def _is_admitted_typed_reference(value: str, invalid_refs: frozenset[str]) -> bool:
    """True when `value` is a TYPED page field's value that was decided and
    ADMITTED -- the item invariant's (b) half: "at least one page
    reference it carries was decided and admitted" (correction round 3's
    LANDMINE fix, replacing the per-field shape reasoning R1-R3 were
    reaching for; correction round 4's T1/T3 restricts (b) to TYPED page
    fields only -- `path`/`anchor`, or a `ref` acting as one on an item
    with no `path`/`anchor` of its own -- since a merely TOLERATED `ref`
    beside a real `path`/`anchor` (T2) never satisfies (b) at all, even
    when it happens to be page-shaped and admitted: T3 is explicit that
    (b) is never satisfied by `ref` ALONE on an item that already has a
    typed field).

    No `_is_page_shaped` gate here (round 4's fix): `_working_set_paths`
    decided this value WITHOUT one, since it came from a typed field
    (`strict=True`) -- `secret`/`secret.markdown` are exactly as candidate
    as `Knowledge Base/Notes/real-page.md` there. Re-applying the gate here
    would incorrectly call a genuinely decided-and-admitted bare-word
    candidate "not admitted" merely for not looking like a path.

    An explicit non-page shape (memory-id ref, `project:<key>`,
    `plan:...`) is excluded FIRST, unconditionally: it was never a
    candidate at all (`_matches_explicit_non_page_shape`,
    `_working_set_paths`'s `_add`), so it must never satisfy (b) on its
    own even though its own raw text can look path-shaped by coincidence
    (a memory ref's `exomem://memory/<uuid>` scheme contains a `/`). Such a
    reference names no page, so it is neither for (b) nor against it; the
    item it belongs to must be carried by another field instead (a unit by
    its `path`/`anchor`, which the compiler guarantees --
    `working_set.py::_provenance` -- or an anchor whose `ref` IS the
    explicit non-page shape is exempted from (b) altogether at its own
    call site, since a project/plan anchor legitimately has no page of
    its own).

    A candidate that is NOT in `invalid_refs` was, by construction,
    decided (`_working_set_paths` tracks every typed-field string) and
    every existing reading it produced was admitted (R3) -- genuinely
    "decided and admitted," not merely "never checked."
    """
    if not value or _matches_explicit_non_page_shape(value):
        return False
    return value not in invalid_refs


def _guarded_anchor(
    anchor: Mapping[str, Any],
    withheld: frozenset[str],
    decisions: Mapping[str, Decision | None],
    invalid_refs: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    if (
        _names_withheld(anchor.get("path"), withheld)
        or _names_withheld(anchor.get("ref"), withheld, reference_field=True)
        # `title` is authored prose: a hub called "Open hub (supersedes
        # [[kill-switch-for-risky-releases]])" names the withheld page as plainly
        # as a path field would.
        or _names_withheld(anchor.get("title"), withheld, reference_field=True)
        # An un-unwrappable candidate never joins `withheld` -- see
        # `guard_working_set` -- so it is checked by exact match here instead.
        or anchor.get("path") in invalid_refs
        or anchor.get("ref") in invalid_refs
    ):
        return None
    anchor_ref = str(anchor.get("ref") or "")
    anchor_path = str(anchor.get("path") or "")
    if not _matches_explicit_non_page_shape(anchor_ref):
        # Item invariant (b), correction round 4's T1/T3: satisfied ONLY by
        # this anchor's own TYPED page field -- `path` when it carries one
        # (an ordinary anchor), or `ref` itself when it does not (a
        # project/plan anchor's exemption is the branch above; a
        # hypothetical future anchor kind with no `path` falls here
        # instead, matching `_working_set_paths`'s own `ref_strict`
        # computation for it exactly). `ref` is NEVER checked when `path`
        # is real (T3): an ordinary anchor's `ref` always equals its
        # `path` in practice, so this changes nothing for one.
        typed_value = anchor_path or anchor_ref
        if not _is_admitted_typed_reference(typed_value, invalid_refs):
            return None
    out = dict(anchor)
    # Private resolution state: never published, at any release level.
    neighbourhood = out.pop("neighbourhood", None)
    # `anchor_neighbourhood` is the same private resolution shape (the
    # ambiguity-disjointness neighbourhood, `working_set_resolve.py`), popped
    # defensively for symmetry even though nothing currently serializes it
    # into a packet either.
    out.pop("anchor_neighbourhood", None)
    if neighbourhood is not None and (
        _names_withheld(neighbourhood, withheld, reference_field=True)
        # An un-unwrappable neighbour never joins `withheld` -- checked by
        # exact match here instead, the same asymmetry every other
        # invalid-reference check in this module already has.
        or _value_names_an_invalid_reference(neighbourhood, invalid_refs)
    ):
        # Corroboration that leaned on a withheld or invalid neighbour is not
        # evidence this audience may be shown to have.
        out["evidence"] = [
            kind for kind in out.get("evidence") or () if kind != "graph_corroboration"
        ]
    decision = decisions.get(str(out.get("path") or ""))
    if decision is not None and decision.release_strip:
        protected = {key: out[key] for key in ("ref", "path", "title", "kind") if key in out}
        detail = {key: value for key, value in out.items() if key not in protected}
        stripped = bridges.strip_provenance(detail, decision.release_strip)
        out = dict(protected)
        if isinstance(stripped, Mapping):
            out.update(stripped)
    return out


def _recent_entry_chars(entry: Mapping[str, Any]) -> int:
    """What one recent entry cost the packet's budget.

    The same arithmetic `working_set._budgeted_recent` charged for it — title
    plus statement — spelled once so the guard's refund cannot drift from the
    compiler's charge.
    """
    return len(str(entry.get("title") or "")) + len(str(entry.get("statement") or ""))


def _charge_back_removed_recent(
    guarded: dict[str, Any],
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> None:
    """Subtract the removed recent entries' characters from `used_chars`."""
    removed = sum(map(_recent_entry_chars, before)) - sum(map(_recent_entry_chars, after))
    if removed <= 0:
        return
    budget = guarded.get("budget")
    if isinstance(budget, Mapping):
        used = budget.get("used_chars")
        if isinstance(used, int):
            guarded["budget"] = {**dict(budget), "used_chars": max(0, used - removed)}


def _guarded_recent(
    entry: Mapping[str, Any],
    withheld: frozenset[str],
    invalid_refs: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """One `recent_context` entry, decided exactly like a unit.

    The block leads the packet and is served on turns that resolved nothing, so
    it is the one place where "what has this vault been working on" could
    become an existence oracle for a page the audience may not have. It gets
    the same three checks a unit gets, for the same reasons:

    * its typed page fields (`path`, and `ref` when it stands in for one) must
      not name a withheld page, and must not be an un-unwrappable candidate
      (which never joins `withheld` and so is matched by exact text);
    * its authored prose (`title`, `statement`, `why`) must not name one
      either — an entry reading "status: superseded by [[…]]" names the page as
      plainly as a path field would, and a statement cannot be edited
      surgically without the server authoring a claim;
    * and the item invariant (b): at least one TYPED page reference it carries
      was decided and ADMITTED. A compiled entry always has a real `path`
      (`working_set._recent_context`), so `path` is the typed field and `ref`
      is merely tolerated beside it.
    """
    path = str(entry.get("path") or "")
    ref = str(entry.get("ref") or "")
    if path in invalid_refs or ref in invalid_refs:
        return None
    if _names_withheld(path, withheld) or _names_withheld(ref, withheld, reference_field=True):
        return None
    if any(
        _names_withheld(entry.get(field), withheld, reference_field=True)
        for field in _WORKING_SET_PROSE_FIELDS
    ):
        return None
    if _value_names_an_invalid_reference(entry, invalid_refs):
        return None
    if not _is_admitted_typed_reference(path or ref, invalid_refs):
        return None
    return dict(entry)


def _guarded_unit(
    unit: Mapping[str, Any],
    withheld: frozenset[str],
    decisions: Mapping[str, Decision | None],
    invalid_refs: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    provenance = unit.get("provenance")
    path = str(provenance.get("path") or "") if isinstance(provenance, Mapping) else ""
    anchor = str(provenance.get("anchor") or "") if isinstance(provenance, Mapping) else ""
    # An un-unwrappable candidate never joins `withheld` -- see
    # `guard_working_set` -- so it is checked by exact match here instead.
    if unit.get("ref") in invalid_refs:
        return None
    if _names_withheld(unit.get("ref"), withheld, reference_field=True):
        return None
    if path and (path in invalid_refs or _names_withheld(path, withheld)):
        return None
    # The unit's own PROSE. A wikilink inside authored text is an unambiguous
    # reference wherever it appears, so a permitted unit that quotes a withheld
    # page's link names it just as plainly as a provenance field would — and
    # truncating the sentence around it would leave a claim nobody can audit.
    #
    # `reference_field=True` because a wikilink TARGET is a bare stem by
    # construction (`[[kill-switch-for-risky-releases]]`), and the stem
    # comparison is what recognises it. On a prose string the bare-word branch
    # can only fire when the whole text IS the stem, which is itself a reference.
    #
    # The asymmetry below is deliberate. A withheld target in
    # `provenance.superseded_by` STRIPS that field and keeps the unit, because a
    # provenance field is a list of references and removing one entry leaves the
    # rest meaning what it meant. The same target in a prose field drops the
    # WHOLE unit, because prose cannot be edited surgically: cutting the link
    # out of a sentence leaves a claim whose warrant nobody can check, and
    # rewriting the sentence would be the server authoring text.
    #
    # Correction round 4's follow-up: checked over EVERY prose field
    # `_working_set_paths`'s own collector scans (`_WORKING_SET_PROSE_FIELDS`
    # — `text`, `statement`, `why`, `title`), not just `text` alone. A unit
    # carries only `text` today, so this changes nothing a real packet emits
    # yet — but hardcoding one field name here, while the collector that
    # DECIDES a wikilink's target is driven by the shared list, is the same
    # kind of drift the BLOCKER already punished once: the field that
    # withholds an item silently falling behind the field that decided what
    # it names.
    if any(
        _names_withheld(unit.get(field), withheld, reference_field=True)
        for field in _WORKING_SET_PROSE_FIELDS
    ):
        return None
    # A unit whose ANCHOR is withheld is dropped rather than kept with the anchor
    # filtered out of its provenance: an unattributable claim in working memory is
    # worse than a missing one, and the audience cannot see the anchor anyway.
    if anchor and (anchor in invalid_refs or _names_withheld(anchor, withheld, reference_field=True)):
        return None
    # Item invariant (b): at least one page reference this unit carries
    # must have been decided and admitted (correction round 3's LANDMINE
    # fix). Correction round 4's T3: satisfied ONLY by a TYPED page field,
    # never by `ref` ALONE on an item that already has one -- `path`/
    # `anchor` are almost always real for a unit
    # (`working_set.py::_provenance`, `{"path": item.path, ..., "anchor":
    # item.anchor}`, unconditional for every real lane), so `ref` is
    # merely TOLERATED there (T2) and never checked for (b) once either
    # one is present. A unit named ONLY through its own `ref` -- no
    # `path`/`anchor` at all, the shape a packet carries when the compiler
    # walks past hit projection (`_ref_only_packet` in the test suite) --
    # has no typed field to fall back to but its own `ref`, and
    # `_working_set_paths` decided it strictly for exactly this reason
    # (`_item_has_own_page_field` returns False, so `ref_strict=True`);
    # checking it here too keeps that shape servable.
    unit_ref = str(unit.get("ref") or "")
    if path or anchor:
        satisfied_b = _is_admitted_typed_reference(
            path, invalid_refs
        ) or _is_admitted_typed_reference(anchor, invalid_refs)
    else:
        satisfied_b = _is_admitted_typed_reference(unit_ref, invalid_refs)
    if not satisfied_b:
        return None
    out = dict(unit)
    if isinstance(provenance, Mapping):
        # `superseded_by` (and any other list-shaped provenance field) gets
        # the SAME treatment an invalid top-level ref gets, at list-entry
        # granularity: an un-unwrappable target strips that key exactly as a
        # withheld one does (`_value_names_an_invalid_reference` walks it the
        # same way `_names_withheld` already does), never `_decide_path`'d.
        # This stripping (and the `path`/`anchor` checks above it) trusts
        # `path`/`anchor` to already name the unit's OWN source page rather
        # than re-deriving it from `superseded_by` or any other provenance
        # field: the compiler guarantees that pairing itself, in
        # `working_set.py::_provenance` (`{"path": item.path, ...,
        # "anchor": item.anchor}`, merged with the lane's own provenance
        # dict), for every lane this guard's field walk understands.
        out["provenance"] = {
            key: value
            for key, value in provenance.items()
            if not _names_withheld(value, withheld, reference_field=True)
            and not _value_names_an_invalid_reference(value, invalid_refs)
        }
    decision = decisions.get(path)
    if decision is not None and decision.release_strip:
        stripped = bridges.strip_provenance(out.get("provenance") or {}, decision.release_strip)
        if isinstance(stripped, Mapping):
            out["provenance"] = dict(stripped)
    return out


# ---------------------------------------------------------------------------
# Direct reads (get / read_memory) — D3 applied to a whole page
# ---------------------------------------------------------------------------

#: Page fields whose values may name other vault items. Scanned so a permitted
#: page cannot act as an existence oracle for a withheld one.
_PAGE_PROVENANCE_FIELDS = ("links", "history", "relations", "sources", "neighborhood")
#: Provenance runs in BOTH directions. `sources` records what a compiled item
#: cited; `ingested_into` records every compiled item that cited a source —
#: `note.py` appends the new note's wikilink to each cited source on every
#: compile. A source released to an audience that cannot see those notes
#: therefore enumerated them, which is the forward leak running backwards.
_FRONTMATTER_PROVENANCE_FIELDS = (
    "superseded_by",
    "supersedes",
    "sources",
    "source",
    "evidence",
    "parent_media",
    "ingested_into",
)


def _iter_path_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if value.endswith(".md"):
            yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_path_strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_path_strings(item)


def _excerpt_of(body: str, limit: int = 600) -> str:
    """The L5 rendering of a body: a bounded, whole-word excerpt."""
    text = " ".join(body.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def _attach_raw_content(
    page: Mapping[str, Any], snapshot_content: str | bytes | None
) -> dict[str, Any]:
    """Attach exact raw text only when the terminal parser finds no secret."""
    if snapshot_content is None:
        raise ValueError("SECRET_BLOCKED: raw content is unavailable")
    try:
        raw_text = (
            snapshot_content
            if isinstance(snapshot_content, str)
            else bytes(snapshot_content).decode("utf-8")
        )
    except UnicodeDecodeError as error:
        raise ValueError("SECRET_BLOCKED: raw content is unavailable") from error
    _cleaned, blocked = scrubber.scrub_text(raw_text)
    if blocked:
        _record_credential_block()
        raise ValueError("SECRET_BLOCKED: raw content contains protected material")
    out = dict(page)
    out["content"] = raw_text
    return out


def annotate_page(
    vault_root: Path,
    page: dict[str, Any],
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
    snapshot_content: str | bytes | None = None,
    stable_ref: str | None = None,
    include_raw: bool = False,
) -> dict[str, Any] | None:
    """Render one page at its release decision's level, or `None` below notice.

    `None` is the caller's signal to answer byte-identically to a missing
    path — an item released below notice must be indistinguishable from one
    that never existed. Raw text is assembled only after an L6 decision and
    only when the terminal secret parser accepts the exact snapshot.
    """
    vault_root = Path(vault_root)
    rel_path = str(page.get("path") or stable_ref or "")
    if rel_path and lifecycle.is_tombstoned(vault_root, rel_path):
        return None
    policy = policy_module.load(vault_root)
    who = principal if principal is not None else effective_principal()

    if policy.empty:
        return _attach_raw_content(page, snapshot_content) if include_raw else page
    if policy.blocked or not who.resolved:
        _record_blocked_outcome(who.audience_id)
        return None

    rel_path = str(page.get("path") or "")
    if not rel_path:
        return None
    grants_hash = _grants_hash(policy)
    declared_purpose = _declared_purpose(vault_root, who, purpose)
    snapshot_hash: str | None = None
    snapshot_size: int | None = None
    if snapshot_content is not None:
        raw = (
            snapshot_content.encode("utf-8")
            if isinstance(snapshot_content, str)
            else bytes(snapshot_content)
        )
        snapshot_hash = hashlib.sha256(raw).hexdigest()
        snapshot_size = len(raw)
        expected_hash = page.get("content_hash")
        if expected_hash is not None and expected_hash != snapshot_hash:
            _record_blocked_outcome(who.audience_id)
            return None
        try:
            # This is a swap detector, not the source of authorization.  The
            # immutable ``raw`` bytes remain the sole representation decided,
            # hashed, receipted, and returned below.
            if (vault_root / rel_path).read_bytes() != raw:
                _record_blocked_outcome(who.audience_id)
                return None
        except OSError:
            _record_blocked_outcome(who.audience_id)
            return None
        parsed = find_corpus.parse_page(
            vault_root / rel_path,
            float(page.get("mtime") or 0.0),
            vault_root,
            content=raw,
        )
        if parsed is None or parsed.rel_path != rel_path:
            _record_blocked_outcome(who.audience_id)
            return None
        # A caller cannot bind arbitrary returned fields to unrelated bytes.
        # Body may already be intentionally truncated, but frontmatter is the
        # complete membership-bearing projection and must match exactly.
        if (
            isinstance(page.get("frontmatter"), Mapping)
            and dict(page["frontmatter"]) != parsed.frontmatter
        ):
            _record_blocked_outcome(who.audience_id)
            return None
        if "body" in page and page.get("body") != parsed.body:
            _record_blocked_outcome(who.audience_id)
            return None
        if "content" in page and page.get("content") != raw.decode("utf-8"):
            _record_blocked_outcome(who.audience_id)
            return None
        try:
            scope_ids = membership_module.evaluate_snapshot(
                parsed, policy, content_hash=snapshot_hash
            )
        except membership_module.MembershipUnresolved:
            _record_blocked_outcome(who.audience_id)
            return None
        active_grants, _session_identity = _active_grants_for_snapshot(
            vault_root,
            policy=policy,
            audience=who.audience_id,
            purpose=declared_purpose,
            rel_path=rel_path,
            content_hash=snapshot_hash,
            scope_ids=scope_ids,
            authorization_context=who.verified_authorization_session,
        )
        decision = decide(
            scope_ids,
            audience=who.audience_id,
            purpose=declared_purpose,
            policy=policy,
            active_grants=active_grants,
        )
        admission = bridges.admit(
            vault_root,
            rel_path,
            raw,
            policy=policy,
            audience=who.audience_id,
        )
        if admission.is_bridge:
            if not admission.allowed:
                decision = replace(
                    decision,
                    level=LEVEL_NONE,
                    options={},
                    notice=None,
                    bridge=None,
                    release_reason=admission.reason,
                )
            else:
                decision = replace(
                    decision,
                    release_grant_id=admission.grant.id if admission.grant else None,
                    release_strip=admission.strip_identities,
                    release_dependency_digest=admission.dependency_digest,
                )
        decision = _resolve_l4_bridge(
            vault_root,
            decision,
            policy=policy,
            audience=who.audience_id,
        )
    else:
        # Compatibility for internal/synthetic callers. Production direct-read
        # leaves always supply ``snapshot_content``.
        decision = _decide_path(
            vault_root,
            rel_path,
            policy=policy,
            audience=who.audience_id,
            purpose=declared_purpose,
            grants_hash=grants_hash,
            authorization_session=who.authorization_session_id,
            authorization_context=who.verified_authorization_session,
        )
    if decision is None or decision.level <= LEVEL_NONE:
        _outcome_for_decision(
            vault_root,
            rel_path,
            decision=decision,
            policy=policy,
            audience=who.audience_id,
            outcome="withheld",
            purpose=declared_purpose,
            content_hash=snapshot_hash,
            size=snapshot_size,
            ref=stable_ref,
        )
        return None

    _outcome_for_decision(
        vault_root,
        rel_path,
        decision=decision,
        policy=policy,
        audience=who.audience_id,
        outcome="released" if decision.level >= RELEASE_FLOOR else "withheld",
        purpose=declared_purpose,
        content_hash=snapshot_hash,
        size=snapshot_size,
        ref=stable_ref,
    )

    level = decision.level
    if level == LEVEL_EXCERPT_REDACTED and not decision.bridge_abstraction:
        level = LEVEL_ABSTRACT
    if level < RELEASE_FLOOR:
        # No path on a sub-floor notice. `op_get`/`op_read_memory` accept a
        # fuzzy identifier and `_resolve_memory_identifier` canonicalizes it
        # BEFORE this point, so echoing the path back would confirm the exact
        # vault location of an item the caller may only have guessed at — the
        # same oracle the L1 allow-list exists to close.
        return _notice(
            level,
            rule_ids=decision.rule_ids,
            scope_label=_scope_label(policy, decision),
            options=decision.options,
            bridge_abstraction=decision.bridge_abstraction,
        )

    # L5/L6: the page is released. Its own provenance must still not name a
    # sub-notice item (D3 applies the strip at EVERY level, not just below
    # full), so decide the items this page points at before answering.
    referenced: set[str] = set()
    bare_stems: set[str] = set()
    frontmatter = page.get("frontmatter")
    if isinstance(frontmatter, Mapping):
        targets: set[str] = set()
        for name in _FRONTMATTER_PROVENANCE_FIELDS:
            value = frontmatter.get(name)
            referenced.update(_iter_path_strings(value))
            # These fields are reference containers by definition, exactly
            # like `_PAGE_PROVENANCE_FIELDS`, and the vault writes them as
            # wikilinks — so collecting only `.md`-suffixed strings decided
            # nothing for the form the vault actually stores.
            targets.update(_iter_reference_targets(value))
            bare_stems.update(_iter_reference_stems(value))
        if targets:
            referenced.update(_resolve_reference_targets(vault_root, targets))
    for name in _PAGE_PROVENANCE_FIELDS:
        referenced.update(_iter_path_strings(page.get(name)))
        # These fields store BARE stems (`links.outbound` is a wikilink list)
        # and `_iter_path_strings` only yields `.md`-suffixed strings, so a
        # stem never entered `referenced`, was never decided, and the strip
        # below had nothing to match. Gathered across ALL fields and resolved
        # ONCE — resolving per field meant five corpus walks per page.
        bare_stems.update(_iter_reference_stems(page.get(name)))
    if bare_stems:
        referenced.update(_resolve_reference_stems(vault_root, bare_stems))
    withheld = frozenset(
        rel
        for rel in referenced
        if rel != rel_path
        and (
            (
                ref_decision := _decide_path(
                    vault_root,
                    rel,
                    policy=policy,
                    audience=who.audience_id,
                    purpose=declared_purpose,
                    grants_hash=grants_hash,
                    authorization_session=who.authorization_session_id,
                    authorization_context=who.verified_authorization_session,
                )
            )
            is None
            or ref_decision.level < RELEASE_FLOOR
        )
    )
    if level == LEVEL_EXCERPT:
        body = parsed.body if snapshot_content is not None else str(page.get("body") or "")
        body = redact_withheld_references(
            vault_root,
            body,
            principal=who,
            purpose=declared_purpose,
        )
        excerpt = {
            "path": rel_path,
            "body": _excerpt_of(body),
            "body_truncated": True,
            "release_level": level,
        }
        if decision.release_strip:
            excerpt = bridges.strip_provenance(
                excerpt,
                decision.release_strip,
                direct_page=True,
            )
        return excerpt

    out = _strip_page_provenance(dict(page), withheld)
    if decision.release_strip:
        out = bridges.strip_provenance(
            out,
            decision.release_strip,
            direct_page=True,
        )
    return _attach_raw_content(out, snapshot_content) if include_raw else out


def _iter_reference_stems(value: Any) -> Iterable[str]:
    """Bare, non-path strings inside a reference container.

    Unwrapped first: a reference field stores `[[stem]]` at least as often as
    a bare `stem`, and the bracketed form was compared against filename stems
    with its brackets still attached, so it never matched anything.
    """
    if isinstance(value, str):
        candidate, _ = _unwrap_reference(value)
        if candidate and "/" not in candidate and not candidate.lower().endswith(".md"):
            yield candidate
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_reference_stems(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_reference_stems(item)


def _iter_reference_targets(value: Any) -> Iterable[str]:
    """Directory-carrying reference tokens inside a reference container.

    Frontmatter provenance stores WIKILINKS (`[[Knowledge Base/Notes/x]]`,
    `[[Notes/x]]`), which carry no `.md` suffix, so `_iter_path_strings`
    yielded nothing for them and the item they name was never decided —
    leaving `_strip_page_provenance` with an empty withheld set and the
    reference in the clear.
    """
    if isinstance(value, str):
        token, _ = _unwrap_reference(value)
        if token and "/" in token:
            yield token
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_reference_targets(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_reference_targets(item)


def _resolve_reference_targets(vault_root: Path, targets: Iterable[str]) -> set[str]:
    """Map directory-carrying references onto the vault paths they name.

    A wikilink is written either vault-relative or Knowledge-Base-relative, so
    both are tried against the filesystem — two `is_file()` calls per
    reference, versus the whole-vault walk a stem lookup would cost on every
    page carrying provenance. An unresolvable reference is kept verbatim so
    `_decide_path` fails it closed rather than silently dropping it from the
    set of things that must be decided.
    """
    out: set[str] = set()
    kb_prefix = f"{kb_dirname()}/"
    for token in targets:
        candidate = token if token.lower().endswith(".md") else f"{token}.md"
        if (vault_root / candidate).is_file():
            out.add(candidate)
            continue
        prefixed = candidate if candidate.startswith(kb_prefix) else kb_prefix + candidate
        out.add(prefixed if (vault_root / prefixed).is_file() else candidate)
    return out


def _resolve_reference_stems(vault_root: Path, stems: Iterable[str]) -> set[str]:
    """Map bare wikilink stems onto the vault paths they name."""
    wanted = {s.casefold() for s in stems}
    if not wanted:
        return set()
    found: set[str] = set()
    for page in Path(vault_root).rglob("*.md"):
        if page.stem.casefold() in wanted and page.is_file():
            found.add(str(page.relative_to(Path(vault_root))).replace("\\", "/"))
    return found


def _strip_page_provenance(page: dict[str, Any], withheld_paths: frozenset[str]) -> dict[str, Any]:
    if not withheld_paths:
        return page
    frontmatter = page.get("frontmatter")
    if isinstance(frontmatter, Mapping):
        clean_fm = dict(frontmatter)
        for name in _FRONTMATTER_PROVENANCE_FIELDS:
            value = clean_fm.get(name)
            if isinstance(value, list):
                kept = [v for v in value if not _names_withheld(v, withheld_paths)]
                if kept:
                    clean_fm[name] = kept
                else:
                    clean_fm.pop(name, None)
            elif value is not None and _names_withheld(value, withheld_paths):
                clean_fm.pop(name, None)
        page["frontmatter"] = clean_fm
    for name in _PAGE_PROVENANCE_FIELDS:
        value = page.get(name)
        if value is None:
            continue
        # Every one of these fields is a reference list, so a bare stem in it
        # is a reference — see `_names_withheld(reference_field=...)`.
        ref = True
        if isinstance(value, list):
            kept = [v for v in value if not _names_withheld(v, withheld_paths, reference_field=ref)]
            page[name] = kept
        elif isinstance(value, Mapping):
            page[name] = {
                key: (
                    [v for v in item if not _names_withheld(v, withheld_paths, reference_field=ref)]
                    if isinstance(item, list)
                    else item
                )
                for key, item in value.items()
                if not (
                    not isinstance(item, list)
                    and _names_withheld(item, withheld_paths, reference_field=ref)
                )
            }
        elif _names_withheld(value, withheld_paths, reference_field=ref):
            page.pop(name, None)
    return page


# ---------------------------------------------------------------------------
# Terminal postfilter (D1 / D7)
# ---------------------------------------------------------------------------


def _withheld_cross_check(
    vault_root: Path,
    result: Any,
    *,
    principal: RequestPrincipal | None = None,
) -> Any:
    """Second, independent check that nothing sub-notice survived to the wire.

    The per-leaf gates (`annotate_hits`, `annotate_page`, `guard_seed`) are
    the primary enforcement; this is the dispatcher backstop, and it is the
    reason a surface nobody remembered to gate still cannot emit a withheld
    path. `writer_lease.invoke_command` is the ONE dispatcher shared by MCP,
    REST, hosted and CLI, so implementing the active-policy case here covers
    every structure/review surface at once — directory listings, review
    queues, inbound-link reports, overview buckets — rather than requiring
    eight bespoke edits that a ninth surface would silently miss.

    Same three-state contract: `empty` -> untouched (the fast path, one
    `is_dir()`); `blocked` or an unresolved principal -> every path-bearing
    entry dropped; otherwise decide each named path.
    """
    return filter_withheld_entries(vault_root, result, principal=principal)


def _scrub_tool_result(result: Any, vault_root: Path) -> tuple[Any, bool]:
    """Scan a FastMCP `ToolResult`'s TEXT blocks only.

    `op_get_video_frames` returns JPEG bytes in image blocks beside its text
    block. Entropy-scanning base64 image data would both blow the latency
    budget and false-positive on every frame, so image content is passed
    through untouched and never inspected (design D1).
    """
    blocked = False
    for block in getattr(result, "content", ()) or ():
        if getattr(block, "type", None) != "text":
            continue
        cleaned, hit = scrubber.scrub_text(getattr(block, "text", "") or "")
        if hit:
            blocked = True
            try:
                block.text = cleaned
            except (AttributeError, ValueError):  # pragma: no cover - frozen model
                object.__setattr__(block, "text", cleaned)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        cleaned_structured, hit = scrubber.scrub_value(structured)
        if hit:
            blocked = True
            try:
                result.structured_content = cleaned_structured
            except (AttributeError, ValueError):  # pragma: no cover
                object.__setattr__(result, "structured_content", cleaned_structured)
    return result, blocked


def is_vault_root(value: Any) -> bool:
    """True when `value` is a filesystem path.

    Every real surface injects the vault root as a command's first positional
    argument (`commands._SPEC`), so this is a structural check on that
    contract, not a defensive shrug: a non-path first argument means the call
    is not vault-scoped and has no content to filter.
    """
    return isinstance(value, (str, os.PathLike))


def postfilter(command_name: str, result: Any, vault_root: Path) -> Any:
    """The terminal egress filter: always-on scrubber + withheld cross-check.

    Called from `writer_lease.invoke_command` — the ONE dispatcher shared by
    MCP, REST, hosted, and CLI, which is why `bind_vault` (MCP-only) is a
    second pass rather than the primary site: the `EXOMEM_RETRIEVE_INJECT`
    hook reaches memory over REST-then-CLI, both of which skip `bind_vault`.

    Idempotent: running it twice over the same result is a no-op, because a
    replaced credential is already `NOTICE` text and matches nothing.
    """
    if result is None:
        return None
    issuance_context = scrubber._issuance_projection_context(result)
    if issuance_context is not None:
        # `rotate` has already invalidated the request's prior credential by
        # the time terminal filtering runs. Re-entering the ordinary artifact
        # gate would revalidate that stale context and swallow the one-time
        # replacement. The sealed issuance projection was created only after
        # exact lifecycle-shape validation and still runs the non-disableable
        # terminal scrubber over every non-bearer field here.
        cleaned, blocked = scrubber._scrub_issuance_projection(
            result,
            issuance_context,
        )
        if blocked:
            _record_credential_block()
        return cleaned
    vault_root = Path(vault_root)
    result = _withheld_cross_check(vault_root, result)
    # Free text and nested resource/prompt strings have no structural entry
    # for the cross-check to drop. Resolve those only after structural paths
    # have been removed; scanning an ordinary released page body would change
    # the content the page decision explicitly authorized.
    result = gate_artifact_references(
        vault_root,
        result,
        scan_all=command_name
        in {"continue_adoption", "adoption_run", "adoption_runs", "adoption_studio"},
    )
    if hasattr(result, "content") and hasattr(result, "structured_content"):
        cleaned, blocked = _scrub_tool_result(result, vault_root)
        if blocked:
            _record_credential_block()
        return cleaned
    cleaned, blocked = scrubber.scrub_value(result)
    if blocked:
        _record_credential_block()
    return cleaned


#: An error's human-readable payload, wherever the codebase parks it —
#: `OpError.message`/`.remediation`, `memory_refs.ReferenceError.reason`.
#: `code` is deliberately absent: a stable error code is the contract a client
#: branches on, and it names no vault item.
_ERROR_TEXT_ATTRIBUTES = ("message", "reason", "remediation")


def _rewrite_error_attribute(error: BaseException, name: str, value: Any) -> None:
    """Rewrite in place so the exception keeps its type, code and traceback.

    Rebuilding an arbitrary exception is not possible in general — signatures
    differ, and `memory_refs.ReferenceError` is a frozen dataclass — so the
    payload is replaced on the object that is already travelling.
    """
    try:
        setattr(error, name, value)
    except (AttributeError, TypeError, ValueError):
        try:  # frozen dataclass exceptions
            object.__setattr__(error, name, value)
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            pass


#: Parameters that carry a vault reference BY CONTRACT. The exemption is seeded
#: from these alone, never from every kwarg.
#:
#: Walking all kwargs looks harmless — each value is caller-supplied, which is
#: the whole safety argument — but it hands the caller a de-anonymisation
#: channel: put a known path in any free-text field (`why`, `intent`, `title`,
#: `query`) and a redacted collision list stops rendering it as `[withheld]`,
#: revealing WHICH slot that path occupies. A list-shaped field turns that from
#: one probe per candidate into one probe per thousand. Restricting the seed to
#: reference-typed parameters keeps the justification ("the caller named this AS
#: a reference") and removes the channel.
_REFERENCE_KWARGS = frozenset(
    {
        "path",
        "paths",
        "id",
        "ids",
        "ref",
        "refs",
        "old_path",
        "new_path",
        "source_path",
        "target_path",
        "manifest_path",
        "collection",
        "only_paths",
        "selector_paths",
    }
)


def _caller_supplied_references(kwargs: Mapping[str, Any] | None) -> frozenset[str]:
    """Canonical keys for every reference the CALLER put in the request.

    See `postfilter_error` for why these must survive filtering verbatim, and
    `_REFERENCE_KWARGS` for why only reference-typed parameters seed the set.
    """
    if not kwargs:
        return frozenset()
    out: set[str] = set()

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            canonical = _canonical_reference(value)
            if canonical is not None:
                out.add(canonical[0])
                # Mirror `_withheld_keys`: a supplied `Notes/x.md` and a
                # volunteered `Knowledge Base/Notes/x.md` are the same item.
                stripped = _kb_stripped(canonical[0])
                if stripped:
                    out.add(stripped)
        elif isinstance(value, Mapping):
            for item in value.values():
                _walk(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                _walk(item)

    _walk({k: v for k, v in kwargs.items() if k in _REFERENCE_KWARGS})
    return frozenset(out)


def postfilter_error(
    command_name: str,
    error: BaseException,
    vault_root: Path,
    *,
    request_kwargs: Mapping[str, Any] | None = None,
) -> BaseException:
    """The terminal egress filter applied to a RAISED payload.

    `postfilter` guards the value a command returns; this guards the value it
    raises. Both leave through `writer_lease.invoke_command`, the ONE
    dispatcher shared by MCP, REST, hosted and CLI, and an error that names a
    withheld item is exactly the disclosure a result naming one would be —
    `AMBIGUOUS_REFERENCE` embedding the colliding vault paths proved the class
    reachable rather than theoretical.

    THE CALLER'S OWN REFERENCES ARE EXEMPT, and that exemption is what makes
    this filter safe rather than actively harmful. Redaction here works by
    SUBSTITUTION — a withheld reference becomes `[withheld]`. That is correct
    for a result payload, where the marker sits inside content the caller was
    already entitled to receive. It is catastrophic for an error whose entire
    payload IS a path the caller just sent, because then the substitution is
    itself the answer: `NOT_FOUND: ... [withheld]` means "exists, restricted"
    while `NOT_FOUND: ... <your path>` means "does not exist". That is a clean
    binary existence oracle over the whole vault, probeable with a guessed
    slug, and it is strictly worse than emitting the path — which the caller
    supplied and therefore already knows.

    So: a reference the caller put in the request is echoed back untouched, and
    everything else is redacted. The invariant is that an error's text stays a
    pure function of the caller's own input. Anything else is a distinguishable
    state, and a distinguishable state is a disclosure.

    An error carries free text and nothing structural for the entry filter to
    drop, so the artifact gate scans every string (the `scan_all` shape) and
    the always-on scrubber runs exactly as it does for results. The gate's own
    empty-policy short circuit keeps an ungoverned vault's text byte-identical.

    Mutates in place and returns the same object, so the caller re-raises with
    its original type, error code and traceback intact.
    """
    del command_name  # an error is free text; there is no per-command shape
    vault_root = Path(vault_root)
    gate = _ArtifactReferenceGate(
        vault_root,
        principal=None,
        purpose=None,
        exempt=_caller_supplied_references(request_kwargs),
    )
    blocked = False

    def _clean(value: Any) -> Any:
        nonlocal blocked
        cleaned = gate.gate_payload(value, scan_strings=True)
        cleaned, hit = scrubber.scrub_value(cleaned)
        blocked = blocked or hit
        return cleaned

    code = getattr(error, "code", None)
    args = tuple(error.args or ())
    if args:
        rewritten = tuple(arg if arg == code else _clean(arg) for arg in args)
        if rewritten != args:
            _rewrite_error_attribute(error, "args", rewritten)
    for name in (*_ERROR_TEXT_ATTRIBUTES, "details"):
        value = getattr(error, name, None)
        if value is None or value == code:
            continue
        cleaned = _clean(value)
        if cleaned != value:
            _rewrite_error_attribute(error, name, cleaned)
    if blocked:
        _record_credential_block()
    return error


# ---------------------------------------------------------------------------
# Startup assertion (D3 risk mitigation)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Structural projector coverage (H7)
# ---------------------------------------------------------------------------
#
# DEFAULT-DENY, DERIVED FROM THE REGISTRY. Every command is assumed to return
# vault content — and therefore to require a registered projector — unless it
# appears on the small opt-out below. A hand-maintained "these are the content
# commands" list is what let `fetch` and eight structure surfaces ship
# ungated: nobody remembers to add a new command to a list they did not know
# existed. Inverting the default makes a new command fail this check ON THE
# DAY IT IS ADDED, which is the only version of this backstop that works.
#
# To opt a command out, it must be genuinely metadata-only: it may report
# counts, health, capability, or scheduling state, but it must never name a
# vault path, title, excerpt, or body.
_METADATA_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        # Server/lease/health state — no vault items named.
        "coordination_status",
        "configure_memory",
        "bootstrap",
        "connect_memory",
        # Governance inspection returns policy ids/counts only; authoring
        # returns receipt-backed mutation metadata, never vault content.
        "govern_memory",
        # Pure mutations: they act on a path the CALLER already supplied, so
        # they disclose nothing the caller did not already hold.
        "add",
        "note",
        "edit",
        "replace",
        "delete",
        "link",
        "move_file",
        "create_file",
        "append_to_file",
        "create_directory",
        "delete_directory",
        "delete_file",
        "recover_from_trash",
        "reconcile",
        "audit_fix",
        "preserve",
        "remember",
        "capture_source",
        "preserve_evidence",
        "preserve_artifacts",
        "compile_source",
        "observe_memory",
        "replace_memory",
        "edit_memory",
        "manage_memory_file",
        "process_media",
        "transfer_artifact",
        "adopt_vault",
        "adoption_studio",
        "triage_memory",
        "query_dataset",
        "read_media",
        "list_trash",
    }
)


#: Which projector kind each content-returning command serializes through.
#: PER COMMAND, not a global "is the registry intact" check: a command absent
#: from this map is unprojected by definition, so adding a surface without
#: declaring how it renders fails the boot assertion immediately.
#:
#: `structure` covers the listing/review/report surfaces. They emit entries
#: naming vault items rather than item bodies, and their gate is the
#: dispatcher-level `filter_withheld_entries` cross-check.
_COMMAND_PROJECTOR_KIND: dict[str, str] = {
    # Retrieval -> hit / semantic_unit projectors, gated in `op_find`.
    "find": "hit",
    "search": "hit",
    "suggest_links": "hit",
    "suggest_relations": "hit",
    # Direct reads -> page projector, gated by `annotate_page`.
    "get": "page",
    "fetch": "page",
    "review_item_context": "page",
    # Graph -> guarded by `guard_graph_context`.
    "graph_context": "structure",
    # Media frames -> gated by `release_allows_frames`; the ToolResult walker
    # scans text blocks only and never the image bytes.
    "get_video_frames": "structure",
    # Structure / review / report surfaces -> dispatcher cross-check.
    "attention": "structure",
    "audit": "structure",
    "overview": "structure",
    "list_directory": "structure",
    "list_inbound_links": "structure",
    "evolution": "structure",
    "propose_compilation": "structure",
    "provenance_report": "structure",
    "query_data": "structure",
    "adopt": "structure",
    "schema_memory": "structure",
    # The action dispatcher routes all content through the typed Records
    # inspection/query/mutation projectors before it returns.
    "record_memory": "structure",
    "plan_memory": "structure",
    # The context packet has its own guard (`guard_working_set`, design D7) and
    # the dispatcher cross-check behind it. It is declared `structure` because
    # what it emits is refs and short provenance-bearing excerpts naming vault
    # items, which is exactly what the structure backstop filters.
    "activate_context": "structure",
    # `inspect` names the caller's own recap by ref; `record` names the page
    # the caller just wrote. Both go through the dispatcher cross-check.
    "episode_memory": "structure",
}

# Receipt adapters follow the same default-deny registry as serializers.  A
# new mode cannot inherit a command's name and silently skip evidence: it must
# name the reduction that contributes its content-free outcome.
_COMMAND_OUTCOME_ADAPTER: dict[str, str] = {
    **{name: "hits" for name in ("find", "search", "suggest_links", "suggest_relations")},
    **{name: "page" for name in ("get", "fetch", "review_item_context")},
    "graph_context": "graph",
    "get_video_frames": "frames",
    **{
        name: "structure"
        for name in (
            "attention",
            "audit",
            "overview",
            "list_directory",
            "list_inbound_links",
            "evolution",
            "propose_compilation",
            "provenance_report",
            "query_data",
            "adopt",
        )
    },
    "record_memory": "structure",
    "plan_memory": "structure",
    "schema_memory": "structure",
    "activate_context": "structure",
    "episode_memory": "structure",
}

# Every content selector declares both evidence collection and tombstone
# suppression.  The values name the concrete gate used by that representation;
# mutation selectors explicitly declare that no content is returned.
_COMMAND_TOMBSTONE_ADAPTER: dict[str, str] = {
    name: adapter for name, adapter in _COMMAND_OUTCOME_ADAPTER.items()
}

_DATA_REPRESENTATION_ADAPTER: dict[str, str] = {
    "rows": "dataset",
    "aggregate": "dataset",
    "profile": "dataset",
}

_SELECTOR_ADAPTERS: dict[tuple[str, str], dict[str, str]] = {
    ("configure_memory", "action"): {
        "inspect": "structure",
        "set": "mutation",
        "clear": "mutation",
    },
    ("connect_memory", "operation"): {
        "suggest-links": "structure",
        "suggest-relations": "structure",
        "context": "structure",
        "graph-context": "structure",
        "inbound-links": "structure",
        "resolve-entity": "structure",
        "create-entity": "mutation",
        "accept-relation": "mutation",
        "resolve-relation": "structure",
    },
    ("adopt_vault", "mode"): {
        "scan-only": "structure",
        "save-manifest": "mutation",
        "copy-as-sources": "mutation",
        "compile-selected": "mutation",
    },
    ("adoption_studio", "action"): {
        "start": "mutation",
        "status": "structure",
        "select": "mutation",
        "plan": "mutation",
        "apply": "mutation",
        "cancel": "mutation",
        "finish": "mutation",
        "work-item": "structure",
        "propose": "mutation",
        "apply-proposal": "mutation",
    },
    ("process_media", "operation"): {
        "process": "mutation",
        "status": "structure",
        "retry": "mutation",
    },
    ("observe_memory", "operation"): {
        "add": "mutation",
        "update": "mutation",
        "remove": "mutation",
        "validate": "structure",
    },
    ("maintain_memory", "mode"): {
        "audit": "structure",
        "fix": "dry-run-default",
        "reconcile": "dry-run-opt-in",
        "backfill-ids": "dry-run-default",
        "structured-files": "apply-conditional",
        "curation": "mutation",
    },
    ("manage_memory_file", "operation"): {
        "list": "structure",
        "create": "validation",
        "append": "validation",
        "move": "mutation",
        "delete": "mutation",
        "trash-list": "structure",
        "recover": "mutation",
        "reclassify": "mutation",
        "propose-reclassification": "structure",
    },
    ("schema_memory", "operation"): {
        "infer": "save-conditional",
        "validate": "structure",
        "diff": "structure",
        "inventory": "structure",
        "inspect": "structure",
        "resolve": "structure",
        "preview": "structure",
        "save": "mutation",
        "refresh": "mutation",
        "save-entity-types": "mutation",
        "resolve-entity-type": "structure",
        "propose-relation": "structure",
        "save-relations": "mutation",
        "census": "structure",
    },
    ("record_memory", "action"): {
        "describe": "structure",
        "validate": "structure",
        "inspect": "structure",
        "create": "mutation",
        "query": "structure",
        "append": "mutation",
        "update": "mutation",
        "revise": "mutation",
        "rebaseline": "mutation",
        "discard": "mutation",
    },
    ("episode_memory", "action"): {
        "record": "mutation",
        "inspect": "structure",
    },
    ("plan_memory", "action"): {
        "inspect": "structure",
        "validate": "structure",
        "create": "mutation",
        "query": "structure",
        "add": "mutation",
        "update": "mutation",
        "triage": "mutation",
        "revise": "mutation",
        "rebaseline": "mutation",
    },
}

_SELECTOR_TOMBSTONE_ADAPTERS: dict[tuple[str, str], dict[str, str]] = {
    key: {
        value: "not-applicable" if adapter == "mutation" else adapter
        for value, adapter in values.items()
    }
    for key, values in _SELECTOR_ADAPTERS.items()
}

_EXPLICIT_TOMBSTONE_ROUTES: dict[str, str] = {
    "direct-read": "page",
    "download": "binary",
    "frame": "binary",
    "prompt": "artifact-reference",
    "resource": "artifact-reference",
}


def selector_registry() -> dict[tuple[str, str], dict[str, str]]:
    """The one finite selector registry used by lease and egress coverage."""
    return {key: dict(values) for key, values in _SELECTOR_ADAPTERS.items()}


def selector_capability_registry() -> dict[tuple[str, str], dict[str, dict[str, str]]]:
    """Selector-level evidence and tombstone capabilities, default-deny."""
    return {
        key: {
            value: {
                "outcome": adapter,
                "tombstone": _SELECTOR_TOMBSTONE_ADAPTERS.get(key, {}).get(value, ""),
            }
            for value, adapter in values.items()
        }
        for key, values in _SELECTOR_ADAPTERS.items()
    }


def assert_tombstone_coverage() -> None:
    missing: list[str] = []
    for command in _COMMAND_OUTCOME_ADAPTER:
        if not _COMMAND_TOMBSTONE_ADAPTER.get(command):
            missing.append(command)
    for (command, selector), values in _SELECTOR_ADAPTERS.items():
        declared = _SELECTOR_TOMBSTONE_ADAPTERS.get((command, selector), {})
        missing.extend(
            f"{command}.{selector}={value}" for value in values if not declared.get(value)
        )
    missing.extend(
        f"explicit:{route}" for route, adapter in _EXPLICIT_TOMBSTONE_ROUTES.items() if not adapter
    )
    if missing:
        raise RuntimeError(
            "TOMBSTONE_GATE_MISSING: content selectors without tombstone suppression: "
            + ", ".join(sorted(missing))
        )


def selector_for_command(command: str) -> str | None:
    selectors = [selector for name, selector in _SELECTOR_ADAPTERS if name == command]
    if len(selectors) > 1:
        raise RuntimeError(f"RECEIPT_OUTCOME_MISSING: ambiguous selectors for {command}")
    return selectors[0] if selectors else None


def assert_selector_covered(command: str, selector: str, value: str) -> str:
    adapter = _SELECTOR_ADAPTERS.get((command, selector), {}).get(value)
    tombstone_adapter = _SELECTOR_TOMBSTONE_ADAPTERS.get((command, selector), {}).get(value)
    if adapter is None or tombstone_adapter is None:
        raise SelectorCoverageError(
            "RECEIPT_OUTCOME_MISSING: command selector without evidence/tombstone adapters: "
            f"{command}.{selector}={value}"
        )
    return adapter


def data_representation_adapter(representation: str) -> str | None:
    return _DATA_REPRESENTATION_ADAPTER.get(representation)


def assert_data_representation_covered(representation: str) -> None:
    if data_representation_adapter(representation) is None:
        raise RuntimeError(
            "RECEIPT_OUTCOME_MISSING: query_data representation without a receipt adapter: "
            f"{representation}"
        )


def unrecorded_commands(registry: Mapping[str, Any]) -> tuple[str, ...]:
    if not isinstance(registry, Mapping):
        raise TypeError("unrecorded_commands expects a {name: command} mapping")
    return tuple(
        sorted(
            name
            for name in content_returning_commands(registry)
            if name not in _COMMAND_OUTCOME_ADAPTER
        )
    )


def assert_outcomes_registered(registry: Mapping[str, Any]) -> None:
    missing = unrecorded_commands(registry)
    if missing:
        raise RuntimeError(
            "RECEIPT_OUTCOME_MISSING: content-returning commands without a "
            f"receipt outcome adapter: {', '.join(missing)}"
        )


def content_returning_commands(registry: Mapping[str, Any]) -> tuple[str, ...]:
    """Registry-derived: every command that is not explicitly metadata-only."""
    return tuple(sorted(set(registry) - _METADATA_ONLY_COMMANDS))


#: Retained as the opt-out's inverse for callers/tests that want the positive
#: set. Derived, never hand-edited.
_CONTENT_RETURNING_COMMANDS: frozenset[str] = frozenset()


def unprojected_commands(registry: Mapping[str, Any]) -> tuple[str, ...]:
    """Content-returning commands with no usable projector.

    Raises on a non-mapping argument rather than silently intersecting to
    nothing — the previous signature declared a mapping but the test handed it
    a tuple of `Command` objects, so the check passed vacuously for months.
    """
    if not isinstance(registry, Mapping):
        raise TypeError(
            "unprojected_commands expects a {name: command} mapping; got "
            f"{type(registry).__name__}. Passing a sequence of Command objects "
            "silently intersects to nothing and makes this check vacuous."
        )
    kinds = registered_kinds() | {"structure"}
    missing = [
        name
        for name in content_returning_commands(registry)
        if _COMMAND_PROJECTOR_KIND.get(name) not in kinds
    ]
    return tuple(sorted(missing))


def assert_projectors_registered(registry: Mapping[str, Any]) -> None:
    """Boot refusal: the process must not start with incomplete coverage.

    Called from `commands` at import time, so a surface added without a
    projector fails the build rather than shipping a leak.
    """
    missing = unprojected_commands(registry)
    if missing:
        raise RuntimeError(
            "PROJECTOR_MISSING: content-returning commands with no registered "
            f"release projector: {', '.join(missing)}"
        )


# ---------------------------------------------------------------------------
# Alias-layer coverage (P1)
# ---------------------------------------------------------------------------
#
# `assert_projectors_registered` runs over the LEAF registry, and the leaf
# registry is not what a client calls. `browse_memory`, `review_memory` and
# `maintain_memory` are product-facing tool names that appear in
# `PRODUCT_COMMANDS` and NOWHERE in `COMMANDS` — so the leaf check is
# structurally blind to them. Any alias layer that dispatches to leaves is a
# second registry, and a second registry with no coverage guarantee is exactly
# the "someone adds a surface and forgets the gate" hole the leaf check was
# built to close.
#
# The alias rule is the same default-deny, one level up: an alias is covered
# only when it can be shown to reach gated code.


def _leaf_is_covered(name: str) -> bool:
    """A leaf name that the release plane already accounts for."""
    if name in _METADATA_ONLY_COMMANDS:
        return True
    kinds = registered_kinds() | {"structure"}
    return _COMMAND_PROJECTOR_KIND.get(name) in kinds


@functools.lru_cache(maxsize=256)
def _derived_routes_from_source(source: str) -> frozenset[str]:
    tree = ast.parse(textwrap.dedent(source))
    return frozenset(
        node.func.id[len("op_") :]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("op_")
    )


def derived_routes(leaf: Any) -> frozenset[str]:
    """Which `op_*` leaves this callable ACTUALLY calls, read from its source.

    N8: alias coverage used to believe a hand-written `routes` tuple, and
    three of those declarations name a leaf the alias never calls —
    `adoption_studio` -> `adopt` most consequentially, because it claimed
    coverage through the very leaf that N1 showed leaking while reaching its
    content by an entirely different path. An annotation cannot be evidence
    of what code does; the code is.

    Deliberately one level deep and syntactic. It answers "does this alias
    demonstrably reach a gated leaf", and anything it cannot see falls back to
    the explicit opt-out rather than to silent coverage — the safe direction.
    """
    try:
        source = inspect.getsource(leaf)
    except (OSError, TypeError):
        return frozenset()
    return _derived_routes_from_source(source)


def unprojected_aliases(
    alias_registry: Mapping[str, Any], leaf_registry: Mapping[str, Any]
) -> tuple[str, ...]:
    """Product-facing aliases that reach no gated leaf.

    An alias is covered when EITHER:

    1. it declares routes and *every* route resolves to a leaf that is itself
       covered (projector-registered, or on the metadata-only opt-out) — the
       strong rule, because it proves the dispatch target is gated; or
    2. the alias name is on the explicit, commented metadata-only opt-out —
       the escape hatch for a product name with no leaf routes of its own
       (`process_media`) or one whose route is a helper rather than a leaf
       (`transfer_artifact` -> `transfer_token`).

    Partial route coverage is NOT coverage: one ungated route is one ungated
    dispatch path. Both arguments must be mappings for the same reason the
    leaf check demands one — a sequence of `Command` objects intersects to
    nothing and makes the whole check vacuous.
    """
    for label, registry in (("alias_registry", alias_registry), ("leaf_registry", leaf_registry)):
        if not isinstance(registry, Mapping):
            raise TypeError(
                f"unprojected_aliases expects a {{name: command}} mapping for {label}; "
                f"got {type(registry).__name__}. Passing a sequence of Command objects "
                "silently intersects to nothing and makes this check vacuous."
            )
    missing: list[str] = []
    for name, alias in alias_registry.items():
        leaf = getattr(alias, "leaf", None)
        if leaf is not None:
            # N8: what the leaf CALLS, never what its annotation claims. An
            # alias that IS a leaf (`review_item_context`) routes to itself.
            # Intersect with the leaf registry: a leaf's recursive self-call
            # (`op_read_memory` re-enters itself to layer `purpose`) is not a
            # route to anything, and an `op_*` name that is not a registered
            # leaf cannot grant coverage either.
            routes = set(derived_routes(leaf)) & set(leaf_registry)
            if name in leaf_registry:
                routes.add(name)
        else:
            routes = set(getattr(alias, "routes", ()) or ())
        if routes and all(route in leaf_registry and _leaf_is_covered(route) for route in routes):
            continue
        if name in _METADATA_ONLY_COMMANDS:
            continue
        missing.append(name)
    return tuple(sorted(missing))


def assert_alias_projectors_registered(
    alias_registry: Mapping[str, Any], leaf_registry: Mapping[str, Any]
) -> None:
    """Boot refusal for the alias layer, called from `commands` at import.

    Separate error code from `PROJECTOR_MISSING` so the failure names which
    registry is incomplete — the fix differs (register a projector for the
    leaf vs. route the alias at a gated leaf).
    """
    missing = unprojected_aliases(alias_registry, leaf_registry)
    if missing:
        raise RuntimeError(
            "ALIAS_PROJECTOR_MISSING: product-facing aliases that reach no "
            f"projector-registered leaf: {', '.join(missing)}"
        )


# ---------------------------------------------------------------------------
# Structured datasets
# ---------------------------------------------------------------------------


def annotate_dataset(
    vault_root: Path,
    payload: Mapping[str, Any],
    *,
    representation: str,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> dict[str, Any] | None:
    """Authorize final CSV/TSV/JSON rows before they cross the boundary."""
    assert_data_representation_covered(representation)
    vault_root = Path(vault_root)
    rel_path = str(payload.get("path") or "")
    if rel_path and lifecycle.is_tombstoned(vault_root, rel_path):
        return None
    policy = policy_module.load(vault_root)
    if policy.empty:
        return dict(payload)
    who = principal if principal is not None else effective_principal()
    rel_path = str(payload.get("path") or "")
    declared_purpose = _declared_purpose(vault_root, who, purpose)
    if policy.blocked or not who.resolved or not rel_path:
        _record_blocked_outcome(who.audience_id)
        return None
    decision = _decide_path(
        vault_root,
        rel_path,
        policy=policy,
        audience=who.audience_id,
        purpose=declared_purpose,
        grants_hash=_grants_hash(policy),
        authorization_session=who.authorization_session_id,
        authorization_context=who.verified_authorization_session,
    )
    if decision is None or decision.level < RELEASE_FLOOR:
        _outcome_for_decision(
            vault_root,
            rel_path,
            decision=decision,
            policy=policy,
            audience=who.audience_id,
            outcome="withheld",
            purpose=declared_purpose,
        )
        return None
    _outcome_for_decision(
        vault_root,
        rel_path,
        decision=decision,
        policy=policy,
        audience=who.audience_id,
        outcome="released",
        purpose=declared_purpose,
    )
    return dict(payload)


# ---------------------------------------------------------------------------
# Binary egress: transfer downloads and media frames (D1 residual surfaces)
# ---------------------------------------------------------------------------


def release_level_for(
    vault_root: Path,
    rel_path: str,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
    receipt_decision: str | None = None,
) -> int | None:
    """The disclosure ceiling for one item, or `None` when it cannot be decided.

    Same three-state contract as every other consumer: `empty` -> the open
    fast path (`DISCLOSURE_MAX`), `blocked` or an unresolved-but-expected
    principal -> `DISCLOSURE_MIN`, otherwise decide. `None` means the item
    could not be read or parsed, which callers must treat as "not permitted"
    rather than "no rule applies".
    """
    vault_root = Path(vault_root)
    if lifecycle.is_tombstoned(vault_root, rel_path):
        return None
    policy = policy_module.load(vault_root)
    if policy.empty:
        return DISCLOSURE_MAX
    who = principal if principal is not None else effective_principal()
    declared_purpose = _declared_purpose(vault_root, who, purpose)
    if policy.blocked or not who.resolved:
        _record_blocked_outcome(who.audience_id)
        return DISCLOSURE_MIN
    decision = _decide_path(
        vault_root,
        rel_path,
        policy=policy,
        audience=who.audience_id,
        purpose=declared_purpose,
        grants_hash=_grants_hash(policy),
        authorization_session=who.authorization_session_id,
        authorization_context=who.verified_authorization_session,
    )
    level = None if decision is None else decision.level
    _outcome_for_decision(
        vault_root,
        rel_path,
        decision=decision,
        policy=policy,
        audience=who.audience_id,
        outcome=(
            receipt_decision
            if level is not None and level >= RELEASE_FLOOR and receipt_decision is not None
            else "released"
            if level is not None and level >= RELEASE_FLOOR
            else "withheld"
        ),
        purpose=declared_purpose,
    )
    return level


#: A unit reference whose parent names no page. A seed whose parent is withheld
#: from the caller is resolved as this instead, so the resolver, its drift
#: accounting and every lane after it take the branch a unit of an absent page
#: takes, rather than a branch that exists only because the page does.
UNRESOLVABLE_UNIT_REF = "exomem://memory/unavailable#unavailable"


def unit_parent_withheld(
    vault_root: Path,
    unit_ref: str,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> bool:
    """True when a page a unit reference names is not released to the caller.

    Whether a unit reference resolves, and the drift reported while resolving
    it, are facts about the pages the resolver consults, so a graph seed is
    decided by those pages before the graph is asked. The candidates are every
    path the graph's own rows can consult for the reference (current or not;
    `epistemic_graph.unit_ref_indexed_paths`), every page the reference's
    memory id names in the reference index, and the page an
    `exomem://vault/` or `exomem://source/` parent names by path — confined to
    the vault the same way any caller-named path is (`vault.resolve_under_vault`);
    a name that does not resolve inside the vault is undecidable and counts as
    withheld without ever being stat'd or read. Each candidate is decided at
    `RELEASE_FLOOR`, the level below which the graph guard already withholds a
    seed; a path that cannot be decided counts as withheld, and a walk with
    more rows than the resolver examines cannot prove every page visible.

    An explicit sub-floor decision withholds the owner's seed exactly as it
    withholds anyone else's, so a rule that names the `owner` audience still
    applies to a unit seed. What does not apply to the owner is an
    UNDECIDABLE candidate: a walk with more rows than the resolver examines
    (`work_exhausted`), or a stale row naming a path that is no longer
    there, is a graph artifact rather than a policy decision, and must not
    cost the owner the stale-status and drift report the unguarded answer
    carries. For anyone else, an undecidable candidate counts as withheld,
    because a walk that cannot prove every candidate visible cannot prove the
    page released either.
    """
    vault_root = Path(vault_root)
    policy = policy_module.load(vault_root)
    if policy.empty and not lifecycle.tombstoned_paths(vault_root):
        return False
    who = principal if principal is not None else effective_principal()
    is_owner = who.resolved and who.audience_id == OWNER_AUDIENCE
    parent_ref, separator, _fragment = str(unit_ref or "").rpartition("#")
    if not separator or not parent_ref:
        return False
    from .. import epistemic_graph

    indexed, work_exhausted = epistemic_graph.unit_ref_indexed_paths(vault_root, unit_ref)
    if work_exhausted and not is_owner:
        return True
    candidates = set(indexed)
    memory_id = memory_refs.parse_memory_ref(parent_ref)
    if memory_id is not None:
        candidates.update(
            memory_refs.paths_for_ids_read_only(vault_root, (memory_id,)).get(memory_id, ())
        )
    elif parent_ref.lower().startswith(("exomem://vault/", "exomem://source/")):
        named = memory_refs.resolve_identifier_read_only(vault_root, parent_ref)
        try:
            _named_abs, named = vault.resolve_under_vault(vault_root, named)
        except vault.VaultPathError:
            return True
        candidates.add(named)
    for rel_path in sorted(candidates):
        level = release_level_for(vault_root, rel_path, principal=who, purpose=purpose)
        if level is None:
            # Undecidable — a stale or budget-truncated graph row pointing at
            # a path that is no longer there, most often. For anyone else
            # that is indistinguishable from a page withheld from them, so it
            # counts as withheld. The owner is never denied a page over a
            # graph artifact; only an explicit sub-floor decision (an
            # owner-targeted rule) withholds the owner's seed, below.
            if is_owner:
                continue
            return True
        if level < RELEASE_FLOOR:
            return True
    return False


def release_level_for_path_only(
    vault_root: Path,
    rel_path: str,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
    receipt_decision: str | None = None,
    policy: Any | None = None,
) -> int:
    """Decide an opaque candidate without parsing its bytes.

    A structured Record must be authorized before parsing. Scopes that need
    the candidate's frontmatter therefore withhold it conservatively.

    `policy` lets a caller that classifies MANY paths under one pass load the
    policy once and hand it in, exactly as `release_walk_filter` already does
    for a walk. The plane does not move while one pass runs, and re-probing the
    authoring guard per path cost 8.9 s of a 33 s structured write — the guard
    probe stats the governance root on every `policy_module.load`. Omitting it
    keeps the original per-call load, so every existing caller is unchanged.
    """
    vault_root = Path(vault_root)
    if lifecycle.is_tombstoned(vault_root, rel_path):
        return DISCLOSURE_MIN
    if policy is None:
        policy = policy_module.load(vault_root)
    if policy.empty:
        return DISCLOSURE_MAX
    who = principal if principal is not None else effective_principal()
    if policy.blocked or not who.resolved:
        return DISCLOSURE_MIN
    try:
        scope_ids = membership_module.evaluate_path_only(
            vault_root, rel_path, policy
        ).require_classified()
    except membership_module.MembershipUnresolved:
        if receipt_decision is not None:
            _outcome_for_decision(
                vault_root,
                rel_path,
                decision=None,
                policy=policy,
                audience=who.audience_id,
                outcome="withheld",
                purpose=purpose,
            )
        return DISCLOSURE_MIN
    declared_purpose = _declared_purpose(vault_root, who, purpose)
    content_hash: str | None = None
    if who.verified_authorization_session is not None:
        try:
            content_hash = hashlib.sha256((vault_root / rel_path).read_bytes()).hexdigest()
        except OSError:
            if receipt_decision is not None:
                _outcome_for_decision(
                    vault_root,
                    rel_path,
                    decision=None,
                    policy=policy,
                    audience=who.audience_id,
                    outcome="withheld",
                    purpose=declared_purpose,
                )
            return DISCLOSURE_MIN
    active_grants, _session_identity = _active_grants_for_snapshot(
        vault_root,
        policy=policy,
        audience=who.audience_id,
        purpose=declared_purpose,
        rel_path=rel_path,
        content_hash=content_hash,
        scope_ids=scope_ids,
        authorization_context=who.verified_authorization_session,
    )
    decision = decide(
        scope_ids,
        audience=who.audience_id,
        purpose=declared_purpose,
        policy=policy,
        active_grants=active_grants,
    )
    if receipt_decision is not None:
        _outcome_for_decision(
            vault_root,
            rel_path,
            decision=decision,
            policy=policy,
            audience=who.audience_id,
            outcome=receipt_decision if decision.level >= LEVEL_FULL else "withheld",
            purpose=declared_purpose,
        )
    return decision.level


def _binary_boundary(
    vault_root: Path,
    rel_path: str,
    *,
    boundary_name: str,
    minimum_level: int,
    principal: RequestPrincipal | None,
    purpose: str | None,
) -> bool:
    """Own direct download/frame authorization when no command dispatcher does."""
    if _collector() is not None:
        level = release_level_for(
            vault_root,
            rel_path,
            principal=principal,
            purpose=purpose,
            receipt_decision="release_authorized",
        )
        return level is not None and level >= minimum_level
    with disclosure_boundary(vault_root, boundary_name) as collector:
        level = release_level_for(
            vault_root,
            rel_path,
            principal=principal,
            purpose=purpose,
            receipt_decision="release_authorized",
        )
        allowed = level is not None and level >= minimum_level
        emit_boundary_receipt(collector)
        return allowed


#: What replaces a withheld reference inside free text. Fixed, like the
#: scrubber's notice: a per-item description would itself carry information.
WITHHELD_REFERENCE = "[withheld]"

_WRAPPED_ARTIFACT_REFERENCE = re.compile(r"\[\[[^\[\]]+\]\]|exomem://[^\s\"'<>)\]]+", re.IGNORECASE)


class _ArtifactReferenceGate:
    """Resolve and gate references against the vault's actual artifact set.

    File existence is the type registry: Markdown, datasets, Office, PDF,
    image/audio/video, extensionless files, and future artifact kinds all take
    the same path.  No suffix allowlist can quietly become incomplete.
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        principal: RequestPrincipal | None,
        purpose: str | None,
        exempt: frozenset[str] = frozenset(),
    ) -> None:
        self.vault_root = Path(vault_root)
        self.policy = policy_module.load(self.vault_root)
        self.who = principal if principal is not None else effective_principal()
        # Canonical keys the caller themselves supplied. Substituting a marker
        # for one of these would answer a question the caller already knew the
        # input to, and the answer is "does this exist?" — see `postfilter_error`.
        self.exempt = exempt
        self.fail_closed = self.policy.blocked or not self.who.resolved
        self.grants_hash = "" if self.fail_closed else _grants_hash(self.policy)
        self.purpose = _declared_purpose(self.vault_root, self.who, purpose)
        self.verdicts: dict[str, bool] = {}
        self.by_path: dict[str, list[str]] = {}
        self.by_name: dict[str, list[str]] = {}
        self.by_stem: dict[str, list[str]] = {}
        self.literal_aliases: set[str] = set()
        self.tombstones = lifecycle.tombstoned_paths(self.vault_root)
        if not self.policy.empty or self.tombstones:
            self._index()

    def _add(self, table: dict[str, list[str]], alias: str, rel: str) -> None:
        key = alias.casefold()
        rows = table.setdefault(key, [])
        if rel not in rows:
            rows.append(rel)

    def _index(self) -> None:
        kb_prefix = f"{kb_dirname()}/"
        try:
            paths = sorted(path for path in self.vault_root.rglob("*") if path.is_file())
        except OSError:
            paths = []
        for path in paths:
            try:
                rel = path.relative_to(self.vault_root).as_posix()
            except ValueError:
                continue
            aliases = {rel, unquote(rel)}
            if rel.startswith(kb_prefix):
                aliases.add(rel[len(kb_prefix) :])
            # `.as_posix()`, not `str()`: `str(WindowsPath)` re-spells the
            # separators, so on Windows this alias was stored with backslash
            # separators and never matched the forward-slash token
            # `resolve` normalises to. An extensionless
            # full-path wikilink to a withheld page -- the ordinary shape of
            # frontmatter provenance -- therefore resolved to nothing there, and
            # a reference that names it survived redaction. `rel` above already
            # uses `as_posix` for exactly this reason.
            without_suffix = Path(rel).with_suffix("").as_posix() if path.suffix else rel
            aliases.add(without_suffix)
            for alias in aliases:
                self._add(self.by_path, alias, rel)
                if len(alias) >= 3:
                    self.literal_aliases.add(alias)
            self._add(self.by_name, path.name, rel)
            self._add(self.by_stem, path.stem, rel)
            if len(path.name) >= 3:
                self.literal_aliases.add(path.name)
        for rel in self.tombstones:
            if rel.startswith(("exomem://", "sha256:")):
                continue
            path = Path(rel)
            aliases = {rel, unquote(rel), path.name, path.stem}
            kb_prefix = f"{kb_dirname()}/"
            if rel.startswith(kb_prefix):
                aliases.add(rel[len(kb_prefix) :])
            for alias in aliases:
                self._add(self.by_path, alias, rel)
                if len(alias) >= 3:
                    self.literal_aliases.add(alias)

    @staticmethod
    def _unwrap(value: str) -> tuple[str, bool]:
        token = value.strip()
        wikilink = token.startswith("[[") and token.endswith("]]")
        if wikilink:
            token = token[2:-2].strip().split("|", 1)[0]
        token = token.split("#", 1)[0].strip().strip("\"'").rstrip(".,;:!?")
        return token, wikilink

    def resolve(self, value: str, *, directory: str | None = None) -> tuple[str, ...]:
        token, wikilink = self._unwrap(value)
        if lifecycle.is_tombstoned(self.vault_root, token):
            return (token,)
        lowered = token.casefold()
        if lowered.startswith(memory_refs.REF_PREFIX):
            try:
                token = memory_refs.resolve_identifier_read_only(self.vault_root, token)
            except memory_refs.ReferenceError:
                return ()
        else:
            for prefix in _EXOMEM_PATH_PREFIXES:
                if lowered.startswith(prefix):
                    token = unquote(token[len(prefix) :])
                    break
        token = unquote(token).replace("\\", "/").strip().strip("/")
        if not token:
            return ()
        direct = self.by_path.get(token.casefold())
        if direct:
            return tuple(sorted(direct))
        if directory is not None and "/" not in token:
            sibling = f"{directory.rstrip('/')}/{token}" if directory else token
            direct = self.by_path.get(sibling.casefold())
            if direct:
                return tuple(sorted(direct))
        if "/" not in token:
            named = self.by_name.get(token.casefold())
            if named:
                return tuple(sorted(named))
            if wikilink or "." not in token:
                stemmed = self.by_stem.get(Path(token).stem.casefold())
                if stemmed:
                    return tuple(sorted(stemmed))
        return ()

    def _permits(self, rel_path: str) -> bool:
        if lifecycle.is_tombstoned(self.vault_root, rel_path):
            self.verdicts[rel_path] = False
            return False
        cached = self.verdicts.get(rel_path)
        if cached is not None:
            return cached
        if self.fail_closed:
            _record_blocked_outcome(self.who.audience_id)
            allowed = False
        else:
            decision = _decide_path(
                self.vault_root,
                rel_path,
                policy=self.policy,
                audience=self.who.audience_id,
                purpose=self.purpose,
                grants_hash=self.grants_hash,
                authorization_session=self.who.authorization_session_id,
            )
            allowed = decision is not None and decision.level >= RELEASE_FLOOR
            _outcome_for_decision(
                self.vault_root,
                rel_path,
                decision=decision,
                policy=self.policy,
                audience=self.who.audience_id,
                outcome="released" if allowed else "withheld",
                purpose=self.purpose,
            )
        self.verdicts[rel_path] = allowed
        return allowed

    def gate_text(self, text: str) -> str:
        if not text or (self.policy.empty and not self.tombstones):
            return text

        def _replace_token(match: re.Match[str]) -> str:
            token = match.group(0)
            if self.exempt:
                canonical = _canonical_reference(token)
                # Compare both forms: the caller may have supplied either the
                # KB-relative or the vault-absolute spelling of the same item.
                if canonical is not None and (
                    canonical[0] in self.exempt or _kb_stripped(canonical[0]) in self.exempt
                ):
                    # The caller sent this. Echoing it back tells them nothing;
                    # replacing it tells them the item exists.
                    return token
            candidates = self.resolve(token)
            if candidates and not all(self._permits(rel) for rel in candidates):
                return WITHHELD_REFERENCE
            return token

        out = _WRAPPED_ARTIFACT_REFERENCE.sub(_replace_token, text)
        if not self.literal_aliases:
            return out
        literal_pattern = re.compile(
            r"(?<![\w./-])(?:"
            + "|".join(
                re.escape(alias)
                for alias in sorted(self.literal_aliases, key=lambda item: (-len(item), item))
            )
            + r")(?![\w./-])",
            re.IGNORECASE,
        )
        return literal_pattern.sub(_replace_token, out)

    def gate_payload(self, value: Any, *, scan_strings: bool = False) -> Any:
        if isinstance(value, str):
            return self.gate_text(value) if scan_strings else value
        if isinstance(value, Mapping):
            gated: dict[Any, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and scan_strings:
                    gated_key = self.gate_text(key)
                    if gated_key != key:
                        # Replacing a key can collide with another withheld
                        # key. Omission is the same fail-closed shape used by
                        # the structural map-key filter.
                        continue
                key_marks_free_text = isinstance(key, str) and any(
                    marker in key.casefold() for marker in ("handoff", "prompt", "resource")
                )
                gated[key] = self.gate_payload(
                    item, scan_strings=scan_strings or key_marks_free_text
                )
            return gated
        if isinstance(value, (list, tuple, set, frozenset)):
            items = [self.gate_payload(item, scan_strings=scan_strings) for item in value]
            if isinstance(value, tuple):
                rebuild = getattr(type(value), "_make", None)
                return rebuild(items) if rebuild is not None else type(value)(items)
            if isinstance(value, (set, frozenset)):
                return type(value)(items)
            return items
        return value


def redact_withheld_references(
    vault_root: Path,
    text: str,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> str:
    """Replace any actual vault-artifact reference withheld from the caller."""
    return _ArtifactReferenceGate(Path(vault_root), principal=principal, purpose=purpose).gate_text(
        text
    )


def gate_artifact_references(
    vault_root: Path,
    payload: Any,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
    scan_all: bool = False,
) -> Any:
    """Recursively gate nested prompt/resource payloads with one verdict cache."""
    gate = _ArtifactReferenceGate(Path(vault_root), principal=principal, purpose=purpose)
    return gate.gate_payload(payload, scan_strings=scan_all or isinstance(payload, str))


def release_walk_filter(
    vault_root: Path,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> Any:
    """A per-file `keep(rel_path) -> bool` predicate for a leaf that WALKS.

    Returns `None` on the empty-policy fast path, so an ungoverned vault pays
    a single `policy.load()` and the walk is byte-for-byte what it was.

    Why this exists at all (N1c). The dispatcher's entry filter can drop a row
    that names a withheld item, but it cannot repair a NUMBER that a walk
    already derived from that item. `files_direct: 1` beside `sample_names: []`
    is a strictly stronger oracle than the sample list was — it states exactly
    how many things are being hidden. Counts, coverage percentages, `largest`,
    `oldest_unmodified` and junk lists are all reductions over the walk, so the
    only place they can be made honest is the walk itself. This mirrors the
    shape PR #321 used to prune access-tier–excluded subtrees from both the
    `os.walk` and the totals; the dispatcher filter stays as the backstop.

    Covers MEDIA as well as pages. It used to exempt every non-markdown file,
    which made withheld media visible to a restricted audience in `largest`,
    `oldest_unmodified`, `sample_names`, `files_direct`, `binary` and
    `totals` — and, because those files kept the folder non-empty, stopped the
    folder from collapsing, so the scoped-probe refusal only ever fired for a
    markdown-ONLY folder. `_decide_path` now decides a binary from its path
    without parsing it.
    """
    policy = policy_module.load(Path(vault_root))
    tombstones = lifecycle.tombstoned_paths(vault_root)
    if policy.empty and not tombstones:
        return None

    vault_root = Path(vault_root)
    who = principal if principal is not None else effective_principal()
    fail_closed = policy.blocked or not who.resolved
    grants_hash = "" if fail_closed else _grants_hash(policy)
    declared_purpose = _declared_purpose(vault_root, who, purpose)
    verdicts: dict[str, bool] = {}

    def keep(rel_path: str) -> bool:
        if lifecycle.is_tombstoned(vault_root, rel_path):
            return False
        if fail_closed:
            _record_blocked_outcome(who.audience_id)
            return False
        cached = verdicts.get(rel_path)
        if cached is not None:
            return cached
        decision = _decide_path(
            vault_root,
            rel_path,
            policy=policy,
            audience=who.audience_id,
            purpose=declared_purpose,
            grants_hash=grants_hash,
            authorization_session=who.authorization_session_id,
            authorization_context=who.verified_authorization_session,
        )
        allowed = decision is not None and decision.level >= RELEASE_FLOOR
        _outcome_for_decision(
            vault_root,
            rel_path,
            decision=decision,
            policy=policy,
            audience=who.audience_id,
            outcome="released" if allowed else "withheld",
            purpose=declared_purpose,
        )
        verdicts[rel_path] = allowed
        return allowed

    return keep


def release_allows_download(
    vault_root: Path,
    rel_path: str,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> bool:
    """True only at FULL disclosure — a download hands over the complete bytes.

    Nothing below L6 can authorize one. An excerpt-level ceiling permits a
    bounded excerpt, not the file that excerpt was cut from; handing over the
    original would let any ceiling be escaped by asking for the artifact
    instead of the text.
    """
    return _binary_boundary(
        vault_root,
        rel_path,
        boundary_name="download",
        minimum_level=LEVEL_FULL,
        principal=principal,
        purpose=purpose,
    )


def release_allows_frames(
    vault_root: Path,
    rel_path: str,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> bool:
    """True only at full disclosure.

    Frames are a structured direct representation of the source video, not the
    registered bounded Markdown excerpt projector. Until a typed image
    projector exists, every level below L6 must refuse rather than decode or
    return partial pixels.
    """
    return _binary_boundary(
        vault_root,
        rel_path,
        boundary_name="video_frame",
        minimum_level=LEVEL_FULL,
        principal=principal,
        purpose=purpose,
    )


# ---------------------------------------------------------------------------
# Structure / review surfaces (C4) — an entry IS an existence oracle
# ---------------------------------------------------------------------------

#: Entry fields that name a vault item. A directory listing, a review queue, or
#: an inbound-link report leaks the same `id` that `fetch` turns into a body —
#: plus filename, size, mtime and frontmatter type — so the gate has to reach
#: these surfaces even though none of them returns "content" in the narrow
#: sense. Same class the excluded-tier change fixed for browse/overview at the
#: access-tier layer; this is the release-plane equivalent.
#:
#: ENUMERATED, not inferred. `_path_like` already requires a `.md` suffix and a
#: directory separator, so a non-path value in one of these fields simply never
#: matches — which is why widening the list is cheap. What it is NOT is a
#: recursive "drop any dict containing a withheld string anywhere": that would
#: erase whole response envelopes over one field, so the containment rule stays
#: keyed on names we have actually seen carry a vault path in a result.
#:
#: The second group is the mutation/adoption vocabulary — where a page WENT.
#: `manage_memory_file`, the trash/restore paths and `adoption_run`'s outcomes
#: all report a destination, and a destination names a page just as surely as
#: `path` does.
_ENTRY_PATH_FIELDS = (
    "path",
    "rel_path",
    "file",
    "target",
    "parent_path",
    "id",
    # Mutation / adoption results: where an item came from and went to.
    "target_path",
    "source_path",
    "original_path",
    "old_path",
    "new_path",
    "destination",
    "trash_path",
    "trash_meta_path",
    "result_path",
    "predecessor_path",
    "resolved_target_path",
    "logical_target_path",
    "logical_source_path",
    "sidecar_path",
    "ordering_path",
    "resource",
    "resource_path",
)


def _decode_pathish(value: str) -> str | None:
    """Percent-decode and fold a path-shaped string into one comparable form.

    Ordered AHEAD of every shape test, which is the fix for a real gap:
    `_path_like` used to require a literal `/` in the RAW string, so
    `Knowledge Base%2FNotes%2F….md` failed the shape test and never reached
    the resolver that would have unquoted it. A decoder that runs after the
    test it is supposed to inform is not a decoder.

    Decoding repeats to a fixed point, bounded at three rounds, so
    `%2520` -> `%20` -> ` ` lands on the same string a plain path would. A
    reference encoded four or more times therefore resolves to nothing and is
    KEPT rather than decided — acceptable because it is not a reachable leak:
    a client would have to decode four times to turn that string back into a
    file, no producer in this system emits that shape, and the bound is what
    stops a crafted string from driving unbounded work here. Trailing dots and whitespace
    go too: `foo.md.` names `foo.md` on the filesystems where it resolves at
    all, and leaving it un-normalized is one more spelling of the same file.
    """
    candidate = value.strip().replace("\\", "/")
    for _ in range(3):
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded.replace("\\", "/")
    candidate = candidate.strip().rstrip(". \t")
    return candidate or None


def _normalize_pathish(value: Any) -> str | None:
    """Canonical vault-relative form of a `.md`-shaped string, or `None`.

    N7: the shape test used to be `endswith(".md")` on the raw string with a
    literal forward-slash requirement, so a backslash-separated variant and an
    uppercase-extension variant both sailed past it while naming the same file
    on the filesystems where that matters. This repo already shipped 481278a
    for exactly that class, so the shape test routes through the same
    normalization `_canonical_reference` performs: separators folded,
    extension matched case-insensitively.

    (Spelled out in prose rather than shown as a literal example on purpose —
    a backslash-separated path in source reads as a Windows absolute path to
    `public_artifact_privacy`'s local-path rule, which then reports a leak.
    `scrubber._CREDENTIAL_PATTERN` carries a note about the same trap.)
    """
    if not isinstance(value, str):
        return None
    candidate = _decode_pathish(value)
    if candidate is None or "/" not in candidate and "." not in candidate:
        return None
    return candidate


def _path_like(value: Any) -> str | None:
    """The vault-relative artifact path `value` names, if it names one."""
    candidate = _normalize_pathish(value)
    if candidate is None or "/" not in candidate:
        return None
    return candidate


def _bare_name(value: Any) -> str | None:
    """A bare artifact filename — meaningful only against a sibling directory."""
    candidate = _normalize_pathish(value)
    if candidate is None or "/" in candidate:
        return None
    return candidate


def _directory_of(node: Mapping[str, Any]) -> str | None:
    """The directory an entry's bare filenames are relative to.

    `browse_memory` and `overview` report folder rows as
    `{"path": "<dir>", "sample_names": ["foo.md", ...]}` — the names carry no
    directory of their own, so filtering them requires reading the sibling
    `path`. Without this, a withheld page leaks by filename from the very
    surface whose job is to enumerate the tree.

    N1(b): the SUBTREE-ROOT node carries `path: ""`, and returning `None` for
    it silently disabled the bare-name filter at the one node that matters —
    the root of the very subtree the caller asked about. `""` is a real
    directory (the scan root), so it is returned as `""` and only a
    non-string is `None`.
    """
    raw = node.get("path")
    if not isinstance(raw, str):
        return None
    return raw.strip().replace("\\", "/").rstrip("/")


def _entry_candidate_paths(entry: Any, directory: str | None = None) -> list[str]:
    """Every vault path this entry names, whether as a field or bare string.

    N1(a): `_bare_name` used to be consulted only for a list element that WAS
    a bare string, never for a bare filename sitting in a `path` field inside
    a dict — which is exactly the shape `largest[]` and `oldest_unmodified[]`
    use (`{"path": "note.md", "bytes": …}` relative to the scan root). Those
    entries therefore had no candidates at all and were kept unconditionally.
    With `directory` in hand a bare name resolves against it.
    """
    found: list[str] = []

    def _add(value: Any) -> None:
        full = _path_like(value)
        if full is not None:
            found.append(full)
            # Also carry the RAW spelling when decoding changed it. A file
            # literally named `a%20b.md` is only findable under its own name,
            # and `_path_like` has already decoded that away — so without this
            # a reference to the withheld percent-literal file is decided
            # against its permitted decoded twin. Both forms must clear.
            if isinstance(value, str):
                raw = value.strip().replace("\\", "/").strip("/")
                if raw and raw != full and raw.lower().endswith(".md"):
                    found.append(raw)
            return
        bare = _bare_name(value)
        if bare is None:
            return
        # `directory` may legitimately be `""` (the scan root), which is not
        # the same as "no directory known" — hence the `is not None` test.
        if directory is not None:
            found.append(f"{directory}/{bare}" if directory else bare)

    _add(entry)
    if isinstance(entry, Mapping):
        for name in _ENTRY_PATH_FIELDS:
            _add(entry.get(name))
    return found


def _bridge_review_audience(entry: Any) -> str | None:
    """Return the approval audience carried only by a bridge-review reason."""
    if not isinstance(entry, Mapping):
        return None
    if "bridge_review" not in entry.get("categories", ()):
        return None
    for reason in entry.get("reasons", ()):
        if not isinstance(reason, Mapping) or reason.get("category") != "bridge_review":
            continue
        value = (reason.get("meta") or {}).get("bridge_audience")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value):
            return value
    return None


def _strip_bridge_review_audience(node: Mapping[Any, Any]) -> dict[Any, Any]:
    """Keep the routing hint internal to the terminal filter."""
    if node.get("category") != "bridge_review" or not isinstance(node.get("meta"), Mapping):
        return dict(node)
    meta = dict(node["meta"])
    meta.pop("bridge_audience", None)
    out = dict(node)
    out["meta"] = meta
    return out


def _reconcile_attention_counts(payload: Any) -> Any:
    """Do not leave filtered review entries reflected in public queue totals."""
    if (
        not isinstance(payload, Mapping)
        or not {"items", "summary", "shown", "total", "truncated", "note"}.issubset(payload)
        or not isinstance(payload.get("items"), list)
    ):
        return payload
    items = payload["items"]
    if not all(isinstance(item, Mapping) for item in items):
        return payload
    summary: dict[str, int] = {}
    states: dict[str, int] = {}
    for item in items:
        for reason in item.get("reasons", ()):
            if isinstance(reason, Mapping) and isinstance(reason.get("category"), str):
                category = reason["category"]
                summary[category] = summary.get(category, 0) + 1
        state = item.get("state")
        if isinstance(state, str):
            states[state] = states.get(state, 0) + 1
    out = dict(payload)
    out.update(
        {
            "summary": summary,
            "shown": len(items),
            "total": len(items),
            "truncated": 0,
            "note": None,
        }
    )
    if "all_total" in out:
        out["all_total"] = len(items)
        out["state_summary"] = states
    return out


def filter_withheld_entries(
    vault_root: Path,
    payload: Any,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> Any:
    """Drop list entries naming an item released below the floor.

    Generic over the payload shape on purpose: these eight surfaces return
    eight different envelopes (`entries`, `inbound`, `items`, `results`,
    per-type buckets), and enumerating each one is how a ninth surface ships
    ungated. Walking any JSON-shaped result and filtering on the paths it
    names covers them all, including shapes added later.

    Same three-state contract as every other consumer: `empty` -> untouched;
    `blocked` or an unresolved-but-expected principal -> every path-bearing
    entry dropped; otherwise decide each named path.
    """
    vault_root = Path(vault_root)
    policy = policy_module.load(vault_root)
    tombstones = lifecycle.tombstoned_paths(vault_root)
    if policy.empty and not tombstones:
        return payload
    who = principal if principal is not None else effective_principal()
    fail_closed = policy.blocked or not who.resolved

    grants_hash = "" if fail_closed else _grants_hash(policy)
    declared_purpose = _declared_purpose(vault_root, who, purpose)
    verdicts: dict[str, bool] = {}
    decisions_by_path: dict[str, Decision | None] = {}

    def _permitted(rel_path: str) -> bool:
        """True when this vault item may be named. Non-vault paths are NOT
        decided here — see `_is_vault_item`."""
        if lifecycle.is_tombstoned(vault_root, rel_path):
            verdicts[rel_path] = False
            return False
        if fail_closed:
            return False
        cached = verdicts.get(rel_path)
        if cached is not None:
            return cached
        decision = _decide_path(
            vault_root,
            rel_path,
            policy=policy,
            audience=who.audience_id,
            purpose=declared_purpose,
            grants_hash=grants_hash,
            authorization_session=who.authorization_session_id,
            authorization_context=who.verified_authorization_session,
        )
        allowed = decision is not None and decision.level >= RELEASE_FLOOR
        decisions_by_path[rel_path] = decision
        _outcome_for_decision(
            vault_root,
            rel_path,
            decision=decision,
            policy=policy,
            audience=who.audience_id,
            outcome="released" if allowed else "withheld",
            purpose=declared_purpose,
        )
        verdicts[rel_path] = allowed
        return allowed

    resolved_items: dict[str, str | None] = {}

    def _resolve_vault_item(rel_path: str) -> str | None:
        """The real vault-relative path this reference names, or `None`.

        N6 established that a reference resolving to nothing under this vault
        is not the release plane's business. NEW-4 is the other half: the
        resolution itself has to be as forgiving as the SHAPE test already is.
        `_normalize_pathish` accepts `.MD` and folds separators, but this used
        to resolve with a bare `is_file()` — case-sensitive on Linux, no
        percent-decoding — so `.MD` and `%20` variants resolved to nothing and,
        under skip-not-deny, sailed through. That is platform-dependent
        disclosure: the same payload leaks on Linux and is filtered on macOS.
        Same class as shipped fix 481278a.

        Resolution walks components case-insensitively via `scandir` rather
        than indexing the whole vault, so the cost is O(depth) directory reads
        on a miss instead of a full walk per result.
        """
        cached = resolved_items.get(rel_path, _UNSET)
        if cached is not _UNSET:
            return cached  # type: ignore[return-value]
        result = _resolve_uncached(rel_path)
        resolved_items[rel_path] = result
        return result

    def _walk_to_real_spelling(parts: list[str]) -> str | None:
        """Name each component the way the filesystem names it, or `None`.

        Prefers an exact match so a page literally called `a%20b.md` still
        resolves to itself, then falls back to a case-insensitive one. The
        cheap `is_file()` probes this replaced were not sound on a
        case-insensitive filesystem: NTFS and APFS answer yes for `NOTE.MD`
        when the page on disk is `note.md`, so the resolver handed back the
        *requested* spelling and the release decision was made against a path
        the policy does not name. Because that decision is fail-closed, the
        observed effect was a permitted page dropped on Windows and macOS
        while the identical payload was released on Linux, where the spelling
        names nothing and the walk below finds the real one. Same payload,
        different answer per platform -- which is the whole thing this
        resolver exists to remove.
        """
        current = vault_root
        real: list[str] = []
        for part in parts:
            folded = part.casefold()
            exact: str | None = None
            insensitive: str | None = None
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.name == part:
                            exact = entry.name
                            break
                        if insensitive is None and entry.name.casefold() == folded:
                            insensitive = entry.name
            except OSError:
                return None
            match = exact if exact is not None else insensitive
            if match is None:
                return None
            real.append(match)
            current = current / match
        try:
            return "/".join(real) if current.is_file() else None
        except OSError:
            return None

    def _resolve_uncached(rel_path: str) -> str | None:
        if lifecycle.is_tombstoned(vault_root, rel_path):
            return _normalize_pathish(rel_path)
        if rel_path.startswith(("http://", "https://", "exomem://")):
            return None
        # RAW spelling first. `_decode_pathish` is otherwise unconditional,
        # which meant a file literally named `a%20b.md` could never resolve to
        # itself — the decode turned it into `a b.md`, so a reference to the
        # withheld percent-literal file landed on its permitted decoded twin
        # and was kept. Exotic, but the file's own name is the most specific
        # evidence available, and `_walk_to_real_spelling` prefers an exact
        # component match, so the literal wins wherever it exists.
        raw = rel_path.strip().replace("\\", "/").strip("/")
        if raw and ".." not in raw.split("/"):
            resolved = _walk_to_real_spelling(raw.split("/"))
            if resolved is not None:
                return resolved
        candidate = _decode_pathish(rel_path)
        if candidate is None:
            return None
        candidate = candidate.strip("/")
        # FOLD `..` rather than rejecting it. Rejecting outright returned
        # `None`, and under the skip-not-deny contract `None` means KEEP — so
        # `…/Insights/../Patterns/withheld.md` survived, where the previous
        # `.resolve()` correctly dropped it. `[x](../Patterns/foo.md)` is the
        # standard relative markdown link, so this shape is ordinary
        # authoring, not an attack. Only a fold that ESCAPES the root is
        # rejected, and then as "not a vault item" rather than as a denial.
        parts: list[str] = []
        for part in candidate.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    return None  # escapes the vault root
                parts.pop()
                continue
            parts.append(part)
        if not parts:
            return None
        # One walk, not a fast `is_file()` probe followed by a slow fallback.
        # The probe was unsound wherever the filesystem folds case: it answered
        # yes for a spelling the page does not have, and the release decision
        # was then made against a path the policy never names.
        resolved = _walk_to_real_spelling(parts)
        if resolved is not None:
            return resolved
        return None

    def _keep(entry: Any, directory: str | None = None) -> bool:
        candidates = _entry_candidate_paths(entry, directory)
        if not candidates:
            return True
        review_audience = _bridge_review_audience(entry)
        for rel_path in candidates:
            if fail_closed:
                _record_blocked_outcome(who.audience_id)
                return False
            # Decide the REAL path the reference resolves to, not the spelling
            # it happened to use — otherwise a `.MD` or percent-encoded variant
            # is decided against a path that does not exist.
            resolved = _resolve_vault_item(rel_path)
            if resolved is None:
                continue  # not a vault item -> not the release plane's business
            if review_audience is not None and who.audience_id == OWNER_AUDIENCE:
                # A bridge-review finding is owner work derived from an exact
                # release approval. It remains actionable when that approval
                # is stale; hiding it then would strand the required reapproval.
                continue
            if not _permitted(resolved):
                return False
        return True

    def _walk(node: Any, *, directory: str | None = None) -> Any:
        if isinstance(node, Mapping):
            node = _strip_bridge_review_audience(node)
            # A terminal surface may assemble a released bridge without using
            # the find/page serializers (review context and structure views do
            # this).  Anchor stripping to the bridge entry itself; never rely
            # on a restricted dependency also appearing in the result pool.
            for candidate in _entry_candidate_paths(node, directory):
                resolved = _resolve_vault_item(candidate)
                if resolved is None or not _permitted(resolved):
                    continue
                decision = decisions_by_path.get(resolved)
                if decision is not None and decision.release_strip:
                    node = bridges.strip_provenance(
                        node,
                        decision.release_strip,
                        direct_page="body" in node or "frontmatter" in node,
                    )
            here = _directory_of(node)
            if here is None:
                here = directory
            kept_pairs: dict[Any, Any] = {}
            for key, value in node.items():
                # A map keyed BY vault path (`outcomes[source] = {...}`) leaks
                # through its KEYS, which no amount of value filtering reaches.
                if _path_like(key) is not None and not _keep({"path": key}, here):
                    continue
                if key in _ENTRY_PATH_FIELDS and not _keep(value, here):
                    continue
                # …and a map VALUE that is itself an entry gets the same
                # predicate a list entry gets. Without this, the whole check
                # was list-shaped: `{"outcomes": {src: {"target_path": X}}}`
                # sailed through because nothing in it was a list.
                if isinstance(value, Mapping) and not _keep(value, here):
                    continue
                kept_pairs[key] = _walk(value, directory=here)
            return kept_pairs
        # N5: tuples, sets and frozensets are ordinary JSON-shaped containers
        # here, and returning them by identity made every one of them an
        # unfiltered channel — `adopt` alone returns 18 tuple-valued fields.
        if isinstance(node, (list, tuple, set, frozenset)):
            kept = [_walk(entry, directory=directory) for entry in node if _keep(entry, directory)]
            if isinstance(node, (set, frozenset)):
                # Rebuilt from the filtered members; a set of dicts is not a
                # real shape, so only hashable members survive this path.
                return type(node)(kept)
            if isinstance(node, tuple):
                rebuild = getattr(type(node), "_make", None)
                return rebuild(kept) if rebuild is not None else type(node)(kept)
            return kept
        return node

    filtered = _walk(payload)
    if (
        isinstance(payload, Mapping)
        and {"items", "summary", "shown", "total", "truncated", "note"}.issubset(payload)
        and isinstance(payload.get("items"), list)
        and isinstance(filtered, Mapping)
        and len(filtered.get("items", ())) != len(payload["items"])
    ):
        return _reconcile_attention_counts(filtered)
    return filtered
