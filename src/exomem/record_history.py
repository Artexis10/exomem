"""Recoverable prior content for corrected Records and Planning file rows.

A guarded row update used to overwrite the item and keep only hashes and the
`why` in the audit chain, so an earlier interpretation could not be recovered
(close-memory-loop 3.15). Every correction that changes a row's payload now
keeps the payload it replaced, in the SAME atomic batch as the correction:

* the prior field values and body, with their payload hash;
* the correction's operation, `why`, audit transition and item hashes;
* the input binding the caller supplied (an episode correction names its
  curation run), or none for a direct update.

An entry is one create-only JSON file per correction, named like
`registry_history` versions and kept under
`<Knowledge Base>/_Collections/history/<collection_id>/<item_key>/`, inside the
collection store's own reserved tree. No read surface serves a reserved file,
so the only way to a kept payload is `read`, which first releases the row
itself exactly as a query would. History is withheld exactly when the row is,
and a withheld row answers like a missing one. Export keeps the history subtree
as canonical content, beside the store's mode marker and replica, so supported
export and restore carry it. (Not `_Governance`: that tree is the governance
authoring workspace and administration state no Records writer may reach.)

This module is the logical interface. Store-mode collections keep row content
in `item_versions`; that backing replaces the file entries without changing
`read`'s answer, and is a later store slice.

A refresh or retry that leaves the payload unchanged keeps nothing: there is
no prior meaning to recover. Corrections made before this module existed
have no entry; `read` reports them as `unavailable_legacy` by matching the
row's verified audit transitions against the kept entries, never as an empty
history presented as complete.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import registry_history
from . import structured_collections as collections
from . import vault as vault_module
from .governance import egress
from .kbdir import kb_dirname

ENTRY_VERSION = 1
PAGE_DEFAULT = 20
PAGE_MAX = 50
_SUFFIX = ".json"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: Audit operations that create a row rather than correct one.
_CREATIONS = frozenset({"append", "create", "plan_add", "plan_create"})
_FIELDS = (
    "collection_id",
    "item_key",
    "status",
    "unavailable",
    "retained",
    "returned",
    "truncated",
    "continuation",
    "revisions",
)
egress.register_projector("record_history", _FIELDS)


def history_dir(collection_id: str, item_key: str) -> str:
    """The vault-relative directory one row's kept corrections live in."""
    for segment in (collection_id, item_key):
        if not isinstance(segment, str) or not _SAFE_SEGMENT.fullmatch(segment):
            raise collections.CollectionError(
                "RECORD_HISTORY_UNAVAILABLE", "row identity cannot name a history location"
            )
    return f"{history_root()}/{collection_id}/{item_key}"


def history_root() -> str:
    """The vault-relative tree every row's kept corrections live under."""
    return f"{kb_dirname()}/_Collections/history"


def plan_entry(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    *,
    item_key: str,
    canonical_path: str,
    prior_values: Mapping[str, Any],
    prior_body: str | None,
    after_values: Mapping[str, Any],
    after_body: str | None,
    operation: str,
    why: str,
    transition_id: str,
    before_item_hash: str,
    after_item_hash: str,
    binding: Mapping[str, Any] | None = None,
) -> vault_module.PlannedWrite | None:
    """The create-only write that keeps one correction's prior payload.

    None when the correction leaves the payload unchanged (a presentation
    refresh, an idempotent retry): there is no earlier meaning to keep.
    """
    from . import records
    from .governance.principal import effective_principal

    before_payload = records._payload_hash(manifest, item_key, prior_values, prior_body or "")
    if before_payload == records._payload_hash(
        manifest, item_key, after_values, after_body or ""
    ):
        return None
    moment = dt.datetime.now(dt.UTC)
    who = effective_principal()
    entry = {
        "version": ENTRY_VERSION,
        "collection_id": manifest.collection_id,
        "item_key": item_key,
        "path": canonical_path,
        "at": moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "operation": operation,
        "why": why,
        "transition_id": transition_id,
        "before_item_hash": before_item_hash,
        "after_item_hash": after_item_hash,
        "prior": {
            "values": records._normalize_json(dict(prior_values)),
            "body": prior_body or "",
            "payload_hash": before_payload,
        },
        "binding": None if binding is None else records._normalize_json(dict(binding)),
        "actor": {"audience": who.audience_id, "surface": who.surface},
    }
    name = registry_history.version_id(moment, transition_id) + _SUFFIX
    target = Path(vault_root) / history_dir(manifest.collection_id, item_key) / name
    return vault_module.PlannedWrite(
        path=target,
        content=json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        create_only=True,
    )


def _page_bounds(limit: Any, continuation: Any) -> tuple[int, str | None]:
    if limit is None:
        limit = PAGE_DEFAULT
    if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
        raise collections.CollectionError(
            "INVALID_HISTORY_LIMIT", f"limit must be an integer from 1 to {PAGE_MAX}"
        )
    if continuation is not None and (
        not isinstance(continuation, str) or not registry_history.VERSION_RE.match(continuation)
    ):
        raise collections.CollectionError(
            "INVALID_HISTORY_CONTINUATION", "continuation is not a history position"
        )
    return limit, continuation


def _revision(text: str | None, version: str) -> dict[str, Any]:
    try:
        entry = json.loads(text) if text is not None else None
    except json.JSONDecodeError:
        entry = None
    if not isinstance(entry, dict) or entry.get("version") != ENTRY_VERSION:
        # Kept but unreadable: say so rather than drop it from the count.
        return {"version": version, "status": "unreadable"}
    return {
        "version": version,
        "status": "available",
        **{
            name: entry.get(name)
            for name in (
                "at",
                "operation",
                "why",
                "transition_id",
                "before_item_hash",
                "after_item_hash",
                "prior",
                "binding",
                "actor",
            )
        },
    }


def read(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    item_key: str,
    semantic_profile: str,
    limit: int | None = None,
    continuation: str | None = None,
) -> dict[str, Any]:
    """One bounded page of a released row's kept corrections, newest first.

    The row is released first, through the same collection and item
    authorization a query uses; an unreleased or absent row is
    `RECORD_NOT_FOUND` either way. Only the page's entries are opened.
    `status` is `complete` when every correction in the row's verified audit
    chain has a kept entry, `unavailable_legacy` when some predate retention
    (`unavailable` counts them), and `unverified` when the chain itself
    cannot be verified.
    """
    from . import record_formats, record_governance, records

    root = Path(vault_root)
    page_size, after = _page_bounds(limit, continuation)
    with egress.disclosure_boundary(root, "record_history", join_existing=True) as collector:
        policy = egress.policy_module.load(root)
        manifest = record_governance._resolve_released_collection(
            root, collection, receipt=True, policy=policy
        )
        if manifest.semantic_profile != semantic_profile:
            raise collections.CollectionError(
                "RECORDS_PROFILE_REQUIRED"
                if semantic_profile == "records"
                else "PLANNING_PROFILE_REQUIRED",
                "collection profile is not available",
            )

        def authorize(path: str) -> bool:
            return record_governance._authorize(root, path, receipt=True, policy=policy)

        if not authorize(manifest.storage.source):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        snapshot = record_formats.load_adapter(root, manifest, authorize_path=authorize).read()
        matches = [record for record in snapshot.records if record.identity.key == item_key]
        if len(matches) != 1 or matches[0].ambiguous or not authorize(matches[0].source.path):
            raise collections.CollectionError("RECORD_NOT_FOUND", "record key does not exist")
        relative = history_dir(manifest.collection_id, item_key)
        names = registry_history.kept_names(root, relative, suffix=_SUFFIX) or []
        versions = [name[: -len(_SUFFIX)] for name in names]
        older = [version for version in versions if after is None or version < after]
        page = older[:page_size]
        revisions = [
            _revision(registry_history.read_kept(root, relative, version + _SUFFIX), version)
            for version in page
        ]
        chain = records._inspect_audit_chain(root, manifest, authorize_path=authorize)
        egress.emit_boundary_receipt(collector)
    kept_tags = {version.rsplit("-", 1)[-1] for version in versions}
    corrections = [
        event["transition_id"]
        for event in chain.events
        if event.get("item_key") == item_key and event.get("operation") not in _CREATIONS
    ]
    missing = sum(1 for transition in corrections if transition[:8] not in kept_tags)
    if chain.status != "ok" or not chain.complete:
        status = "unverified"
    elif missing:
        status = "unavailable_legacy"
    else:
        status = "complete"
    truncated = len(older) > len(page)
    result = {
        "collection_id": manifest.collection_id,
        "item_key": item_key,
        "status": status,
        **({"unavailable": missing} if missing else {}),
        "retained": len(versions),
        "returned": len(revisions),
        "truncated": truncated,
        "continuation": page[-1] if truncated else None,
        "revisions": revisions,
    }
    from .record_governance import _RecordEnvelope

    return egress.project(_RecordEnvelope(result), egress.LEVEL_FULL, kind="record_history") or {}
