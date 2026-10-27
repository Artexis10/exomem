"""Markdown projection metadata; canonical collection data stays in the store."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from .. import held_fs, record_formats, reserved_paths, vault
from . import authority, capability, connection, governance, log_view, summary, typed_storage

STAGE_PREFIX = ".exomem-collection-stage-"
ASIDE_PREFIX = ".exomem-collection-aside-"
_DESCRIPTOR = "collection-publication"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pair(version, digest):
    return [version, digest] if digest is not None else None


def source_pair(projection, name):
    """A recorded render basis survives a log's digest-free pending aggregate basis."""
    pair = _pair(projection[name + "_row_version"], projection[name + "_sha256"])
    if pair is None and projection["kind"] == "log" and projection["install_json"]:
        pair = json.loads(projection["install_json"]).get("previous_" + name)
    return pair


def source_pairs(descriptor):
    """Possible pre-install sources; legacy pending renders may already have escaped."""
    pairs = descriptor.get("source_pairs", [descriptor.get("previous_published"), descriptor.get("previous_pending")])
    if not isinstance(pairs, list) or any(pair is not None and (
            not isinstance(pair, (list, tuple)) or len(pair) != 2
            or type(pair[0]) is not int or not isinstance(pair[1], str)) for pair in pairs):
        return set()
    return {tuple(pair) for pair in pairs if pair is not None}


def target_pairs(projection, descriptor=None):
    """Only a staged committed render can join a new descriptor's exposed sources."""
    descriptor = descriptor if descriptor is not None else json.loads(projection["install_json"] or "{}")
    pairs = source_pairs(descriptor)
    if "source_pairs" not in descriptor:
        # Schema 12 published before acknowledgement; its pending pair is conservatively exposed.
        pairs.update(tuple(pair) for name in ("published", "pending") if (pair := source_pair(projection, name)))
        context = descriptor.get("render_context", {})
        if context.get("sha256") is not None:
            pairs.add((context["row_version"], context["sha256"]))
    elif (ready := descriptor.get("install_ready")) is not None:
        if not (staged := source_pairs({"source_pairs": [ready]})):
            return set()
        pairs.update(staged)
    return pairs


def unresolved_target(projection, source=None):
    """A pending adoption cannot grant release authority to later saves or displaced inputs."""
    descriptor = json.loads(projection["install_json"]) if projection.get("install_json") else {}
    # Fixed publication protocol state survives hold discard; accepted digests authorize displacement, never release.
    return bool(descriptor.get("unresolved_target_provenance") or (source or {}).get("unresolved_target_provenance"))


def may_have_reached_path(projection):
    """Whether bytes of this view may be at its path: published, readied, carried, accepted or adopted."""
    descriptor = json.loads(projection["install_json"] or "{}")
    return (projection["published_sha256"] is not None or bool(target_pairs(projection, descriptor))
            or bool(descriptor.get("accepted")) or unresolved_target(projection))


@dataclass
class _Publication:
    path: str
    descriptor: dict
    parent: held_fs.HeldDirectory | None = None
    projection: dict | None = None
    context: tuple | None = None


@dataclass
class _AliasInput:
    """A current two-link input held through COMMIT, never evidence of an offline file's provenance."""

    guard: vault.PathGuard
    filesystem: held_fs.HeldFilesystem
    parent: held_fs.HeldDirectory
    file: held_fs.HeldFile
    version: held_fs.FileVersion
    raw: bytes
    digest: str

    @property
    def leaf_identity(self):
        return self.guard.leaf_identity

    def recheck(self, root):
        self.guard.recheck(root)
        self.filesystem.validate_directory(self.parent).require()
        before = self.file.version()
        raw = self.filesystem.read(self.file, max_bytes=record_formats._MAX_ITEM_BYTES).require()
        if (before != self.version or self.file.version() != before or before.identity.link_count != 2
                or raw != self.raw or _digest(raw) != self.digest):
            raise held_fs.HeldFsError("INPUT_CHANGED", "held stage alias input changed")
        self.guard.recheck(root)


class PublicationBatch:
    """Stage in one canonical transaction, then install after its COMMIT."""

    def __init__(self, writer, *, stop: Callable[[], bool] | None = None):
        self.writer = writer
        self.stop = stop
        self.identity = store_identity(writer.connection)
        self.stack = ExitStack()
        self.filesystem = None
        self.publications = {}
        self.parents = {}
        self.parent_paths = {}
        self.responses = []
        self.cleanup = []
        self.alias_cleanup = []
        self.pending = False
        self.committed = False
        self.contexts = {}
        self.history_contexts = {}
        self.precommits = {}
        self.guards = {}
        self.identities = None
        self.baselines = {}
        self.pair_cleanup = {}
        self.recovered_stages = []
        self.captured = {}
        self.recovered_inputs = {}
        self.source_manifests = {}
        self.foreign_discovered = False
        self.business_started = False
        self.deferred_create = None
        # path -> digests of bytes the store adopted or preserved, so displacing them loses nothing.
        self.accepted = {}
        # Paths whose held human bytes stay in place until the owner resolves the hold.
        self.kept = set()
        # path -> the settled edit-back observation this batch acts on, or None for every path.
        self.observed = None
        # Whether the release runs edit-back; without it, view inputs are classified as before it.
        self.edit_back = capability.collection_views_enabled(conn=writer.connection)

    def stopped(self):
        if self.stop is not None and self.stop():
            self.pending = True
            return True
        return False

    def accept(self, path, digest):
        self.accepted.setdefault(path, set()).add(digest)

    def accepted_digests(self, path, descriptor=None):
        return self.accepted.get(path, set()) | set((descriptor or {}).get("accepted", ()))

    def eligible(self, projection):
        if (projection["collection_id"] != self.deferred_create
                and authority.projection_eligible(self.writer, projection)):
            return True
        self.pending = True
        return False

    def _render_context(self, projection, manifest=None, *, pair=None):
        """The governance a view's render was released under, with its version and digest.

        ``pair`` names a recorded ``(version, digest)`` of a log view: a log view is governed by
        its collection's current manifest, as a summary page is, whatever rows it rendered, so
        the pair needs no render to be proved.
        """
        conn = self.writer.connection
        if projection["kind"] == "item":
            row = conn.execute("SELECT governance_json,updated_txn,values_json,encoding,collection_id FROM items "
                               "WHERE row_id=?", (projection["row_id"],)).fetchone()
            if row is not None:
                item = {"row_id": projection["row_id"], "values_json": row[2], "encoding": row[3],
                        "collection_id": row[4]}
                row = (*row[:2], typed_storage.hydrate(conn, [item])[0]["values_json"])
        # nosemgrep: ep-word-membership -- The projection_state.kind CHECK fixes these kinds.
        elif projection["kind"] in ("manifest", "summary", "log", "history"):
            # A summary page and a log view are governed by their collection's current manifest.
            if manifest is not None and projection["kind"] == "manifest":
                row = conn.execute("SELECT governance_json FROM collection_manifests WHERE collection_id=? AND manifest_version=?",
                                   (projection["collection_id"], projection["pending_row_version"])).fetchone()
            else:
                row = conn.execute(
                    "SELECT m.governance_json FROM collection_manifests m JOIN collections c "
                    "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
                    (projection["collection_id"],),
                ).fetchone()
        else:
            row = conn.execute("SELECT governance_json FROM held_candidates WHERE held_id=? AND view_path=?",
                               (Path(projection["path"]).stem, projection["path"])).fetchone()
        if row is None:
            return None
        metadata = governance._json(governance._metadata(row[0]))
        cid = projection["collection_id"]
        if manifest is not None:
            version, digest = projection["pending_row_version"], projection["pending_sha256"]
        elif pair is not None and projection["kind"] == "log":
            version, digest = pair
        else:
            current = self.writer._collection_row(cid)
            if projection["kind"] == "item":
                original = conn.execute(
                    "SELECT m.manifest_version,m.manifest_text,m.governance_json FROM txns t "
                    "JOIN collection_manifests m ON m.collection_id=t.collection_id "
                    "AND m.manifest_version=t.manifest_version_after WHERE t.txn_id=?",
                    (row[1],),
                ).fetchone()
                if original is None:
                    return None
                key = (cid, original[0])
                if key not in self.source_manifests:
                    if original[0] == current["manifest_version"]:
                        self.source_manifests[key] = self.writer._collection_manifest(current)[0]
                    else:
                        from .. import structured_collections as collections

                        self.source_manifests[key] = collections.parse_manifest_bytes(
                            self.writer.root, current["manifest_path"], original[1].encode()
                        )
                manifest = self.source_manifests[key]
                if governance.row_metadata(manifest.schema, json.loads(row[2]), original[2]) != metadata:
                    return None
            else:
                key = (cid, current["manifest_version"])
                if key not in self.source_manifests:
                    self.source_manifests[key] = self.writer._collection_manifest(current)[0]
                manifest = self.source_manifests[key]
            version, text = render_view(conn, self.identity, projection, manifest, root=self.writer.root,
                                        cache=self.writer.handle.release_cache, operation=self.writer._operation,
                                       observe_read=self.writer._recheck_guard)
            digest = _digest(text.encode())
        context = {"path": projection["path"], "collection_id": cid, "kind": projection["kind"],
                "row_id": projection.get("row_id"), "row_version": version,
                "sha256": digest, "governance_json": metadata}
        if projection["kind"] == "log" and pair is None:
            # Only a render of this snapshot proves its frame; historical governance backfill cannot.
            frame = conn.execute("SELECT log_frame_json FROM collections WHERE collection_id=?", (cid,)).fetchone()[0]
            context["frame_hash"] = log_view.frame_hash(log_view.load(frame, manifest))
        return context

    def capture_previous(self, path):
        """Capture render-bound policy and recover inputs before replacing their source."""
        if path in self.captured:
            return self.captured[path]
        previous = self.previous(path)
        self.captured[path] = previous
        if previous is None:
            self.recovered_inputs[path] = []
            return None
        if not self.eligible(previous):
            self.recovered_inputs[path] = None
            return previous
        descriptor = json.loads(previous["install_json"]) if previous["install_json"] else {
            "token": secrets.token_hex(16),
            "previous_published": _pair(previous["published_row_version"], previous["published_sha256"]),
            "previous_pending": _pair(previous["pending_row_version"], previous["pending_sha256"]),
        }
        pairs = target_pairs(previous, descriptor)
        try:
            parent = self._parent(path)
            result = self._fs().file(parent, Path(path).name)
            if result.ok:
                with result.require() as file:
                    raw = self._fs().read(file).require()
                    digest = _digest(raw)
                pair = next((pair for pair in pairs if pair[1] == digest), None)
                if pair is not None:
                    _, guard = vault.read_bounded_guarded_bytes(self.writer.root, path, limit=len(raw), expected_hash=digest)
                    self.writer._recheck_guard(guard)
                    # Only a guarded physical observation narrows possible historical authority.
                    pairs = {pair}
                    self.writer._execute(
                        "UPDATE projection_state SET published_row_version=?,published_sha256=? WHERE path=?",
                        (*pair, path),
                    )
                    previous["published_row_version"], previous["published_sha256"] = pair
            elif result.error.code != "MISSING":
                raise result.error
        except (held_fs.HeldFsError, vault.PathGuardError, OSError):
            self.pending = True
        previous["source_pairs"] = sorted(pairs)
        known = [*descriptor.get("source_contexts", []), descriptor.get("render_context")]
        canonical = None
        backfilled = False
        contexts = []
        for version, digest in pairs:
            matches = {}
            invalid = False
            for context in known:
                if (isinstance(context, dict)
                        and all(context.get(name) == previous.get(name) for name in ("path", "collection_id", "kind", "row_id"))
                        and (context.get("row_version"), context.get("sha256")) == (version, digest)):
                    try:
                        metadata = governance._json(governance._metadata(context["governance_json"]))
                    except (KeyError, TypeError, ValueError):
                        invalid = True
                    else:
                        matches[metadata] = {**context, "governance_json": metadata}
            if not matches and not invalid and previous["kind"] == "log":
                # Any bytes the store recorded for a log view, the imported file's included, have this context.
                try:
                    context = self._render_context(previous, pair=(version, digest))
                except (TypeError, ValueError):
                    context = None
                if context:
                    matches[context["governance_json"]] = context
            elif not matches and not invalid:
                if not backfilled:
                    try:
                        canonical = self._render_context(previous)
                    except (TypeError, ValueError):
                        canonical = None
                    backfilled = True
                if canonical and (canonical["row_version"], canonical["sha256"]) == (version, digest):
                    matches[canonical["governance_json"]] = canonical
            if not invalid and len(matches) == 1:
                contexts.append(next(iter(matches.values())))
        previous["source_contexts"] = contexts
        displaced_pairs = source_pairs(descriptor)
        if "source_contexts" not in descriptor:
            descriptor["source_contexts"] = [context for context in contexts
                                             if (context["row_version"], context["sha256"]) in displaced_pairs]
        previous["install_json"] = json.dumps(descriptor, separators=(",", ":"))
        self.writer._execute("UPDATE projection_state SET install_json=? WHERE path=?",
                             (previous["install_json"], path))
        try:
            self.recovered_inputs[path] = self.recover_inputs(previous)
            parent = self._parent(path)
            opened = self._fs().file(parent, Path(path).name)
            if opened.ok:
                with opened.require() as file:
                    raw = self._fs().read(file).require()
                digest = _digest(raw)
                if digest in self.accepted_digests(path, descriptor):
                    # An earlier transaction adopted or preserved these bytes; this install may displace them.
                    self.accept(path, digest)
                elif digest not in {previous["published_sha256"], previous["pending_sha256"]}:
                    source = {"source_pairs": previous["source_pairs"],
                              "source_contexts": contexts}
                    if not self.writer._register_view_input(previous, raw, descriptor["token"], "offline", source=source):
                        self.pending = True
            elif opened.error.code != "MISSING":
                raise opened.error
        except (held_fs.HeldFsError, OSError, connection.CollectionStoreError):
            self.pending = True
            self.recovered_inputs[path] = None
        return previous

    def bind(self, response):
        self.responses.append(response)

    def close(self):
        self.stack.close()

    @contextmanager
    def _boundary(self):
        with (
            reserved_paths._subsystem_authority_scope("collection_store.views"),
            reserved_paths._identity_coordination_scope(
                self.writer.root, descriptor_ids=(_DESCRIPTOR,)
            ),
        ):
            self.identities = reserved_paths._reachable_owner_publications(
                self.writer.root, _DESCRIPTOR
            )
            try:
                yield
            finally:
                try:
                    reserved_paths._publish_owner_identities(
                        self.writer.root, _DESCRIPTOR, self.identities
                    )
                finally:
                    self.identities = None

    def _fs(self):
        if self.filesystem is None:
            self.filesystem = self.stack.enter_context(held_fs.acquire(self.writer.root).require())
        return self.filesystem

    def _parent(self, path, *, create=False):
        relative = str(Path(path).parent)
        if relative not in self.parents:
            self.parents[relative] = self.stack.enter_context(
                self._fs().parent(relative, create=create, access="mutate").require()
            )
            self.parent_paths[self.parents[relative]] = Path(relative)
        return self.parents[relative]

    def _record(self, path, identity):
        if identity is not None and identity.kind == "file" and identity.link_count != 1:
            if self.identities is not None:
                self.identities.pop(path, None)
            raise held_fs.HeldFsError(
                "IDENTITY_CHANGED", "projection scratch identity could not be reserved"
            )
        if self.identities is None:
            raise RuntimeError("projection identity update lacks its filesystem phase")
        if identity is None:
            self.identities.pop(path, None)
        else:
            self.identities[path] = identity

    def _remove(self, parent, leaf):
        result = self._fs().file(parent, leaf, access="mutate")
        if result.error is not None and result.error.code == "MISSING":
            return
        with result.require() as file:
            if file.identity.link_count != 1:
                raise held_fs.HeldFsError("IDENTITY_CHANGED", "projection scratch has aliases")
            self._fs().unlink(file).require()
        self._fs().flush_directory(parent).require()

    def previous(self, path):
        cursor = self.writer.connection.execute(
            "SELECT * FROM projection_state WHERE path=?", (path,)
        )
        row = cursor.fetchone()
        return (
            None if row is None else dict(zip((c[0] for c in cursor.description), row, strict=True))
        )

    def prepare(self, projection, previous, manifest):
        path = projection["path"]
        if path not in self.captured:
            raise RuntimeError("projection source was not captured before mutation")
        previous = self.captured[path]
        publication = self.publications.get(path)
        if publication is None:
            preserved = self.recovered_inputs[path]
            if preserved is None:
                self.pending = True
                # The old token still owns its stages/asides and their exact readiness and policy.
                return previous["install_json"]
            descriptor = {
                "token": secrets.token_hex(16),
                "source_pairs": previous["source_pairs"] if previous else [],
                "previous_published": source_pair(previous, "published")
                if previous
                else None,
                "previous_pending": source_pair(previous, "pending")
                if previous
                else None,
                "preserved": preserved,
                "source_contexts": previous.get("source_contexts", []) if previous else [],
            }
            if previous and unresolved_target(previous):
                descriptor["unresolved_target_provenance"] = True
            publication = self.publications[path] = _Publication(path, descriptor)
        if self.accepted.get(path):
            publication.descriptor["accepted"] = sorted(self.accepted[path])
        publication.descriptor["render_context"] = self._render_context(projection, manifest)
        publication.projection = projection
        publication.context = (projection["collection_id"], manifest.manifest_version.hash)
        self.contexts[publication.context] = manifest
        return json.dumps(publication.descriptor, separators=(",", ":"))

    def confirm_target(self, projection):
        """Clear adoption's owner floor only when this transaction observes the committed canonical render."""
        if not unresolved_target(projection):
            return True
        descriptor = json.loads(projection["install_json"])
        context = descriptor.get("render_context", {})
        pair = (projection["pending_row_version"], projection["pending_sha256"])
        if (pair[1] is None or (context.get("row_version"), context.get("sha256")) != pair
                or any(context.get(name) != projection.get(name) for name in ("path", "collection_id", "kind", "row_id"))):
            return False
        current = self.previous(projection["path"])
        if (current is None or not current["install_json"]
                or (current["pending_row_version"], current["pending_sha256"]) != pair
                or json.loads(current["install_json"]).get("token") != descriptor.get("token")):
            return False
        try:
            parent = self._parent(projection["path"])
            with self._fs().file(parent, Path(projection["path"]).name).require() as file:
                version = file.version()
                raw = self._fs().read(file, max_bytes=record_formats._MAX_ITEM_BYTES).require()
                if file.version() != version or _digest(raw) != pair[1]:
                    return False
            self._fs().validate_directory(parent).require()
            _, guard = vault.read_bounded_guarded_bytes(self.writer.root, projection["path"],
                limit=record_formats._MAX_ITEM_BYTES, expected_hash=pair[1])
            if (guard.leaf_identity.device, guard.leaf_identity.inode) != (version.identity.device, version.identity.inode):
                return False
            self.writer._recheck_guard(guard)
        except (vault.PathGuardError, held_fs.HeldFsError, OSError):
            return False
        descriptor.pop("unresolved_target_provenance")
        projection["install_json"] = json.dumps(descriptor, separators=(",", ":"))
        self.writer._execute("UPDATE projection_state SET install_json=? WHERE path=?",
                             (projection["install_json"], projection["path"]))
        return True

    def stage_all(self):
        publications = [publication for publication in self.publications.values()
                        if publication.projection is not None and self.eligible(publication.projection)]
        pairs = {path: pair for path, pair in self.pair_cleanup.items()
                 if self.eligible(self.captured[path])}
        if not pairs and not publications:
            return
        with self._boundary():
            for path, (parent, leaf, digest) in pairs.items():
                if self.stopped():
                    break
                try:
                    self._cleanup_stage_pair(path, parent, leaf, digest)
                except (held_fs.HeldFsError, OSError):
                    self.pending = True
            for publication in publications:
                if self.stopped():
                    break
                if publication.projection["kind"] == "history":
                    # Its prepared policy predates filesystem identity exclusion; do not widen that domain here.
                    version, text = self.history_contexts[publication.context].render_many((publication.path,))[
                        publication.path]
                else:
                    version, text = self.writer._render_view(
                        publication.projection, self.contexts[publication.context]
                    )
                data = text.encode()
                if (
                    version != publication.projection["pending_row_version"]
                    or _digest(data) != publication.projection["pending_sha256"]
                ):
                    raise RuntimeError("canonical projection changed after preparation")
                try:
                    fs = self._fs()
                    publication.parent = self._parent(publication.path, create=True)
                    leaf = STAGE_PREFIX + publication.descriptor["token"]
                    with fs.file(
                        publication.parent, leaf, access="write", create=True, exclusive=True
                    ).require() as file:
                        self._record(str(Path(publication.path).parent / leaf), file.identity)
                        fs.write(file, data).require()
                        self._record(str(Path(publication.path).parent / leaf), file.identity)
                    fs.flush_directory(publication.parent).require()
                    publication.descriptor["install_ready"] = [version, _digest(data)]
                    self.writer._execute("UPDATE projection_state SET install_json=? WHERE path=?",
                        (json.dumps(publication.descriptor, separators=(",", ":")), publication.path))
                except (held_fs.HeldFsError, OSError):
                    self.pending = True

    def _cleanup_stage_pair(self, path, parent, leaf, digest):
        proof = self.baselines[path]
        descriptor = json.loads(proof["install_json"])
        ready = descriptor.get("install_ready") or (source_pair(proof, "pending") if "source_pairs" not in descriptor else None)
        if leaf != STAGE_PREFIX + descriptor["token"] or ready is None or digest != ready[1]:
            raise held_fs.HeldFsError("IDENTITY_CHANGED", "redundant stage lacks committed proof")
        fs = self._fs()
        fs.validate_directory(parent).require()
        with (
            fs.file(parent, leaf, access="mutate").require() as stage,
            fs.file(parent, Path(path).name).require() as target,
        ):
            if (
                stage.identity.link_count != 2
                or target.identity.link_count != 2
                or stage.identity != target.identity
                or _digest(fs.read(stage).require()) != digest
                or _digest(fs.read(target).require()) != digest
            ):
                raise held_fs.HeldFsError("IDENTITY_CHANGED", "redundant stage pair changed")
            fs.unlink(stage).require()
            self._record(str(Path(path).parent / leaf), None)
            fs.flush_directory(parent).require()
        self.recovered_stages.append(
            {
                "path": path,
                "token": descriptor["token"],
                "version": ready[0],
                "sha256": digest,
                "provenance": "committed_baseline",
            }
        )

    def recover_inputs(self, previous):
        # Deferred displaced bytes are registered by the next touching transaction.
        if previous and previous["install_json"]:
            # BEGIN IMMEDIATE keeps this committed pre-UPSERT proof current until our COMMIT.
            self.baselines.setdefault(previous["path"], dict(previous))
            preserved = self.writer._recover_view_inputs(self, previous)
            return preserved
        return []

    def _asides(self, publication):
        fs = self._fs()
        expected = {pair[1] for pair in source_pairs(publication.descriptor)} | set(publication.descriptor.get("accepted", ()))
        for slot in range(2):
            leaf = ASIDE_PREFIX + publication.descriptor["token"] + f"-{slot}"
            result = fs.file(publication.parent, leaf, access="mutate")
            if result.error is not None and result.error.code == "MISSING":
                continue
            with result.require() as file:
                if _digest(fs.read(file).require()) in expected:
                    fs.unlink(file).require()
                    self._record(str(Path(publication.path).parent / leaf), None)
                    fs.flush_directory(publication.parent).require()
                else:
                    self._record(str(Path(publication.path).parent / leaf), file.identity)
                    self.pending = True

    def _install(self, publication):
        fs = self._fs()
        parent = publication.parent
        if parent is None:
            raise held_fs.HeldFsError("MISSING", "projection parent is unavailable")
        committed = self.writer.connection.execute(
            "SELECT install_json,pending_row_version,pending_sha256 FROM projection_state WHERE path=?",
            (publication.path,),
        ).fetchone()
        if (
            committed is None
            or not committed[0]
            or json.loads(committed[0])["token"] != publication.descriptor["token"]
        ):
            raise held_fs.HeldFsError("IDENTITY_CHANGED", "projection descriptor was superseded")
        fs.validate_directory(parent).require()
        target = Path(publication.path).name
        retired = publication.descriptor.get("retired")
        descriptor = json.loads(committed[0])
        ready = descriptor.get("install_ready")
        context = descriptor.get("render_context", {})
        if not retired and (ready != list(committed[1:]) or ready is None
                or ready != [context.get("row_version"), context.get("sha256")]
                or any(context.get(name) != publication.projection.get(name)
                       for name in ("path", "collection_id", "kind", "row_id"))):
            raise held_fs.HeldFsError("IDENTITY_CHANGED", "projection lacks committed installation readiness")
        stage_leaf = STAGE_PREFIX + publication.descriptor["token"]
        with (
            nullcontext(None) if retired else fs.file(parent, stage_leaf, access="mutate").require()
        ) as stage:
            if not retired and _digest(fs.read(stage).require()) != ready[1]:
                raise held_fs.HeldFsError(
                    "IDENTITY_CHANGED", "projection staging does not match committed bytes"
                )
            for slot in range(2):
                result = fs.file(parent, target, access="mutate")
                if result.ok:
                    aside = ASIDE_PREFIX + publication.descriptor["token"] + f"-{slot}"
                    with result.require() as file:
                        if os.name == "nt":
                            fs.rename(file, parent, aside).require()
                        else:
                            fs.link(file, parent, aside).require()
                            fs.unlink(file).require()
                    with fs.file(parent, aside).require() as file:
                        self._record(str(Path(publication.path).parent / aside), file.identity)
                elif result.error.code != "MISSING":
                    raise result.error
                if retired:
                    break
                install = (
                    fs.rename(stage, parent, target)
                    if os.name == "nt"
                    else fs.link(stage, parent, target)
                )
                if install.ok:
                    if os.name != "nt":
                        fs.unlink(stage).require()
                    self._record(str(Path(publication.path).parent / stage_leaf), None)
                    fs.flush_directory(parent).require()
                    break
                if install.error.code != "DESTINATION_EXISTS" or slot == 1:
                    raise install.error
        self._asides(publication)

    def publish(self):
        publications = [publication for publication in self.publications.values()
                        if self.eligible(publication.projection or self.previous(publication.path))]
        if self.cleanup or self.alias_cleanup or publications:
            with self._boundary():
                for projection, parent, leaf, destination, digest in self.alias_cleanup:
                    if self.stopped():
                        break
                    if not self.eligible(projection):
                        self.pending = True
                        continue
                    try:
                        fs = self._fs()
                        fs.validate_directory(parent).require()
                        with (fs.file(parent, leaf, access="mutate").require() as stage,
                              fs.file(self._parent(destination), Path(destination).name).require() as target):
                            stage_version, target_version = stage.version(), target.version()
                            if (stage.identity.link_count != 2 or stage.identity != target.identity
                                    or _digest(fs.read(stage, max_bytes=record_formats._MAX_ITEM_BYTES).require()) != digest
                                    or _digest(fs.read(target, max_bytes=record_formats._MAX_ITEM_BYTES).require()) != digest
                                    or stage.version() != stage_version or target.version() != target_version):
                                raise held_fs.HeldFsError("IDENTITY_CHANGED", "moved stage pair changed")
                            # The quarantine is durable after COMMIT; current aliases alone never prove provenance.
                            fs.unlink(stage).require()
                            self._record(str(self.parent_paths[parent] / leaf), None)
                            fs.flush_directory(parent).require()
                    except (held_fs.HeldFsError, OSError):
                        self.pending = True
                for projection, parent, leaf, digest in self.cleanup:
                    if self.stopped():
                        break
                    eligible = (self.eligible(projection) if projection is not None else
                                authority.orphan_cleanup_eligible(self.writer, self.parent_paths[parent]))
                    if not eligible:
                        self.pending = True
                        continue
                    try:
                        with self._fs().file(parent, leaf, access="mutate").require() as file:
                            if file.identity.link_count != 1:
                                self.pending = True
                                continue
                            if _digest(self._fs().read(file).require()) == digest:
                                self._fs().unlink(file).require()
                                self._record(str(self.parent_paths[parent] / leaf), None)
                                self._fs().flush_directory(parent).require()
                            else:
                                self.pending = True
                    except (held_fs.HeldFsError, OSError):
                        self.pending = True
                for publication in publications:
                    if self.stopped():
                        break
                    try:
                        self._install(publication)
                    except (held_fs.HeldFsError, OSError):
                        self.pending = True
        if self.pending:
            for response in self.responses:
                warnings = response.setdefault("warnings", [])
                if "projection_pending" not in warnings:
                    warnings.append("projection_pending")

    def retire(self, previous):
        if previous is None:
            return
        if not self.eligible(previous):
            return
        path = previous["path"]
        previous = self.captured[path]
        preserved = self.recovered_inputs[path]
        if preserved is None:
            self.pending = True
            return
        descriptor = {
            "token": secrets.token_hex(16),
            "retired": True,
            "source_pairs": previous["source_pairs"],
            "previous_published": _pair(
                previous["published_row_version"], previous["published_sha256"]
            ),
            "previous_pending": _pair(previous["pending_row_version"], previous["pending_sha256"]),
            "preserved": preserved,
            "source_contexts": previous.get("source_contexts", []),
        }
        if unresolved_target(previous):
            descriptor["unresolved_target_provenance"] = True
        publication = self.publications[path] = _Publication(path, descriptor)
        try:
            publication.parent = self._parent(path)
        except (held_fs.HeldFsError, OSError):
            self.pending = True
        self.writer._execute(
            "UPDATE projection_state SET pending_row_version=NULL,pending_sha256=NULL,"
            "state='pending',install_json=? WHERE path=?",
            (json.dumps(descriptor, separators=(",", ":")), path),
        )

    def retire_moved_inputs(self, previous, destination, *, preserve):
        """Retire old installation inputs without recreating their old destination.

        A fresh destination parent is a search location, never historical provenance.
        Unknown input bytes are held before cleanup; token spelling never releases bytes.
        """
        if not previous["install_json"]:
            return
        descriptor = json.loads(previous["install_json"])
        try:
            parent = self._parent(previous["path"])
        except held_fs.HeldFsError as error:
            if error.code != "MISSING":
                raise
            parent = self._parent(destination)
        fs = self._fs()
        witness = None
        with self._boundary():
            for source in (descriptor, *descriptor.get("preserved", [])):
                expected = {pair[1] for pair in source_pairs(source)} | set(source.get("accepted", ()))
                for slot in range(3):  # The publication protocol has two asides and one stage.
                    leaf = (ASIDE_PREFIX + source["token"] + f"-{slot}" if slot < 2
                            else STAGE_PREFIX + source["token"])
                    opened = fs.file(parent, leaf, access="mutate")
                    if opened.error is not None and opened.error.code == "MISSING":
                        continue
                    with opened.require() as file:
                        version = file.version()
                        raw = fs.read(file, max_bytes=record_formats._MAX_ITEM_BYTES).require()
                        digest = _digest(raw)
                        ready = source.get("install_ready") or (source_pair(previous, "pending")
                                                               if "source_pairs" not in source else None)
                        known = digest in expected or (slot == 2 and ready is not None and digest == ready[1])
                        if not known and not preserve(raw, source["token"], slot):
                            raise held_fs.HeldFsError("INPUT_UNPRESERVED", "moved publication input is not held")
                        if file.version() != version:
                            raise held_fs.HeldFsError("INPUT_CHANGED", "moved publication input changed")
                        if file.identity.link_count == 2 and slot == 2:
                            target_parent = self._parent(destination)
                            target = self.stack.enter_context(fs.file(target_parent, Path(destination).name).require())
                            target_version = target.version()
                            if (witness is not None or file.identity != target.identity
                                    or _digest(fs.read(target, max_bytes=record_formats._MAX_ITEM_BYTES).require()) != digest
                                    or target.version() != target_version or file.version() != version):
                                raise held_fs.HeldFsError("IDENTITY_CHANGED", "moved stage pair changed")
                            witness = _AliasInput(vault.PathGuard.capture(self.writer.root, destination, leaf_policy="stable"),
                                                  fs, target_parent, target, target_version, raw, digest)
                            # Even an alias stays until unknown bytes have a committed quarantine copy.
                            self.alias_cleanup.append((previous, parent, leaf, destination, digest))
                        elif file.identity.link_count == 1:
                            self.cleanup.append((previous, parent, leaf, digest))
                        else:
                            raise held_fs.HeldFsError("IDENTITY_CHANGED", "moved publication input has aliases")
        return witness

    def rollback(self):
        if not any(publication.parent is not None for publication in self.publications.values()):
            return
        with self._boundary():
            for publication in self.publications.values():
                if publication.parent is None or publication.descriptor.get("retired"):
                    continue
                try:
                    self._remove(publication.parent, STAGE_PREFIX + publication.descriptor["token"])
                    self._record(
                        str(
                            Path(publication.path).parent
                            / (STAGE_PREFIX + publication.descriptor["token"])
                        ),
                        None,
                    )
                except (held_fs.HeldFsError, OSError):
                    pass

    def recover_orphans(self, path, *, limit):
        """Remove only proven journal-free inputs; unknown bytes stay private."""
        previous = self.previous(path)
        if previous is not None and not self.eligible(previous):
            return
        if not authority.orphan_cleanup_eligible(self.writer, Path(path).parent):
            self.pending = True
            return
        parent = self._parent(path)
        processed = 0
        for child in self._fs().children(parent).require():
            if self.stopped():
                break
            leaf = child.relative_path
            if not leaf.startswith((STAGE_PREFIX, ASIDE_PREFIX)):
                continue
            if (
                reserved_paths.classify_logical(str(Path(path).parent / leaf)).descriptor_id
                != _DESCRIPTOR
            ):
                continue
            if processed >= limit:
                self.pending = True
                break
            processed += 1
            token = (
                leaf[len(STAGE_PREFIX) :]
                if leaf.startswith(STAGE_PREFIX)
                else leaf[len(ASIDE_PREFIX) :].rsplit("-", 1)[0]
            )
            owned = self.writer.connection.execute(
                "SELECT 1 FROM projection_state WHERE json_extract(install_json,'$.token')=? OR EXISTS "
                "(SELECT 1 FROM json_each(install_json,'$.preserved') WHERE json_extract(value,'$.token')=?) LIMIT 1",
                (token, token),
            ).fetchone()
            if owned:
                continue
            with self._fs().file(parent, leaf).require() as file:
                raw = self._fs().read(file).require()
            digest = _digest(raw)
            preserved = self.writer.connection.execute(
                "SELECT 1 FROM held_candidates WHERE held_bytes=? LIMIT 1", (raw,)
            ).fetchone()
            expected = self.writer.connection.execute(
                "SELECT 1 FROM projection_state WHERE published_sha256=? OR pending_sha256=? LIMIT 1",
                (digest, digest),
            ).fetchone()
            # A token name proves no bytes; unknown staging needs the same preservation as unknown asides.
            if preserved or expected:
                self.cleanup.append((None, parent, leaf, digest))
            else:
                self.pending = True


def store_identity(conn: sqlite3.Connection) -> dict[str, str]:
    """Read the physical store identity once for one rendering batch."""
    return dict(
        conn.execute("SELECT key, value FROM store_meta WHERE key IN ('store_id', 'instance_id')")
    )


def stamp(identity: Mapping[str, str], version: int, payload_hash: str) -> dict[str, str | int]:
    """Bind a rendered view to the physical store and canonical content version."""
    return {
        "s": identity["store_id"],
        "i": identity["instance_id"],
        "v": version,
        "h": payload_hash[:12],
    }


def manifest_view(text: str, view_stamp: Mapping[str, str | int]) -> str:
    """Normalize the manifest's properties, preserving its authored body."""
    frontmatter, body, _ = vault.parse_frontmatter(text, strict=True)
    for name in ("record_audit", "plan_audit"):
        frontmatter.pop(name, None)
    frontmatter["exomem_view"] = dict(view_stamp)
    return (
        "---\n"
        + vault.serialize_frontmatter(frontmatter)
        + "\n---\n"
        + ("\n" + body if body else "")
    )


#: Every file the store projects: published or pending views and each item's view path.
OWNED_VIEW_PATHS = "SELECT path FROM projection_state UNION SELECT view_path FROM items WHERE view_path IS NOT NULL"


def owned_view_paths(operation) -> frozenset[str]:
    """The vault-relative files this store owns as views, read in the operation's snapshot."""
    return frozenset(path for (path,) in operation.conn.execute(OWNED_VIEW_PATHS))


def presentation_wikilinks(operation, *, fresh=False):
    """Overlay the owned namespace on ordinary files within this operation's SQL snapshot."""
    cached = operation._presentation_wikilinks
    if not fresh and cached is not None and cached[0] == operation.conn.total_changes:
        return cached[1]
    resolver = vault.WikilinkResolver(operation.root)
    proof = operation.projection_ownership()
    deleted = [path + ".md" for path in resolver.full_paths
               if proof.owner(operation.root, path + ".md") is not None]
    paths = operation.conn.execute(
        f"{OWNED_VIEW_PATHS} UNION SELECT manifest_path FROM collections"
    ).fetchall()
    entries = []
    for (path,) in paths:
        handled, label = presentation_label(operation, path)
        if handled:
            entries.append((path, label))
    resolver.on_entries_changed(entries, deleted)
    operation._presentation_wikilinks = (operation.conn.total_changes, resolver)
    return resolver


def presentation_label(operation, path):
    """An owned target takes its title from canonical data, never its physical projection."""
    from .. import structured_collections as collections

    try:
        subjects = operation.projection_subjects(path)
    except collections.CollectionError:
        # An owned path this operation cannot release stays owned and unlabelled, as projection_decision treats it.
        return True, None
    if subjects is None:
        return False, None
    if not subjects or not operation.allows_file(path):
        return True, None
    conn = operation.conn
    manifest = operation.field_manifest(subjects[0].collection_id)
    item = conn.execute(
        "SELECT row_id,body FROM items WHERE collection_id=? AND view_path=?",
        (manifest.collection_id, path),
    ).fetchone()
    if item is not None:
        return True, record_formats.item_display_title(
            manifest, typed_storage.item_values(conn, item[0]), item[1], path
        )
    if path == manifest.path:
        text = conn.execute(
            "SELECT m.manifest_text FROM collection_manifests m JOIN collections c "
            "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version "
            "WHERE c.collection_id=?", (manifest.collection_id,),
        ).fetchone()[0]
    else:
        cursor = conn.execute("SELECT * FROM projection_state WHERE path=?", (path,))
        row = cursor.fetchone()
        if row is None:
            return True, None
        projection = dict(zip((column[0] for column in cursor.description), row, strict=True))
        if projection["kind"] == "item":
            return True, None
        _, text = render_view(conn, store_identity(conn), projection, manifest,
                              root=operation.root, cache=operation.cache, operation=operation)
    frontmatter, body, _ = vault.parse_frontmatter(text, strict=True)
    return True, vault.resolve_display_title(frontmatter, body, path)


def presentation_namespace_contains(operation, candidate, listings):
    """Probe the same owned overlay and ordinary namespace as `presentation_wikilinks`."""
    paths = [candidate + ".md"]
    # The resolver strips only lowercase .md; uppercase Markdown suffixes stay
    # in its key, so that exact physical spelling is also a possible entry.
    if Path(candidate).suffix.lower() == ".md" and not candidate.endswith(".md"):
        paths.append(candidate)
    proof = operation.projection_ownership()
    for path in paths:
        if proof.owner(operation.root, path) is not None:
            registered = operation.conn.execute(
                "SELECT EXISTS(SELECT 1 FROM projection_state WHERE path=?) "
                "OR EXISTS(SELECT 1 FROM items WHERE view_path=?) "
                "OR EXISTS(SELECT 1 FROM collections WHERE manifest_path=?)",
                (path, path, path),
            ).fetchone()[0]
            if registered:
                return True
        elif vault.walk_vault_md_admits(operation.root, operation.root / path, listings):
            relative = (operation.root / path).resolve().relative_to(operation.root.resolve()).as_posix()
            if relative.removesuffix(".md") == candidate:
                return True
    return False


@contextmanager
def presentation_resolver(conn, root, manifest, *, operation=None, observe_read=None, cache=None):
    """Use one canonical snapshot and authorization pass for a batch of item labels."""
    if manifest.item_presentation is None or not manifest.item_presentation.relationships:
        yield None
        return
    owned = operation is None
    operation = operation or governance.OperationAuthorization(root, conn, mutation=False, cache=cache)
    listings = {}

    def lookup_item(key):
        row = conn.execute(
            "SELECT row_id,view_path,body FROM items WHERE collection_id=? AND item_key=?",
            (manifest.collection_id, key),
        ).fetchone()
        return (row[1], typed_storage.item_values(conn, row[0]), row[2]) if row else None

    try:
        yield record_formats.presentation_relationship_resolver(
            root, manifest, None, authorize_path=operation.allows_file,
            lookup_item=lookup_item, lookup_path=lambda path: presentation_label(operation, path),
            observe_read=observe_read,
            wikilink_resolver=lambda: presentation_wikilinks(operation),
            targeted_namespace_contains=lambda candidate: presentation_namespace_contains(
                operation, candidate, listings
            ),
        )
    finally:
        if owned:
            operation.close()


def render_view(conn, identity, projection, manifest, *, root=None, cache=None,
                operation=None, observe_read=None):
    """Render an indexed canonical reference; the caller owns its read snapshot and context."""
    if projection["kind"] == "history":
        from .history import render_page

        return render_page(conn, root, manifest, projection["path"], cache=cache)
    if projection["kind"] == "item":
        version, payload, key, body = conn.execute(
            "SELECT row_version,payload_hash,item_key,body FROM items WHERE row_id=?",
            (projection["row_id"],),
        ).fetchone()
        with presentation_resolver(conn, root, manifest, operation=operation,
                                   observe_read=observe_read, cache=cache) as resolver:
            return version, record_formats.render_markdown_item(
                manifest,
                typed_storage.item_values(conn, projection["row_id"]),
                key,
                body,
                view_stamp=stamp(identity, version, payload),
                resolve_relationship=resolver,
            )
    if projection["kind"] == "manifest":
        version, text, payload = conn.execute(
            "SELECT m.manifest_version,m.manifest_text,m.manifest_hash FROM collection_manifests m JOIN collections c "
            "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
            (projection["collection_id"],),
        ).fetchone()
        return version, manifest_view(text, stamp(identity, version, payload))
    if projection["kind"] == "summary":
        generation, head = conn.execute("SELECT generation,audit_head FROM collections WHERE collection_id=?",
                                        (projection["collection_id"],)).fetchone()
        return generation, summary.render_page(conn, manifest, stamp(identity, generation, head))
    if projection["kind"] == "log":
        generation, frame = conn.execute("SELECT generation,log_frame_json FROM collections WHERE collection_id=?",
                                         (projection["collection_id"],)).fetchone()
        frame = log_view.load(frame, manifest)
        return generation, log_view.render(
            manifest, frame, log_view.rows(conn, manifest),
            stamp=lambda row: stamp(identity, row["row_version"], row["payload_hash"]),
            frame_stamp={**stamp(identity, generation, log_view.frame_hash(frame)), "h": log_view.frame_hash(frame)})
    cursor = conn.execute(
        "SELECT * FROM held_candidates WHERE held_id=? AND view_path=?",
        (projection.get("held_id", Path(projection["path"]).stem), projection["path"]),
    )
    held = dict(zip((column[0] for column in cursor.description), cursor.fetchone(), strict=True))
    return 1, held_view(held, identity)


def held_view(held, identity):
    """Keep exact correction bytes canonical while rendering their held envelope."""
    if held["kind"] == "write-refusal":
        return held["held_bytes"].decode()
    frontmatter = {
        "type": "held-record",
        "collection_id": held["collection_id"],
        "held_id": held["held_id"],
        "attempted_action": "view-edit",
        "held_at": held["updated_at"],
        "diagnostics": held["diagnostics_json"],
        "exomem_view": stamp(identity, 1, _digest(held["held_bytes"])),
        **json.loads(held["governance_json"]),
    }
    return (
        "---\n"
        + vault.serialize_frontmatter(frontmatter)
        + "\n---\n\n"
        + held["held_bytes"].decode(errors="replace")
    )
