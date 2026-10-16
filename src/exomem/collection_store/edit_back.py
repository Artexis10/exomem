"""Edit-back: an owner's edit of a store collection view becomes a governed update or a held correction.

OpenSpec move-structured-collections-to-sqlite design §6 and §16 A1, A2. A reconcile
pass reads every item, manifest and log view at startup and every 60 s, with a
stat-identity fast path, outside the store's write transaction. A changed view settles
first: it acts once its bytes stay the same for 2 s, and holds a parse or validation
failure only once the view stays invalid for 10 s, so a typing storm leaves no hold.
Classification runs the stamp rules first, then the §6 rows. A valid edit at the current
base becomes ONE governed update through the ordinary writer leaf (``update_record``, a
Planning ``triage``, or ``revise_collection`` for a manifest), recorded as a ``view_edit``
transition by ``owner:view-edit``. A log view is classified block by block: each edited
block is one such update of its row. Everything else is a held view correction, keyed so
that a repeat of the same observation adds nothing.

Only the serving store thread runs a pass, as the writer-lease holder, and only while the
release enables ``collection-views-v1`` (``capability.collection_views_enabled``). A
collection behind a closed route fence is skipped. A vault without the mode marker never
reaches this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from .. import held_fs, memory_refs, planning, record_formats, records, vault
from .. import structured_collections as collections
from . import connection, log_view, views

logger = logging.getLogger(__name__)

#: How long a changed view stays unchanged before edit-back acts on it (design §6).
SETTLE_SECONDS = 2.0
#: How long a view stays invalid before it is held (design §6).
INVALID_SECONDS = 10.0
#: How often a pass reads every view, whether or not a watcher reported a change (§16 A1).
FULL_PASS_SECONDS = 60.0
#: The audit actor of every view edit (design §2, ``txns.actor``).
ACTOR = "owner:view-edit"
#: The audited operation of a view edit; Planning receipts keep their update or triage name.
OPERATION = "view_edit"

#: Classification of bytes that carry the row's own values and body: re-render, no transaction.
SAME = "SAME"
# The held view-correction codes design §6 and §8 C5/C21 fix.
CONFLICT = "VIEW_CONFLICT"
INVALID = "VIEW_INVALID"
FOREIGN = "VIEW_FOREIGN"
DELETED = "VIEW_DELETED"
UNBOUND = "VIEW_UNBOUND"

#: PROVISIONAL (operator choice O2, not yet ruled): an invalid item or manifest view keeps the
#: owner's bytes at the view path, held in place, and re-renders only when the hold is
#: discarded. Until then get_page answers that path as absent for every caller, because its
#: bytes are not the current render. False re-renders at once, displacing the bytes into the hold.
KEEP_INVALID_IN_PLACE = True

#: The view kinds edit-back reads; the others are generated read-only output.
# nosemgrep: ep-word-membership -- The projection_state.kind CHECK fixes these kinds.
EDITABLE_KINDS = ("item", "manifest", "log")
#: The kinds whose invalid edit stays at its view path (O2). A log view shows every row, so a hold
#: kept there would keep every later row out of it; a log view's holds take its bytes aside instead.
# nosemgrep: ep-word-membership -- The projection_state.kind CHECK fixes these kinds.
IN_PLACE_KINDS = ("item", "manifest")

# The issues that say why a log view or block is held and how the owner goes on (design §6, §16 A2).
_UNSTAMPED = {
    "code": "VIEW_UNSTAMPED", "reason": "the edited log view carries no view stamp, so it names no row version",
    "remedy": "The log view shows the stored rows again, with a stamp in every block. Make the edit again in "
              "that view and save it, then discard this correction.",
}
_FRAME_CHANGED = {
    "code": "VIEW_FRAME_CHANGED", "reason": "the text outside the log's item section changed",
    "remedy": "The store keeps the log's frame as it was imported or created and applies no edit to it yet. "
              "Discard this correction to keep the stored frame.",
}
_BLOCK_TEXT = {
    "code": "VIEW_BLOCK_TEXT", "reason": "a log block holds text that is none of its declared values",
    "remedy": "The store keeps a block's declared values only. Copy the text where it belongs, then discard "
              "this correction.",
}

_HEX12 = re.compile(r"[0-9a-f]{12}")
#: The id shape of a view correction an earlier release held, before ids were UUID-shaped.
_LEGACY_HELD_ID = re.compile(r"[0-9a-f]{24}")


def legacy_held_id(reference) -> bool:
    """Whether ``reference`` is a view-correction id an earlier release gave (it checks shape only)."""
    return isinstance(reference, str) and _LEGACY_HELD_ID.fullmatch(reference) is not None


def kept_in_place(code: str, kind: str) -> bool:
    """Whether a hold of ``code`` leaves the owner's bytes at an item or manifest view path."""
    return KEEP_INVALID_IN_PLACE and code == INVALID and kind in IN_PLACE_KINDS


def whole_log_candidate() -> dict:
    """What a hold of a whole log view would change: every row the log shows (``governance.held_targets``)."""
    return {"action": "view-edit", "item_key": None, "view": "log"}


def held_id(path: str, *, base: int | None = None, raw: bytes | None = None) -> str:
    """A hold's stable identity, UUID-shaped like every held reference (design §8 C10).

    Bytes still at the view path are keyed by the path and their base row version, so a
    later save at that base replaces the hold in place. Displaced bytes keep content
    identity, so the same bytes displaced twice are held once.
    """
    if raw is not None:
        material = b"exomem-view-displaced:v1\0" + path.encode() + b"\0" + raw
    else:
        material = f"exomem-view-at-path:v1\0{path}\0{'' if base is None else base}".encode()
    return str(uuid.UUID(bytes=hashlib.sha256(material).digest()[:16], version=5))


def request_id(path: str, base: int | None, digest: str) -> str:
    """The view edit's store request identity (design §4), so a replay of the same bytes is one transition."""
    return hashlib.sha256(f"exomem-view-edit:v1\0{path}\0{base}\0{digest}".encode()).hexdigest()


def why(path: str) -> str:
    reason = f"edited view {path}"
    return reason if len(reason.encode()) <= records._MAX_WHY_BYTES else "edited a collection view"


@dataclass(frozen=True)
class Classification:
    """What one view's bytes are. ``code`` None is a valid change at the current base.

    A log view with ``code`` None is classified block by block: ``blocks`` holds one
    classification per block, each naming its item's view ``path`` and its bytes ``raw``.
    """

    code: str | None
    stamp: dict | None = None
    base: int | None = None
    current: int | None = None
    change: dict | None = None
    issues: tuple = ()
    path: str | None = None
    raw: bytes | None = None
    blocks: tuple = ()


def _refused(error: collections.CollectionError) -> tuple:
    issues = error.details.get("issues") if isinstance(error.details, Mapping) else None
    found = [dict(issue) for issue in issues if isinstance(issue, Mapping)] if isinstance(issues, list) else []
    return tuple(found[: records._MAX_HELD_DIAGNOSTICS] or [{"code": error.code, "reason": error.reason}])


def _stamp(frontmatter) -> dict | None:
    stamp = frontmatter.get("exomem_view") if isinstance(frontmatter, Mapping) else None
    if (not isinstance(stamp, dict) or set(stamp) != {"s", "i", "v", "h"}
            or any(memory_refs.normalize_id(stamp.get(name)) is None for name in ("s", "i"))
            or type(stamp.get("v")) is not int or stamp["v"] < 1
            or not isinstance(stamp.get("h"), str) or _HEX12.fullmatch(stamp["h"]) is None):
        return None
    return stamp


def current_version(conn, projection) -> int | None:
    """The row or manifest version a view of ``projection`` renders now."""
    if projection["kind"] == "item":
        row = conn.execute("SELECT row_version FROM items WHERE row_id=?", (projection["row_id"],)).fetchone()
    elif projection["kind"] == "log":
        row = conn.execute("SELECT generation FROM collections WHERE collection_id=?",
                           (projection["collection_id"],)).fetchone()
    else:
        row = conn.execute("SELECT manifest_version FROM collections WHERE collection_id=?",
                           (projection["collection_id"],)).fetchone()
    return None if row is None else row[0]


def classify(writer, projection, raw: bytes, *, refusal: tuple = (), edit_back: bool = True) -> Classification:
    """Classify a view's bytes: the stamp rules of §16 A2 first, then the §6 rows.

    The caller holds a read snapshot or the mutation. ``refusal`` carries a leaf refusal
    edit-back met when it applied these bytes, which holds them as invalid; for a log view,
    one ``(block path, issues)`` pair per refused block. Without the edit-back release
    (``edit_back`` False), a store validates as it did before edit-back: without the
    Planning graph or the stored row, so such an edit waits on disk.
    """
    conn = writer.connection
    if projection["kind"] == "log":
        return _classify_log(writer, projection, raw, dict(refusal))
    try:
        text = raw.decode()
        frontmatter, body, _ = vault.parse_frontmatter(text, strict=True)
    except (UnicodeError, ValueError):
        frontmatter, body = None, ""
    stamp = _stamp(frontmatter)
    if stamp is None:
        # Only the exact imported bytes may be unstamped, and those are the published digest.
        return Classification(INVALID, None, None, current_version(conn, projection)
                              if projection["kind"] in EDITABLE_KINDS else None)
    identity = views.store_identity(conn)
    lineage = json.loads(conn.execute("SELECT value FROM store_meta WHERE key='lineage'").fetchone()[0])
    if stamp["s"] != identity["store_id"] or stamp["i"] not in {entry["instance_id"] for entry in lineage}:
        return Classification(FOREIGN, stamp, stamp["v"])
    if projection["kind"] == "summary":
        from . import derived_rows

        # Generated read-only output: hold any edit with its bytes, never parse it into rows.
        generation, derived = conn.execute("SELECT generation,derived FROM collections WHERE collection_id=?",
                                           (projection["collection_id"],)).fetchone()
        code = derived_rows.COLLECTION_DERIVED if derived else "SUMMARY_VIEW_READ_ONLY"
        return Classification(code, stamp, stamp["v"], generation)
    if projection["kind"] not in EDITABLE_KINDS:
        return Classification(INVALID, stamp, stamp["v"], projection["pending_row_version"])
    if projection["kind"] == "item":
        current, payload = conn.execute("SELECT row_version,payload_hash FROM items WHERE row_id=?",
                                        (projection["row_id"],)).fetchone()
    else:
        current, payload = conn.execute(
            "SELECT c.manifest_version,m.manifest_hash FROM collections c JOIN collection_manifests m "
            "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version WHERE c.collection_id=?",
            (projection["collection_id"],)).fetchone()
    if stamp["v"] < current:
        return Classification(CONFLICT, stamp, stamp["v"], current)
    if stamp["v"] != current or stamp["h"] != payload[:12]:
        # A stamp that was tampered with or copied never names the current base.
        return Classification(INVALID, stamp, stamp["v"], current)
    found = Classification(None, stamp, current, current)
    try:
        if projection["kind"] == "item":
            found = _item_change(writer, projection, frontmatter, body, found, edit_back)
        else:
            found = _manifest_change(writer, projection, frontmatter, body, found, edit_back)
    except collections.CollectionError as error:
        return Classification(INVALID, stamp, current, current, issues=_refused(error))
    if refusal and found.code is None:
        return Classification(INVALID, stamp, current, current, issues=tuple(refusal))
    return found


def _view_values(writer, manifest, row_id: int, frontmatter, body: str) -> tuple[str, dict, str]:
    """The item key, declared values and authored body an item view's bytes carry.

    Refuses a changed system field. The managed presentation block is not authored body.
    """
    from .legacy_import import _body

    key, schema_version = writer.connection.execute(
        "SELECT item_key,schema_version FROM items WHERE row_id=?", (row_id,)).fetchone()
    profile = record_formats.profile_for(manifest.semantic_profile)
    system = {"type": profile.item_type, "collection_id": manifest.collection_id,
              profile.item_id_property: key, "schema_version": schema_version}
    if any(frontmatter.get(name) != value for name, value in system.items()):
        raise collections.CollectionError("RESERVED_RECORD_FIELD", "a system field of the view changed")
    values = {name: record_formats._json_value(value) for name, value in frontmatter.items()
              if name not in system and name != "exomem_view"}
    return key, values, _body(manifest, values, body)


def _changes(old: Mapping, values: Mapping) -> tuple[dict, list]:
    """The fields an edit sets and the fields it deletes, relative to ``old``."""
    return ({name: value for name, value in values.items() if name not in old or old[name] != value},
            sorted(set(old) - set(values)))


def _item_change(writer, projection, frontmatter, body, found, edit_back):
    row, manifest, declared = writer._collection(projection["collection_id"])
    key, values, body = _view_values(writer, manifest, projection["row_id"], frontmatter, body)
    before = writer._item(manifest.collection_id, key)
    old = json.loads(before["values_json"])
    if values == old and body == before["body"]:
        return Classification(SAME, found.stamp, found.base, found.current)
    if not edit_back:
        writer._validate(manifest, declared, key, values, operation="update", validate_graph=False)
        return found
    changes, deleted = _changes(old, values)
    operation = "update"
    if (declared.kind == "intended" and changes and not deleted and body == before["body"]
            and set(changes) <= planning.TRIAGE_FIELDS):
        operation = "triage"
    final = {name: value for name, value in old.items() if name not in deleted}
    final.update(changes)
    # The same validation the leaf runs, with the hierarchy graph and the stored row as its base.
    writer._validate(manifest, declared, key, final, before=old, operation=operation)
    return Classification(None, found.stamp, found.base, found.current, change={
        "item_key": key, "changes": changes, "delete_fields": deleted,
        "body": None if body == before["body"] else body, "operation": operation,
        "expected_container_hash": writer._container(row), "expected_item_version": writer._version(before),
    })


def _manifest_change(writer, projection, frontmatter, body, found, edit_back):
    row, manifest, _ = writer._collection(projection["collection_id"])
    stored = writer.connection.execute(
        "SELECT manifest_text FROM collection_manifests WHERE collection_id=? AND manifest_version=?",
        (row["collection_id"], row["manifest_version"])).fetchone()[0]
    edited = {name: value for name, value in frontmatter.items() if name != "exomem_view"}
    text = "---\n" + vault.serialize_frontmatter(edited) + "\n---\n" + ("\n" + body if body else "")
    # Parsed, not as text: reordering the manifest's properties is formatting, not a revise.
    if _parsed_view(text, found.stamp) == _parsed_view(stored, found.stamp):
        return Classification(SAME, found.stamp, found.base, found.current)
    proposed = collections.parse_manifest_bytes(writer.root, projection["path"], text.encode())
    if proposed.collection_id != manifest.collection_id and edit_back:  # before edit-back, such an edit waited
        raise collections.CollectionError("RESERVED_RECORD_FIELD", "a system field of the view changed")
    from . import summary

    if proposed.derived != manifest.derived or (proposed.view_mode != manifest.view_mode
                                                and summary.populated(writer.connection, manifest.collection_id)):
        return Classification(summary.MODE_CHANGE_UNSUPPORTED, found.stamp, found.base, found.current)
    return Classification(None, found.stamp, found.base, found.current, change={
        "manifest_text": text, "expected_manifest_hash": manifest.manifest_version.hash,
        "expected_container_hash": writer._container(row),
    })


def _parsed_view(manifest_text: str, stamp) -> tuple:
    frontmatter, body, _ = vault.parse_frontmatter(views.manifest_view(manifest_text, stamp), strict=True)
    return frontmatter, body.strip()


def _classify_log(writer, projection, raw, refusals) -> Classification:
    """Classify a log view (design §6): its frame and stamps first, then block by block.

    The whole view is held when it does not parse, its frame changed, a block carries another
    store's stamp, or it shows stored rows and no block carries a stamp. Otherwise each block
    is classified by the A2 stamp rules against its own row, and each stored row with no block
    is a removed block.
    """
    conn = writer.connection
    _, manifest, declared = writer._collection(projection["collection_id"])
    generation, frame_json = conn.execute("SELECT generation,log_frame_json FROM collections WHERE collection_id=?",
                                          (manifest.collection_id,)).fetchone()
    try:
        parsed = record_formats.MarkdownLogAdapter(writer.root, manifest).read_bytes(raw)
        start, end = log_view.bounds(manifest, raw, parsed.records)
    except collections.CollectionError as error:
        return Classification(INVALID, None, generation, generation, issues=_refused(error))
    except UnicodeError:
        return Classification(INVALID, None, generation, generation,
                              issues=({"code": "INVALID_RECORD_SOURCE", "reason": "markdown log is not UTF-8"},))
    if parsed.diagnostics:
        return Classification(INVALID, None, generation, generation, issues=tuple(
            {"code": diagnostic.code, "reason": diagnostic.reason} for diagnostic in parsed.diagnostics))
    frame = log_view.load(frame_json, manifest)
    if raw[:start] != frame.text.encode() or raw[end:] != frame.suffix.encode():
        return Classification(INVALID, None, generation, generation, issues=(_FRAME_CHANGED,))
    blocks = [(record, raw[record.span.start:record.span.end]) for record in parsed.records]
    stamps = [record_formats.read_log_block_stamp(block) for _, block in blocks]
    identity = views.store_identity(conn)
    lineage = {entry["instance_id"] for entry in
               json.loads(conn.execute("SELECT value FROM store_meta WHERE key='lineage'").fetchone()[0])}
    for stamp in stamps:
        if stamp is not None and (stamp["s"] != identity["store_id"] or stamp["i"] not in lineage):
            return Classification(FOREIGN, stamp, stamp["v"])
    stored = {row["item_key"]: row for row in log_view.rows(conn, manifest)}
    if all(stamp is None for stamp in stamps) and any(record.identity.key in stored for record, _ in blocks):
        # Only the exact imported bytes may be unstamped, and those are the published digest.
        return Classification(INVALID, None, generation, generation, issues=(_UNSTAMPED,))
    found, rendered, bound = [], [], set()
    for (record, block), stamp in zip(blocks, stamps, strict=True):
        row = None if record.identity.inferred else stored.get(record.identity.key)
        if row is None:
            found.append(Classification(UNBOUND, stamp, path=projection["path"], raw=block))
            continue
        bound.add(row["item_key"])
        if stamp is None:
            found.append(Classification(INVALID, None, None, row["row_version"], path=row["view_path"], raw=block,
                                        issues=(_UNSTAMPED,)))
            continue
        rendered.append([row["row_id"], min(stamp["v"], row["row_version"])])
        found.append(_classify_block(writer, manifest, declared, frame, row, record, stamp, block,
                                     refusals.get(row["view_path"])))
    # The newest row version a block of this buffer was rendered at: the buffer is at least that recent.
    newest = conn.execute(
        "SELECT MAX(v.txn_id) FROM json_each(?) j JOIN item_versions v "
        "ON v.row_id=json_extract(j.value,'$[0]') AND v.row_version=json_extract(j.value,'$[1]')",
        (json.dumps(rendered),)).fetchone()[0]
    for key, row in stored.items():
        # A row committed after the newest render this buffer shows was never in it, so it was not removed.
        if key in bound or (newest is not None and row["created_txn"] > newest):
            continue
        found.append(Classification(DELETED, None, row["row_version"], row["row_version"], path=row["view_path"],
                                    raw=b""))
    return Classification(None, None, generation, generation, blocks=tuple(found))


def _block_lines(data: bytes) -> list[bytes]:
    """A block's lines without trailing spaces or blank lines: what a formatting-only edit keeps."""
    return [line.rstrip() for line in data.splitlines() if line.strip()]


def _renders_block(manifest, frame, key, values, stamp, block) -> bool:
    """Whether ``block`` is the store's render of ``values`` under ``stamp``, up to formatting."""
    rendered = record_formats.render_markdown_log_item(manifest, values, key, frame.newline, view_stamp=stamp)
    return _block_lines(rendered.encode()) == _block_lines(block)


def _classify_block(writer, manifest, declared, frame, row, record, stamp, block, refused) -> Classification:
    """One log block bound to a stored row, by the A2 stamp rules, then as one item (§6)."""
    from . import typed_storage

    conn, path, current = writer.connection, row["view_path"], row["row_version"]

    def found(code, **fields):
        return Classification(code, stamp, stamp["v"], current, path=path, raw=block, **fields)

    if stamp["v"] < current:
        # Edit-back applied this very block already, or the owner saved an untouched block of an older render.
        digest = hashlib.sha256(block).hexdigest()
        if conn.execute("SELECT 1 FROM txns WHERE request_id=?", (request_id(path, stamp["v"], digest),)).fetchone():
            return found(SAME)
        payload = conn.execute("SELECT payload_hash FROM item_versions WHERE row_id=? AND row_version=?",
                               (row["row_id"], stamp["v"])).fetchone()
        if payload is not None and payload[0][:12] == stamp["h"] and _renders_block(
                manifest, frame, row["item_key"], typed_storage.version_values(conn, row["row_id"], stamp["v"])[0],
                stamp, block):
            return found(SAME)
        return found(CONFLICT)
    if stamp["v"] != current or stamp["h"] != row["payload_hash"][:12]:
        # A stamp that was tampered with or copied never names the current base.
        return found(INVALID)
    try:
        values = collections.normalize_item_values(manifest.schema, record.values)
    except collections.CollectionError as error:
        return found(INVALID, issues=_refused(error))
    if values == row["values"]:
        return found(SAME) if _renders_block(manifest, frame, row["item_key"], values, stamp, block) \
            else found(INVALID, issues=(_BLOCK_TEXT,))
    if refused:
        return found(INVALID, issues=tuple(refused))
    changes, deleted = _changes(row["values"], values)
    final = {name: value for name, value in row["values"].items() if name not in deleted}
    final.update(changes)
    try:
        # The same validation the leaf runs, with the stored row as its base.
        writer._validate(manifest, declared, row["item_key"], final, before=row["values"], operation="update")
    except collections.CollectionError as error:
        return found(INVALID, issues=_refused(error))
    return found(None, change={
        "item_key": row["item_key"], "changes": changes, "delete_fields": deleted, "body": None,
        "operation": "update", "expected_item_version": writer._version(row),
    })


def resumed_update(writer, held, manifest, before) -> dict:
    """The Records update a held ``VIEW_CONFLICT`` correction resumes: the owner's edit, rebased.

    The update carries only what the held save changed from the row version it was based on,
    so resuming never reverts a later change to a field the owner did not touch. Only a
    stale-buffer save resumes; other corrections are resolved by discarding them or by a later
    valid edit of the view. The caller's guards and field overrides still apply.
    """
    from . import typed_storage

    try:
        frontmatter, body, _ = vault.parse_frontmatter(held["held_bytes"].decode(), strict=True)
        base, base_body = typed_storage.version_values(writer.connection, before["row_id"], held["base_row_version"])
    except (UnicodeError, ValueError, TypeError, typed_storage.TypedStorageError):
        frontmatter = None
    if held["code"] != CONFLICT or not isinstance(frontmatter, Mapping):
        raise records._held_not_found()
    key, values, body = _view_values(writer, manifest, before["row_id"], frontmatter, body)
    changes, deleted = _changes(base, values)
    return {"action": "update", "item_key": key, "changes": changes, "delete_fields": deleted,
            "body": None if body == base_body else body}


@dataclass(frozen=True)
class Observation:
    """One view as a pass found it on disk; ``digest`` None is a missing file."""

    path: str
    projection: dict
    digest: str | None
    stat: str | None
    raw: bytes | None = None
    refusal: tuple = ()

    @property
    def own(self) -> bool:
        """The view's own render. Adopted bytes still at the path are not: they await a re-render."""
        return self.digest is not None and self.digest in {
            self.projection["published_sha256"], self.projection["pending_sha256"]}


def _served(writer, cid) -> bool:
    """Whether this release serves the collection's views: eligible, and behind no closed route fence."""
    from . import authority, capability

    if not authority.projection_eligible(writer, {"collection_id": cid}):
        return False
    try:
        _, manifest, _ = writer._collection(cid)
    except (collections.CollectionError, connection.CollectionStoreError):
        return False
    return capability.summary_route_open(writer.root, manifest)


def _stat(version) -> str:
    return f"{version.identity.device}:{version.identity.inode}:{version.size}:{version.mtime_ns}"


def observe(writer, paths=None) -> list[Observation]:
    """Read the item, manifest and log views a pass checks, outside the write transaction.

    A view whose stat identity matches the one recorded with its current bytes costs one
    stat. ``paths`` limits the pass to the views still settling. A collection behind a
    closed route fence is not read at all.
    """
    with writer.read_snapshot():
        if paths is None:
            cursor = writer.connection.execute(
                "SELECT * FROM projection_state WHERE kind IN ('item','manifest','log') ORDER BY path")
        else:
            cursor = writer.connection.execute(
                "SELECT * FROM projection_state WHERE kind IN ('item','manifest','log') AND path IN "
                "(SELECT value FROM json_each(?)) ORDER BY path", (json.dumps(sorted(paths)),))
        names = [column[0] for column in cursor.description]
        rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        admitted = {cid: _served(writer, cid) for cid in {row["collection_id"] for row in rows}}
    found = []
    with ExitStack() as stack:
        fs = stack.enter_context(held_fs.acquire(writer.root).require())
        parents = {}
        for row in rows:
            if not admitted[row["collection_id"]]:
                continue
            relative = str(Path(row["path"]).parent)
            try:
                if relative not in parents:
                    opened = fs.parent(relative)
                    parents[relative] = stack.enter_context(opened.require()) if opened.ok else None
                parent = parents[relative]
                if parent is None:
                    found.append(Observation(row["path"], row, None, None))
                    continue
                opened = fs.file(parent, Path(row["path"]).name)
                if not opened.ok:
                    if opened.error.code != "MISSING":
                        continue
                    found.append(Observation(row["path"], row, None, None))
                    continue
                with opened.require() as file:
                    before = file.version()
                    if _stat(before) == row["stat_identity"] and row["state"] in ("current", "held"):
                        continue
                    raw = fs.read(file).require()
                    if file.version() != before:
                        continue  # still being written; the next pass reads it again
            except (held_fs.HeldFsError, OSError):
                continue
            found.append(Observation(row["path"], row, hashlib.sha256(raw).hexdigest(), _stat(before), raw))
    return found


@dataclass
class _Seen:
    digest: str | None
    since: float
    invalid_since: float | None = None
    retry: float | None = None  # a settled view waits until then: invalid, not yet held, or refused

    def due(self) -> float:
        return self.since + SETTLE_SECONDS if self.retry is None else max(self.since + SETTLE_SECONDS, self.retry)


class EditBack:
    """One served store's edit-back passes, with each changed view's settle state.

    Times are monotonic seconds: the settle windows must not move with a wall clock that
    can step backwards.
    """

    def __init__(self):
        self._seen: dict[str, _Seen] = {}
        self._next_full = 0.0

    def run(self, writer, *, now: float, mutate: Callable | None = None) -> float:
        """Run the pass that is due and return the seconds until the next one."""
        mutate = mutate or (lambda method, *args, **kwargs: method(*args, **kwargs))
        full = now >= self._next_full
        if not full and self.wait(now) > 0:
            return self.wait(now)
        observations = observe(writer, None if full else set(self._seen))
        if full:
            self._next_full = now + FULL_PASS_SECONDS
            present = {observation.path for observation in observations}
            # A view that matched its stat identity again is no longer changing.
            for path in [path for path in self._seen if path not in present]:
                del self._seen[path]
        own, settled = [], []
        for observation in observations:
            if observation.own:
                self._seen.pop(observation.path, None)
                own.append(observation)
                continue
            seen = self._seen.get(observation.path)
            if seen is None or seen.digest != observation.digest:
                self._seen[observation.path] = _Seen(observation.digest, now, seen.invalid_since if seen else None)
            elif now >= seen.due():
                settled.append((observation, seen))
        if own:
            mutate(writer.record_view_observations, own)
        for observation, seen in settled:
            try:
                acted = self._act(writer, observation, seen, now, mutate)
            except (collections.CollectionError, connection.CollectionStoreError):
                # One view the store cannot act on now must not starve the others; it is retried a pass later.
                logger.warning("a collection view edit could not be applied; retrying later", exc_info=True)
                seen.retry = now + FULL_PASS_SECONDS
                continue
            if acted:
                self._seen.pop(observation.path, None)
        return self.wait(now)

    def wait(self, now: float) -> float:
        """Seconds until a pass is due: the next full pass, or a changed view that may have settled."""
        deadlines = [self._next_full, *(seen.due() for seen in self._seen.values())]
        return max(0.0, min(deadlines) - now)

    def _act(self, writer, observation, seen, now, mutate) -> bool:
        """Act on one settled view; False keeps it settling, as an invalid view not yet held."""
        projection = observation.projection
        if observation.digest is None:
            if projection["published_sha256"] is not None:  # else it was never installed
                mutate(writer.reconcile_views, paths={observation.path: observation})
            return True
        with writer.read_snapshot():
            found = classify(writer, projection, observation.raw)
        if found.code is None and projection["kind"] == "log":
            # Each edited block is its own governed update; the pass then holds the other blocks.
            refused = []
            for block in found.blocks:
                if block.code is None:
                    try:
                        mutate(writer.apply_view_edit, observation, block)
                    except collections.CollectionError as error:
                        refused.append((block.path, _refused(error)))
            observation = Observation(observation.path, projection, observation.digest, observation.stat,
                                      observation.raw, tuple(refused))
            if refused or any(block.code == INVALID for block in found.blocks):
                found = Classification(INVALID)
        elif found.code is None:
            try:
                mutate(writer.apply_view_edit, observation, found)
                return True
            except collections.CollectionError as error:
                observation = Observation(observation.path, projection, observation.digest, observation.stat,
                                          observation.raw, _refused(error))
                found = Classification(INVALID)
        if found.code == INVALID:
            seen.invalid_since = seen.invalid_since if seen.invalid_since is not None else seen.since
            if now - seen.invalid_since < INVALID_SECONDS:
                seen.retry = seen.invalid_since + INVALID_SECONDS
                return False
        mutate(writer.reconcile_views, paths={observation.path: observation})
        return True


def attention_findings(vault_root) -> tuple[list, str | None]:
    """The held view corrections the caller may see, for the attention queue of a store vault.

    Returns the findings and None, or no findings and ``"unknown"`` when the store that
    holds them cannot answer. Withheld corrections are absent, as if they did not exist.
    """
    root = Path(vault_root).resolve()
    from .preview import bound_writer
    from .runtime import _server

    writer = bound_writer(root)
    try:
        if writer is not None:
            return _findings(writer), None
        server = _server(root)
        if server is None:
            return [], "unknown"
        return server.call(lambda: _findings(bound_writer(root)), wake_maintenance=False), None
    except Exception:  # noqa: BLE001 - an unavailable store is reported as unknown, never as none
        return [], "unknown"


def _findings(writer):
    from ..audit import AuditFinding
    from . import capability

    findings = []
    with writer.read_snapshot():
        operation = writer._operation
        for (cid,) in writer.connection.execute("SELECT collection_id FROM collections ORDER BY collection_id").fetchall():
            try:
                _, manifest, _ = writer._collection(cid)
                if not capability.summary_route_open(writer.root, manifest):
                    continue  # a collection this release does not serve has no actionable correction
                released = operation.inspection_selection(cid).released
            except (collections.CollectionError, connection.CollectionStoreError):
                continue  # a collection the caller cannot see is absent, not an error
            ids = sorted(subject.row_id for subject in released if isinstance(subject.row_id, str))
            if not ids:
                continue
            for held, path, code in writer.connection.execute(
                    "SELECT held_id,view_path,code FROM held_candidates WHERE collection_id=? AND kind='view-correction' "
                    "AND held_id IN (SELECT value FROM json_each(?)) ORDER BY held_id", (cid, json.dumps(ids))):
                findings.append(AuditFinding(
                    category="collection_view_corrections", path=path, severity="warn",
                    detail=f"{code}: an owner edit of a collection view is held for review",
                    meta={"collection_id": cid, "held_id": held, "code": code},
                ))
    return findings
