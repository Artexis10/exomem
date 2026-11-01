"""A curation or episode Records step on a collection the store owns (P1b.4b).

The step runs on the store thread, through the writer's own append and update, under the
store's guards: the collection's container hash and the item's version. The write carries
the step's request id (the run and the operation identity), so the store's transaction row
for that id is the witness that the step committed. Curation derives its evidence file from
that row.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from .. import structured_collections as collections
from .. import writer_lease
from . import authority, ownership, preview


def _stale(reason):
    return collections.CollectionError("CURATION_BINDING_STALE", reason)


class _Prepared(Exception):
    """Carries the writer's last check out of it before it commits."""

    def __init__(self, prepared):
        super().__init__("prepared")
        self.prepared = prepared


def prepare(vault_root, action, args):
    """What the writer would commit for this step now, without committing it.

    The writer itself runs resolution, release, value validation, key derivation and both
    guards, and stops at its last check before the transaction, so a step is prepared by the
    very path that later executes it. A refused value is raised, never held.
    """
    from .. import curation

    try:
        preview.dispatch(vault_root, "records", action, {**args, "hold": False, "_prepare": _stop})
    except _Prepared as stopped:
        prepared = stopped.prepared
    else:
        raise collections.CollectionError(
            "RECORD_PREPARE_INCOMPLETE", "the write did not stop before its transaction"
        )
    manifest = prepared["manifest"]
    curation.require_item_files(manifest)
    return {
        "collection_id": manifest.collection_id,
        "manifest_path": manifest.path,
        "semantic_profile": manifest.semantic_profile,
        "item_key": prepared["item_key"],
        "path": prepared["path"],
        "payload_hash": prepared.get("payload_hash"),
        "body": prepared.get("body"),
        "expected_container_hash": prepared["container_hash"],
        "expected_item_version": prepared.get("item_version"),
    }


def _stop(prepared):
    raise _Prepared(prepared)


def resolve_guards(vault_root, writer, binding, args):
    """The container hash and item version the store write must carry, or a stale-seal refusal.

    A step sealed for the store binds `{collection_id, expected_container_hash,
    expected_item_version}` and must still equal the store's own. A step sealed before the
    migration bound the collection's file views; it maps to the store only when those views
    are the store's published state, so it never applies blind.
    """
    prepared = binding["prepared"]
    collection_id, item_key = prepared["collection_id"], prepared.get("item_key")
    update = "expected_item_version" in args
    try:
        # A seal is usable only under the write policy, including before a direct apply creates approval.
        with writer.read_snapshot(mutation=True):
            container, version = writer.current_guards(collection_id, item_key if update else None)
    except collections.CollectionError as error:
        # nosemgrep: ep-word-membership -- the writer's two absence codes, fixed by the writer.
        if error.code in {"COLLECTION_NOT_FOUND", "RECORD_NOT_FOUND"}:
            raise _stale("the sealed collection or item is gone") from error
        raise
    sealed = prepared.get("store")
    if sealed is not None:
        if sealed["expected_container_hash"] != container:
            raise _stale("the collection changed after the seal")
        if update and sealed["expected_item_version"] != version:
            raise _stale("the item changed after the seal")
        return container, version
    _prove_views(vault_root, writer, binding, args)
    return container, version


def _prove_views(vault_root, writer, binding, args):
    """Whether the views a pre-migration seal bound are, byte for byte, what the store has published.

    The seal's container hash and item hash were taken from the collection's files. They map to
    the store only if the collection's current views hash to the sealed container, the store
    holds no view of it unpublished, and the files and the store's published views are the same
    set, each file with the bytes the store published. A view the store published and a person
    then deleted is a row the sealed container no longer shows, so it refuses.
    """
    from .. import curation, record_formats, records

    prepared = binding["prepared"]
    root = Path(vault_root)
    manifest_path, collection_id = prepared["manifest_path"], prepared["collection_id"]
    try:
        text, manifest_hash = curation._read_target(root, manifest_path)  # noqa: SLF001
        manifest = collections.parse_manifest_bytes(root, manifest_path, text.encode("utf-8"))
        # The file adapter directly: `load_adapter` would answer from the store this proof checks.
        snapshot = record_formats.MarkdownItemsAdapter(root, manifest, None, None).read()
    except (curation.CurationError, collections.CollectionError, OSError, ValueError) as error:
        raise _stale("the sealed collection's views cannot be read") from error
    if records._container_hash(manifest, snapshot) != args["expected_container_hash"]:  # noqa: SLF001
        raise _stale("the collection's views are not the state the seal bound")
    with writer.read_snapshot():
        states = writer.published_views(collection_id)
    if any(state != "current" for _path, _kind, _digest, state in states):
        raise _stale("the store has views of this collection that it has not published")
    # The container hash covers the manifest and the item files; the other views do not enter it.
    # nosemgrep: ep-word-membership -- the two projection kinds the store schema fixes for them.
    published = {path: digest for path, kind, digest, _state in states if kind in {"manifest", "item"}}
    views = {manifest_path: manifest_hash, **{version.path: version.hash for version in snapshot.source_versions}}
    if published != views:
        raise _stale("the collection's views are not the ones the store published")
    if "expected_item_version" in args and args["expected_item_version"] != prepared.get("before_item_hash"):
        raise _stale("the sealed item version is not the item the seal bound")


def commit(vault_root, writer, action, args, binding, request_id):
    """Run the writer's own append or update with the step's request id."""
    container, version = resolve_guards(vault_root, writer, binding, args)
    values = {
        **args,
        "item_key": binding["prepared"]["item_key"],
        "expected_container_hash": container,
        "hold": False,
        "request_id": request_id,
    }
    if action == "update":
        values["expected_item_version"] = version
    _selected, result = preview.dispatch(vault_root, "records", action, values)
    # The dispatcher classes a failure from here on as one after the commit, as for a file leaf.
    writer_lease.mark_active_mutation_committed()
    return result


def committed(writer, request_id):
    """The store's transaction row for a request id: its receipt and commit time, or None.

    That row is the step's witness: the store commits it with the write, so it exists exactly
    when the step committed. A row in a collection this caller may not see leaves the outcome
    uncertain to that caller, never "not committed".
    """
    from .. import curation

    try:
        with writer.read_snapshot():
            found = writer.request_receipt(request_id)
    except collections.CollectionError as error:
        raise curation.CurationError(
            "CURATION_OUTCOME_UNCERTAIN", "the store's transaction cannot be read by this caller"
        ) from error
    return None if found is None else {"receipt": found["receipt"], "committed_at": found["committed_at"]}


def verify(writer, request_id, *, collection_id, item_key, path, after_item_hash):
    """Whether the transaction row for ``request_id`` shows this step's committed result, still current.

    A row, collection or item this caller cannot find is no corroboration: False, not an error.
    """
    try:
        with writer.read_snapshot():
            found = writer.request_receipt(request_id)
            receipt = None if found is None else found["receipt"]
            if (
                receipt is None
                or receipt.get("outcome") != "committed"
                or receipt.get("collection_id") != collection_id
                or receipt.get("item_key") != item_key
                or path not in receipt.get("affected_paths", ())
                or receipt.get("after_item_hash") != after_item_hash
            ):
                return False
            # An item changed since is no longer the step's result, as an edited file's receipt is not.
            return writer.current_guards(collection_id, item_key)[1] == after_item_hash
    except collections.CollectionError:
        return False


def verify_imported(writer, prepared, postimage, *, kind):
    """Corroborate an existing file witness from immutable import evidence and current state.

    This never recovers a missing witness or decides whether to repeat a write. Older
    checkpoints without the original byte binding cannot supply this proof.
    """
    from .. import records
    from . import legacy_import, tokens

    try:
        cid, key = prepared["collection_id"], prepared["item_key"]
        path, digest = postimage["path"], postimage["content_hash"]
        if "store" in prepared or path != prepared["destination"]:
            return False
        with writer.read_snapshot(mutation=True):
            collection, manifest, _ = writer._collection(cid)
            if manifest.storage.strategy != "markdown-items" or manifest.path != prepared["manifest_path"]:
                return False
            item = writer._item(cid, key)
            if item is None or item["view_path"] != path:
                return False
            initial = writer.connection.execute(
                "SELECT payload_hash,txn_id FROM item_versions WHERE row_id=? AND row_version=1",
                (item["row_id"],),
            ).fetchone()
            if (initial is None or item["created_txn"] != initial[1]
                    or collection["created_txn"] != initial[1]
                    or writer._version(item) != tokens.item_version(cid, key, 1, initial[0])):
                return False
            checkpoint = writer.connection.execute(
                "SELECT receipt_json,event_hash FROM txns WHERE txn_id=? AND collection_id=? "
                "AND operation='legacy_import' AND legacy_event_json IS NULL",
                (initial[1], cid),
            ).fetchone()
            if checkpoint is None:
                return False
            envelope = json.loads(checkpoint[0])
            if not isinstance(envelope, Mapping):
                return False
            originals = envelope.get("original_items")
            binding = originals.get(key) if isinstance(originals, Mapping) else None
            if (not isinstance(binding, Mapping) or binding.get("source_sha256") != digest
                    or not isinstance(binding.get("transition_id"), str)
                    or envelope.get("collection_id") != cid
                    or legacy_import._event_hash(envelope) != checkpoint[1]):
                return False
            historical = writer.connection.execute(
                "SELECT legacy_event_json,receipt_json,event_hash FROM txns "
                "WHERE transition_id=? AND collection_id=? AND operation='legacy_import' "
                "AND legacy_event_json IS NOT NULL",
                (binding["transition_id"], cid),
            ).fetchone()
            if historical is None:
                return False
            event, imported = json.loads(historical[0]), json.loads(historical[1])
            if not isinstance(event, Mapping) or not isinstance(imported, Mapping):
                return False
            if (imported.get("legacy_event_sha256") != hashlib.sha256(historical[0].encode()).hexdigest()
                    or legacy_import._event_hash(imported) != historical[2]
                    or not records._event_matches_transition(event, manifest)
                    or event["transition_id"] != binding["transition_id"]
                    or event["item_key"] != key or event["canonical_path"] != path
                    or event["after_item_hash"] != digest):
                return False
            # Curation fixes these two leaf kinds and their sealed preimage field names.
            if kind == "update-record":
                return event["operation"] == "update" and all(
                    event[name] == prepared[name]
                    for name in ("before_manifest_hash", "before_container_hash", "before_item_hash")
                )
            if kind == "append-record":
                return (event["operation"] == "append"
                        and event["payload_hash"] == prepared["payload_hash"] == initial[0])
            return False
    except (collections.CollectionError, KeyError, TypeError, ValueError, sqlite3.Error):
        return False


def needs_store(root, command, arguments):
    """Whether a curation or episode command has a Records step in a canonically owned collection.

    Structured-files maintenance on such a collection counts too: it moves the collection's views.

    A step on a store collection runs only on the store thread, so the whole command runs there;
    every other curation keeps its current path. The collections come from the sealed plans the
    command names, or from the leaves an episode prepare proposes. Anything unreadable is not
    routed: the command's own checks then refuse it.
    """
    from .. import curation, episode_workflow
    from ..cli_ops import OpError

    selectors = set()
    runs = []
    if command == "maintain_memory":
        if arguments.get("mode") == "curation" and isinstance(arguments.get("run_id"), str):
            runs.append(arguments["run_id"])
        elif arguments.get("mode") == "structured-files" and isinstance(arguments.get("collection"), str):
            # Filename maintenance on a store collection moves its views through the store writer.
            selectors.add(arguments["collection"])
    elif arguments.get("action") == "prepare":
        proposal = arguments.get("proposal")
        leaves = proposal.get("leaves") if isinstance(proposal, Mapping) else None
        for leaf in leaves if isinstance(leaves, list) else ():
            args = leaf.get("args") if isinstance(leaf, Mapping) else None
            if (
                isinstance(args, Mapping)
                and leaf.get("kind") in curation.RECORDS_STEP_KINDS
                and isinstance(args.get("collection"), str)
            ):
                selectors.add(args["collection"])
    # nosemgrep: ep-word-membership -- the episode command's own action enum fixes these two.
    elif arguments.get("action") in {"resume", "coverage"}:
        try:
            runs.extend(episode_workflow.bound_runs(root, arguments.get("episode")))
        except (OSError, ValueError, OpError):
            return False
    for run in runs:
        selectors.update(curation.records_collections(root, run))
    if not selectors:
        return False
    try:
        marker = ownership.current(root).marker
        return marker is not None and any(
            authority.selected_entry(root, marker, selector) is not None for selector in selectors)
    except (collections.CollectionError, ValueError):
        return False
