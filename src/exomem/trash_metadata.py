"""The existing Trash placement format, shared by recovery and reference accounting."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from . import held_fs, originals, reserved_paths
from .kbdir import managed_split, managed_split_parts

if TYPE_CHECKING:
    from .graph_sync import GraphLifecycleTransition
    from .media_store import ReferencePublication
    from .vault import PlannedWrite

# These names belong to the fixed vault placement format, not a content vocabulary.
TRASH_SUBPATH = "_trash"
METADATA_SUFFIX = ".meta.json"
_completing: ContextVar[tuple[str, str, str] | None] = ContextVar("trash_retirement_completion", default=None)
_ending: ContextVar[str | None] = ContextVar("trash_retirement_ending", default=None)


@dataclass(frozen=True, slots=True)
class Retirement:
    source: originals.BinaryBinding
    companion: str
    before: str
    after: str
    trash: originals.BinaryBinding
    reference: originals.StoredObject


def prepare_retirement(
    vault: Path, snapshot: originals.OriginalSnapshot, trash_path: str, metadata: bytes,
) -> tuple[bytes, ReferencePublication]:
    """Prepublish the exact result of the existing reference-removal renderer."""
    import json

    from .media_store import prepare_reference
    from .relation_review import parse_exact_json_object
    from .vault import content_hash

    assert snapshot.reference is not None and snapshot.sidecar_hash is not None
    assert snapshot.binary_sha256 is not None and snapshot.binary_size is not None
    publication = prepare_reference(vault, snapshot, None, retirement_meta=trash_path + METADATA_SUFFIX)
    fields = parse_exact_json_object(metadata.decode("utf-8"))
    fields["original_retirement"] = {
        "schema": 1,
        "source": snapshot.path.relative_to(vault).as_posix(),
        "sha256": snapshot.binary_sha256,
        "size": snapshot.binary_size,
        "companion": snapshot.sidecar_path.relative_to(vault).as_posix(),
        "before": snapshot.sidecar_hash,
        "after": content_hash(publication.write.content),
        "trash": trash_path,
        "reference": snapshot.reference.fields(),
    }
    return json.dumps(fields, indent=2).encode("utf-8"), publication


@contextmanager
def completing_retirement(publication: ReferencePublication):
    """Only the prepared deletion may complete a witness whose before image still has its reference."""
    from .vault import content_hash

    proof = None if publication.retirement_meta is None else (
        publication.retirement_meta, publication.write.expected_hash, content_hash(publication.write.content))
    token = _completing.set(proof)
    try:
        yield
    finally:
        _completing.reset(token)


@contextmanager
def ending_retirement(metadata: str):
    """Mark the placement metadata that a recovery settles, so a batch leaves it alone while the page loses its backlink."""
    token = _ending.set(metadata)
    try:
        yield
    finally:
        _ending.reset(token)


def withdrawal(vault: Path, placement: dict[str, object], trash: str, restored: str) -> PlannedWrite | None:
    """The page write that ends a recovered original's retirement, or None when the recovery leaves none to end.

    Recovery removes the placement metadata, so a backlink left on the page would name nothing and read as an emptied
    entry. The page loses only the backlink, and keeps no stored-copy reference, as recovery has always left it.
    """
    from .vault import (
        PlannedWrite,
        content_hash,
        parse_frontmatter,
        read_guarded_text,
        render_frontmatter_document,
        serialize_frontmatter,
    )

    if "original_retirement" not in placement:
        return None
    proof = _retirement(placement["original_retirement"], root=trash, origin=placement.get("original_path"))
    if proof.source.path != restored:
        return None  # The original went elsewhere, so the page still names a deleted original at its source path.
    page = vault / proof.companion
    try:
        text, guard = read_guarded_text(vault, page)
    except FileNotFoundError:
        return None
    fields, body, _marker = parse_frontmatter(text, custody=True)
    if _backlink(fields) != trash + METADATA_SUFFIX:
        return None
    fields.pop("original_retirement_meta")
    rendered = render_frontmatter_document(serialize_frontmatter(fields), body)
    return PlannedWrite(page, rendered, guard=guard, expected_hash=content_hash(text))


def _retirement(value: object, *, root: str, origin: object) -> Retirement:
    # These are closed version-1 placement witness fields, not a content vocabulary.
    if not isinstance(value, dict) or set(value) != {
        "schema", "source", "sha256", "size", "companion", "before", "after", "trash", "reference",
    } or type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("Trash retirement has an unknown format")
    source, companion, trash = value["source"], value["companion"], value["trash"]
    if (not isinstance(source, str) or not isinstance(companion, str) or not isinstance(trash, str)
            or source != origin or trash != root or is_trash_path(source)
            or reserved_paths.classify_logical(source).blocked
            or companion not in originals.companion_paths(source)
            or not all(originals.is_sha256(value[key]) for key in ("sha256", "before", "after"))
            or type(value["size"]) is not int or value["size"] < 0):
        raise ValueError("Trash retirement has an invalid binding")
    reference = originals.stored_reference(value["reference"])
    if reference is None:
        raise ValueError("Trash retirement has an invalid recovery reference")
    return Retirement(originals.BinaryBinding(source, value["sha256"], value["size"]), companion,
                      value["before"], value["after"],
                      originals.BinaryBinding(trash, value["sha256"], value["size"]), reference)


def _document(vault: Path, path: str) -> tuple[dict[str, object], str, held_fs.FileVersion]:
    from . import runtime_resources
    from .relation_review import parse_exact_json_object

    with reserved_paths.open_generic_binary(vault, path) as source:
        size = source.version.size
        with runtime_resources.parser_preparation(size) as preparation:
            preparation.reserve_source(size)
            raw = source.stream.read(size + 1)
            if len(raw) != size:
                raise ValueError("Trash metadata changed during read")
            text = raw.decode("utf-8")
            preparation.reserve_parser(text, json_bytes=len(raw))
            parsed = parse_exact_json_object(text)
            source.require_current()
            return parsed, text, source.version


def _backlink(fields: dict[str, object]) -> str | None:
    path = fields.get("original_retirement_meta")
    if (not isinstance(path, str) or not path.endswith(METADATA_SUFFIX) or not is_trash_path(path)
            or PurePosixPath(path).as_posix() != path or any(part in {".", ".."} for part in PurePosixPath(path).parts)
            or reserved_paths.classify_logical(path).blocked):
        return None
    return path


def _companion_fields(text: str | None) -> dict[str, object]:
    from .vault import MALFORMED_FRONTMATTER_ERRORS, parse_frontmatter

    if text is None:
        return {}
    try:
        return parse_frontmatter(text, custody=True)[0]
    except MALFORMED_FRONTMATTER_ERRORS:
        # A retirement image is re-rendered from parsed fields, so a text that fails to parse never
        # hashes to a witness and has no backlink to honour. A RecursionError depends on stack depth and propagates.
        return {}


def _matches_retirement(text: str | None, fields: dict[str, object], metadata: str, proof: Retirement) -> bool:
    from .vault import content_hash

    return text is not None and _matches_retirement_fields(fields, metadata, content_hash(text), proof)


def _matches_retirement_fields(fields: dict[str, object], metadata: str, digest: str, proof: Retirement) -> bool:
    return (_backlink(fields) == metadata and originals.binary_binding(fields) == proof.source
            and "stored_object" not in fields and digest == proof.after)


def prepare_binding_changes(vault: Path, writes: Iterable[PlannedWrite]) -> list[PlannedWrite]:
    """Carry exact retirement edits, or invalidate before a new original can reuse its source path.

    The reciprocal companion backlink makes lookup bounded and independent of derived state. A batch that writes a
    new original and its page ends the deletion, so that page goes without its backlink (`withdraw_ended_backlinks`);
    any other page write keeps its bytes.
    """
    writes = list(writes)
    updates, ended = _binding_changes(vault, writes)
    return [*updates.values(), *_without_backlinks(vault, writes, ended)]  # New-original invalidation lands first.


def withdraw_ended_backlinks(vault: Path, writes: Iterable[PlannedWrite]) -> tuple[PlannedWrite, ...]:
    """The batch's writes, each page without a backlink that the batch ends, for a caller that binds page bytes first.

    A backlink is the one record of a deletion that survives an emptied Trash entry, so a page loses it only in the
    batch that writes a new original at its source. The batch withdraws the same backlinks itself, so a caller that
    publishes a catalogue of the page bytes takes them from here first and the two agree.
    """
    writes = list(writes)
    if not any(_backlink(_companion_fields(write.content)) for write in writes
               if isinstance(write.content, str) and write.path.suffix.lower() == ".md"):
        return tuple(writes)  # Only a page write that carries a backlink can lose one.
    _updates, ended = _binding_changes(vault, writes)
    return tuple(_without_backlinks(vault, writes, ended))


def _without_backlinks(vault: Path, writes: list[PlannedWrite], ended: set[str]) -> list[PlannedWrite]:
    import dataclasses

    from .vault import parse_frontmatter, render_frontmatter_document, serialize_frontmatter

    kept = []
    for write in writes:
        if write.path.is_relative_to(vault) and write.path.relative_to(vault).as_posix() in ended:
            fields, body, _marker = parse_frontmatter(write.content, custody=True)
            fields.pop("original_retirement_meta")
            write = dataclasses.replace(write, content=render_frontmatter_document(serialize_frontmatter(fields), body))
        kept.append(write)
    return kept


def _binding_changes(vault: Path, writes: list[PlannedWrite]) -> tuple[dict[str, PlannedWrite], set[str]]:
    """The metadata writes that carry or invalidate each witness, and the incoming pages whose backlink ends."""
    import json
    import os
    import unicodedata

    from .kbdir import managed_join
    from .vault import MALFORMED_FRONTMATTER_ERRORS, PlannedWrite, content_hash, read_guarded_bytes
    from .vault_store import read as store_read

    incoming = {write.path.relative_to(vault).as_posix(): write for write in writes if write.path.is_relative_to(vault)}
    relevant = {path for path in incoming if managed_split(path) is not None
                and not reserved_paths.classify_logical(path).blocked}
    companions = {path for path in relevant if path.lower().endswith(".md")}
    renewed: set[str] = set()  # The pages whose original this batch writes anew.
    for path in relevant:
        if not path.lower().endswith(".md") and not is_trash_path(path):
            renewed.update(originals.companion_paths(path))
    companions.update(renewed)
    # A backlink names only Trash placement metadata, read at its NFKC spelling, so with no Trash folder there is no
    # witness to carry or invalidate, and the batch reads nothing.
    updates: dict[str, PlannedWrite] = {}
    ended: set[str] = set()
    if not os.path.lexists(vault / unicodedata.normalize("NFKC", managed_join(TRASH_SUBPATH))):
        return updates, ended
    # One store snapshot reads every prior companion in a few queries, so a batch's cost does not grow per page.
    with store_read.request_snapshot(vault):
        store_read.prefetch_pages(vault, companions)
        for path in sorted(companions):
            try:
                # The batch's own guarded reader needs no reserved identity catalogue, which only a witness that a
                # backlink names requires: a vault that the catalogue cannot acquire still takes every other write.
                before = read_guarded_bytes(vault, vault / path)[0].decode("utf-8")
            except FileNotFoundError:
                before = None
            except UnicodeError:
                before = None  # A malformed prior companion cannot prove retirement.
            after_write = incoming.get(path)
            after = before if after_write is None else after_write.content
            if not isinstance(after, str):
                after = None
            before_fields, after_fields = _companion_fields(before), _companion_fields(after)
            for metadata in dict.fromkeys(link for image in (before_fields, after_fields) if (link := _backlink(image)) is not None):
                if _ending.get() == metadata:
                    continue  # The recovery that settles this metadata withdraws the backlink itself.
                if path in renewed and path in incoming and _backlink(after_fields) == metadata:
                    # A new original ends the deletion, so its page no longer names it, whatever the metadata holds.
                    ended.add(path)
                try:
                    fields, text, _version = _document(vault, metadata)
                except reserved_paths.ReservedPathLeafError as error:
                    if error.code != "MISSING":
                        raise
                    continue  # An emptied Trash entry leaves no witness, and its backlink keeps the page deleted.
                value = fields.get("original_retirement")
                if value is None:
                    continue  # A compatibility export invalidated this witness, and its original stays deleted.
                proof = _retirement(value, root=metadata[:-len(METADATA_SUFFIX)], origin=fields.get("original_path"))
                if path != proof.companion:
                    continue  # A forged locator never supplies mutation authority over another filing.

                old_match = _matches_retirement(before, before_fields, metadata, proof)
                new_match = _matches_retirement(after, after_fields, metadata, proof)
                if not old_match and not new_match:
                    continue
                if _completing.get() == (metadata, proof.before, proof.after) and new_match and proof.source.path not in incoming:
                    continue  # The deletion owns this one prepared reference-removal transition.
                if (proof.source.path not in incoming and old_match and _backlink(after_fields) == metadata
                        and originals.binary_binding(after_fields) == proof.source and "stored_object" not in after_fields):
                    if content_hash(after) == proof.after:
                        continue
                    try:
                        confirmed = _confirmed_by_trash(vault, proof)
                    except MALFORMED_FRONTMATTER_ERRORS:
                        confirmed = False
                    if not confirmed:
                        # Recovery or the owner ended this witness, and readers refuse it while its metadata remains. An
                        # edit neither refuses nor revives it, so it leaves alone the metadata that a recovery is settling.
                        continue
                    value["after"] = content_hash(after)
                else:
                    fields.pop("original_retirement")
                if metadata in incoming or metadata in updates:
                    raise ValueError("one batch contains conflicting Trash placement changes")
                updates[metadata] = PlannedWrite(vault / metadata, json.dumps(fields, indent=2), expected_hash=content_hash(text))
    return updates, ended


def without_retirement(vault: Path, path: str) -> bytes | None:
    """The placement metadata at `path` with its retirement witness invalidated, or None if it holds none.

    A copy for a reader that keeps no witness current invalidates it as a binding change does, so a later
    runtime cannot honour a witness that the reader's writes made stale.
    """
    import json

    try:
        fields, _text, _version = _document(vault, path)
    except ValueError:
        return None  # Metadata that does not parse confers no retirement, so its bytes go unchanged.
    if "original_retirement" not in fields:
        return None
    fields.pop("original_retirement")
    return json.dumps(fields, indent=2).encode("utf-8")


def is_trash_path(path: str) -> bool:
    below = managed_split_parts(PurePosixPath(path).parts)
    return below is not None and below[:1] == (TRASH_SUBPATH,)


def is_inside_trash(path: str, *, key: Callable[[str], str] | None = None) -> bool:
    """Whether the vault-relative ``path`` lies below the managed root's trash folder.

    The folder's own path is not below it, whereas ``is_trash_path`` accepts that path too. A ``key``
    (such as ``str.casefold``) compares the managed root's name and the trash folder's name
    through it, as ``managed_split`` does, for callers whose own rule is looser than byte equality.
    """
    below = managed_split(path, key=key)
    if below is None:
        return False
    if key is None:
        return below.startswith(TRASH_SUBPATH + "/")
    return key(below).startswith(key(TRASH_SUBPATH) + "/")


def destination_paths(target: Path) -> tuple[Path, Path, Path]:
    """The payload, placement metadata and canonical companion share one namespace."""
    return target, target.with_name(target.name + METADATA_SUFFIX), originals.sidecar_path(target)


def preserve_floor(source: str, name: str) -> str:
    """Keep the existing copy-stable RAW floor on a relocated Trash root."""
    from .governance.raw_protection import PREFIX, marked

    return PREFIX + name if marked(source) else name


def original_path(
    metadata: dict[str, object], *, trash_root: str, current_path: str,
    fallback_root: str | None = None,
) -> str:
    """Map a retained item through its declared original root and descendant suffix."""
    original = metadata.get("original_path") or fallback_root
    if not isinstance(original, str) or not original or reserved_paths.classify_logical(original).blocked:
        raise ValueError("Trash metadata has no valid original path")
    origin = PurePosixPath(original)
    root, current = PurePosixPath(trash_root), PurePosixPath(current_path)
    if current == root:
        return origin.as_posix()
    if current.is_relative_to(root):
        return (origin / current.relative_to(root)).as_posix()
    # A single binary's companion is adjacent, rather than a descendant of the binary.
    if current.as_posix() == originals.sidecar_path(Path(root)).as_posix():
        return originals.sidecar_path(Path(origin)).as_posix()
    raise ValueError("Trash item is outside its declared placement")


class PlacementCensus:
    """Reuse bounded placement parsing and recheck every acquired metadata version."""

    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self.placements: dict[str, dict[str, object] | None] = {}
        self.versions: dict[str, held_fs.FileVersion] = {}

    def _placement(self, root: str) -> dict[str, object] | None:
        if root not in self.placements:
            path = root + METADATA_SUFFIX
            try:
                parsed, _text, version = _document(self.vault, path)
                placement = {"original_path": parsed.get("original_path")}
                original_path(placement, trash_root=root, current_path=root)
                if "original_retirement" in parsed:
                    placement["original_retirement"] = _retirement(
                        parsed["original_retirement"], root=root, origin=placement["original_path"])
                self.versions[path] = version
                self.placements[root] = placement
            except reserved_paths.ReservedPathLeafError as error:
                if error.code != "MISSING":
                    raise
                self.placements[root] = None
        return self.placements[root]

    def retirement(self, binary: str) -> Retirement | None:
        """Read only the named Trash placement; the caller supplies its current binding."""
        if not is_trash_path(binary):
            return None
        placement = self._placement(binary)
        return None if placement is None else placement.get("original_retirement")

    def retired_source(self, companion: str, fields: dict[str, object], digest: str) -> bool:
        """Resolve one reciprocal retirement without a media index or archive scan."""
        metadata = _backlink(fields)
        if metadata is None:
            return False
        proof = self.retirement(metadata[:-len(METADATA_SUFFIX)])
        if (proof is None or companion != proof.companion
                or not _matches_retirement_fields(fields, metadata, digest, proof)):
            return False
        try:
            reserved_paths.inspect_generic_path(self.vault, proof.source.path)
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                raise
        else:
            return False
        return _confirmed_by_trash(self.vault, proof)

    def resolve(self, path: str, fields: dict[str, object]) -> originals.BinaryBinding | None:
        """Return the current payload path without rewriting the canonical companion."""
        try:
            return originals.companion_binding(fields, sidecar_path=path)
        except ValueError:
            if not is_trash_path(path):
                raise
        binary = originals.binary_binding(fields)
        if binary is None:
            return None
        # A Trash page that sat away from its companion path before its deletion raises the
        # `DisplacedReference` of that placement, for the census to confirm from the media ledger.
        displaced: originals.DisplacedReference | None = None
        current = PurePosixPath(path)
        for root in (current, current.with_suffix(""), *current.parents):
            if not is_trash_path(root.as_posix()):
                continue
            placement = self._placement(root.as_posix())
            if placement is None:
                continue
            try:
                historical = original_path(placement, trash_root=root.as_posix(), current_path=path)
                admitted = originals.companion_binding(fields, sidecar_path=historical)
                if admitted is None:
                    continue
                origin = PurePosixPath(placement["original_path"])
                payload = root / PurePosixPath(admitted.path).relative_to(origin)
                return originals.BinaryBinding(payload.as_posix(), admitted.sha256, admitted.size)
            except originals.DisplacedReference as error:
                displaced = error
            except ValueError:
                continue
        if displaced is not None:
            raise displaced
        raise ValueError("original in Trash has no trustworthy placement")

    def require_current(self) -> None:
        for path, version in self.versions.items():
            with reserved_paths.open_generic_binary(self.vault, path) as source:
                if source.version != version:
                    raise ValueError("Trash placement changed during the census")
                source.require_current()


def retired_companion(vault: Path, companion: str, fields: dict[str, object], digest: str,
                      *, census: PlacementCensus | None = None) -> bool:
    """Whether a companion was retired to Trash, as the reciprocal witness in canonical Trash metadata confirms.

    The one verdict for a filing that no file at its path holds: reads and the conversion that pools such files
    both take it, so the conversion never pools a filing that reads would then take for deleted.

    ``digest`` is the companion's content hash. The read is bounded: the placement metadata that the backlink
    names, then that entry's reference companion. True as well when that metadata is gone (the owner emptied the
    entry) or holds no witness (a compatibility export invalidated it), unless the page holds a stored-copy
    reference. False with no backlink, or one whose witness names another companion. Raises
    `originals.RetirementUnconfirmed` when a witness names it but the metadata, the companion or the Trash entry no
    longer confirms it. ``census`` lets a caller that reads many pages share one.
    """
    metadata = _backlink(fields)
    if metadata is None:
        return False
    census = PlacementCensus(vault) if census is None else census
    try:
        proof = census.retirement(metadata[:-len(METADATA_SUFFIX)])
        if proof is None:
            # Only recovery, or a new original written with its page, takes a backlink off. So a backlink whose
            # metadata is gone or holds no witness is a deleted original: equal bytes elsewhere are not its own.
            # A delete takes the stored-copy reference off the page, so a page that holds one was offloaded again
            # after a restore that left the backlink behind, and files a live original.
            return "stored_object" not in fields
        if proof.companion != companion:
            return False
        confirmed = census.retired_source(companion, fields, digest)
    except (ValueError, reserved_paths.ReservedPathLeafError) as error:
        raise originals.RetirementUnconfirmed(companion) from error
    if not confirmed:
        raise originals.RetirementUnconfirmed(companion)
    return True


def _confirmed_by_trash(vault: Path, proof: Retirement) -> bool:
    """Whether the Trash entry still holds the exact reference-only companion that the witness names."""
    from .vault import parse_frontmatter

    trash = reference_companion(vault, proof.trash.path)
    if trash is None:
        return False
    current = parse_frontmatter(trash[1].data.decode("utf-8"), custody=True)[0]
    return (originals.binary_binding(current) == proof.trash
            and originals.stored_reference(current.get("stored_object")) == proof.reference)


def reference_companion(vault: Path, binary_path: str) -> tuple[str, reserved_paths.GenericFileSnapshot] | None:
    """Acquire the exact reference-only companion that recovery may retire."""
    from .vault import parse_frontmatter

    path = originals.sidecar_path(Path(binary_path)).as_posix()
    try:
        snapshot = reserved_paths.read_generic_bytes(vault, path)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            return None
        raise
    fields, body, _marker = parse_frontmatter(snapshot.data.decode("utf-8"), custody=True)
    if originals.bound_path(fields) != binary_path or not originals.is_reference_only_companion(fields, body):
        raise ValueError("Trash companion contains independent or unresolved content")
    return path, snapshot


def addressed_original(vault: Path, binary_path: str) -> tuple[str, bytes, originals.BinaryBinding] | None:
    """The page of a trashed digest-addressed original that has no payload at ``binary_path``.

    Its bytes stay addressed by digest. Its page keeps naming the original path, as a trashed folder's
    pages do, and this placement maps that name to ``binary_path``. Any other path is None. A page
    store decides whether a page that it owns exists; its projection may trail the commit.
    """
    from . import original_pool
    from .vault import parse_frontmatter
    from .vault_store import read as store_read

    if not is_trash_path(binary_path) or originals.is_note_path(binary_path) or not original_pool.enabled(vault):
        return None
    try:
        reserved_paths.inspect_generic_path(vault, binary_path)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code != "MISSING":
            return None
    else:
        return None
    page = originals.sidecar_path(Path(binary_path)).as_posix()
    try:
        data = store_read.owned_bytes(vault, page)
        if data is None:
            data = reserved_paths.read_generic_bytes(vault, page).data
        fields, _body, marker = parse_frontmatter(data.decode("utf-8"), custody=True)
        if originals.bound_path(fields) == binary_path:
            return None  # A companion that binds the Trash path itself retains an ordinary Trash entry, not a filing.
        census = PlacementCensus(vault)
        binding = census.resolve(page, fields) if marker is not None else None
        census.require_current()
    except (FileNotFoundError, UnicodeError, ValueError, reserved_paths.ReservedPathLeafError):
        return None
    if binding is None or binding.path != binary_path:
        return None
    return page, data, binding


def remove_unchanged(
    vault: Path, path: str, identity: held_fs.StableIdentity | None, data: bytes
) -> None:
    """Retire only the acquired metadata bytes, retaining any replacement or edit.

    ``identity`` is None for a companion that the vault's store holds; a stored page has no file to
    retire.
    """
    try:
        current = reserved_paths.read_generic_bytes(vault, path)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            return
        raise
    if current.identity != identity or current.data != data:
        raise reserved_paths.ReservedPathLeafError("IDENTITY_CHANGED")
    if identity is None:
        # Retiring a stored companion is a store delete (the write path); an unlink here would drop
        # only its projection.
        raise reserved_paths.ReservedPathLeafError("UNSAFE_PATH")
    reserved_paths.unlink_generic_file(vault, path, expected_identity=identity)


@contextmanager
def preserve_placement(
    vault: Path, transition: GraphLifecycleTransition, files: Iterable[tuple[str, bytes]],
):
    """Sync reference custody before rename; undo only this transition's caught writes."""
    from . import graph_sync

    created: list[tuple[str, held_fs.StableIdentity, bytes]] = []

    def cleanup() -> None:
        try:
            for path, identity, data in reversed(created):
                remove_unchanged(vault, path, identity, data)
        except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError) as error:
            raise graph_sync.GraphLifecycleRollbackError("Trash placement cleanup remains unresolved") from error

    publishing: str | None = None
    try:
        for path, data in files:
            publishing = path
            identity = reserved_paths.publish_generic_bytes(vault, path, data, expected_identity=None)
            created.append((path, identity, data))
    except Exception as error:
        transition.abort()
        if publishing is not None:
            try:
                reserved_paths.inspect_generic_path(vault, publishing)
            except reserved_paths.ReservedPathLeafError as inspection:
                if inspection.code != "MISSING":
                    raise graph_sync.GraphLifecycleRollbackError("Trash placement publication remains uncertain") from error
            else:
                # A publisher can fail after its write; no returned identity proves ownership.
                raise graph_sync.GraphLifecycleRollbackError("Trash placement publication remains uncertain") from error
        cleanup()
        raise
    try:
        yield
    except Exception:
        # An incomplete inverse rename still needs every durable placement record.
        if not transition.renamed:
            cleanup()
        raise
