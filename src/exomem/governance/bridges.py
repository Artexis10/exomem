"""Strict bridge parsing and exact-item release admissibility.

Bridge text is ordinary compiled Markdown.  This module is deliberately the
single place that recognizes its governance frontmatter and validates a
release approval against immutable bridge/source snapshots.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .. import find_corpus, memory_refs
from ..vault import VaultPathError, resolve_under_vault
from . import membership
from .policy import Policy, ReleaseGrant

RELEASE_UNAPPROVED = "RELEASE_UNAPPROVED"
RELEASE_STALE = "RELEASE_STALE"
DUE_REVIEW = "DUE_REVIEW"
BRIDGE_EDITED = "BRIDGE_EDITED"
SOURCE_CHANGED_OR_RESTRICTION_CHANGED = "SOURCE_CHANGED_OR_RESTRICTION_CHANGED"
SOURCE_UNAVAILABLE_OR_AMBIGUOUS = "SOURCE_UNAVAILABLE_OR_AMBIGUOUS"

_BRIDGE_REQUIRED = frozenset({"bridge_of", "bridge_scope", "bridge_review"})
_BRIDGE_PREFIX = "bridge_"
_COMPILED_TYPES = frozenset(
    {"experiment", "failure", "insight", "pattern", "production-log", "research-note"}
)
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_BRIDGE_BYTES_RE = re.compile(
    rb"(?m)^(?:bridge_[A-Za-z0-9_-]+|['\"]bridge_[A-Za-z0-9_-]+['\"])\s*:"
)
_WIKILINK_RE = re.compile(r"\[\[([^]]+)\]\]")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_REFERENCE_FIELDS = frozenset(
    {
        "bridge_of",
        "evidence",
        "graph",
        "history",
        "inbound",
        "links",
        "neighborhood",
        "outbound",
        "parent_ref",
        "parent_path",
        "parent_title",
        "provenance",
        "ref",
        "relation",
        "relation_match",
        "relations",
        "source",
        "sources",
        "superseded_by",
        "supersedes",
        "target",
        "target_path",
        "title",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "path",
        "ref",
        "source",
        "source_path",
        "source_ref",
        "source_title",
        "target",
        "target_path",
        "target_ref",
        "title",
        "parent_path",
        "parent_ref",
        "parent_title",
    }
)
_REMOVE = object()


class _CanonicalizationError(ValueError):
    """A policy option cannot be represented in an exact approval snapshot."""


@dataclass(frozen=True)
class BridgeMetadata:
    ref: str
    source_refs: tuple[str, ...]
    scope: str
    review: str


@dataclass(frozen=True)
class StripIdentity:
    path: str
    ref: str
    title: str


@dataclass(frozen=True)
class BridgeAdmission:
    is_bridge: bool
    allowed: bool
    reason: str | None = None
    metadata: BridgeMetadata | None = None
    grant: ReleaseGrant | None = None
    strip_identities: tuple[StripIdentity, ...] = ()
    dependency_digest: str | None = None


@dataclass(frozen=True)
class BridgeProjection:
    """Projection material derived from one exact current release grant."""

    allowed: bool
    reason: str | None
    abstraction: str | None = None
    grant: ReleaseGrant | None = None
    strip_identities: tuple[StripIdentity, ...] = ()
    dependency_digest: str | None = None


@dataclass(frozen=True)
class BridgeReviewSignal:
    cause: str
    bridge_hash: str
    review_date: str
    dependency_digest: str
    signal_version: str


def maybe_bridge(raw: bytes) -> bool:
    """Cheap cache guard: bridge-shaped bytes depend on live source state."""
    if not raw.startswith(b"---"):
        return False
    boundary = raw.find(b"\n---", 3)
    frontmatter = raw if boundary < 0 else raw[:boundary]
    return _BRIDGE_BYTES_RE.search(frontmatter) is not None


def _decode_fixed_point(value: str) -> str:
    """Percent-decode until stable, with a small cycle/abuse bound."""
    current = value
    for _ in range(8):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return current


def _reference_key(value: str) -> str:
    """Canonical comparison key for one path/ref/title spelling."""
    text = _decode_fixed_point(value).strip().strip("'\"")
    if text.startswith("[[") and text.endswith("]] ".strip()):
        text = text[2:-2]
    text = text.split("|", 1)[0].split("#", 1)[0]
    text = text.replace("\\", "/").strip().strip("/").casefold()
    prefix = "knowledge base/"
    if text.startswith(prefix):
        text = text[len(prefix) :]
    if text.endswith(".md"):
        text = text[:-3]
    return text


def _identity_keys(identities: tuple[StripIdentity, ...]) -> frozenset[str]:
    keys: set[str] = set()
    for identity in identities:
        path_key = _reference_key(identity.path)
        keys.update(
            {
                path_key,
                path_key.rsplit("/", 1)[-1],
                _reference_key(identity.ref),
                _reference_key(identity.title),
            }
        )
    return frozenset(key for key in keys if key)


_MAX_PERCENT_DECODE_PASSES = 8
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _embedded_identity_aliases(
    identities: tuple[StripIdentity, ...],
) -> tuple[str, ...]:
    """Finite exact dependency aliases shared with structured stripping."""
    terms: set[str] = set()

    def add(value: str) -> None:
        text = value.strip()
        if text:
            decoded, _spans = _decoded_shadow(text)
            canonical = decoded.strip()
            if canonical:
                terms.add(canonical)

    for identity in identities:
        path = identity.path.replace("\\", "/")
        add(path)
        add(path.removesuffix(".md"))
        add(path.rsplit("/", 1)[-1])
        add(path.rsplit("/", 1)[-1].removesuffix(".md"))
        add(_reference_key(path))
        add(_reference_key(path).rsplit("/", 1)[-1])
        add(identity.ref)
        add(_reference_key(identity.ref))
        add(identity.ref.rsplit("/", 1)[-1])
        add(identity.title)

    return tuple(sorted(terms, key=len, reverse=True))


def _identity_canonicalizes_nonempty(identity: StripIdentity) -> bool:
    """Whether every approval-resolved identity survives bounded decoding."""
    for value in (identity.path, identity.ref, identity.title):
        decoded, _spans = _decoded_shadow(value)
        if not decoded.strip():
            return False
    return True


def _embedded_identity_patterns(
    aliases: tuple[str, ...],
) -> tuple[re.Pattern[str], ...]:
    """Compile Unicode-aware decoded-token matchers once per strip operation."""
    return tuple(
        re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", re.IGNORECASE)
        for alias in aliases
    )


def _decode_shadow_once(
    text: str, spans: list[tuple[int, int]]
) -> tuple[str, list[tuple[int, int]], bool]:
    """Decode valid percent runs once while preserving original codepoint spans."""
    out: list[str] = []
    out_spans: list[tuple[int, int]] = []
    changed = False
    index = 0
    while index < len(text):
        if not (
            text[index] == "%"
            and index + 2 < len(text)
            and text[index + 1] in _HEX_DIGITS
            and text[index + 2] in _HEX_DIGITS
        ):
            out.append(text[index])
            out_spans.append(spans[index])
            index += 1
            continue

        run_start = index
        encoded: list[int] = []
        while (
            index + 2 < len(text)
            and text[index] == "%"
            and text[index + 1] in _HEX_DIGITS
            and text[index + 2] in _HEX_DIGITS
        ):
            encoded.append(int(text[index + 1 : index + 3], 16))
            index += 3
        byte_offset = 0
        while byte_offset < len(encoded):
            first = encoded[byte_offset]
            if first < 0x80:
                width = 1
            elif 0xC2 <= first <= 0xDF:
                width = 2
            elif 0xE0 <= first <= 0xEF:
                width = 3
            elif 0xF0 <= first <= 0xF4:
                width = 4
            else:
                width = 0
            sequence = encoded[byte_offset : byte_offset + width]
            valid = width and len(sequence) == width and all(
                0x80 <= byte <= 0xBF for byte in sequence[1:]
            )
            if valid:
                try:
                    character = bytes(sequence).decode("utf-8")
                except UnicodeDecodeError:
                    valid = False
            if not valid:
                # Preserve invalid triplets in the returned source while
                # treating them as a decoded delimiter, so a valid alias on
                # either side cannot be poisoned by one bad byte.
                out.append("\ufffd")
                out_spans.append(
                    (spans[run_start + 3 * byte_offset][0], spans[run_start + 3 * byte_offset + 2][1])
                )
                byte_offset += 1
                changed = True
                continue
            out.append(character)
            out_spans.append(
                (
                    spans[run_start + 3 * byte_offset][0],
                    spans[run_start + 3 * (byte_offset + width) - 1][1],
                )
            )
            byte_offset += width
            changed = True
    return "".join(out), out_spans, changed


def _decoded_shadow(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Bounded fixed-point percent decoding with original-source span mapping."""
    spans = [(index, index + 1) for index in range(len(text))]
    for _ in range(_MAX_PERCENT_DECODE_PASSES):
        decoded, decoded_spans, changed = _decode_shadow_once(text, spans)
        if not changed:
            break
        text, spans = decoded, decoded_spans
    return text, spans


def _embedded_identity_spans(
    text: str, patterns: tuple[re.Pattern[str], ...]
) -> list[tuple[int, int]]:
    """Find decoded token-exact aliases and map matches back to source spans."""
    shadow, spans = _decoded_shadow(text)
    matches: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(shadow):
            matches.append((spans[match.start()][0], spans[match.end() - 1][1]))
    if not matches:
        return []
    merged: list[tuple[int, int]] = []
    for start, end in sorted(matches):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _redact_embedded_identities(
    text: str, patterns: tuple[re.Pattern[str], ...]
) -> str:
    """Redact exact identity tokens, never arbitrary title substrings."""
    spans = _embedded_identity_spans(text, patterns)
    if not spans:
        return text
    out: list[str] = []
    offset = 0
    for start, end in spans:
        out.append(text[offset:start])
        offset = end
    out.append(text[offset:])
    return "".join(out)


def _string_names_identity(
    value: str,
    keys: frozenset[str],
    patterns: tuple[re.Pattern[str], ...],
    *,
    reference_context: bool,
) -> bool:
    """Match exact identities and explicit link targets, never arbitrary prose."""
    if _reference_key(value) in keys and (reference_context or " " not in value.strip()):
        return True
    for target in (*_WIKILINK_RE.findall(value), *_MARKDOWN_LINK_RE.findall(value)):
        if _reference_key(target) in keys:
            return True
    return reference_context and bool(_embedded_identity_spans(value, patterns))


def _entry_names_identity(
    value: Any,
    keys: frozenset[str],
    patterns: tuple[re.Pattern[str], ...],
    *,
    reference_context: bool = False,
) -> bool:
    """Whether a structured entry is itself the restricted dependency."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).casefold()
            child_context = reference_context or name in _REFERENCE_FIELDS
            if _string_names_identity(
                str(key), keys, patterns, reference_context=True
            ):
                return True
            if name in _IDENTITY_FIELDS and isinstance(item, str):
                if _string_names_identity(
                    item, keys, patterns, reference_context=True
                ):
                    return True
            if _entry_names_identity(
                item, keys, patterns, reference_context=child_context
            ):
                return True
        return False
    if isinstance(value, str):
        return _string_names_identity(
            value, keys, patterns, reference_context=reference_context
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _entry_names_identity(
                item, keys, patterns, reference_context=reference_context
            )
            for item in value
        )
    return False


def _strip_body_lines(
    body: str,
    keys: frozenset[str],
    patterns: tuple[re.Pattern[str], ...],
    *,
    reference_context: bool = False,
) -> str:
    """Drop provenance-bearing Markdown lines while preserving bridge prose."""
    kept = []
    for line in body.splitlines(keepends=True):
        if _string_names_identity(
            line, keys, patterns, reference_context=reference_context
        ):
            continue
        kept.append(_redact_embedded_identities(line, patterns))
    return "".join(kept)


def strip_provenance(
    payload: Any,
    identities: tuple[StripIdentity, ...],
    *,
    direct_page: bool = False,
) -> Any:
    """Return a copy with every dependency identity removed recursively.

    A bridge approval authorizes the compiled claim, not its source trail.
    The stripping target comes from the approval snapshot itself, so this is
    independent of which dependencies happened to enter a retrieval pool.
    """
    keys = _identity_keys(identities)
    aliases = _embedded_identity_aliases(identities)
    patterns = _embedded_identity_patterns(aliases)

    def walk(
        node: Any,
        *,
        field: str | None = None,
        root: bool = False,
        reference_context: bool = False,
    ) -> Any:
        if isinstance(node, str):
            if field == "body":
                return _strip_body_lines(
                    node, keys, patterns, reference_context=reference_context
                )
            if _string_names_identity(
                node, keys, patterns, reference_context=reference_context
            ):
                return _REMOVE
            return _redact_embedded_identities(node, patterns)
        if isinstance(node, Mapping):
            # Graph edges can name a removed source only through opaque node
            # keys.  Resolve that indirection before the generic recursion.
            dropped_node_keys: set[str] = set()
            for node_name in ("nodes", "seeds", "seed"):
                raw_nodes = node.get(node_name)
                if isinstance(raw_nodes, Mapping):
                    candidates = [raw_nodes]
                elif isinstance(raw_nodes, list):
                    candidates = raw_nodes
                else:
                    continue
                dropped_node_keys.update(
                    str(item.get("node_key") or "")
                    for item in candidates
                    if isinstance(item, Mapping)
                    and _entry_names_identity(
                        item, keys, patterns, reference_context=True
                    )
                )
            out: dict[Any, Any] = {}
            for key, value in node.items():
                name = str(key).casefold()
                child_context = reference_context or name in _REFERENCE_FIELDS
                if name in {"bridge_of", "bridge_scope"} or (
                    direct_page and root and name == "content"
                ):
                    continue
                if _string_names_identity(
                    str(key), keys, patterns, reference_context=True
                ):
                    continue
                if name in {"nodes", "seeds"} and isinstance(value, list):
                    value = [
                        item
                        for item in value
                        if not _entry_names_identity(
                            item, keys, patterns, reference_context=True
                        )
                    ]
                if name == "seed" and isinstance(value, Mapping):
                    if _entry_names_identity(
                        value, keys, patterns, reference_context=True
                    ):
                        continue
                if name == "edges" and isinstance(value, list) and dropped_node_keys:
                    value = [
                        item
                        for item in value
                        if not (
                            isinstance(item, Mapping)
                            and {
                                str(item.get("src_key") or ""),
                                str(item.get("dst_key") or ""),
                            }
                            & dropped_node_keys
                        )
                    ]
                cleaned = walk(
                    value,
                    field=name,
                    reference_context=child_context,
                )
                if cleaned is _REMOVE:
                    continue
                out[key] = cleaned
            return out
        if isinstance(node, (list, tuple, set, frozenset)):
            cleaned_items = []
            for item in node:
                if _entry_names_identity(
                    item, keys, patterns, reference_context=reference_context
                ):
                    continue
                cleaned = walk(
                    item, field=field, reference_context=reference_context
                )
                if cleaned is not _REMOVE:
                    cleaned_items.append(cleaned)
            if isinstance(node, tuple):
                return tuple(cleaned_items)
            if isinstance(node, (set, frozenset)):
                return type(node)(cleaned_items)
            return cleaned_items
        return node

    cleaned = walk(payload, root=True)
    return payload if cleaned is _REMOVE else cleaned


def parse_bridge_frontmatter(frontmatter: Mapping[str, Any]) -> tuple[BridgeMetadata | None, str | None]:
    """Parse the all-or-none bridge shape without resolving any references."""
    bridge_keys = {str(key) for key in frontmatter if str(key).startswith(_BRIDGE_PREFIX)}
    present = bridge_keys | ({"bridge_of"} if "bridge_of" in frontmatter else set())
    if not present:
        return None, None
    if present != _BRIDGE_REQUIRED:
        return None, "bridge frontmatter must contain exactly bridge_of, bridge_scope, bridge_review"
    if str(frontmatter.get("type") or "") not in _COMPILED_TYPES:
        return None, "a bridge must be an ordinary compiled note"
    identity = memory_refs.normalize_id(frontmatter.get("exomem_id"))
    if identity is None:
        return None, "a bridge requires one valid immutable exomem_id"
    raw_sources = frontmatter.get("bridge_of")
    if not isinstance(raw_sources, list) or not raw_sources:
        return None, "bridge_of must be a non-empty list of stable memory refs"
    refs: list[str] = []
    for value in raw_sources:
        if not isinstance(value, str) or memory_refs.parse_memory_ref(value) is None:
            return None, "bridge_of accepts stable memory refs only"
        canonical = memory_refs.memory_ref(memory_refs.parse_memory_ref(value) or "")
        if canonical in refs:
            return None, "bridge_of refs must be unique"
        refs.append(canonical)
    scope = frontmatter.get("bridge_scope")
    if not isinstance(scope, str) or _SCOPE_RE.fullmatch(scope) is None:
        return None, "bridge_scope must be a lowercase slug"
    review = frontmatter.get("bridge_review")
    review_text = str(review) if isinstance(review, dt.date) else review
    if not isinstance(review_text, str):
        return None, "bridge_review must be an ISO date"
    try:
        parsed_review = dt.date.fromisoformat(review_text)
    except ValueError:
        return None, "bridge_review must be an ISO date"
    return (
        BridgeMetadata(
            ref=memory_refs.memory_ref(identity),
            source_refs=tuple(sorted(refs)),
            scope=scope,
            review=parsed_review.isoformat(),
        ),
        None,
    )


def restriction_signature(
    scope_ids: frozenset[str] | set[str] | tuple[str, ...],
    *,
    policy: Policy,
    audience: str,
) -> str:
    """Bind source membership plus evaluator-relevant audience restrictions."""
    scopes = frozenset(scope_ids)

    def encoded(value: Any, ancestors: set[int]) -> Any:
        def sort_key(item: Any) -> str:
            return json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )

        if value is None:
            return {"type": "none"}
        if isinstance(value, bool):
            return {"type": "bool", "value": value}
        if isinstance(value, int):
            return {"type": "int", "value": str(value)}
        if isinstance(value, str):
            return {"type": "str", "value": value}
        if isinstance(value, float):
            if math.isnan(value):
                spelling = "nan"
            elif math.isinf(value):
                spelling = "inf" if value > 0 else "-inf"
            else:
                spelling = value.hex()
            return {"type": "float", "value": spelling}
        if isinstance(value, bytes):
            return {"type": "bytes", "hex": value.hex()}
        if isinstance(value, dt.datetime):
            return {"type": "datetime", "value": value.isoformat()}
        if isinstance(value, dt.date):
            return {"type": "date", "value": value.isoformat()}
        if isinstance(value, dt.time):
            return {"type": "time", "value": value.isoformat()}

        identity = id(value)
        if identity in ancestors:
            raise _CanonicalizationError("cyclic rule option value")
        if isinstance(value, Mapping):
            ancestors.add(identity)
            try:
                entries = [
                    {"key": encoded(key, ancestors), "value": encoded(item, ancestors)}
                    for key, item in value.items()
                ]
            finally:
                ancestors.remove(identity)
            return {"type": "mapping", "entries": sorted(entries, key=sort_key)}
        if isinstance(value, list):
            ancestors.add(identity)
            try:
                items = [encoded(item, ancestors) for item in value]
            finally:
                ancestors.remove(identity)
            return {"type": "list", "items": items}
        if isinstance(value, tuple):
            ancestors.add(identity)
            try:
                items = [encoded(item, ancestors) for item in value]
            finally:
                ancestors.remove(identity)
            return {"type": "tuple", "items": items}
        if isinstance(value, set):
            ancestors.add(identity)
            try:
                items = [encoded(item, ancestors) for item in value]
            finally:
                ancestors.remove(identity)
            return {"type": "set", "items": sorted(items, key=sort_key)}
        if isinstance(value, frozenset):
            ancestors.add(identity)
            try:
                items = [encoded(item, ancestors) for item in value]
            finally:
                ancestors.remove(identity)
            return {"type": "frozenset", "items": sorted(items, key=sort_key)}
        raise _CanonicalizationError(
            f"unsupported option value type: {type(value).__module__}.{type(value).__qualname__}"
        )

    def canonical_option(value: Any) -> Any:
        return encoded(value, set())

    rules = [
        {
            "id": rule.id,
            "kind": rule.kind,
            "ceiling": rule.ceiling,
            "purpose": rule.purpose,
            "purpose_condition": rule.purpose_condition,
            "options": canonical_option(rule.options),
            "scope_ids": sorted(scopes & set(rule.scope_ids)),
        }
        for rule in policy.rules
        if rule.audience == audience and bool(scopes & set(rule.scope_ids))
    ]
    grants = [
        {
            "id": grant.id,
            "ceiling": grant.ceiling,
            "scope_ids": sorted(scopes & set(grant.scope_ids)),
        }
        for grant in policy.grants
        if grant.audience == audience and bool(scopes & set(grant.scope_ids))
    ]

    def scope_row(scope_id: str) -> dict[str, Any]:
        scope = policy.scopes.get(scope_id)
        return {
            "id": scope_id,
            "constraint": None if scope is None else scope.constraint,
            # A `default_deny` declaration restricts as much as an authored
            # `ceiling: 0` does, and it names no rule — so without it here,
            # locking the source scope leaves a previously approved
            # abstraction of a private source flowing on a fresh-looking
            # signature.
            "default_deny": False if scope is None else bool(scope.default_deny),
        }

    payload = {
        "audience": audience,
        "scope_ids": sorted(scopes),
        "scopes": [scope_row(scope_id) for scope_id in sorted(scopes)],
        "rules": sorted(rules, key=lambda row: (row["id"], row["kind"], row["ceiling"])),
        "grants": sorted(grants, key=lambda row: (row["id"], row["ceiling"])),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_exact(vault_root: Path, rel_path: str, raw: bytes):
    try:
        target, canonical = resolve_under_vault(
            vault_root, rel_path, must_exist=True, must_be_file=True
        )
    except VaultPathError:
        return None
    if canonical != rel_path or target.suffix.casefold() != ".md":
        return None
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return None
    parsed = find_corpus.parse_page(target, mtime, vault_root, content=raw)
    if parsed is None or parsed.rel_path != canonical:
        return None
    return parsed


def _reference_resolves_exactly_to(
    vault_root: Path, rel_path: str, ref: str
) -> bool:
    """Require one immutable ref to resolve to this exact canonical bridge path."""
    try:
        return memory_refs.resolve_identifier_read_only(vault_root, ref) == rel_path
    except memory_refs.ReferenceError:
        return False


def _dependency_snapshot(
    vault_root: Path,
    ref: str,
    *,
    policy: Policy,
    audience: str,
) -> tuple[str, str, str, str, str] | None:
    try:
        resolved = memory_refs.resolve_identifier_read_only(vault_root, ref)
        target, canonical = resolve_under_vault(
            vault_root, resolved, must_exist=True, must_be_file=True
        )
        raw = target.read_bytes()
    except (memory_refs.ReferenceError, VaultPathError, OSError):
        return None
    parsed = _parse_exact(vault_root, canonical, raw)
    if parsed is None:
        return None
    parsed_ref = memory_refs.parse_memory_ref(ref)
    live_id = memory_refs.normalize_id(parsed.frontmatter.get("exomem_id"))
    if parsed_ref is None or live_id != parsed_ref:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        scopes = membership.evaluate_snapshot(parsed, policy, content_hash=digest)
    except membership.MembershipUnresolved:
        return None
    try:
        signature = restriction_signature(scopes, policy=policy, audience=audience)
    except _CanonicalizationError:
        return None
    live_ref = memory_refs.memory_ref(live_id)
    if not _identity_canonicalizes_nonempty(
        StripIdentity(path=canonical, ref=live_ref, title=parsed.title)
    ):
        return None
    return canonical, digest, signature, parsed.title, live_ref


def admit(
    vault_root: Path,
    rel_path: str,
    raw: bytes,
    *,
    policy: Policy,
    audience: str,
) -> BridgeAdmission:
    """Validate the exact bridge bytes and every dependency snapshot."""
    parsed = _parse_exact(Path(vault_root), rel_path, raw)
    if parsed is None:
        return BridgeAdmission(True, False, RELEASE_STALE)
    metadata, error = parse_bridge_frontmatter(parsed.frontmatter)
    bridge_shaped = metadata is not None or error is not None
    if not bridge_shaped:
        return BridgeAdmission(False, True)
    if metadata is None:
        return BridgeAdmission(True, False, RELEASE_UNAPPROVED)
    if not _reference_resolves_exactly_to(Path(vault_root), parsed.rel_path, metadata.ref):
        return BridgeAdmission(True, False, RELEASE_STALE, metadata=metadata)

    candidates = [
        grant
        for grant in policy.release_grants
        if grant.path == rel_path
        and grant.ref == metadata.ref
        and grant.to_audience == audience
    ]
    if not candidates:
        return BridgeAdmission(True, False, RELEASE_UNAPPROVED, metadata=metadata)
    bridge_hash = hashlib.sha256(raw).hexdigest()
    valid: list[tuple[ReleaseGrant, tuple[StripIdentity, ...], str]] = []
    for grant in candidates:
        if (
            grant.content_hash != bridge_hash
            or grant.bridge_scope != metadata.scope
            or tuple(dep.ref for dep in grant.bridge_of) != metadata.source_refs
            or not _reference_resolves_exactly_to(Path(vault_root), parsed.rel_path, grant.ref)
        ):
            continue
        live_identities: list[StripIdentity] = []
        dependency_rows: list[dict[str, str]] = []
        matched = True
        for dependency in grant.bridge_of:
            live = _dependency_snapshot(
                Path(vault_root), dependency.ref, policy=policy, audience=audience
            )
            if live is None:
                matched = False
                break
            path, content_hash, signature, title, live_ref = live
            if (
                path != dependency.path
                or live_ref != dependency.ref
                or content_hash != dependency.content_hash
                or signature != dependency.restriction_signature
            ):
                matched = False
                break
            identity = StripIdentity(path=path, ref=live_ref, title=title)
            if not _identity_canonicalizes_nonempty(identity):
                matched = False
                break
            live_identities.append(identity)
            dependency_rows.append(
                {"path": path, "ref": live_ref, "content_hash": content_hash, "restriction_signature": signature}
            )
        if not matched:
            continue
        digest = hashlib.sha256(
            json.dumps(dependency_rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        valid.append((grant, tuple(live_identities), digest))
    if len(valid) != 1:
        return BridgeAdmission(True, False, RELEASE_STALE, metadata=metadata)
    grant, identities, dependency_digest = valid[0]
    return BridgeAdmission(
        True,
        True,
        metadata=metadata,
        grant=grant,
        strip_identities=identities,
        dependency_digest=dependency_digest,
    )


def resolve_approved_abstraction(
    vault_root: Path,
    bridge_id: str,
    *,
    policy: Policy,
    audience: str,
) -> BridgeProjection:
    """Resolve an opaque policy option to exact approved bridge content.

    The authored option names a release-grant id; it is never response text.
    Resolution rereads the grant-bound bridge bytes and delegates all path,
    ref, audience, byte-hash, dependency, restriction-signature, and
    provenance checks to :func:`admit`.  Only the approved bridge body after
    release-bound provenance stripping is returned to the caller.
    """
    candidates = [
        grant
        for grant in policy.release_grants
        if grant.id == bridge_id and grant.to_audience == audience
    ]
    if not candidates:
        return BridgeProjection(False, RELEASE_UNAPPROVED)
    if len(candidates) != 1:
        return BridgeProjection(False, RELEASE_STALE)
    grant = candidates[0]
    try:
        target, canonical = resolve_under_vault(
            Path(vault_root),
            grant.path,
            must_exist=True,
            must_be_file=True,
        )
        if canonical != grant.path:
            return BridgeProjection(False, RELEASE_STALE)
        raw = target.read_bytes()
    except (VaultPathError, OSError):
        return BridgeProjection(False, RELEASE_STALE)

    admission = admit(
        Path(vault_root),
        canonical,
        raw,
        policy=policy,
        audience=audience,
    )
    if (
        not admission.allowed
        or admission.grant is None
        or admission.grant.id != bridge_id
    ):
        return BridgeProjection(False, admission.reason or RELEASE_STALE)
    parsed = _parse_exact(Path(vault_root), canonical, raw)
    if parsed is None:
        return BridgeProjection(False, RELEASE_STALE)
    projected = strip_provenance(
        {"body": parsed.body},
        admission.strip_identities,
        direct_page=True,
    )
    abstraction = projected.get("body") if isinstance(projected, Mapping) else None
    if not isinstance(abstraction, str) or not abstraction.strip():
        return BridgeProjection(False, RELEASE_STALE)
    return BridgeProjection(
        True,
        None,
        abstraction=abstraction,
        grant=admission.grant,
        strip_identities=admission.strip_identities,
        dependency_digest=admission.dependency_digest,
    )


def review_signal(
    vault_root: Path,
    grant: ReleaseGrant,
    *,
    policy: Policy,
    today: dt.date,
) -> BridgeReviewSignal | None:
    """Derive one approval-bound review signal without writing sidecars."""
    unavailable_hash = hashlib.sha256(b"bridge-unavailable").hexdigest()
    try:
        target, canonical = resolve_under_vault(
            vault_root,
            grant.path,
            must_exist=True,
            must_be_file=True,
        )
        raw = target.read_bytes()
    except (VaultPathError, OSError):
        raw = b""
        canonical = ""
    bridge_hash = hashlib.sha256(raw).hexdigest() if raw else unavailable_hash
    parsed = _parse_exact(vault_root, grant.path, raw) if raw else None
    metadata: BridgeMetadata | None = None
    if parsed is not None:
        metadata, _error = parse_bridge_frontmatter(parsed.frontmatter)

    dependency_rows: list[dict[str, Any]] = []
    unavailable = False
    changed = False
    for index, dependency in enumerate(grant.bridge_of):
        live = _dependency_snapshot(
            vault_root,
            dependency.ref,
            policy=policy,
            audience=grant.to_audience,
        )
        if live is None:
            unavailable = True
            dependency_rows.append({"index": index, "state": "unavailable"})
            continue
        path, content_hash, signature, _title, live_ref = live
        exact = (
            path == dependency.path
            and live_ref == dependency.ref
            and content_hash == dependency.content_hash
            and signature == dependency.restriction_signature
        )
        changed = changed or not exact
        dependency_rows.append(
            {
                "index": index,
                "state": "exact" if exact else "changed",
                "content_hash": content_hash,
                "restriction_signature": signature,
                "path_match": path == dependency.path,
                "ref_match": live_ref == dependency.ref,
            }
        )
    dependency_digest = hashlib.sha256(
        json.dumps(dependency_rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    bridge_exact = (
        canonical == grant.path
        and _reference_resolves_exactly_to(vault_root, canonical, grant.ref)
        and bridge_hash == grant.content_hash
        and metadata is not None
        and metadata.ref == grant.ref
        and metadata.scope == grant.bridge_scope
        and metadata.source_refs == tuple(dep.ref for dep in grant.bridge_of)
    )
    if not bridge_exact:
        cause = BRIDGE_EDITED
    elif unavailable:
        cause = SOURCE_UNAVAILABLE_OR_AMBIGUOUS
    elif changed:
        cause = SOURCE_CHANGED_OR_RESTRICTION_CHANGED
    elif metadata is not None and dt.date.fromisoformat(metadata.review) <= today:
        cause = DUE_REVIEW
    else:
        return None

    review_date = metadata.review if metadata is not None else "unknown"
    signal_payload = {
        "approval_id": grant.id,
        "bridge_hash": bridge_hash,
        "cause": cause,
        "dependency_digest": dependency_digest,
        "review_date": review_date,
    }
    signal_version = hashlib.sha256(
        json.dumps(signal_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return BridgeReviewSignal(
        cause=cause,
        bridge_hash=bridge_hash,
        review_date=review_date,
        dependency_digest=dependency_digest,
        signal_version=signal_version,
    )
