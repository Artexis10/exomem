"""The `schema_memory` contract every vocabulary registry shares.

`inspect`, `propose`, `save`, `history` and `restore` over one `RegistrySpec`.
`commands.op_schema_memory` routes a registry subject here; the old operation
names keep their own handlers.

Mutation authority uses the bound owner principal and the activated v2 writer.
Disclosure admission runs separately before any private registry bytes are read.
A resolved nonowner may queue an admitted, validated proposal for the owner.
An unresolved caller cannot mutate or queue a proposal.

Every registry write answers with `vocabulary_receipt`: one line per key it
registered or changed, naming the registry, the key, the parent and the
restore that reverts it. The line belongs to that write's response only, so it
appears once and asks for nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import registry
from .registry import Entry, RegistryError, RegistrySpec, Snapshot

INSPECT_DEFAULT_LIMIT = 20
INSPECT_MAX_LIMIT = 200
#: The longest delta a restricted principal's pending proposal can carry.
PENDING_MAX_CHARS = 2000
#: Near-duplicates are keys within this edit distance of a proposed key or alias.
NEAR_DUPLICATE_DISTANCE = 2
# Continuations use the fixed v1 protocol: a digest prefix and a bounded page offset.
_CONTINUATION_RE = re.compile(r"^v1:([0-9a-f]{16}):(\d{1,6})$")


def restricted_reason(vault_root: Path) -> str | None:
    """Why whole-vault content is unavailable to the caller, or None."""
    from ..governance import egress

    refusal = egress.owner_only_aggregate(vault_root)
    return None if refusal is None else str(refusal.get("reason") or "audience_restricted")


def queues_for_owner(vault_root: Path) -> str | None:
    """Why a save should be queued for the owner instead of written, or None.

    A vault that activated v2 additive authority decided delegation already:
    its writer gate admits a save only under an exact approval or a current
    grant, so a restricted save goes to that gate rather than to the queue.
    """
    from ..governance.principal import effective_principal
    from ..governance.raw_protection import is_owner
    from ..vocabulary_authority import VocabularyAuthority

    who = effective_principal()
    if not who.resolved:
        raise RegistryError("UNRESOLVED_PRINCIPAL: registry mutation requires a bound principal")
    if is_owner(who):
        return None
    try:
        mode = VocabularyAuthority(Path(vault_root)).runtime_status().mode
    except Exception:  # noqa: BLE001 — unreadable authority: the writer gate refuses
        return None
    return "audience_restricted" if mode == "v1" else None


def admission_refusal(vault_root: Path, spec: RegistrySpec) -> dict[str, Any] | None:
    """Admit the adapter's complete overlay before hashes, aliases or entries are read.

    Each S1 adapter reads only its declared overlay and shipped packs. The two
    taxonomy subjects share one overlay, so admission covers both axes together.
    A withheld overlay makes the operation unavailable until domain-aware delivery.
    """
    from ..governance import egress
    from ..governance.principal import effective_principal
    from ..governance.raw_protection import is_owner

    root = Path(vault_root)
    who = effective_principal()
    if is_owner(who) and restricted_reason(root) is None:
        return None
    path = spec.overlay(root).relative_to(root).as_posix()
    if egress.release_level_for_path_only(root, path) < egress.LEVEL_FULL:
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    return None


def history_refusal(vault_root: Path, spec: RegistrySpec) -> dict[str, Any] | None:
    """Admit every kept snapshot before a history producer reads its header or bytes."""
    from .. import registry_history
    from ..governance import egress
    from ..governance.principal import effective_principal
    from ..governance.raw_protection import is_owner

    root = Path(vault_root)
    if is_owner(effective_principal()) and restricted_reason(root) is None:
        return None
    directory = registry_history.history_dir(root, spec.stem).relative_to(root).as_posix()
    if egress.release_level_for_path_only(root, directory) < egress.LEVEL_FULL:
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    names = registry_history.kept_names(root, directory, suffix=".yaml")
    if names is None or any(
        egress.release_level_for_path_only(root, f"{directory}/{name}") < egress.LEVEL_FULL
        for name in names[: registry_history.HISTORY_KEEP]
    ):
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    return None


def _usage(vault_root: Path, spec: RegistrySpec, snapshot: Snapshot) -> Any:
    from .usage import Usage

    if spec.usage is None:
        return Usage(available=False, reason="not_counted")
    return spec.usage(vault_root, snapshot)


def _ordered(snapshot: Snapshot, usage: Any) -> list[Entry]:
    entries = list(snapshot.entries.values())
    if usage.available:
        return sorted(
            entries,
            key=lambda entry: (
                entry.status != "active",
                -usage.counts.get(entry.key, 0),
                entry.key,
            ),
        )
    return entries


def save_contract(spec: RegistrySpec) -> dict[str, Any]:
    """What a `save` delta for this registry may carry, for a caller to read."""
    return {
        "verbs": ["upsert", "alias", "deprecate"],
        "fields": sorted(spec.fields),
        "attributes": sorted(spec.attributes),
        "fixed_once_saved": sorted(spec.immutable),
        "replacement_required": spec.replacement_required,
        "promotion": spec.promotion,
    }


def inspect(
    vault_root: Path,
    spec: RegistrySpec,
    *,
    limit: int | None = None,
    continuation: str | None = None,
) -> dict[str, Any]:
    """The live registry in the generic entry shape, paginated, with usage counts."""
    size = INSPECT_DEFAULT_LIMIT if limit is None else int(limit)
    if not 1 <= size <= INSPECT_MAX_LIMIT:
        raise RegistryError(f"INVALID_REGISTRY_ARGUMENT: limit must be 1 to {INSPECT_MAX_LIMIT}")
    refusal = admission_refusal(vault_root, spec)
    if refusal is not None:
        return refusal
    snapshot = registry.load(spec, vault_root)
    offset = 0
    if continuation is not None:
        match = _CONTINUATION_RE.fullmatch(str(continuation))
        if match is None or match.group(1) != snapshot.effective_digest[:16]:
            raise RegistryError(
                "STALE_REGISTRY_CONTINUATION: the registry changed; inspect again from the start"
            )
        offset = int(match.group(2))
    usage = _usage(vault_root, spec, snapshot)
    ordered = _ordered(snapshot, usage)
    page = ordered[offset : offset + size]
    entries = []
    for entry in page:
        row = entry.as_dict()
        if usage.available:
            row["count"] = usage.counts.get(entry.key, 0)
        entries.append(row)
    following = offset + len(page)
    out: dict[str, Any] = {
        "subject": spec.name,
        "content_hash": snapshot.content_hash,
        "effective_digest": snapshot.effective_digest,
        "total": len(ordered),
        "returned": len(page),
        "counts": "available" if usage.available else "unavailable",
        "entries": entries,
        "findings": [dict(item) for item in snapshot.findings],
        "save": save_contract(spec),
        "continuation": (
            f"v1:{snapshot.effective_digest[:16]}:{following}" if following < len(ordered) else None
        ),
    }
    if usage.available:
        out["count_source"] = usage.source
    else:
        out["reason"] = usage.reason
    return out


def _tokens(entry: Entry) -> dict[str, str]:
    """Each comparison token an entry claims, with the field that claims it."""
    tokens: dict[str, str] = {registry.token(entry.key): "key"}
    if entry.label:
        tokens.setdefault(registry.token(entry.label), "label")
    for alias in entry.aliases:
        tokens.setdefault(registry.token(alias), "aliases")
    for name in ("folder", "path_label"):
        value = entry.attributes.get(name)
        if isinstance(value, str) and value:
            tokens.setdefault(registry.token(value), f"attributes.{name}")
    tokens.pop("", None)
    return tokens


def _collisions(snapshot: Snapshot, proposed: Entry) -> list[dict[str, str]]:
    owners: dict[str, tuple[str, str]] = {}
    for entry in snapshot.entries.values():
        if entry.key == proposed.key:
            continue
        for value, field_name in _tokens(entry).items():
            owners.setdefault(value, (entry.key, field_name))
    out = []
    for value, field_name in _tokens(proposed).items():
        owner = owners.get(value)
        if owner is not None:
            out.append(
                {"key": owner[0], "field": field_name, "value": value, "existing_field": owner[1]}
            )
    return out


def _near_duplicates(
    snapshot: Snapshot, proposed: Entry, usage: Any, exact: set[str]
) -> list[dict[str, Any]]:
    from ..project_keys import _levenshtein

    mine = set(_tokens(proposed))
    out: list[dict[str, Any]] = []
    for entry in snapshot.entries.values():
        if entry.key == proposed.key or entry.key in exact:
            continue
        theirs = set(_tokens(entry))
        distance = min(
            (
                _levenshtein(left, right, max_dist=NEAR_DUPLICATE_DISTANCE)
                for left in mine
                for right in theirs
            ),
            default=NEAR_DUPLICATE_DISTANCE + 1,
        )
        if distance <= NEAR_DUPLICATE_DISTANCE:
            row: dict[str, Any] = {"key": entry.key, "distance": distance, "status": entry.status}
            if usage.available:
                row["count"] = usage.counts.get(entry.key, 0)
            out.append(row)
    return sorted(out, key=lambda row: (row["distance"], row["key"]))


def _inherited(spec: RegistrySpec, snapshot: Snapshot, entry: Entry) -> dict[str, Any]:
    """Attributes the entry takes from its parent because it does not set them."""
    parent = snapshot.entries.get(entry.parent or "")
    if parent is None:
        return {}
    return {
        name: registry._plain(value)
        for name, value in parent.attributes.items()
        if name in spec.attributes and name not in entry.attributes
    }


def _candidate(
    spec: RegistrySpec, snapshot: Snapshot, delta: object
) -> tuple[dict[str, Any], dict[str, Entry], Any, list[dict[str, Any]]]:
    """The overlay document after a delta, its typed registry and blocking findings."""
    document, touched = registry.apply_delta(spec, snapshot, delta)
    candidate = spec.adapter.parse_document(document)
    registry.validate_replacements(spec, spec.adapter.entries(candidate))
    blocking = registry.new_findings(snapshot, tuple(spec.adapter.findings(candidate)))
    continuity = getattr(spec.adapter, "continuity", None)
    if continuity is not None:
        blocking.extend(dict(item) for item in continuity(snapshot.typed, candidate))
    vault_entries = sum(
        1 for entry in spec.adapter.entries(candidate).values() if entry.origin == "vault"
    )
    if vault_entries > spec.cap:
        blocking.append(
            {
                "code": "registry_cap",
                "path": spec.name,
                "severity": "error",
                "detail": f"{spec.name} holds at most {spec.cap} vault entries",
            }
        )
    return document, touched, candidate, blocking


def propose(vault_root: Path, spec: RegistrySpec, delta: object) -> dict[str, Any]:
    """Read-only: what a save of `delta` would register, and what it resembles."""
    refusal = admission_refusal(vault_root, spec)
    if refusal is not None:
        return refusal
    snapshot = registry.load(spec, vault_root)
    out: dict[str, Any] = {"subject": spec.name, "expected_hash": snapshot.content_hash}
    try:
        _document, touched, candidate, blocking = _candidate(spec, snapshot, delta)
    except RegistryError as exc:
        code, _, detail = str(exc).partition(": ")
        return {
            **out,
            "valid": False,
            "entries": [],
            "collisions": [],
            "near_duplicates": [],
            "findings": [{"code": code, "severity": "error", "detail": detail or str(exc)}],
            "save": save_contract(spec),
        }
    usage = _usage(vault_root, spec, snapshot)
    candidate_entries = spec.adapter.entries(candidate)
    rows: list[dict[str, Any]] = []
    collisions: list[dict[str, str]] = []
    near: list[dict[str, Any]] = []
    for key, proposed in touched.items():
        # The validated entry when it survived validation, else the proposal as sent.
        entry = candidate_entries.get(key, proposed)
        found = _collisions(snapshot, entry)
        collisions.extend(found)
        near.extend(_near_duplicates(snapshot, entry, usage, {item["key"] for item in found}))
        rows.append(
            {
                "key": key,
                "exists": key in snapshot.entries,
                "valid": key in candidate_entries,
                "entry": entry.as_dict(),
                "inherited": _inherited(spec, snapshot, entry),
            }
        )
    return {
        **out,
        "valid": not blocking,
        "entries": rows,
        "collisions": collisions,
        "near_duplicates": near,
        "counts": "available" if usage.available else "unavailable",
        "findings": blocking,
        "save": save_contract(spec),
    }


def _restore_route(spec: RegistrySpec, version: str) -> str:
    return (
        f'schema_memory(subject="{spec.name}", operation="restore", version="{version}", '
        "expected_hash, why)"
    )


def receipt_lines(
    spec: RegistrySpec,
    before: Snapshot,
    after_entries: Mapping[str, Entry],
    touched: tuple[str, ...],
    version: str,
) -> list[str]:
    """One line per key a write registered or changed, with its revert route."""
    lines: list[str] = []
    for key in touched:
        entry = after_entries.get(key)
        if entry is None:
            continue
        previous = before.entries.get(key)
        if previous is None:
            verb = "registered"
        elif entry.status == "deprecated" and previous.status != "deprecated":
            verb = (
                f"deprecated in favour of {entry.replaced_by}"
                if entry.replaced_by
                else "deprecated"
            )
        else:
            verb = "changed"
        parent = f"parent {entry.parent}" if entry.parent else "no parent"
        lines.append(
            f"{spec.name}: {verb} {key} ({parent}); in effect now for every agent. "
            f"Revert: {_restore_route(spec, version)}"
        )
    return lines


def first_use_lines(added: Mapping[str, Sequence[str]], version: str) -> list[str]:
    """One line per key a write registered on first use, with its revert route.

    An open registry (source kinds, domains) registers a key the first time a
    write uses it. The line is the write's own receipt: it appears once, in
    that write's response, and asks for nothing.
    """
    from . import registry_spec

    return [
        f"{name}: registered {key} on first use; in effect now for every agent. "
        f"Revert: {_restore_route(registry_spec(name), version)}"
        for name, keys in added.items()
        for key in keys
    ]


def queue_for_owner(
    vault_root: Path,
    spec: RegistrySpec,
    snapshot: Snapshot,
    proposal: object,
    why: str,
    reason: str,
    *,
    operation: str = "save",
) -> dict[str, Any]:
    """Queue a restricted principal's save for the owner; change nothing.

    The pending item carries the operation, the reason and the proposal as the
    caller sent them, so the owner reviews exactly what was asked.
    """
    if spec.family is None:
        return {"subject": spec.name, "available": False, "reason": reason, "saved": None}
    from .. import vocabulary_review
    from ..vocabulary_state import VocabularyState
    from ..vocabulary_workflow import Evidence, make_item

    question = json.dumps(
        {"registry": spec.name, "operation": operation, "why": why, "proposal": proposal},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(question) > PENDING_MAX_CHARS:
        raise RegistryError(
            f"PENDING_PROPOSAL_TOO_LARGE: a proposal for the owner holds at most "
            f"{PENDING_MAX_CHARS} characters; save one entry at a time"
        )
    root = Path(vault_root)
    target = spec.overlay(root).relative_to(root).as_posix()
    item = make_item(
        family=spec.family,
        signal=vocabulary_review.REGISTRY_PROPOSAL_SIGNAL,
        targets={target: snapshot.content_hash},
        evidence=[
            Evidence(target, snapshot.content_hash, vocabulary_review.REGISTRY_PROPOSAL_SIGNAL)
        ],
        # Validation reads only this target overlay; its server hash binds currency.
        registry_hashes={},
        projection_status="current",
        paths={target: target},
        question=question,
        logical_identity=f"registry-proposal:{spec.name}:{registry.content_hash(question)[:32]}",
    )
    VocabularyState(root).observe(item)
    return {
        "subject": spec.name,
        "state": "pending_review",
        "item_ref": item.ref,
        "reason": reason,
        "saved": None,
        "vocabulary_receipt": [
            f"{spec.name}: proposal queued for the owner as {item.ref}; the registry is "
            "unchanged. Write with the raw label meanwhile."
        ],
    }


def save(
    vault_root: Path,
    spec: RegistrySpec,
    *,
    delta: object,
    expected_hash: str | None,
    why: str | None,
) -> dict[str, Any]:
    """Apply one hash-guarded delta with its reason, keeping history."""
    if not isinstance(why, str) or not why.strip():
        raise RegistryError("WHY_REQUIRED: save requires why")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise RegistryError(
            "EXPECTED_HASH_REQUIRED: save requires expected_hash from inspect or propose"
        )
    root = Path(vault_root)
    reason = queues_for_owner(root)
    refusal = admission_refusal(root, spec)
    if refusal is not None:
        return refusal
    snapshot = registry.load(spec, root)
    if expected_hash != snapshot.content_hash:
        raise RegistryError(
            f"STALE_REGISTRY: {spec.name} changed since expected_hash was read; inspect again"
        )
    document, touched, candidate, blocking = _candidate(spec, snapshot, delta)
    if blocking:
        return {"subject": spec.name, "valid": False, "findings": blocking, "saved": None}
    if reason is not None:
        # Validated like the owner's save, so the owner reviews only deltas that apply.
        return queue_for_owner(root, spec, snapshot, delta, why.strip(), reason)
    rendered = spec.adapter.render(document)
    if len(rendered.encode("utf-8")) > registry.MAX_OVERLAY_BYTES:
        raise RegistryError(
            f"REGISTRY_TOO_LARGE: {spec.name} overlay would exceed "
            f"{registry.MAX_OVERLAY_BYTES} bytes"
        )
    after_entries = spec.adapter.entries(candidate)
    added = registry.added_keys(snapshot.entries, after_entries)
    history = registry.commit(
        spec,
        root,
        rendered,
        operation="save",
        why=why.strip(),
        before_hash=snapshot.content_hash,
        previous=snapshot.overlay_text,
        added=added,
    )
    _mark_committed()
    after = registry.load(spec, root)
    return {
        "subject": spec.name,
        "valid": True,
        "findings": [],
        "why": why.strip(),
        "changed_keys": list(touched),
        "vocabulary_receipt": receipt_lines(
            spec, snapshot, after.entries, tuple(touched), history["version"]
        ),
        "saved": {
            "path": spec.overlay(root).relative_to(root).as_posix(),
            "content_hash": after.content_hash,
            "previous_hash": snapshot.content_hash,
            "effective_digest": after.effective_digest,
            "history": history,
        },
    }


def history(vault_root: Path, spec: RegistrySpec) -> dict[str, Any]:
    """The kept versions, newest first; reasons and principals for the owner only."""
    from .. import registry_history

    root = Path(vault_root)
    refusal = admission_refusal(root, spec) or history_refusal(root, spec)
    if refusal is not None:
        return refusal
    snapshot = registry.load(spec, root)
    return {
        "subject": spec.name,
        "content_hash": snapshot.content_hash,
        **registry_history.history_view(root, stem=spec.stem),
    }


def restore(
    vault_root: Path,
    spec: RegistrySpec,
    *,
    version: str | None,
    expected_hash: str | None,
    why: str | None,
) -> dict[str, Any]:
    """Write a kept version's exact bytes back as a governed save of its own.

    Reverting is the point, so the in-place meaning rules do not apply; pages
    are never touched, and a page that used a removed key becomes debt.
    """
    from .. import registry_history

    if not version:
        raise RegistryError("INVALID_REGISTRY_ARGUMENT: restore requires version from history")
    if not isinstance(why, str) or not why.strip():
        raise RegistryError("WHY_REQUIRED: restore requires why")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise RegistryError("EXPECTED_HASH_REQUIRED: restore requires expected_hash")
    root = Path(vault_root)
    from ..governance.principal import effective_principal
    from ..governance.raw_protection import is_owner

    if not is_owner(effective_principal()):
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    refusal = admission_refusal(root, spec) or history_refusal(root, spec)
    if refusal is not None:
        return refusal
    snapshot = registry.load(spec, root)
    if expected_hash != snapshot.content_hash:
        raise RegistryError(
            f"STALE_REGISTRY: {spec.name} changed since expected_hash was read; inspect again"
        )
    text = registry_history.read_version(root, stem=spec.stem, version=version)
    restored = spec.adapter.parse(text, registry.content_hash(text))
    # Restore preserves legacy reader meaning, but never republishes invalid bytes.
    blocking = registry.new_findings(None, tuple(spec.adapter.findings(restored)))
    if blocking:
        return {"subject": spec.name, "valid": False, "findings": blocking, "saved": None}
    after_entries = spec.adapter.entries(restored)
    removed = sorted(set(snapshot.entries) - set(after_entries))
    # Taxonomy subjects share one overlay; an exact restore can remove keys on both axes.
    from . import registry_specs

    removed_by_registry = {}
    for related in registry_specs().values():
        if related.overlay(root) != spec.overlay(root):
            continue
        before = related.adapter.entries(
            related.adapter.parse(
                snapshot.overlay_text,
                snapshot.content_hash,
            )
        )
        after_related = related.adapter.entries(
            related.adapter.parse(text, registry.content_hash(text))
        )
        missing = sorted(set(before) - set(after_related))
        if missing:
            removed_by_registry[related.name] = missing
    added = registry.added_keys(snapshot.entries, after_entries)
    history = registry.commit(
        spec,
        root,
        text,
        operation="restore",
        why=why.strip(),
        before_hash=snapshot.content_hash,
        previous=snapshot.overlay_text,
        added=added,
    )
    _mark_committed()
    after = registry.load(spec, root)
    return {
        "subject": spec.name,
        "valid": True,
        "why": why.strip(),
        "restored": version,
        "removed_keys": removed,
        "removed_by_registry": removed_by_registry,
        "findings": [dict(item) for item in after.findings],
        "vocabulary_receipt": [
            f"{spec.name}: restored version {version}"
            + (f"; removed {', '.join(removed)}" if removed else "")
            + ". Pages keep their bytes; a page using a removed key is now unregistered debt. "
            f"Undo: {_restore_route(spec, history['version'])}"
        ],
        "saved": {
            "path": spec.overlay(root).relative_to(root).as_posix(),
            "content_hash": after.content_hash,
            "previous_hash": snapshot.content_hash,
            "effective_digest": after.effective_digest,
            "history": history,
        },
    }


def _mark_committed() -> None:
    from ..writer_lease import mark_active_mutation_committed

    mark_active_mutation_committed()
