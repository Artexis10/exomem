"""Owner operations on a vault's collection store: backup, adopt-local preview, reconcile plan.

Every vault-side store file is read only through a private validated copy; the
shared replica and its preserved foreign evidence are never opened by SQLite in
place. Applying adopt-local or reconcile needs writer authority, so those live in
``admission``; everything here only reads.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from . import chain, connection, custody, replica, schema, snapshot, typed_storage
from .connection import CollectionStoreError

#: The recorded head was not read: no coordinator is configured for this process.
UNKNOWN = "unknown"
_FOREIGN_PREFIX = ".foreign-"


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _sealed(preview: dict) -> dict:
    """A preview and the identity an apply must name, so it acts on what the owner saw."""
    return {"preview": preview, "preview_id": hashlib.sha256(_json(preview).encode()).hexdigest()}


def _live(root: Path) -> Path:
    path = connection.store_path(root)
    if not path.exists():
        raise CollectionStoreError("COLLECTION_STORE_UNAVAILABLE", "this host has no live collection store")
    return path


def backup(vault_root, *, destination=None, stream=None, timeout=300.0) -> dict:
    """`exomem collections backup`: a validated single-file snapshot, never a copy of the live files.

    The snapshot passes integrity, foreign-key, schema, identity and lineage checks
    before it replaces ``destination`` atomically or streams to ``stream``. The
    destination is checked, written and staged at its real path, so a symlink cannot
    land it inside the vault. A synced destination is the owner's choice and only warns.
    """
    root = Path(vault_root).resolve()
    _live(root)
    if (destination is None) == (stream is None):
        raise ValueError("backup needs exactly one of a destination or a stream")
    target, warnings = None, []
    scratch = connection.store_path(root).parent
    if destination is not None:
        target = Path(destination).expanduser().resolve()
        verdict = custody.verify_backup_destination(root, target)
        if not verdict.verified or target.is_dir():
            raise CollectionStoreError("COLLECTION_BACKUP_DESTINATION_UNSAFE",
                                       verdict.reason if not verdict.verified else "the destination is a directory")
        warnings = [found] if (found := custody.backup_sync_warning(root, target)) else []
        scratch = target.parent
    with tempfile.TemporaryDirectory(prefix=".exomem-collection-backup-", dir=scratch) as private, \
            snapshot.staged_snapshot(root, directory=Path(private), deadline=time.monotonic() + timeout) as artifact:
        if target is None:
            with open(artifact.path, "rb") as source:
                shutil.copyfileobj(source, stream)
            stream.flush()
        else:
            os.replace(artifact.path, target)
            if os.name != "nt":  # Windows has no directory descriptor to flush
                directory = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        return {"path": None if target is None else str(target), "sha256": artifact.file_sha256,
                "size_bytes": artifact.size_bytes, "store_id": artifact.store_id,
                "commit_seq": artifact.commit_seq, "head_hash": artifact.head_hash, "integrity_check": "ok",
                "warnings": warnings}


def _head(meta) -> dict:
    return {"instance_id": meta[schema.META_INSTANCE_ID], "commit_seq": int(meta[schema.META_COMMIT_SEQ]),
            "head_hash": meta.get(schema.META_STORE_HEAD_HASH)}


@contextmanager
def _evidence(path: Path, scratch: Path):
    """Yield (digest, validated copy or None, its store_meta or None) for a vault-side store file."""
    with tempfile.TemporaryDirectory(prefix=".exomem-collection-evidence-", dir=scratch) as private:
        copy, digest = Path(private) / connection.STORE_FILENAME, hashlib.sha256()
        with open(path, "rb") as shared, open(copy, "xb") as target:
            while chunk := shared.read(1 << 20):
                digest.update(chunk)
                target.write(chunk)
        with closing(sqlite3.connect(copy, isolation_level=None)) as conn:
            try:
                meta = snapshot._validate(conn, lambda: None)
                chain.verify_store_chain(conn)
            except (CollectionStoreError, chain.StoreChainError, sqlite3.DatabaseError):
                meta = None
            yield digest.hexdigest(), conn if meta is not None else None, meta


def _common_ancestor(local, foreign) -> int:
    """The last commit both chains share; chained heads make every earlier one shared too."""
    for sequence, head in foreign.execute("SELECT commit_seq,store_head_hash FROM txns ORDER BY commit_seq DESC"):
        if local.execute("SELECT 1 FROM txns WHERE commit_seq=? AND store_head_hash=?", (sequence, head)).fetchone():
            return sequence
    return 0


def _foreign_file(local, path, source, scratch) -> dict:
    with _evidence(path, scratch) as (digest, conn, meta):
        entry = {"source": source, "leaf": path.name, "sha256": digest, "readable": meta is not None}
        if meta is not None:
            entry.update(_head(meta), store_id=meta[schema.META_STORE_ID],
                         common_ancestor_commit_seq=_common_ancestor(local, conn))
        return entry


def _file_digest(path: Path) -> str:
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def adopt_local_preview(vault_root, *, recorded=UNKNOWN) -> dict:
    """What adopt-local continues from and which foreign heads or bytes it stops waiting on (A3).

    ``recorded`` is the coordinator's head, None when none was ever recorded, or
    ``UNKNOWN`` when this process has no coordinator to ask.
    """
    root = Path(vault_root).resolve()
    path = _live(root)
    directory = replica.replica_path(root).parent
    with closing(connection.open_reader(path)) as local:
        local.execute("BEGIN")
        meta = dict(local.execute("SELECT key,value FROM store_meta"))
        head = _head(meta)
        lineage = {entry["instance_id"] for entry in json.loads(meta[schema.META_LINEAGE])}
        raw = meta.get(schema.META_REPLICA_DIVERGENCE)
        divergence = None if raw is None else json.loads(raw)
        # A view stamped by another store instance fences business writes the same way.
        view_diverged = meta.get(schema.META_VIEW_DIVERGED) == "1"
        foreign, recorded_view = [], recorded
        if recorded not in (UNKNOWN, None):
            relation = chain.head_relation(local, recorded.commit_seq, recorded.head_hash)
            recorded_view = {"instance_id": recorded.instance_id, "commit_seq": recorded.commit_seq,
                             "head_hash": recorded.head_hash, "relation": relation}
            if recorded.instance_id not in lineage or relation in {"ahead", "fork"}:
                foreign.append({"source": "coordinator", **recorded_view})
        leaf = (divergence or {}).get("foreign_leaf")
        if leaf is not None and (directory / leaf).exists():
            foreign.append(_foreign_file(local, directory / leaf, "evidence", path.parent))
        replica_view = None
        if replica.replica_path(root).exists():
            digest = _file_digest(replica.replica_path(root))
            replica_view = {"sha256": digest,
                            "published_by_this_store": digest == meta.get(schema.META_LAST_PUBLISHED_REPLICA_SHA256)}
            if not replica_view["published_by_this_store"]:
                foreign.append(_foreign_file(local, replica.replica_path(root), "replica", path.parent))
        # A publication the divergence interrupted belongs to the old tenure; adopting abandons
        # it and removes its workspace.
        abandoned = meta.get(schema.META_PENDING_REPLICA_PUBLICATION)
    state = "diverged" if divergence is not None or view_diverged else "foreign" if foreign else "in_sync"
    return _sealed({"state": state, "divergence": divergence, "view_diverged": view_diverged,
                    "recorded_head": recorded_view, "replica": replica_view,
                    "abandoned_publication": abandoned, "fork_point": {"local": head, "foreign": foreign}})


def adopt_replica_preview(vault_root, *, recorded=UNKNOWN) -> dict:
    """Preview a fresh host's copy without creating live state or opening the shared replica."""
    from . import authority

    root = Path(vault_root).resolve()
    raw = authority.read_marker(root)
    marker = authority.parse_marker(root, raw)
    with tempfile.TemporaryDirectory(prefix="exomem-adopt-preview-") as scratch, \
            _evidence(replica.replica_path(root), Path(scratch)) as (digest, conn, meta):
        if meta is None:
            raise CollectionStoreError("COLLECTION_STORE_SYNC_PENDING", "the vault replica is incomplete or invalid")
        for entry in marker["collections"]:
            authority.require_selected(conn, marker, entry, root=root)
        head = _head(meta)
    recorded_view = recorded if recorded in (UNKNOWN, None) else {
        "store_id": recorded.store_id, "instance_id": recorded.instance_id,
        "commit_seq": recorded.commit_seq, "head_hash": recorded.head_hash}
    return _sealed({"state": "replica", "store_id": marker["store_id"],
                    "marker_sha256": hashlib.sha256(raw).hexdigest(), "replica": {"sha256": digest, **head},
                    "recorded_head": recorded_view})


def record_fork(conn, preview: dict, *, why: str, token: int):
    """Continue this store as a new lineage tenure past the previewed fork; returns its identity.

    The fork record keeps the previewed divergence marker, including the source leaf the
    foreign bytes came from. A foreign replica still at the shared name is named for the
    next publication to keep as evidence; the owner's own published replica stays the swap
    predecessor by digest, while its published head (the old tenure's) is cleared so the
    new tenure republishes. The old tenure's interrupted publication intent is dropped;
    adopt-local removes its workspace.
    """
    meta = dict(conn.execute("SELECT key,value FROM store_meta"))
    local = preview["fork_point"]["local"]
    instance = str(uuid.uuid4())
    lineage = [*json.loads(meta[schema.META_LINEAGE]), {
        "instance_id": instance, "adopted_from": local["instance_id"],
        "adopted_at_commit_seq": local["commit_seq"], "head_hash": local["head_hash"]}]
    forks = [*json.loads(meta.get(schema.META_FORKS) or "[]"), {
        "recorded_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "why": why,
        "lease_epoch": token, "local": local, "foreign": preview["fork_point"]["foreign"],
        "divergence": preview["divergence"], "view_diverged": preview["view_diverged"],
        "abandoned_publication": preview["abandoned_publication"]}]
    shared = preview["replica"]
    ours = shared is not None and shared["published_by_this_store"]
    values = {schema.META_INSTANCE_ID: instance, schema.META_LINEAGE: _json(lineage),
              schema.META_FORKS: _json(forks), schema.META_REPLICA_DIVERGENCE: None,
              schema.META_VIEW_DIVERGED: None, schema.META_PENDING_REPLICA_PUBLICATION: None,
              schema.META_PUBLISHED_REPLICA_HEAD: None,
              schema.META_ADOPTED_FOREIGN_REPLICA: None if shared is None or ours else shared["sha256"]}
    if not ours:
        values[schema.META_LAST_PUBLISHED_REPLICA_SHA256] = None
    for key, value in values.items():
        if value is None:
            conn.execute("DELETE FROM store_meta WHERE key=?", (key,))
        else:
            conn.execute("INSERT INTO store_meta(key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    return meta[schema.META_STORE_ID], instance


#: Why reconcile cannot hold a change; the code names its ids in an acknowledgement receipt.
_SKIPPED = {code: {"code": code, "reason": reason} for code, reason in (
    ("unreadable", "unreadable"),
    ("another_store", "another store"),
    ("collection_absent", "collection absent from this store"),
    ("no_committed_row", "the foreign change has no committed row to hold"),
)}


def reconcile_plan(vault_root, local) -> tuple[dict, list[dict]]:
    """Every item that unreconciled foreign evidence changed after its common ancestor (§15 item 5).

    Returns the sealed preview and the full items, with the foreign values to hold. Every
    effect kind counts as a change, a foreign hold included. Several evidence files from
    one foreign instance collapse to its latest change per item, by the commit that made
    it, so an older change never supersedes a newer hold. Items equal to this
    store's row carry nothing to decide and are only counted, as are changes already held
    (``already_held``) on the same local base, so a repeat with nothing new applies
    nothing; a changed local base re-holds. ``reconciled`` names the evidence
    that applying marks done: only files whose every changed item becomes a hold. A
    change that cannot be held here is listed in ``skipped`` with its code and reason, and
    its file stays unreconciled until the owner acknowledges the skipped changes.
    """
    root = Path(vault_root).resolve()
    meta = dict(local.execute("SELECT key,value FROM store_meta"))
    done = set(json.loads(meta.get(schema.META_RECONCILED_FOREIGN) or "[]"))
    sources, skipped, latest = [], [], {}
    scratch = connection.store_path(root).parent
    for path in sorted(replica.replica_path(root).parent.glob(_FOREIGN_PREFIX + "*")):
        with _evidence(path, scratch) as (digest, conn, foreign):
            if digest in done:
                continue
            if foreign is None or foreign[schema.META_STORE_ID] != meta[schema.META_STORE_ID]:
                skipped.append({"leaf": path.name, "sha256": digest,
                                **(_SKIPPED["unreadable"] if foreign is None else _SKIPPED["another_store"])})
                continue
            ancestor = _common_ancestor(local, conn)
            sources.append({"leaf": path.name, "sha256": digest, **_head(foreign),
                            "common_ancestor_commit_seq": ancestor})
            for cid, key, effects, changed_at in conn.execute(
                "SELECT t.collection_id,e.item_key,group_concat(DISTINCT e.effect),max(t.commit_seq) FROM audit_effects e "
                "JOIN txns t ON t.txn_id=e.txn_id WHERE t.commit_seq>? AND e.item_key IS NOT NULL "
                "GROUP BY 1,2 ORDER BY 1,2",
                (ancestor,),
            ).fetchall():
                where = {"leaf": path.name, "sha256": digest, "collection_id": cid, "item_key": key}
                if local.execute("SELECT 1 FROM collections WHERE collection_id=?", (cid,)).fetchone() is None:
                    skipped.append({**where, **_SKIPPED["collection_absent"]})
                    continue
                row = conn.execute("SELECT row_id,row_version,body,payload_hash FROM items "
                                   "WHERE collection_id=? AND item_key=?", (cid, key)).fetchone()
                if row is None:
                    skipped.append({**where, **_SKIPPED["no_committed_row"]})
                    continue
                held_id = hashlib.sha256(
                    f"store-delta\0{foreign[schema.META_INSTANCE_ID]}\0{cid}\0{key}".encode()).hexdigest()[:24]
                found = {"collection_id": cid, "item_key": key, "held_id": held_id, "evidence_leaf": path.name,
                         "evidence_sha256": digest, "foreign_instance_id": foreign[schema.META_INSTANCE_ID],
                         "foreign_commit_seq": int(foreign[schema.META_COMMIT_SEQ]), "foreign_change_commit_seq": changed_at,
                         "common_ancestor_commit_seq": ancestor, "foreign_effects": sorted(effects.split(",")),
                         "foreign_row_version": row[1], "foreign_payload_hash": row[3]}
                if held_id not in latest or _change_order(found) > _change_order(latest[held_id]):
                    latest[held_id] = {**found, "values": typed_storage.item_values(conn, row[0]), "body": row[2]}
    items, unchanged, already_held = [], 0, 0
    for held_id, item in sorted(latest.items()):
        mine = local.execute("SELECT row_version,payload_hash FROM items WHERE collection_id=? AND item_key=?",
                             (item["collection_id"], item["item_key"])).fetchone()
        if mine is not None and mine[1] == item["foreign_payload_hash"]:
            unchanged += 1
            continue
        item["local_row_version"] = None if mine is None else mine[0]
        held = local.execute("SELECT diagnostics_json FROM held_candidates WHERE held_id=?", (held_id,)).fetchone()
        if held is not None:
            current = json.loads(held[0])[0]
            if _change_order(current) > _change_order(item) or (
                    _change_order(current) == _change_order(item)
                    and current.get("local_row_version") == item["local_row_version"]):
                already_held += 1
                continue
        items.append(item)
    unresolved = {entry["sha256"] for entry in skipped}
    reconciled = [source["sha256"] for source in sources if source["sha256"] not in unresolved]
    shown = [{name: value for name, value in item.items() if name not in {"values", "body"}} for item in items]
    return _sealed({"sources": sources, "skipped": skipped, "items": shown, "unchanged": unchanged,
                    "already_held": already_held, "reconciled": reconciled}), items


def _change_order(change):
    """A foreign change's place in its instance's history: the commit that last changed the item."""
    return change.get("foreign_change_commit_seq", -1)
