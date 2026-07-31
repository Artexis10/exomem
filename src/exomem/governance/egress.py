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
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .. import find_corpus, memory_refs
from ..find_types import Hit, SemanticUnitHit
from ..kbdir import kb_dirname
from . import bridges, lifecycle, receipts, scrubber, store, tokens
from . import membership as membership_module
from . import policy as policy_module
from .decisions import Decision, decide
from .policy import DISCLOSURE_MAX, DISCLOSURE_MIN, Policy, StandingGrant
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


@contextmanager
def disclosure_boundary(vault_root: Path, command_name: str):
    """Collect one top-level read's decisions and emit only on its success."""
    collector = DisclosureCollector(Path(vault_root), uuid.uuid4().hex, command_name)
    token = _DISCLOSURE_COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _DISCLOSURE_COLLECTOR.reset(token)


def _collector() -> DisclosureCollector | None:
    return _DISCLOSURE_COLLECTOR.get()


def _record_outcome(value: Mapping[str, Any]) -> None:
    collector = _collector()
    if collector is None:
        return
    collector.outcomes.append(DisclosureOutcome(dict(value)))


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
    declared_purpose = _declared_purpose(vault_root, who, purpose)
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
        try:
            target = Path(vault_root) / rel_path
            raw = target.read_bytes()
            value["content_hash"] = hashlib.sha256(raw).hexdigest()
            value["size"] = len(raw)
        except OSError:
            pass
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
                        {"principal": collector.credential_principal, "audience": collector.credential_principal}
                        if collector.credential_principal else {}
                    ),
                    **({"purpose": collector.credential_purpose} if collector.credential_purpose else {}),
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
    if (
        len(values) <= receipts.MAX_OUTCOMES
        and raw_size <= receipts.MAX_RECORD_BYTES // 2
    ):
        return values

    # At most 4 decisions x 7 disclosure levels (including a missing level).
    # Higher-cardinality typed identities become deterministic set/manifest
    # digests inside those audit-useful buckets instead of one row per
    # principal/scope/purpose, which could itself exceed MAX_OUTCOMES.
    buckets: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        typed = {
            key: value[key] for key in ("decision", "level") if key in value
        }
        key = json.dumps(typed, sort_keys=True, separators=(",", ":"))
        buckets.setdefault(key, []).append(value)

    def _digest(items: Iterable[Any], *, unique: bool = False) -> str:
        encoded = [
            json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            for item in items
        ]
        manifest = sorted(set(encoded) if unique else encoded)
        return hashlib.sha256(
            json.dumps(manifest, separators=(",", ":")).encode()
        ).hexdigest()

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
                    [
                        member.get("content_hash") or member.get("ref") or ""
                        for member in members
                    ]
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
LEVEL_EXCERPT_REDACTED = 4  # L4 — renders as L3 until `add-redaction-levels`
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


def _unwrap_reference(raw: str) -> tuple[str, bool]:
    """`(path-ish text, explicitly-a-reference)` for one reference string.

    Shared by the withheld-key comparison and by the reference COLLECTION in
    `annotate_page`, so both read a wikilink, an `exomem://` ref and a plain
    path exactly the same way.
    """
    text = raw.strip()
    if not text:
        return "", False
    explicit = False
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].strip()
        explicit = True
    lowered = text.lower()
    for prefix in _EXOMEM_PATH_PREFIXES:
        if lowered.startswith(prefix):
            text = unquote(text[len(prefix) :])
            explicit = True
            break
    # Wikilink display alias (`[[target|label]]`) and heading anchor
    # (`path.md#Section`) are presentation, not identity.
    text = text.split("|", 1)[0]
    text = text.split("#", 1)[0]
    text = text.replace("\\", "/").strip().strip("/")
    return text, explicit


def _canonical_reference(raw: str) -> tuple[str, bool] | None:
    """`(canonical key, compare-against-stems)` for one reference string.

    The second element marks a reference that carries no directory of its own
    (a wikilink target, a lone `foo.md`) but IS unambiguously a reference —
    wikilink-wrapped, `exomem://`-prefixed, or `.md`-suffixed. Only those may
    be compared against filename stems. Two exclusions keep the normalization
    from degenerating into a blocklist:

    - a reference that carries a directory is compared against FULL paths
      only, so a permitted `Sources/index.md` is not stripped because some
      withheld `Patterns/index.md` shares a filename;
    - a bare word that is not marked as a reference is not compared at all,
      so an ordinary title (`Overview`) is not stripped because a withheld
      page happens to be named `overview.md`.
    """
    text, explicit = _unwrap_reference(raw)
    if not text:
        return None
    key = text.casefold()
    if key.endswith(".md"):
        key = key[: -len(".md")]
        explicit = True
    return key, explicit and "/" not in key


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

    def _hit(candidate: str) -> bool:
        canonical = _canonical_reference(candidate)
        if canonical is None:
            return False
        key, compare_stems = canonical
        # Inside a reference field a bare name needs no `[[…]]` or `.md` to
        # count as a reference — that is what the field means.
        if compare_stems or (reference_field and "/" not in key):
            return key in stems
        return key in full or _kb_stripped(key) in full

    if _hit(value):
        return True
    # A wikilink is an unambiguous reference wherever it appears, so a
    # structured field carrying one inside a longer label still names its
    # target.
    return any(_hit(target) for target in _WIKILINK_ANYWHERE.findall(value))


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
        return _string_names_withheld(
            value, withheld_paths, reference_field=reference_field
        )
    if isinstance(value, Mapping):
        return any(
            _names_withheld(v, withheld_paths, reference_field=reference_field)
            for v in value.values()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _names_withheld(v, withheld_paths, reference_field=reference_field)
            for v in value
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
) -> dict[str, Any]:
    """L1–L4 rendering: rule id + scope label, then constraint, then abstract.

    Deliberately carries no path, title, excerpt, score, or provenance at any
    of these levels — see the `release-gate` spec's "Low levels strip metadata
    oracles" scenario.
    """
    options = options or {}
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

    if level <= LEVEL_NONE:
        return None
    if level == LEVEL_EXCERPT_REDACTED:
        # L4 needs redaction span maps, which `add-redaction-levels` ships.
        # Until then it renders exactly as L3 — an approved abstraction —
        # rather than releasing an unredacted excerpt.
        level = LEVEL_ABSTRACT
    if level < RELEASE_FLOOR:
        return _notice(level, rule_ids=rule_ids, scope_label=scope_label, options=options)

    resolved_kind = kind or _kind_for(payload)
    allowed = _PROJECTORS.get(resolved_kind or "")
    if not allowed:
        log.warning(
            "governance.egress: no projector registered for payload kind %r; "
            "failing closed",
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
        for section in ("packed_paths", "claims", "neighborhood", "contradictions",
                        "semantic_units", "semantic_blocks"):
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


def register_projector(kind: str, allowed_fields: Iterable[str]) -> None:
    """Register the wire allow-list for one payload kind."""
    _PROJECTORS[kind] = frozenset(allowed_fields)


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
    return store.active_session_purpose(
        vault_root,
        audience=who.audience_id,
        authorization_session=who.authorization_session_id,
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


def _decide_path(
    vault_root: Path,
    rel_path: str,
    *,
    policy: Policy,
    audience: str,
    purpose: str | None,
    grants_hash: str,
    authorization_session: str | None = None,
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
    if rel_path.lower().endswith(".md"):
        try:
            raw = full_path.read_bytes()
        except OSError:
            return None
        live_content_hash = hashlib.sha256(raw).hexdigest()
        if expected_content_hash is not None and expected_content_hash != live_content_hash:
            return None

    session_rows, session_identity = store.active_session_grants(
        vault_root,
        audience=audience,
        authorization_session=authorization_session,
        rel_path=rel_path,
        purpose=purpose,
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
        return cached

    mtime = st.st_mtime
    if not rel_path.lower().endswith(".md"):
        # NON-MARKDOWN. Never hand a binary to the markdown parser: it cannot
        # decode one, and its failure used to arrive here as `None` — a value
        # meaning BOTH "unreadable" and "not permitted". That single
        # conflation broke both directions at once (withheld media stayed
        # enumerated in the walk; permitted media stopped downloading for
        # everyone, owner included) and logged a `utf-8 codec` warning per
        # decision on the way. Path/ref selectors decide a binary with no
        # parse at all; a sidecar page supplies frontmatter when one exists.
        scope_ids = membership_module.evaluate_path_only(vault_root, rel_path, policy)
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
    session_grants = tuple(
        StandingGrant(
            id=str(row["grant_id"]),
            source="session",
            scope_ids=tuple(scope_ids),
            audience=audience,
            ceiling=int(row["ceiling"]),
        )
        for row in session_rows
    )
    decision = decide(
        scope_ids,
        audience=audience,
        purpose=purpose,
        policy=policy,
        active_grants=(*policy.grants, *session_grants),
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
    return decision


def _scope_label(policy: Policy, decision: Decision) -> str | None:
    labels = [
        policy.scopes[sid].name or sid
        for sid in decision.scope_ids
        if sid in policy.scopes
    ]
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
        path for hit in hits if (path := _hit_path(hit)) and lifecycle.is_tombstoned(vault_root, path)
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
            RELEASE_FLOOR if who.authorization_session_id else decision.level
        )
        org_ceiling = _applicable_org_ceiling(policy, decision)
        token = tokens.mint_quietly(
            vault_root,
            paths=[rel_path],
            audience=who.audience_id,
            max_level=requested_level,
            authorization_session=who.authorization_session_id,
            purpose=declared_purpose,
            org_ceiling=org_ceiling,
        )
        if token is not None:
            notice["escalation_token"] = token
        notices.append(notice)
        _outcome_for_decision(
            vault_root, rel_path, decision=decision, policy=policy,
            audience=who.audience_id, outcome="withheld", purpose=declared_purpose,
        )
    for hit in released:
        rel_path = _hit_path(hit)
        decision = getattr(hit, "decision", None)
        if rel_path and decision is not None:
            _outcome_for_decision(
                vault_root, rel_path, decision=decision, policy=policy,
                audience=who.audience_id, outcome="released", purpose=declared_purpose,
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


def guard_seed(
    payload: dict[str, Any], withheld_paths: frozenset[str]
) -> dict[str, Any]:
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
                vault_root, rel_path, policy=policy, audience=who.audience_id,
                purpose=declared_purpose, grants_hash=grants_hash,
                authorization_session=who.authorization_session_id,
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
        )
        if decision is not None and decision.release_strip:
            payload = bridges.strip_provenance(payload, decision.release_strip)
    return payload


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


def annotate_page(
    vault_root: Path,
    page: dict[str, Any],
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
    snapshot_content: str | bytes | None = None,
    stable_ref: str | None = None,
) -> dict[str, Any] | None:
    """Render one page at its release decision's level, or `None` below notice.

    `None` is the caller's signal to answer byte-identically to a missing
    path — an item released below notice must be indistinguishable from one
    that never existed.
    """
    vault_root = Path(vault_root)
    rel_path = str(page.get("path") or stable_ref or "")
    if rel_path and lifecycle.is_tombstoned(vault_root, rel_path):
        return None
    policy = policy_module.load(vault_root)
    who = principal if principal is not None else effective_principal()

    if policy.empty:
        return page
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
        if isinstance(page.get("frontmatter"), Mapping) and dict(
            page["frontmatter"]
        ) != parsed.frontmatter:
            _record_blocked_outcome(who.audience_id)
            return None
        if "body" in page and page.get("body") != parsed.body:
            _record_blocked_outcome(who.audience_id)
            return None
        if "content" in page and page.get("content") != raw.decode("utf-8"):
            _record_blocked_outcome(who.audience_id)
            return None
        scope_ids = membership_module.evaluate_snapshot(
            parsed, policy, content_hash=snapshot_hash
        )
        session_rows, _session_identity = store.active_session_grants(
            vault_root,
            audience=who.audience_id,
            authorization_session=who.authorization_session_id,
            rel_path=rel_path,
            purpose=declared_purpose,
        )
        session_grants = tuple(
            StandingGrant(
                id=str(row["grant_id"]),
                source="session",
                scope_ids=tuple(scope_ids),
                audience=who.audience_id,
                ceiling=int(row["ceiling"]),
            )
            for row in session_rows
        )
        decision = decide(
            scope_ids,
            audience=who.audience_id,
            purpose=declared_purpose,
            policy=policy,
            active_grants=(*policy.grants, *session_grants),
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
        )
    if decision is None or decision.level <= LEVEL_NONE:
        _outcome_for_decision(
            vault_root, rel_path, decision=decision, policy=policy,
            audience=who.audience_id, outcome="withheld", purpose=declared_purpose,
            content_hash=snapshot_hash, size=snapshot_size, ref=stable_ref,
        )
        return None

    _outcome_for_decision(
        vault_root, rel_path, decision=decision, policy=policy,
        audience=who.audience_id,
        outcome="released" if decision.level >= RELEASE_FLOOR else "withheld",
        purpose=declared_purpose,
        content_hash=snapshot_hash,
        size=snapshot_size,
        ref=stable_ref,
    )

    level = LEVEL_ABSTRACT if decision.level == LEVEL_EXCERPT_REDACTED else decision.level
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
                )
            )
            is None
            or ref_decision.level < RELEASE_FLOOR
        )
    )
    out = _strip_page_provenance(dict(page), withheld)
    if decision.release_strip:
        out = bridges.strip_provenance(
            out,
            decision.release_strip,
            direct_page=True,
        )
    if level == LEVEL_EXCERPT and isinstance(out.get("body"), str):
        out["body"] = _excerpt_of(out["body"])
        out["body_truncated"] = True
        # Marked only BELOW full disclosure. A page released at L6 must be
        # byte-identical to its ungoverned response — a `release_level: 6`
        # marker would itself tell an audience that governance is in effect.
        out["release_level"] = level
    return out



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
        prefixed = (
            candidate if candidate.startswith(kb_prefix) else kb_prefix + candidate
        )
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


def _strip_page_provenance(
    page: dict[str, Any], withheld_paths: frozenset[str]
) -> dict[str, Any]:
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
            kept = [
                v for v in value if not _names_withheld(v, withheld_paths, reference_field=ref)
            ]
            page[name] = kept
        elif isinstance(value, Mapping):
            page[name] = {
                key: (
                    [
                        v
                        for v in item
                        if not _names_withheld(v, withheld_paths, reference_field=ref)
                    ]
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
    if not scrubber.enabled(vault_root):
        return result
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


def postfilter_error(
    command_name: str, error: BaseException, vault_root: Path
) -> BaseException:
    """The terminal egress filter applied to a RAISED payload.

    `postfilter` guards the value a command returns; this guards the value it
    raises. Both leave through `writer_lease.invoke_command`, the ONE
    dispatcher shared by MCP, REST, hosted and CLI, and an error that names a
    withheld item is exactly the disclosure a result naming one would be —
    `AMBIGUOUS_REFERENCE` embedding the colliding vault paths proved the class
    reachable rather than theoretical.

    An error carries free text and nothing structural for the entry filter to
    drop, so the artifact gate scans every string (the `scan_all` shape) and
    the always-on scrubber runs exactly as it does for results. The gate's own
    empty-policy short circuit keeps an ungoverned vault's text byte-identical.

    Mutates in place and returns the same object, so the caller re-raises with
    its original type, error code and traceback intact.
    """
    del command_name  # an error is free text; there is no per-command shape
    vault_root = Path(vault_root)
    gate = _ArtifactReferenceGate(vault_root, principal=None, purpose=None)
    scrub = scrubber.enabled(vault_root)
    blocked = False

    def _clean(value: Any) -> Any:
        nonlocal blocked
        cleaned = gate.gate_payload(value, scan_strings=True)
        if scrub:
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
        "bootstrap",
        "connect_memory",
        "schema_memory",
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
            "attention", "audit", "overview", "list_directory", "list_inbound_links",
            "evolution", "propose_compilation", "provenance_report", "query_data", "adopt",
        )
    },
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
    ("connect_memory", "operation"): {
        "suggest-links": "structure",
        "suggest-relations": "structure",
        "context": "structure",
        "graph-context": "structure",
        "inbound-links": "structure",
        "resolve-entity": "structure",
        "create-entity": "mutation",
        "accept-relation": "mutation",
    },
    ("adopt_vault", "mode"): {
        "scan-only": "structure",
        "save-manifest": "mutation",
        "copy-as-sources": "mutation",
        "compile-selected": "mutation",
    },
    ("adoption_studio", "action"): {
        "start": "mutation", "status": "structure", "select": "mutation",
        "plan": "mutation", "apply": "mutation", "cancel": "mutation",
        "finish": "mutation", "work-item": "structure", "propose": "mutation",
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
    },
    ("manage_memory_file", "operation"): {
        "list": "structure",
        "create": "validation",
        "append": "validation",
        "move": "mutation",
        "delete": "mutation",
        "trash-list": "structure",
        "recover": "mutation",
    },
    ("schema_memory", "operation"): {
        "infer": "save-conditional",
        "validate": "structure",
        "diff": "structure",
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
            f"{command}.{selector}={value}"
            for value in values
            if not declared.get(value)
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
        sorted(name for name in content_returning_commands(registry) if name not in _COMMAND_OUTCOME_ADAPTER)
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
        if routes and all(
            route in leaf_registry and _leaf_is_covered(route) for route in routes
        ):
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
    )
    if decision is None or decision.level < RELEASE_FLOOR:
        _outcome_for_decision(
            vault_root, rel_path, decision=decision, policy=policy,
            audience=who.audience_id, outcome="withheld", purpose=declared_purpose,
        )
        return None
    _outcome_for_decision(
        vault_root, rel_path, decision=decision, policy=policy,
        audience=who.audience_id, outcome="released", purpose=declared_purpose,
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
            else "released" if level is not None and level >= RELEASE_FLOOR else "withheld"
        ),
        purpose=declared_purpose,
    )
    return level


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
            vault_root, rel_path, principal=principal, purpose=purpose,
            receipt_decision="release_authorized",
        )
        return level is not None and level >= minimum_level
    with disclosure_boundary(vault_root, boundary_name) as collector:
        level = release_level_for(
            vault_root, rel_path, principal=principal, purpose=purpose,
            receipt_decision="release_authorized",
        )
        allowed = level is not None and level >= minimum_level
        emit_boundary_receipt(collector)
        return allowed


#: What replaces a withheld reference inside free text. Fixed, like the
#: scrubber's notice: a per-item description would itself carry information.
WITHHELD_REFERENCE = "[withheld]"

_WRAPPED_ARTIFACT_REFERENCE = re.compile(
    r"\[\[[^\[\]]+\]\]|exomem://[^\s\"'<>)\]]+", re.IGNORECASE
)


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
    ) -> None:
        self.vault_root = Path(vault_root)
        self.policy = policy_module.load(self.vault_root)
        self.who = principal if principal is not None else effective_principal()
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
            without_suffix = str(Path(rel).with_suffix("")) if path.suffix else rel
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
                    marker in key.casefold()
                    for marker in ("handoff", "prompt", "resource")
                )
                gated[key] = self.gate_payload(
                    item, scan_strings=scan_strings or key_marks_free_text
                )
            return gated
        if isinstance(value, (list, tuple, set, frozenset)):
            items = [
                self.gate_payload(item, scan_strings=scan_strings) for item in value
            ]
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
    return _ArtifactReferenceGate(
        Path(vault_root), principal=principal, purpose=purpose
    ).gate_text(text)


def gate_artifact_references(
    vault_root: Path,
    payload: Any,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
    scan_all: bool = False,
) -> Any:
    """Recursively gate nested prompt/resource payloads with one verdict cache."""
    gate = _ArtifactReferenceGate(
        Path(vault_root), principal=principal, purpose=purpose
    )
    return gate.gate_payload(
        payload, scan_strings=scan_all or isinstance(payload, str)
    )


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
        )
        allowed = decision is not None and decision.level >= RELEASE_FLOOR
        _outcome_for_decision(
            vault_root, rel_path, decision=decision, policy=policy,
            audience=who.audience_id, outcome="released" if allowed else "withheld",
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
        vault_root, rel_path, boundary_name="download", minimum_level=LEVEL_FULL,
        principal=principal, purpose=purpose,
    )


def release_allows_frames(
    vault_root: Path,
    rel_path: str,
    *,
    principal: RequestPrincipal | None = None,
    purpose: str | None = None,
) -> bool:
    """True at the release floor and above.

    Sampled keyframes are a bounded excerpt of a video — the image-shaped
    equivalent of the excerpt a hit already carries at L5 — so they ride the
    same floor rather than requiring full disclosure. Below it there is no
    'abstracted frame', so the answer is a refusal, not a degraded render.
    """
    return _binary_boundary(
        vault_root, rel_path, boundary_name="video_frame", minimum_level=RELEASE_FLOOR,
        principal=principal, purpose=purpose,
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
        if isinstance(value, str) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value
        ):
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

    def _resolve_uncached(rel_path: str) -> str | None:
        if lifecycle.is_tombstoned(vault_root, rel_path):
            return _normalize_pathish(rel_path)
        if rel_path.startswith(("http://", "https://", "exomem://")):
            return None
        # RAW exact hit first. `_decode_pathish` is otherwise unconditional,
        # which meant a file literally named `a%20b.md` could never resolve to
        # itself — the decode turned it into `a b.md`, so a reference to the
        # withheld percent-literal file landed on its permitted decoded twin
        # and was kept. Exotic, but the file's own name is the most specific
        # evidence available and it costs one stat.
        raw = rel_path.strip().replace("\\", "/").strip("/")
        if raw and ".." not in raw.split("/"):
            try:
                if (vault_root / raw).is_file():
                    return raw
            except OSError:
                pass
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
        # Exact hit first — the overwhelmingly common case, one stat.
        direct = vault_root.joinpath(*parts)
        try:
            if direct.is_file():
                return "/".join(parts)
        except OSError:
            return None
        # Case-insensitive, component by component.
        current = vault_root
        real: list[str] = []
        for part in parts:
            folded = part.casefold()
            try:
                match = next(
                    (e.name for e in os.scandir(current) if e.name.casefold() == folded),
                    None,
                )
            except OSError:
                return None
            if match is None:
                return None
            real.append(match)
            current = current / match
        try:
            return "/".join(real) if current.is_file() else None
        except OSError:
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
            kept = [
                _walk(entry, directory=directory)
                for entry in node
                if _keep(entry, directory)
            ]
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
