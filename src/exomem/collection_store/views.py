"""Markdown projection metadata; canonical collection data stays in the store."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from .. import held_fs, record_formats, reserved_paths, vault
from . import authority, connection, governance

STAGE_PREFIX = ".exomem-collection-stage-"
ASIDE_PREFIX = ".exomem-collection-aside-"
_DESCRIPTOR = "collection-publication"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pair(version, digest):
    return [version, digest] if digest is not None else None


@dataclass
class _Publication:
    path: str
    descriptor: dict
    parent: held_fs.HeldDirectory | None = None
    projection: dict | None = None
    context: tuple | None = None


class PublicationBatch:
    """Stage in one canonical transaction, then install after its COMMIT."""

    def __init__(self, writer):
        self.writer = writer
        self.identity = store_identity(writer.connection)
        self.stack = ExitStack()
        self.filesystem = None
        self.publications = {}
        self.parents = {}
        self.parent_paths = {}
        self.responses = []
        self.cleanup = []
        self.pending = False
        self.committed = False
        self.contexts = {}
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

    def eligible(self, projection):
        if (projection["collection_id"] != self.deferred_create
                and authority.projection_eligible(self.writer, projection)):
            return True
        self.pending = True
        return False

    def _render_context(self, projection, manifest=None):
        conn = self.writer.connection
        if projection["kind"] == "item":
            row = conn.execute("SELECT governance_json,updated_txn,values_json FROM items WHERE row_id=?",
                               (projection["row_id"],)).fetchone()
        elif projection["kind"] == "manifest":
            if manifest is not None:
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
            version, text = render_view(conn, self.identity, projection, manifest)
            digest = _digest(text.encode())
        return {"path": projection["path"], "collection_id": cid, "kind": projection["kind"],
                "row_id": projection.get("row_id"), "row_version": version,
                "sha256": digest, "governance_json": metadata}

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
        pairs = {tuple(pair) for name in ("published", "pending")
                 if (pair := _pair(previous[name + "_row_version"], previous[name + "_sha256"]))}
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
            if not matches and not invalid:
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
        displaced_pairs = {tuple(pair) for name in ("previous_published", "previous_pending")
                           if (pair := descriptor.get(name)) is not None}
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
                if digest not in {previous["published_sha256"], previous["pending_sha256"]}:
                    source = {"previous_published": _pair(previous["published_row_version"], previous["published_sha256"]),
                              "previous_pending": _pair(previous["pending_row_version"], previous["pending_sha256"]),
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
                descriptor = json.loads(previous["install_json"])
                descriptor["render_context"] = self._render_context(projection, manifest)
                return json.dumps(descriptor, separators=(",", ":"))
            descriptor = {
                "token": secrets.token_hex(16),
                "previous_published": _pair(
                    previous["published_row_version"], previous["published_sha256"]
                )
                if previous
                else None,
                "previous_pending": _pair(
                    previous["pending_row_version"], previous["pending_sha256"]
                )
                if previous
                else None,
                "preserved": preserved,
                "source_contexts": previous.get("source_contexts", []) if previous else [],
            }
            publication = self.publications[path] = _Publication(path, descriptor)
        publication.descriptor["render_context"] = self._render_context(projection, manifest)
        publication.projection = projection
        publication.context = (projection["collection_id"], manifest.manifest_version.hash)
        self.contexts[publication.context] = manifest
        return json.dumps(publication.descriptor, separators=(",", ":"))

    def stage_all(self):
        publications = [publication for publication in self.publications.values()
                        if publication.projection is not None and self.eligible(publication.projection)]
        pairs = {path: pair for path, pair in self.pair_cleanup.items()
                 if self.eligible(self.captured[path])}
        if not pairs and not publications:
            return
        with self._boundary():
            for path, (parent, leaf, digest) in pairs.items():
                try:
                    self._cleanup_stage_pair(path, parent, leaf, digest)
                except (held_fs.HeldFsError, OSError):
                    self.pending = True
            for publication in publications:
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
                except (held_fs.HeldFsError, OSError):
                    self.pending = True

    def _cleanup_stage_pair(self, path, parent, leaf, digest):
        proof = self.baselines[path]
        descriptor = json.loads(proof["install_json"])
        if leaf != STAGE_PREFIX + descriptor["token"] or digest != proof["pending_sha256"]:
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
                "version": proof["pending_row_version"],
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
            descriptor = json.loads(previous["install_json"])
            parent = self._parent(previous["path"])
            result = self._fs().file(parent, Path(previous["path"]).name)
            if result.ok:
                with result.require() as file:
                    digest = _digest(self._fs().read(file).require())
                for source in [descriptor, *descriptor.get("preserved", [])]:
                    pair = next(
                        (
                            source.get(name)
                            for name in ("previous_published", "previous_pending")
                            if source.get(name) and source[name][1] == digest
                        ),
                        None,
                    )
                    if pair is not None:
                        # A failed install may leave an older committed render at the target.
                        self.writer._execute(
                            "UPDATE projection_state SET published_row_version=?,published_sha256=? WHERE path=?",
                            (*pair, previous["path"]),
                        )
                        previous["published_row_version"], previous["published_sha256"] = pair
                        break
            elif result.error.code != "MISSING":
                raise result.error
            return preserved
        return []

    def _asides(self, publication):
        fs = self._fs()
        expected = {
            pair[1]
            for name in ("previous_published", "previous_pending")
            if (pair := publication.descriptor.get(name)) is not None
        }
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
            "SELECT install_json,pending_sha256 FROM projection_state WHERE path=?",
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
        stage_leaf = STAGE_PREFIX + publication.descriptor["token"]
        with (
            nullcontext(None) if retired else fs.file(parent, stage_leaf, access="mutate").require()
        ) as stage:
            if not retired and _digest(fs.read(stage).require()) != committed[1]:
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
        if self.cleanup or publications:
            with self._boundary():
                for projection, parent, leaf, digest in self.cleanup:
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
            "previous_published": _pair(
                previous["published_row_version"], previous["published_sha256"]
            ),
            "previous_pending": _pair(previous["pending_row_version"], previous["pending_sha256"]),
            "preserved": preserved,
            "source_contexts": previous.get("source_contexts", []),
        }
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
        """Remove journal-free staging only; unknown displaced bytes stay private."""
        previous = self.previous(path)
        if previous is not None and not self.eligible(previous):
            return
        if not authority.orphan_cleanup_eligible(self.writer, Path(path).parent):
            self.pending = True
            return
        parent = self._parent(path)
        processed = 0
        for child in self._fs().children(parent).require():
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
            if leaf.startswith(STAGE_PREFIX) or preserved or expected:
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


def render_view(conn, identity, projection, manifest):
    """Render an indexed canonical reference; the caller owns its read snapshot and context."""
    if projection["kind"] == "item":
        version, payload, key, values, body = conn.execute(
            "SELECT row_version,payload_hash,item_key,values_json,body FROM items WHERE row_id=?",
            (projection["row_id"],),
        ).fetchone()
        return version, record_formats.render_markdown_item(
            manifest,
            json.loads(values),
            key,
            body,
            view_stamp=stamp(identity, version, payload),
        )
    if projection["kind"] == "manifest":
        version, text, payload = conn.execute(
            "SELECT m.manifest_version,m.manifest_text,m.manifest_hash FROM collection_manifests m JOIN collections c "
            "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
            (projection["collection_id"],),
        ).fetchone()
        return version, manifest_view(text, stamp(identity, version, payload))
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
