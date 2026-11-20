"""The `move_file` Tier 2 op: relocate a file, optionally update wikilinks.

Append-only trees (Sources/, Evidence/): relocation WITHIN the same tree is
allowed (a move carries bytes verbatim — only location changes, content
stays immutable per rule 2), enabling themed sub-folders. Moves that cross
the boundary are refused: OUT of an append-only tree, or INTO one from
elsewhere (those land via `add` / `preserve`).
Curated trees on either end need `allow_curated=true`.

When `update_wikilinks=true` (default), scans the full vault for
`[[<old>]]` and `[[<basename>]]` references and rewrites them to point at
the new location. Returns the count of touched files.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from . import (
    call_spans,
    capabilities,
    graph_sync,
    media_bindings,
    original_pool,
    originals,
    page_lineage,
    path_mentions,
    pointer_write,
    recap_folder,
    reserved_paths,
    semantic_index,
    semantic_writes,
    source_custody,
    source_taxonomy,
    trash_metadata,
    working_set_heat,
    writer_lease,
)
from .cli_ops import OpError
from .governance import catalog_publication, graph_producer
from .kbdir import managed_split_parts
from .vault import (
    BatchWriteError,
    ContentHashMismatchError,
    InboundLink,
    LogWritePlan,
    PathGuard,
    PathGuardError,
    PlannedWrite,
    VaultPathError,
    batch_atomic_write,
    content_hash,
    find_inbound_wikilinks,
    folder_split_warnings,
    in_append_only_tree,
    in_curated_tree,
    parse_frontmatter,
    plan_log_entry,
    read_guarded_text,
    resolve_under_vault,
    respell_new_path,
    walk_vault_md,
    write_log_entry,
)
from .vault_store import availability as store_availability
from .vault_store import ownership as store_ownership
from .vault_store import read as store_read
from .vault_store import targets as store_targets

log = logging.getLogger(__name__)

@dataclass
class MoveFileResult:
    old_path: str
    new_path: str
    wikilinks_updated: int
    files_touched: list[str]
    warnings: list[str]
    semantic: dict | None = None
    index: dict | None = None
    #: Prose mentions of the old path in page bodies that the move renamed.
    path_mentions_updated: int = 0
    #: The pointer-write records of the append-only referrers the move changed.
    pointer_writes: list[str] = field(default_factory=list)
    #: Those referrers, title and path, in record order: the pages whose links the move repointed.
    pointer_repointed: list[dict[str, str]] = field(default_factory=list)
    #: A preview's one outcome per referrer the caller may see; None for a commit.
    referrers: list[dict[str, str]] | None = None
    #: A preview's outcome for the rows the collection store keeps (`store_rows_outcome`).
    store_rows: str | None = None
    #: The digest a commit that repoints append-only referrers passes; None when it cannot commit.
    preview_digest: str | None = None

    def as_dict(self) -> dict:
        answer = {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "wikilinks_updated": self.wikilinks_updated,
            "path_mentions_updated": self.path_mentions_updated,
            "files_touched": self.files_touched,
            "warnings": self.warnings,
            "semantic": self.semantic,
            "index": self.index,
        }
        if self.pointer_writes:
            answer["pointer_writes"] = self.pointer_writes
            answer["pointer_repointed"] = self.pointer_repointed
        if self.referrers is not None:
            answer["referrers"] = self.referrers
            answer["preview_digest"] = self.preview_digest
        if self.store_rows is not None:
            answer["store_rows"] = self.store_rows
        return answer


@dataclass
class MoveFileError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


@dataclass(frozen=True)
class MoveFileValidation:
    """Private exact content manifest returned by the canonical move preflight."""

    result: MoveFileResult
    before: tuple[dict[str, Any], ...]
    after: tuple[dict[str, Any], ...]
    rename_after: tuple[dict[str, Any], ...]
    atomic_supported: bool
    #: The moved page's bytes at its new path, for a Markdown page; curation seals them so a later
    #: step of its plan can bind that page and its compensation can restore the pre-step bytes.
    after_text: str | None = None


def _in_episode_folder(vault_root: Path, rel: str) -> bool:
    """Whether `rel` lies anywhere under `Sources/Episodes/` or a capture path's `Episodes/`, case-insensitively.

    A recap's revisions are found by one listing of that one folder, by a
    filename token. A move into it plants a page that listing may take for a
    recap; a move out of it, into a subfolder or by a rename that strips the
    token hides a live revision from it, so the next record leaves two live.
    """
    if recap_folder.in_inbox(vault_root, rel, casefold=True):
        return True
    parts = rel.replace("\\", "/").split("/")
    below = managed_split_parts(parts, key=str.casefold)
    if len(parts) > 1 and below is not None:
        parts = below
    return (
        len(parts) > 2
        and parts[0].casefold() == source_taxonomy.SOURCES_ROOT.casefold()
        and parts[1].casefold() == source_taxonomy.EPISODE_PATH_LABEL.casefold()
    )


def _admit_destination(vault_root: Path, rel: str) -> None:
    """Admit a move destination before any existence check.

    A limited caller may create only in a capture namespace, and this policy answers first. A
    hidden page can still change the later answer, and the two cases answer differently: a
    hidden file at the name refuses with `DEST_EXISTS`, and a withheld page that claims the name
    as a prior path refuses with `WRITE_REFUSED`. Neither names the page. The second tells the
    caller that a page it cannot see once lived at that path (`prior_claims.require_unreserved`
    says what that discloses and why the reservation holds).
    """
    from .governance import connector_boundary

    try:
        connector_boundary.require_create(vault_root, rel)
    except ValueError:
        raise MoveFileError("WRITE_REFUSED", "target is unavailable") from None


def _admit_publication(vault_root: Path, rel: str, content: bytes | None) -> None:
    """Admit the moved bytes at their destination, as the batch target check would.

    The rename publishes outside that check, so the move asks here: every
    writer keeps capture namespaces free of protected content.
    """
    from .governance import connector_boundary

    try:
        connector_boundary.require_write(vault_root, rel, content=content)
    except ValueError:
        raise MoveFileError("WRITE_REFUSED", "target is unavailable") from None


def _held_rename(
    vault_root: Path, old_rel: str, new_rel: str, *, rollback: bool = False, kind: str = "file"
) -> None:
    try:
        # `old_rel` is always a source this call's caller has already resolved
        # to an existing on-disk spelling (including a macOS-origin NFD name
        # on a byte-exact filesystem); `physical=True` opens exactly that
        # spelling rather than re-normalizing it away. `new_rel` -- the
        # destination -- is a name being written, so it always takes the
        # ordinary NFKC spelling.
        reserved_paths.move_generic_path(
            vault_root,
            old_rel,
            new_rel,
            source_kind=kind,
            physical=True,
            rollback=rollback,
        )
    except OpError as error:
        raise MoveFileError(error.code, error.message) from error
    except reserved_paths.ReservedPathLeafError as error:
        raise _rename_error(error) from None


def _rename_error(error: reserved_paths.ReservedPathLeafError) -> MoveFileError:
    """The move's error for a held rename that the filesystem refused."""
    if error.code == "CROSS_DEVICE":
        return MoveFileError("CROSS_DEVICE_MOVE", "file moves require one filesystem")
    if error.code == "DESTINATION_EXISTS":
        return MoveFileError("DEST_EXISTS", "destination already exists")
    if error.code == "PATH_VARIANT_EXISTS":
        return MoveFileError(
            "PATH_VARIANT_EXISTS",
            "a destination folder already exists in another case or Unicode form",
        )
    if error.code in {
        "CAPABILITY_UNAVAILABLE",
        "IDENTITY_CHANGED",
        "RESERVED_PATH",
        "UNSAFE_PATH",
    }:
        return MoveFileError("RESERVED_PATH", "path is reserved for its owning subsystem")
    return MoveFileError("MOVE_FAILED", "held file move was refused")


def _compose_transforms(first, second):
    """Run a caller's own content transform, then this module's repointing.

    Order matters only in that the caller declared its change first; the
    repointing is mechanical and must see the final text.
    """
    if first is None:
        return second
    return lambda text: second(first(text))


def _paired_artifact(
    vault_root: Path, rel: str, page_held: Callable[[str], bool] | None = None
) -> tuple[str, str] | None:
    """Return `(page_rel, binary_rel)` when `rel` is half of a stored artifact.

    A captured artifact is two files — the bytes and the page that addresses
    them, named `<filename>` and `<filename>.md`. Relocating one without the
    other leaves the bytes with no page, which is the unaddressable state the
    page contract exists to prevent, or a page describing an artifact that is
    no longer there. So either half names the whole unit.

    Returns None for an ordinary page, which is the common case: a source page
    whose stem happens to have no sibling file is not a pair. ``page_held`` says
    whether a page exists; a store-mode vault answers it from the store, where a
    page may have no file yet. An evicted original that the caller may not see is
    no pair either: it stays in the store, and its page moves alone.
    """
    from .governance.raw_protection import marked

    if marked(rel):
        from .governance.raw_protection import binding

        pair = binding(vault_root, rel)
        if pair is not None:
            block, companion, _ = pair
            return companion, block["artifact_path"]
    if rel.lower().endswith(".md"):
        binary_rel = rel[:-3]
        if not binary_rel or binary_rel.lower().endswith(".md"):
            return None
        # A pooled filing has no file at its logical path: its bytes are a shared object.
        present = (
            (vault_root / binary_rel).is_file()
            or originals.pooled(vault_root, binary_rel) is not None
            or originals.evicted_visible_original(vault_root, binary_rel) is not None
        )
        return (rel, binary_rel) if present else None
    page_rel = f"{rel}.md"
    held = (vault_root / page_rel).is_file() if page_held is None else page_held(page_rel)
    return (page_rel, rel) if held else None


def paired_move(vault_root: Path, old_page: str, new_page: str) -> tuple[str, str] | None:
    """The (old, new) binary path that moves with the page `old_page`, or None."""
    pair = _paired_artifact(vault_root, old_page)
    if pair is None or pair[0] != old_page:
        return None
    return pair[1], new_page.removesuffix(".md")


def _repoint_artifact(old_binary: str, new_binary: str):
    """A content transform that repoints a moved page at its moved bytes.

    Only the pointer fields change, patched where the YAML parser places them,
    so an append-only page keeps the bytes of its other fields and its body.
    """

    def transform(text: str) -> str:
        from .record_formats import FrontmatterSpliceError, splice_markdown_frontmatter

        frontmatter, _body, block = parse_frontmatter(text, strict=True)
        if block is None:
            return text
        changes: dict[str, Any] = {}
        for pointer in originals.SCALAR_POINTERS:
            if frontmatter.get(pointer) == old_binary:
                changes[pointer] = new_binary
        path_key = originals.NESTED_PATH_KEY
        descriptor = frontmatter.get(originals.GOVERNANCE_COMPANION)
        if isinstance(descriptor, dict) and descriptor.get(path_key) == old_binary:
            moved_descriptor = {**descriptor, path_key: new_binary}
            if "original_filename" in descriptor:
                moved_descriptor["original_filename"] = Path(new_binary).name
            changes[originals.GOVERNANCE_COMPANION] = moved_descriptor
        if frontmatter.get("original_filename") == Path(old_binary).name:
            changes["original_filename"] = Path(new_binary).name
        protection = frontmatter.get(originals.RAW_PROTECTION)
        if isinstance(protection, dict) and protection.get(path_key) == old_binary:
            changes[originals.RAW_PROTECTION] = {**protection, path_key: new_binary}
        if not changes:
            return text
        try:
            return splice_markdown_frontmatter(text, changes)
        except FrontmatterSpliceError as error:
            raise MoveFileError(error.code, error.reason) from error

    return transform


def _lineage_transform(
    old_rel: str,
    new_rel: str,
    extra: Mapping[str, Any] | None,
    undo: bool,
    *,
    keep_vacated: bool = False,
) -> Callable[[str], str]:
    """A content transform that records the move in the page's lineage, or undoes the entry.

    With `keep_vacated`, an undo also records the path it vacates, as a move back does.
    """
    from . import page_lineage

    change = (
        page_lineage.undo_transform(new_rel, old_rel if keep_vacated else None)
        if undo
        else page_lineage.append_transform(old_rel, extra)
    )

    def transform(text: str) -> str:
        try:
            return change(text)
        except page_lineage.LineageError as error:
            raise MoveFileError(error.code, error.reason) from error

    return transform


@dataclass(frozen=True)
class MoverView:
    """What a mover may see, as every relocation reads it (`mover_view`)."""

    #: The caller's view. It always filters what the caller is shown: a bound store gives the
    #: owner one too.
    visible: Callable[[str], bool]
    #: Whether the caller is restricted at all.
    restricted: bool
    #: A connector-limited mover never reads, rewrites or counts a page it cannot see, so its
    #: answer is the same with and without hidden linkers. A hidden linker keeps its old link,
    #: which resolves through the moved page's lineage.
    limited: bool

    @property
    def scope(self) -> Callable[[str], bool] | None:
        """The filter for the pages a sealed link rewrite reads and lists, or None for every page.

        It filters every restricted caller, not only a connector-limited one: a curation plan
        lists each page it rewrites and its approval names them, so a page outside the caller's
        view stays out of the plan and keeps its old link. A move filters its answer instead
        (`shown`), so an unlimited restricted mover may still rewrite a hidden linker silently.
        """
        return self.visible if self.restricted else None


def mover_view(vault_root: Path) -> MoverView:
    """The caller's view for a relocation: one rule for a move and a curation link rewrite."""
    from .governance import connector_boundary, egress
    from .governance.principal import effective_principal

    restricted = egress.caller_restricted(vault_root)
    return MoverView(
        visible=egress.restricted_release_filter(vault_root),
        restricted=restricted,
        limited=restricted
        and not connector_boundary.unrestricted(vault_root, effective_principal()),
    )


def _own_lineage_write(
    vault_root: Path, rel: str, before: str, after: str, reason: str
) -> pointer_write.PointerWrite:
    """The proven write by which a move's lineage entry changes an append-only page.

    It admits one entry appended after every earlier byte of `moved_from` and nothing else, and
    its record lets the move's change be read back. An undo removes an entry, which no
    declaration admits; `page_lineage.remove_latest` checks that the entry is its own.
    """
    try:
        return pointer_write.plan(
            vault_root, rel, before, after, pointer_write.Declared(lineage=True), reason=reason
        )
    except pointer_write.PointerWriteError as error:
        raise MoveFileError(code=error.code, reason=error.reason) from error


def records_lineage(vault_root: Path, old_rel: str, new_rel: str, *, undo: bool) -> bool:
    """Whether the move leaves the moved page a lineage entry that resolves its old path.

    Only a Markdown page with a frontmatter block records lineage, and an undo removes the
    entry instead of adding one.
    """
    from .vault import served_page_bytes

    if undo or not (old_rel.lower().endswith(".md") and new_rel.lower().endswith(".md")):
        return False
    raw = served_page_bytes(vault_root, old_rel)
    if raw is None:
        return False
    _front, _body, block = parse_frontmatter(raw.decode("utf-8", errors="replace"))
    return block is not None


def _undo_records(vault_root: Path, old_rel: str) -> dict[str, str]:
    """The pointer-write record id of each page that the move an undo reverses repointed.

    The page's newest lineage entry names that move's operation; the owner-only map of the
    operation gives the records, by page (`pointer_write.operation_records`).
    """
    from . import page_lineage, pointer_write
    from .vault import served_page_bytes

    raw = served_page_bytes(vault_root, old_rel)
    if raw is None:
        return {}
    operation = page_lineage.latest_operation(raw.decode("utf-8", errors="replace"))
    return pointer_write.operation_records(vault_root, operation)


def _repoint_parts(new_page: str):
    """A content transform that points a moved page's `next_part` at its moved folder of parts."""

    def transform(text: str) -> str:
        from . import extraction_parts
        from .record_formats import FrontmatterSpliceError, splice_markdown_frontmatter

        frontmatter, _body, block = parse_frontmatter(text, strict=True)
        page = Path(new_page)
        moved = extraction_parts.part_path(page, 2).relative_to(page.parent).as_posix()
        if block is None or frontmatter.get("next_part") in (None, moved):
            return text
        try:
            return splice_markdown_frontmatter(text, {"next_part": moved})
        except FrontmatterSpliceError as error:
            raise MoveFileError(error.code, error.reason) from error

    return transform


@dataclass(frozen=True)
class InboundRewrite:
    """What a move does to one page that links the moved file."""

    path: str
    absolute: Path
    #: The append-only tree that holds the page, or None when the move may write it freely.
    append_only: str | None
    #: Links whose bytes the move changes; zero leaves the page untouched.
    changes: int
    content: str
    guard: PathGuard
    #: Prose mentions of the moved path that `content` renames; the page is written when any change.
    mentions: int = 0
    #: Mentions a page with an immutable body keeps, as captured.
    left_mentions: int = 0
    #: The proven pointer write that changes an append-only page; `content` is its result.
    repoint: Any = None
    #: True when `repoint` restores the page from the record of the move this one undoes.
    restored: bool = False
    #: The code and reason that refuse the change to an append-only page.
    refusal: tuple[str, str] | None = None


#: The outcome a move preview gives each referrer; the command-surface specification fixes the set.
REWRITTEN = "rewritten"
REPOINTED = "pointer-repointed (append-only page)"
KEPT = "unchanged: resolves via prior path"
REFUSED = "refused"
#: A reference a move leaves while the moved file records no lineage, so nothing resolves it.
KEPT_DANGLING = "unchanged: keeps the old path"


def store_rows_outcome(vault_root: Path, unchanged: str) -> str | None:
    """The preview outcome of the rows that the vault's collection store keeps without a page.

    A move writes no such row, and no index maps a row's values to the pages they name, so a
    preview cannot list the rows that name the moved path. They share `unchanged`, the outcome of
    every reference the move leaves. None when the vault keeps every collection in files.
    """
    from .collection_store import ownership

    return None if ownership.file_only(vault_root) else unchanged

#: The `manage_memory_file` call whose preview digest approves a move's repoints of append-only
#: referrers; a refusal spells it for the surface that serves the call.
_MOVE_PREVIEW = MappingProxyType({"operation": "move", "validate_only": True})
#: Stands for the store writer in a store-mode validation, which returns before any commit.
_STORE_VALIDATION = object()


def plan_inbound_rewrites(
    vault_root: Path,
    old_rel: str,
    new_rel: str,
    inbound: list[InboundLink],
    *,
    named_in: Sequence[str] = (),
    paired: tuple[str, str] | None = None,
    undo: bool = False,
) -> list[InboundRewrite]:
    """Rewrite each page `inbound` or `named_in` names, in path order, without writing it.

    The move and its preview both read their referrer outcomes from here. A
    page that cannot be read is skipped, and each guard binds the bytes read.
    `named_in` are pages that may cite the moved paths in prose, and `paired` is the
    moved file's binary when the page moves with one; the mention rewrite covers both.

    A page in an append-only tree changes only through the governed pointer write: its links
    follow the move when they name a page with an `exomem_id`, and its registry pointer fields
    may change. An `undo` returns each append-only page to the before bytes of the record
    that the move it undoes left on it, and refuses one that move did not change.
    """
    from . import page_fields, pointer_write

    plans: list[InboundRewrite] = []
    pages = sorted({hit.path for hit in inbound} | set(named_in))
    moves = path_mentions.renames(
        vault_root, [(old_rel, new_rel), *([paired] if paired is not None else [])]
    ) if pages else ()
    pointers = page_fields.pointer_fields(vault_root) if pages else frozenset()
    # One matcher holds one resolver for every append-only referrer of the move.
    matcher = pointer_write.page_matcher(vault_root, {old_rel: new_rel})
    # Read on the first append-only page the undo changes, so pages it does not plan never
    # reach the records.
    restore: dict[str, str] | None = None
    identified = (
        bool(pages)
        and old_rel.lower().endswith(".md")
        and pointer_write.identity_at(vault_root, old_rel) is not None
    )
    for rel in pages:
        if rel == old_rel:
            continue
        try:
            abs_file = (vault_root / rel).resolve()
            abs_file.relative_to(vault_root.resolve())
        except (ValueError, OSError):
            continue
        try:
            text, guard = read_guarded_text(vault_root, abs_file)
        except (OSError, UnicodeDecodeError, PathGuardError):
            continue
        new_text, n_changed = _rewrite_wikilinks(text, old_rel, new_rel)
        append_tree = in_append_only_tree(rel)
        # A generated index inside an append-only tree is not captured
        # content: `add` rewrites `Sources/index.md` on every capture.
        # Rule 2 protects the material, and refusing to repoint an index
        # at a file this op just moved would leave the index dangling —
        # the opposite of what the guard is for.
        generated_index = rel.rsplit("/", 1)[-1] == "index.md"
        body_immutable = append_tree is not None and not generated_index
        if generated_index:
            append_tree = None
        repoint = refusal = None
        restored = False
        if append_tree is not None and n_changed:
            record = None
            try:
                if undo:
                    if restore is None:
                        restore = _undo_records(vault_root, old_rel)
                    if rel in restore:
                        record = pointer_write.load_record(vault_root, restore[rel])
                if undo and record is None:
                    raise pointer_write.PointerWriteError(
                        "POINTER_RECORD_UNKNOWN",
                        f"undoing the move would change {rel} in append-only {append_tree}/, "
                        "which the move it undoes did not change, so no pointer-write record "
                        "restores it. Move the page explicitly instead.",
                    )
                if record is not None:
                    new_text, restored = pointer_write.reverted_text(record, text), True
                repoint = pointer_write.plan(
                    vault_root,
                    rel,
                    text,
                    new_text,
                    # The move changes links to the moved page wherever they sit, and a registry
                    # pointer field whose value holds one changes as a field.
                    pointer_write.Declared(fields=pointers, moves={old_rel: new_rel}),
                    reason=(
                        f"undo of the move of {new_rel} to {old_rel}"
                        if restored
                        else f"move of {old_rel} to {new_rel}"
                    ),
                    same_page=matcher,
                )
            except pointer_write.PointerWriteError as error:
                reason = error.reason
                if error.code == "APPEND_ONLY" and not identified:
                    reason = (
                        f"updating inbound wikilinks would rewrite {rel} in append-only "
                        f"{append_tree}/, and {old_rel} has no `exomem_id` to prove that a "
                        "repointed link still names it. Retry with `update_wikilinks=false` to "
                        "preserve exact bytes and run final-corpus semantic evaluation."
                    )
                new_text, refusal = text, (error.code, reason)
        # A prose mention in an immutable body can never be rewritten, by this move or any
        # other, so refusing would block every later move of the file; it is left and reported.
        new_text, n_mentions = path_mentions.rewrite(
            new_text, moves, apply=not body_immutable
        )
        left = n_mentions if body_immutable else 0
        plans.append(
            InboundRewrite(
                rel, abs_file, append_tree, n_changed, new_text, guard,
                mentions=0 if body_immutable else n_mentions, left_mentions=left,
                repoint=repoint, restored=restored, refusal=refusal,
            )
        )
    return plans


def move_file(
    vault_root: Path,
    *,
    old_path: str,
    new_path: str,
    update_wikilinks: bool = True,
    allow_curated: bool = False,
    today: dt.date | None = None,
    promotion_reason: str | None = None,
    content_transform: Callable[[str], str] | None = None,
    extra_writes: tuple[PlannedWrite, ...] = (),
    validate_only: bool = False,
    _validate_prepared_bindings: Callable[[], None] | None = None,
    lineage_entry: Mapping[str, Any] | None = None,
    undo_lineage: bool = False,
    preview: bool = False,
    preview_digest: str | None = None,
    record_repoints: bool = False,
    _preview_call: Mapping[str, object] = _MOVE_PREVIEW,
    _stage: Callable[[str], None] | None = None,
    _exact_content: bool = False,
    _caller_judges_contract: bool = False,
) -> MoveFileResult | MoveFileValidation:
    """Relocate a file; see `_move_file_local`.

    `lineage_entry` adds keys to the lineage entry the move appends, such as a
    reclassification's prior classification. `undo_lineage` marks the move as an undo:
    it removes the newest lineage entry, which must name the destination, and appends none,
    except for a restricted caller, whose undo appends an entry naming the path it vacates.
    `record_repoints` marks the move undoable: its entry names its operation, which the
    owner-only map of the move's pointer-write records keys, so the undo restores each
    append-only referrer from its record.

    `preview` validates without writing and returns the result with one outcome per referrer
    and the `preview_digest` that a commit repointing append-only pages must pass.

    `_stage` is curation's seam around a file-mode rename: it is called once with
    `before-rename` immediately before the rename, `after-rename` after it and `after-batch`
    after the batch commits. `_exact_content` makes `content_transform` alone decide the moved
    bytes, with no lineage entry added or removed: curation's guarded inverse restores sealed bytes.
    `_caller_judges_contract` leaves a validation's contract to its caller, which judges the move
    from another state: curation's inverse move, from what the plan's earlier steps leave
    (`semantic_writes.ProjectedMoves`). Its commit judges the vault as it is then.
    """
    manager = writer_lease.active_manager()
    validate_only = validate_only or preview
    boundary = manager.consistency_guard if validate_only else manager.mutation_guard
    # Protect semantic creation and rollback, then release before standalone index fan-out.
    with ExitStack() as mutation_context, ExitStack() as originals_context:
        mutation_context.enter_context(boundary(vault_root, operation="move_file"))
        try:
            return _move_file_local(vault_root, old_path=old_path, new_path=new_path, update_wikilinks=update_wikilinks, allow_curated=allow_curated, today=today, promotion_reason=promotion_reason, content_transform=content_transform, extra_writes=extra_writes, validate_only=validate_only, _validate_prepared_bindings=_validate_prepared_bindings, originals_context=originals_context, mutation_context=mutation_context, lineage_entry=lineage_entry, undo_lineage=undo_lineage, preview=preview, preview_digest=preview_digest, record_repoints=record_repoints, _preview_call=_preview_call, _stage=_stage, _exact_content=_exact_content, _caller_judges_contract=_caller_judges_contract)
        except originals.RetirementUnconfirmed as error:
            raise MoveFileError(code=error.code, reason=error.reason) from error


def _evicted_source(
    vault_root: Path, old_path: str, *, addressed_only: bool = False
) -> tuple[Path, str, originals.OriginalSnapshot] | None:
    """An original with no file at its path that the caller may see; any other absent path stays NOT_FOUND.

    It is evicted (`originals.evicted_visible_original`), or, in a pooled archive, addressed by digest.
    A store-mode caller asks only for the second: the store keeps a record of every evicted original it moves.
    """
    if not addressed_only and (evicted := originals.evicted_visible_original(vault_root, old_path)) is not None:
        return evicted
    if not original_pool.enabled(vault_root):
        return None
    try:
        abs_path, rel_path = resolve_under_vault(vault_root, old_path, must_be_file=True)
    except VaultPathError:
        return None
    stored = originals.digest_addressed(vault_root, abs_path)
    if stored is None or not originals.caller_may_see(vault_root, rel_path):
        return None
    return abs_path, rel_path, stored


def _materialize_originals(vault_root: Path, originals_context: ExitStack, rel: str) -> None:
    """Hydrate an evicted original only once the move is about to rename it."""
    from .media_store import moved_originals

    originals_context.enter_context(moved_originals(vault_root, rel))


def _refuse_reserved_destination(
    vault_root: Path, old_rel: str, new_rel: str, *, validate_only: bool
) -> None:
    """Refuse a destination that another live page's lineage claims; the page's own lineage is free."""
    from . import prior_claims

    try:
        prior_claims.require_unreserved(vault_root, new_rel, own=old_rel, newest=not validate_only)
    except prior_claims.ReservedPathError as error:
        code = "DEST_EXISTS" if error.code == "FILE_EXISTS" else error.code
        raise MoveFileError(code=code, reason=error.reason) from None


def _refuse_taken_destination(vault_root: Path, new_rel: str) -> None:
    """Refuse a file-mode destination that a file, a folder or an original holds, or that is reserved."""
    try:
        reserved_paths.inspect_generic_path(vault_root, new_rel)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code in {
            "CAPABILITY_UNAVAILABLE",
            "IDENTITY_CHANGED",
            "RESERVED_PATH",
            "UNSAFE_PATH",
        }:
            raise MoveFileError(
                "RESERVED_PATH",
                "destination is reserved for its owning subsystem",
            ) from None
        if error.code != "MISSING":
            raise MoveFileError(
                "MOVE_FAILED", "held destination acquisition was refused"
            ) from None
        # A pooled or evicted original holds its logical path with no file there.
        if originals.is_note_path(new_rel) or not originals.available(vault_root, new_rel):
            return
    raise MoveFileError(
        code="DEST_EXISTS",
        reason=(
            f"destination already exists: {new_rel}. "
            f"This op refuses to overwrite — pick a different name."
        ),
    )


def _resolve_source_on_files(
    vault_root: Path, old_path: str
) -> tuple[Path, str, originals.OriginalSnapshot | None]:
    """The file a file-mode move renames: its path, spelling and evicted original, if any."""
    stored_original = None
    try:
        old_abs, old_rel = resolve_under_vault(
            vault_root, old_path, must_exist=True, must_be_file=True, refuse_withheld=True
        )
    except VaultPathError as e:
        if e.code != "NOT_FOUND":
            raise MoveFileError(code=e.code, reason=e.reason) from e
        # NOT_FOUND also hides existing withheld files. A spelling fallback
        # is valid only when the canonical source really is absent.
        canonical_source, logical_rel = resolve_under_vault(vault_root, old_path)
        if canonical_source.exists():
            raise MoveFileError(code=e.code, reason=e.reason) from e
        evicted_source = _evicted_source(vault_root, old_path)
        if evicted_source is not None:
            old_abs, old_rel, stored_original = evicted_source
        else:
            # The literal (NFKC) spelling may simply be absent because the
            # on-disk name is a different Unicode normalization -- a macOS-origin
            # NFD name on a byte-exact filesystem (Linux ext4). Substitute the
            # confirmed physical spelling and re-resolve, so every downstream
            # read AND rename (which just open literal path strings, no further
            # normalization) target the real file. Refuse outright, rather than
            # guess, if two physical spellings collide.
            try:
                physical_rel = reserved_paths.resolve_physical_relative(vault_root, old_path)
            except reserved_paths.ReservedPathLeafError as fallback_error:
                if fallback_error.code == "AMBIGUOUS_PATH":
                    from .get_page import path_withheld

                    if path_withheld(vault_root, logical_rel):
                        raise MoveFileError(code=e.code, reason=e.reason) from None
                    raise MoveFileError(
                        code="AMBIGUOUS_PATH",
                        reason=(
                            f"{old_path} matches more than one on-disk spelling; "
                            "refusing to guess which"
                        ),
                    ) from None
                raise MoveFileError(code=e.code, reason=e.reason) from e
            try:
                old_abs, old_rel = resolve_under_vault(
                    vault_root, physical_rel, must_exist=True, must_be_file=True,
                    refuse_withheld=True,
                )
            except VaultPathError as e2:
                if e2.code == "NOT_FOUND":
                    # Keep the caller's logical spelling in the absent response.
                    raise MoveFileError(code=e.code, reason=e.reason) from None
                raise MoveFileError(code=e2.code, reason=e2.reason) from e2
    else:
        # The NFKC spelling opened, but an NFD twin beside it would make the
        # read door refuse this same path; refuse the move too rather than
        # silently pick the NFKC file.
        try:
            reserved_paths.resolve_physical_relative(vault_root, old_rel)
        except reserved_paths.ReservedPathLeafError as collision:
            if collision.code == "AMBIGUOUS_PATH":
                from .get_page import path_withheld

                if path_withheld(vault_root, old_rel):
                    # A withheld page answers exactly like a missing one.
                    raise MoveFileError(
                        code="NOT_FOUND", reason=f"path does not exist: {old_rel}"
                    ) from None
                raise MoveFileError(
                    code="AMBIGUOUS_PATH",
                    reason=(
                        f"{old_path} matches more than one on-disk spelling; "
                        "refusing to guess which"
                    ),
                ) from None
    try:
        if stored_original is None:
            reserved_paths.inspect_generic_file(vault_root, old_rel, physical=True)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code in {
            "CAPABILITY_UNAVAILABLE",
            "IDENTITY_CHANGED",
            "RESERVED_PATH",
            "UNSAFE_PATH",
        }:
            raise MoveFileError(
                "RESERVED_PATH",
                "path is reserved for its owning subsystem",
            ) from None
        raise MoveFileError("MOVE_FAILED", "held file acquisition was refused") from None
    return old_abs, old_rel, stored_original


def _move_file_local(
    vault_root: Path,
    *,
    old_path: str,
    new_path: str,
    update_wikilinks: bool = True,
    allow_curated: bool = False,
    today: dt.date | None = None,
    promotion_reason: str | None = None,
    content_transform: Callable[[str], str] | None = None,
    extra_writes: tuple[PlannedWrite, ...] = (),
    validate_only: bool = False,
    _validate_prepared_bindings: Callable[[], None] | None = None,
    originals_context: ExitStack,
    mutation_context: ExitStack,
    lineage_entry: Mapping[str, Any] | None = None,
    undo_lineage: bool = False,
    preview: bool = False,
    preview_digest: str | None = None,
    record_repoints: bool = False,
    _preview_call: Mapping[str, object] = _MOVE_PREVIEW,
    _stage: Callable[[str], None] | None = None,
    _exact_content: bool = False,
    _caller_judges_contract: bool = False,
) -> MoveFileResult | MoveFileValidation:
    """Relocate a file, optionally rewriting wikilinks that point at it.

    `content_transform` lets a caller that owns a *declared* content change —
    today only source reclassification — supply the moved file's new bytes. It
    runs after the self-wikilink pass, so the append-only refusal that pass
    raises still fires first: the guard exists to stop an incidental link
    rewrite mutating append-only bytes, not to stop a caller that has already
    justified the change. `extra_writes` joins the same atomic batch so a
    caller's own registry updates commit with the move or not at all.
    A caller's binding witness is checked before rename and batch publication.
    """
    # None in file mode, and for a move between two paths that the store does not own. A store-mode
    # move of a page is one store transaction, planned against one snapshot of the store.
    store_writer = None
    if store_targets.owns_any(vault_root, files=(old_path, new_path)):
        try:
            ends = [store_targets.relative(vault_root, path) for path in (old_path, new_path)]
        except VaultPathError as e:
            raise MoveFileError(code=e.code, reason=e.reason) from e
        store_targets.refuse_crossing("move_file", ends)
        # A validation writes nothing, so it reads the store off the writer thread a commit needs.
        store_writer = (
            _STORE_VALIDATION if validate_only else store_availability.page_writer(vault_root)
        )
    # The read phase sees one revision; it ends before the commit, which rechecks what it read.
    reading = mutation_context.enter_context(ExitStack())
    if store_writer is not None:
        reading.enter_context(store_read.request_snapshot(vault_root))
    if store_writer is None:
        old_abs, old_rel, stored_original = _resolve_source_on_files(vault_root, old_path)
    else:
        # A store key is an exact path: the store answers NOT_FOUND and withheld as a read does.
        try:
            old_abs, old_rel = store_targets.resolve_existing(vault_root, old_path)
        except VaultPathError as e:
            # The store records no binary for a digest-addressed original: its companion page is the filing.
            addressed = (
                _evicted_source(vault_root, old_path, addressed_only=True) if e.code == "NOT_FOUND" else None
            )
            if addressed is None:
                raise MoveFileError(code=e.code, reason=e.reason) from e
            old_abs, old_rel, stored_original = addressed
        else:
            stored_original = store_targets.evicted_original(vault_root, old_rel)
    from .collection_store import authority as collection_authority

    collection_authority.refuse_view_paths(vault_root, (old_rel,), MoveFileError)
    try:
        new_abs, new_rel = resolve_under_vault(vault_root, new_path)
    except VaultPathError as e:
        raise MoveFileError(code=e.code, reason=e.reason) from e
    respelled = respell_new_path(vault_root, new_rel, replacing=old_rel)
    if respelled == old_rel != new_rel:
        # Re-spelling led back to the file's own folder: only moving that
        # folder can change its spelling.
        raise MoveFileError(
            code="PATH_VARIANT_EXISTS",
            reason=(
                f"folder already exists as '{old_rel.rsplit('/', 1)[0]}'; "
                "move that folder to change its spelling"
            ),
        )
    if respelled != new_rel:
        new_abs, new_rel = vault_root / respelled, respelled
    from .governance import raw_protection

    if raw_protection.marked(old_rel) and not raw_protection.marked(new_abs.name):
        new_abs = new_abs.with_name(raw_protection.PREFIX + new_abs.name)
        new_rel = new_abs.relative_to(vault_root).as_posix()
    _admit_destination(vault_root, new_rel)
    # Decided before any destination existence check, so it cannot tell a withheld view from none.
    collection_authority.refuse_view_paths(vault_root, (new_rel,), MoveFileError)
    if _in_episode_folder(vault_root, old_rel) or _in_episode_folder(vault_root, new_rel):
        raise MoveFileError(
            code="EPISODE_KIND_RESERVED",
            reason=(
                f"Sources/{source_taxonomy.EPISODE_PATH_LABEL}/ holds only conversation "
                "recaps saved as episodes: nothing moves into it, and a recap "
                "is not moved, renamed or promoted out of it"
            ),
        )

    # An artifact and its page move as one unit. Whichever half the caller
    # named, the operation is normalized onto the page — that is the `.md` path
    # the semantic write machinery below is written for — and the bytes are
    # renamed alongside it inside the same transaction.
    paired_binary: tuple[str, str] | None = None
    # A digest-addressed original moves by repointing its companion; no bytes are renamed or fetched.
    addressed_binary = False
    paired_parts: tuple[str, str] | None = None
    # A raw-marked page's part pages that the store holds, with their hashes.
    stored_parts: dict[str, str] = {}
    moved_parts: list[Path] = []
    reconcile_parts = False
    recovering_from_trash = trash_metadata.is_trash_path(old_rel) and not trash_metadata.is_trash_path(new_rel)
    # Recovery owns retirement of a Trash reference companion; it is not a page relocation.
    pair = (
        None
        if recovering_from_trash
        else _paired_artifact(
            vault_root,
            old_rel,
            None if store_writer is None else partial(store_targets.holds, vault_root),
        )
    )
    if pair is not None:
        page_rel, binary_rel = pair
        if old_rel == binary_rel:
            if not new_rel.lower().endswith(".md") or raw_protection.marked(old_rel):
                paired_binary = (binary_rel, new_rel)
                new_rel = f"{new_rel}.md"
                new_abs = new_abs.with_name(new_abs.name + ".md")
            else:
                paired_binary = (binary_rel, new_rel[:-3])
            old_rel = page_rel
            old_abs = old_abs.with_name(old_abs.name + ".md")
        else:
            new_binary = new_rel[:-3] if new_rel.lower().endswith(".md") else new_rel
            if not new_rel.lower().endswith(".md"):
                new_rel = f"{new_rel}.md"
                new_abs = new_abs.with_name(new_abs.name + ".md")
            paired_binary = (binary_rel, new_binary)
        _admit_destination(vault_root, new_rel)
        _admit_destination(vault_root, paired_binary[1])
        collection_authority.refuse_view_paths(
            vault_root, (old_rel, new_rel, *paired_binary), MoveFileError
        )
        if (
            (vault_root / paired_binary[1]).exists()
            or originals.available(vault_root, paired_binary[1])
            or (store_writer is not None and store_targets.holds(vault_root, paired_binary[1]))
        ):
            raise MoveFileError(
                code="DEST_EXISTS",
                reason=(
                    f"destination already exists: {paired_binary[1]}. "
                    f"This op refuses to overwrite — pick a different name."
                ),
            )
        content_transform = _compose_transforms(
            content_transform, _repoint_artifact(*paired_binary)
        )
        # A store record that an older eviction left still moves with its bytes, as before.
        addressed_binary = originals.digest_addressed(vault_root, paired_binary[0]) is not None and not (
            store_writer is not None and store_targets.holds(vault_root, paired_binary[0])
        )
        # A shared original keeps no file at its path: renamed to a suffix-less name its companion
        # would read as a note and the census could no longer name it to a restricted caller.
        if addressed_binary and not originals.may_share_bytes(paired_binary[1]):
            suffix = PurePosixPath(paired_binary[0]).suffix
            example = f", such as '{paired_binary[1]}{suffix}'" if suffix else ""
            raise MoveFileError(
                code="SHARED_ORIGINAL_NEEDS_EXTENSION",
                reason=(
                    f"A shared original keeps its filename extension, so it cannot be renamed "
                    f"to '{paired_binary[1]}'. Add an extension to the destination name{example}."
                ),
            )
    if not recovering_from_trash and old_rel.lower().endswith(".md"):
        # The hidden pages that hold the rest of a long extraction move with their page, paired
        # with an original or not.
        from . import extraction_parts

        old_folder = extraction_parts.folder_for(Path(old_rel))
        if store_writer is not None and store_ownership.owns_folder(old_folder.as_posix()):
            # A raw-marked page's parts are pages: the store moves them, and holds the destination.
            new_folder = extraction_parts.folder_for(Path(new_rel)).as_posix()
            with store_read.snapshot(vault_root) as view:
                stored_parts = {meta.path: meta.sha256 for meta in view.pages(f"{old_folder.as_posix()}/")}
                parts_taken = view.holds_below(new_folder)
            if stored_parts:
                paired_parts = (old_folder.as_posix(), new_folder)
                if parts_taken:
                    raise MoveFileError(
                        code="DEST_EXISTS",
                        reason=f"destination already exists: {new_folder}. This op refuses to overwrite.",
                    )
                content_transform = _compose_transforms(content_transform, _repoint_parts(new_rel))
        elif (vault_root / old_folder).is_dir():
            moved_parts = sorted(
                old_folder / entry.name
                for entry in (vault_root / old_folder).iterdir()
                if extraction_parts.part_number(entry) is not None
            )
            paired_parts = (old_folder.as_posix(), extraction_parts.folder_for(Path(new_rel)).as_posix())
            if (vault_root / paired_parts[1]).exists():
                raise MoveFileError(
                    code="DEST_EXISTS",
                    reason=f"destination already exists: {paired_parts[1]}. This op refuses to overwrite.",
                )
            content_transform = _compose_transforms(content_transform, _repoint_parts(new_rel))

    if store_writer is not None:
        store_targets.refuse_kind_change("move_file", old_rel, new_rel)
        # The store decides a page's destination: a file left there by a queued delete is no page.
        if reserved_paths.classify_logical(new_rel).blocked:
            raise MoveFileError(
                "RESERVED_PATH", "destination is reserved for its owning subsystem"
            )
        if store_targets.holds(vault_root, new_rel):
            raise MoveFileError(
                code="DEST_EXISTS",
                reason=(
                    f"destination already exists: {new_rel}. "
                    f"This op refuses to overwrite — pick a different name."
                ),
            )
    else:
        _refuse_taken_destination(vault_root, new_rel)
    if not recovering_from_trash:
        _refuse_reserved_destination(vault_root, old_rel, new_rel, validate_only=validate_only)

    if (held := source_custody.erase_refusal_at(vault_root, old_rel)) is not None:
        raise MoveFileError(*held)

    # Append-only guards. Rule 2 protects content *immutability*, not file
    # *location*: a move that stays WITHIN the same append-only tree
    # (Sources/ -> Sources/, Evidence/ -> Evidence/) carries the bytes
    # verbatim and only relocates them, so it is permitted — this is the
    # sanctioned way to organize Sources/ into themed sub-folders. Crossing
    # the boundary is still forbidden in both directions: moving OUT of an
    # append-only tree, or INTO one from elsewhere (those go via `add` /
    # `preserve`).
    src_append = in_append_only_tree(old_rel)
    dst_append = in_append_only_tree(new_rel)
    intra_append = bool(src_append) and src_append == dst_append
    # Sources -> Evidence is a *promotion*: the same raw item, reclassified once
    # it becomes proof-bearing. Whether an item is a Source or Evidence is a
    # judgement about purpose, made at capture time when the answer is often
    # unknowable — a receipt is reference material until the appliance fails.
    # The move carries bytes verbatim, exactly like a sanctioned intra-tree
    # relocation, so content immutability is untouched.
    #
    # The reverse is refused, and the asymmetry is the point. `Evidence/<scope>/`
    # claims to hold everything preserved for that case; handing over a case
    # means handing over that folder. Removing an item alters what the folder
    # claims to contain — bytes unchanged, integrity broken. `Sources/` carries
    # no such completeness property, so promoting out of it subtracts from
    # nothing.
    promotion = src_append == "Sources" and dst_append == "Evidence"
    if promotion and not (promotion_reason or "").strip():
        raise MoveFileError(
            code="PROMOTION_REASON_REQUIRED",
            reason=(
                f"promoting {old_rel} into {dst_append}/ reclassifies it as "
                f"proof-bearing; supply `promotion_reason` naming the claim, "
                f"case, dispute, or record it is being preserved for."
            ),
        )
    if not intra_append and not promotion:
        if src_append == "Evidence":
            raise MoveFileError(
                code="APPEND_ONLY",
                reason=(
                    f"{old_rel} is in Evidence/ and cannot be moved out. A case "
                    f"scope must stay complete: handing over a case means "
                    f"handing over its folder, so removing an item changes what "
                    f"that folder claims to contain. Relocation WITHIN "
                    f"Evidence/ is allowed."
                ),
            )
        if src_append:
            raise MoveFileError(
                code="APPEND_ONLY",
                reason=(
                    f"{old_rel} is in {src_append}/ which is append-only "
                    f"(SKILL.md rule 2). Moves OUT of {src_append}/ are "
                    f"forbidden; relocation WITHIN {src_append}/ is allowed, "
                    f"and Sources/ may be promoted into Evidence/."
                ),
            )
        if dst_append:
            raise MoveFileError(
                code="APPEND_ONLY",
                reason=(
                    f"destination {new_rel} is in {dst_append}/. "
                    f"Use `add` (sources) or `preserve` (evidence) to land "
                    f"content there from outside {dst_append}/."
                ),
            )

    # Curated-tree guards on EITHER end.
    src_curated = in_curated_tree(old_rel)
    dst_curated = in_curated_tree(new_rel)
    if (src_curated or dst_curated) and not allow_curated:
        which = src_curated or dst_curated
        raise MoveFileError(
            code="CURATED_PROTECTED",
            reason=(
                f"move touches curated tree {which!r}. "
                f"Pass `allow_curated=true` to override."
            ),
        )

    if recovering_from_trash:
        from . import recover_from_trash as recovery

        _admit_publication(
            vault_root,
            new_rel,
            (
                (store_read.owned_bytes(vault_root, old_rel) if store_writer else old_abs.read_bytes())
                if new_rel.lower().endswith(".md")
                else None
            ),
        )
        reading.close()

        try:
            recovered = recovery.recover_from_trash(
                vault_root,
                trash_path=old_rel,
                restore_path=new_rel,
                allow_curated=allow_curated,
                today=today,
            )
        except recovery.RecoverError as error:
            raise MoveFileError(code=error.code, reason=error.reason) from error
        return MoveFileResult(
            old_path=old_rel,
            new_path=new_rel,
            wikilinks_updated=0,
            files_touched=[],
            warnings=recovered.warnings,
            semantic=recovered.semantic,
            index=recovered.index,
        )

    from .governance import connector_boundary

    view = mover_view(vault_root)
    visible, restricted, limited_mover = view.visible, view.restricted, view.limited
    # Scan inbound links BEFORE the move, while the old path still exists.
    stored_check: store_targets.InboundCheck | None = None
    if store_writer is not None and update_wikilinks:
        # The store's links and the disk's, each once; the commit binds the store's linking pages.
        stored_check = store_targets.inbound_links(vault_root, [old_rel])[old_rel]
        inbound = list(stored_check.links)
    else:
        inbound = (
            find_inbound_wikilinks(
                vault_root, old_rel, visible=visible if limited_mover else None, lineage=False
            )
            if update_wikilinks
            else []
        )
    if limited_mover:
        inbound = [hit for hit in inbound if visible(hit.path)]
    # Pages that cite the moved paths in prose come from the catalogue, never from a walk.
    mention_lookup = (
        path_mentions.find_pages(
            vault_root, [old_rel, *([paired_binary[0]] if paired_binary is not None else [])]
        )
        if update_wikilinks
        else None
    )
    named_in = [
        rel
        for rel in (mention_lookup.pages if mention_lookup is not None else ())
        if not limited_mover or visible(rel)
    ]

    warnings: list[str] = folder_split_warnings(vault_root, new_abs.parent)
    if mention_lookup is not None and not mention_lookup.complete:
        warnings.append(path_mentions.NOT_CHECKED)
    if (
        stored_check is not None
        and stored_check.unimported
        and store_targets.caller_is_owner(vault_root)
    ):
        # A file that nothing imported has no store page to rewrite; it keeps its link. Only the
        # owner hears of it: no policy covers such a file yet.
        warnings.append(
            f"{len({link.path for link in stored_check.unimported})} file(s) that the store has "
            "not imported yet keep their link to the old path"
        )
    files_touched: list[str] = []
    wikilinks_updated = 0
    # For any other mover, every linking page is rewritten, including pages the
    # mover may not see, so the vault stays consistent; the counts and paths
    # reported to a mover other than the owner cover the pages it may see, by a
    # policy or by RAW. The activity log keeps the full figures.
    reported_touched: list[str] = []
    reported_updated = 0
    # For such a mover, a withheld linker the move cannot rewrite cleanly is
    # left as it is, so its link dangles exactly as with
    # `update_wikilinks=false`; the owner's audit reports it. Its change count
    # is kept here so the full figures drop it too.
    hidden_changes: dict[str, int] = {}
    hidden_mentions: dict[str, int] = {}
    mentions_updated = 0
    reported_mentions = 0
    kept_mentions = 0

    # Stage inbound-link rewrites. The file itself moves with one filesystem
    # rename so bytes of any type are preserved without a copy/unlink window.
    writes: list[PlannedWrite] = []
    batch_index_reports: list[object] = []
    batch_fanout_paths: list[Path] = []
    left_unchanged: list[str] = []
    writable_page = connector_boundary.effect_admission(vault_root)
    # A restricted caller may be denied a page that the move it undoes repointed, so its undo
    # cannot restore every repoint. It records the path it vacates instead, as a move back does,
    # whatever pages exist: its answer and the page's bytes never depend on a withheld page, and
    # a withheld page that still links the vacated path resolves through the lineage.
    keep_vacated = undo_lineage and restricted
    keeps_lineage = records_lineage(
        vault_root, old_rel, new_rel, undo=undo_lineage and not keep_vacated
    )
    store_rows = (
        store_rows_outcome(vault_root, KEPT if keeps_lineage else KEPT_DANGLING) if preview else None
    )
    # One outcome per referrer the mover may see, and the pointer writes the move makes to
    # append-only pages: those it restores from the records of the move it undoes, and the
    # others, which the call does not name and the preview digest approves.
    referrers: list[dict[str, str]] = []
    repoints: list[Any] = []
    restored: list[Any] = []
    refused: list[MoveFileError] = []
    if update_wikilinks and (inbound or named_in):
        for item in plan_inbound_rewrites(
            vault_root, old_rel, new_rel, inbound, named_in=named_in, paired=paired_binary,
            undo=undo_lineage,
        ):
            rel = item.path
            shown = visible is None or visible(rel)
            unchanged = KEPT if keeps_lineage else KEPT_DANGLING
            if item.left_mentions and shown:
                kept_mentions += 1
            if not (item.changes or item.mentions):
                if shown and item.left_mentions:
                    referrers.append({"path": rel, "outcome": unchanged})
                continue
            if not writable_page(rel):
                # A page the mover may not write keeps its link and the move commits. Only a page
                # the mover may see is counted, so a withheld page leaves the answer as it is without it.
                if shown:
                    left_unchanged.append(rel)
                    referrers.append({"path": rel, "outcome": unchanged})
                continue
            if not shown:
                if item.append_only:
                    continue
                hidden_changes[rel] = item.changes
                hidden_mentions[rel] = item.mentions
            if item.refusal is not None:
                code, reason = item.refusal
                referrers.append({"path": rel, "outcome": REFUSED, "code": code, "cause": reason})
                refused.append(MoveFileError(code=code, reason=reason))
                continue
            if item.repoint is not None:
                (restored if item.restored else repoints).append(item.repoint)
            if shown:
                referrers.append(
                    {"path": rel, "outcome": REPOINTED if item.repoint is not None else REWRITTEN}
                )
            writes.append(
                PlannedWrite(path=item.absolute, content=item.content, guard=item.guard)
            )
            files_touched.append(rel)
            wikilinks_updated += item.changes
            mentions_updated += item.mentions
            if shown:
                reported_touched.append(rel)
                reported_updated += item.changes
                reported_mentions += item.mentions
    moves = {old_rel: new_rel}
    digest = None if refused else pointer_write.preview_digest(moves, repoints)
    if refused and not preview:
        raise refused[0]
    if preview and refused:
        # A refused move cannot commit, so its preview stops at the outcomes it reports.
        return MoveFileResult(
            old_path=old_rel,
            new_path=new_rel,
            wikilinks_updated=reported_updated,
            files_touched=list(reported_touched),
            warnings=list(warnings),
            path_mentions_updated=reported_mentions,
            referrers=referrers,
            store_rows=store_rows,
        )
    if not validate_only:
        try:
            pointer_write.require_preview(
                moves, repoints, preview_digest,
                preview_call=capabilities.active_call_text("manage_memory_file", _preview_call),
            )
        except pointer_write.PointerWriteError as error:
            raise MoveFileError(code=error.code, reason=error.reason) from None
    # An undoable move's lineage entry names its operation whether or not it repoints a page, so
    # the page never says how many it repointed; owner-only state maps the operation to them.
    operation = pointer_write.new_operation_id() if record_repoints else None
    if operation is not None:
        lineage_entry = {**dict(lineage_entry or {}), page_lineage.OPERATION_KEY: operation}
    repoint_records = [(write, pointer_write.new_record_id()) for write in repoints]
    restore_records = [(write, pointer_write.new_record_id()) for write in restored]
    # The moved append-only page's own lineage write, proven once its final bytes are known.
    own_records: list[tuple[pointer_write.PointerWrite, str]] = []

    def record_pointer_writes() -> list[str]:
        """Record each pointer write around the commit: its record outlives the move only when
        its page holds the write (`pointer_write.recording`)."""
        return [
            *mutation_context.enter_context(
                pointer_write.recording(
                    vault_root, [*repoint_records, *own_records], operation=operation
                )
            ),
            *mutation_context.enter_context(pointer_write.recording(vault_root, restore_records)),
        ]

    if kept_mentions:
        # Captured pages cannot change; the page count carries no path, so a restricted caller keeps it.
        warnings.append(
            f"{kept_mentions} append-only page(s) cite the old path in prose and keep it; "
            "a Source's own pointer is repaired with reclassify_source and artifact_path"
        )
    if left_unchanged:
        # No path in the sentence: a restricted caller's response drops a warning that embeds one.
        warnings.append(f"{len(left_unchanged)} visible linking page(s) outside your write scope keep their link to the old path")

    def validation_result(
        *, source_hash: str, destination_hash: str, after_text: str | None = None
    ) -> MoveFileValidation:
        before = [{"path": old_rel, "content_hash": source_hash}]
        after = [
            {"path": old_rel, "absent": True},
            {"path": new_rel, "content_hash": destination_hash},
        ]
        for write in writes:
            relative = write.path.relative_to(vault_root).as_posix()
            guard_hash = getattr(write.guard, "expected_content_hash", None)
            if not isinstance(guard_hash, str):
                raise MoveFileError(
                    "MOVE_FAILED", "canonical inbound rewrite lacks an exact source guard"
                )
            before.append({"path": relative, "content_hash": guard_hash})
            after.append({"path": relative, "content_hash": content_hash(write.content)})
        return MoveFileValidation(
            MoveFileResult(
                old_path=old_rel,
                new_path=new_rel,
                wikilinks_updated=reported_updated,
                files_touched=list(reported_touched),
                warnings=list(warnings),
                path_mentions_updated=reported_mentions,
                referrers=referrers if preview else None,
                preview_digest=digest if preview else None,
                store_rows=store_rows,
            ),
            tuple(before),
            tuple(after),
            (
                {"path": old_rel, "absent": True},
                {"path": new_rel, "content_hash": source_hash},
                *tuple(before[1:]),
            ),
            paired_binary is None
            and old_rel.lower().endswith(".md")
            and new_rel.lower().endswith(".md"),
            after_text,
        )

    today = today or dt.date.today()

    def plan_activity_log() -> tuple[str, str, LogWritePlan]:
        date_iso = today.isoformat()
        new_rel_no_ext = (
            new_rel.removesuffix(".md") if new_rel.endswith(".md") else new_rel
        )
        # The activity log is readable by other audiences: for a mover other
        # than the owner it carries the figures that mover may see.
        logged_updated, logged_files = (
            (reported_updated, len(reported_touched))
            if restricted
            else (wikilinks_updated, len(files_touched))
        )
        logged_mentions = reported_mentions if restricted else mentions_updated
        body = (
            f"Moved {old_rel!r} → {new_rel!r} via exomem Tier 2. "
            f"wikilinks_updated={logged_updated} across "
            f"{logged_files} file(s)."
        )
        if logged_mentions:
            body += f" path_mentions_updated={logged_mentions}."
        if src_curated or dst_curated:
            body += f" allow_curated=true (tree: {src_curated or dst_curated})."
        if promotion:
            body += f" Promoted Sources → Evidence: {promotion_reason.strip()}"
        return (
            new_rel_no_ext,
            body,
            plan_log_entry(
                vault_root,
                date_iso=date_iso,
                op="move_file",
                rel_path_no_ext=new_rel_no_ext,
                body=body,
            ),
        )

    semantic: dict | None = None
    semantic_states: dict[str, semantic_index.SemanticParentIndexState] = {}
    catalog_target: catalog_publication.PreparedMarkdownCatalogPublication | None = None
    log_rel_no_ext: str
    log_body: str
    log_plan: LogWritePlan
    if old_rel.lower().endswith(".md") and new_rel.lower().endswith(".md"):
        # The page's lineage changes last, so it records the move of the final bytes.
        declared_transform = content_transform
        lineage = (
            None
            if _exact_content
            else _lineage_transform(
                old_rel, new_rel, lineage_entry, undo_lineage, keep_vacated=keep_vacated
            )
        )
        if lineage is not None:
            content_transform = _compose_transforms(declared_transform, lineage)
        try:
            source, source_guard = read_guarded_text(vault_root, old_abs)
            moved_source = source
            if update_wikilinks:
                moved_source, source_changes = _rewrite_wikilinks(
                    source, old_rel, new_rel
                )
                if source_changes:
                    if src_append:
                        raise MoveFileError(
                            code="APPEND_ONLY",
                            reason=(
                                f"updating self-links would rewrite moved source "
                                f"{old_rel} in append-only {src_append}/. Retry "
                                f"with `update_wikilinks=false` to preserve exact "
                                f"bytes and run final-corpus semantic evaluation."
                            ),
                        )
                    files_touched.append(old_rel)
                    wikilinks_updated += source_changes
                    reported_touched.append(old_rel)
                    reported_updated += source_changes
            if declared_transform is not None:
                moved_source = declared_transform(moved_source)
            if lineage is not None:
                declared_source, moved_source = moved_source, lineage(moved_source)
                if src_append and not undo_lineage:
                    own_records[:] = [(
                        _own_lineage_write(vault_root, new_rel, declared_source, moved_source,
                                           f"lineage of the move of {old_rel} to {new_rel}"),
                        pointer_write.new_record_id(),
                    )]
            _admit_publication(vault_root, new_rel, moved_source.encode("utf-8"))
            log_rel_no_ext, log_body, log_plan = plan_activity_log()
            destination_guard = (
                PathGuard.capture(vault_root, new_rel, leaf_policy="absent")
                if store_writer is None
                else PathGuard.capture_store(vault_root, new_rel)
            )
            def run_preflight() -> semantic_writes.MovePreflight:
                call_spans.measure(call_spans.MOVE_PREFLIGHTS)
                from . import state_paths

                # Reuse host placement for this read-only preflight; policy and commit checks stay live.
                with state_paths.resolution_scope(), call_spans.measure_ms(call_spans.MOVE_PREFLIGHT_MS):
                    return semantic_writes.preflight_move(
                        vault_root,
                        old_path=old_rel,
                        new_path=new_rel,
                        source=source,
                        moved_source=moved_source,
                        source_guard=source_guard,
                        destination_guard=destination_guard,
                        rewrites=writes,
                        content_transform=content_transform,
                    )

            preflight = run_preflight()
            while visible is not None:
                # A withheld linker whose rewrite its contract would refuse
                # is skipped, and the move is judged again without it.
                refused = {
                    item.after.path
                    for item in preflight.evaluations
                    if item.after.path in hidden_changes
                    and item.contract_result.should_block
                }
                if not refused:
                    break
                writes[:] = [
                    write
                    for write in writes
                    if write.path.relative_to(vault_root).as_posix() not in refused
                ]
                for rel in refused:
                    files_touched.remove(rel)
                    wikilinks_updated -= hidden_changes.pop(rel)
                    mentions_updated -= hidden_mentions.pop(rel, 0)
                log_rel_no_ext, log_body, log_plan = plan_activity_log()
                preflight = run_preflight()
            semantic_states = {
                item.after.path: semantic_index.from_semantic_page_state(item.after)
                for item in preflight.evaluations
            }

            def prepared_catalog(
                lifecycle_writes: tuple[PlannedWrite, ...],
            ) -> catalog_publication.PreparedMarkdownCatalogPublication | None:
                """The catalog publication of this move, prepared before anything changes."""
                catalog_removals = [
                    catalog_publication.CatalogRemoval(
                        old_rel,
                        content_hash(source),
                    )
                ]
                if paired_binary is not None:
                    catalog_removals.append(
                        catalog_publication.CatalogRemoval(
                            paired_binary[0],
                            None,
                        )
                    )
                catalog_writes = [
                    *lifecycle_writes,
                    PlannedWrite(
                        path=new_abs,
                        content=moved_source,
                        create_only=True,
                    ),
                    *writes,
                    *extra_writes,
                    *log_plan.writes,
                ]
                try:
                    graph_states = {
                        item.after.path: item.after for item in preflight.evaluations
                    }

                    def graph_replacement_provider():
                        return graph_producer.replacements_for_semantic_transition(
                            vault_root,
                            before_corpus=preflight.before_corpus,
                            after_corpus=preflight.after_corpus,
                            writes=tuple(catalog_writes),
                            semantic_states=graph_states,
                        )

                    return catalog_publication.prepare_catalog_membership_batch(
                        vault_root,
                        writes=tuple(catalog_writes),
                        removals=tuple(catalog_removals),
                        graph_replacement_provider=graph_replacement_provider,
                        now=int(time.time()),
                        # The part pages follow the page only after the catalog is ready.
                        part_folders=(
                            {new_rel: paired_parts[0]} if paired_parts is not None else {}
                        ),
                    )
                except catalog_publication.CatalogPublicationError as error:
                    raise MoveFileError(
                        code="GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                        reason=str(error),
                    ) from error

            if validate_only:
                if not (preview or _caller_judges_contract):
                    # A curation plan seals this validation, so a step that validates is one
                    # whose contract its commit accepts.
                    semantic_writes.refuse_blocked_move(preflight)
                if store_writer is not None:
                    # Prepared as the commit prepares it, so a move that validates is one whose
                    # catalog accepts it; file mode prepares it only at the commit.
                    prepared_catalog(())
                validation = validation_result(
                    source_hash=content_hash(source),
                    destination_hash=content_hash(moved_source),
                    after_text=moved_source,
                )
                return validation.result if preview else validation

            def mutate(
                lifecycle_writes: tuple[PlannedWrite, ...],
                required_guards,
                bound_destination: PathGuard,
            ) -> None:
                # The renames run before the batch, so one projection delta covers both.
                with media_bindings.operation(vault_root, (old_rel, new_rel, *(paired_binary or ()))):
                    _mutate_files(lifecycle_writes, required_guards, bound_destination)

            def _mutate_files(
                lifecycle_writes: tuple[PlannedWrite, ...],
                required_guards,
                bound_destination: PathGuard,
            ) -> None:
                nonlocal catalog_target
                bound_destination.recheck(vault_root)
                catalog_target = prepared_catalog(lifecycle_writes)
                destination_writes = (
                    # Rename preserves source bytes; bind the overwrite to that exact preimage.
                    [PlannedWrite(
                        path=new_abs,
                        content=moved_source,
                        expected_hash=content_hash(source),
                    )]
                    if moved_source != source
                    else []
                )
                combined = [
                    *lifecycle_writes,
                    *destination_writes,
                    *writes,
                    *extra_writes,
                ]
                if catalog_target is not None:
                    combined.extend(log_plan.writes)
                # The moved page is the move's one piece of work, on its new
                # path; the link rewrites around it earn nothing (review F1).
                if _validate_prepared_bindings is not None:
                    _validate_prepared_bindings()
                if not addressed_binary:
                    _materialize_originals(vault_root, originals_context, old_rel)
                heat_move = working_set_heat.begin_commit(vault_root, [new_abs])
                try:
                    move_epoch = graph_sync.prepare_rename_epoch(
                        vault_root,
                        [(old_rel, new_rel), *([paired_binary] if paired_binary is not None else [])],
                    )
                except BaseException:
                    working_set_heat.abandon_commit(heat_move)
                    raise
                try:
                    if _stage is not None:
                        _stage("before-rename")
                    _held_rename(vault_root, old_rel, new_rel)
                    # The bytes follow the page inside the same transaction.
                    # A rollback below undoes both, so a failure can never
                    # leave an artifact split across two trees.
                    if paired_binary is not None and not addressed_binary:
                        try:
                            _held_rename(vault_root, *paired_binary)
                        except MoveFileError:
                            _held_rename(vault_root, new_rel, old_rel, rollback=True)
                            raise
                    if paired_parts is not None:
                        try:
                            _held_rename(vault_root, *paired_parts, kind="directory")
                        except MoveFileError:
                            if paired_binary is not None and not addressed_binary:
                                _held_rename(vault_root, paired_binary[1], paired_binary[0], rollback=True)
                            _held_rename(vault_root, new_rel, old_rel, rollback=True)
                            raise
                    if _stage is not None:
                        _stage("after-rename")
                except BaseException:
                    working_set_heat.abandon_commit(heat_move)
                    if move_epoch is not None:
                        # A held rename can fail after mutation; retain its floor and repair custody.
                        graph_sync.provision_rename_recovery(move_epoch)
                    raise
                child_started = False
                try:
                    if move_epoch is not None:
                        graph_sync.publish_rename_epoch(move_epoch)
                    if combined:
                        batch_fanout_paths[:] = [write.path for write in combined]
                        with working_set_heat.primary_paths(()):
                            child_started = True
                            batch_atomic_write(
                                combined,
                                vault_root=vault_root,
                                required_guards=required_guards,
                                index_reports=batch_index_reports,
                                _validate_prepared_bindings=_validate_prepared_bindings,
                                semantic_states={
                                    write.path.relative_to(vault_root).as_posix(): semantic_states[
                                        write.path.relative_to(vault_root).as_posix()
                                    ]
                                    for write in combined
                                    if write.path.relative_to(vault_root).as_posix()
                                    in semantic_states
                                },
                            )
                except Exception as error:
                    working_set_heat.abandon_commit(heat_move)
                    if _move_outcome_is_unproved(error, move_epoch, child_started):
                        if move_epoch is not None:
                            graph_sync.provision_rename_recovery(move_epoch)
                        raise
                    log.exception(
                        "move_file: link-update batch failed for %s -> %s",
                        old_rel,
                        new_rel,
                    )
                    try:
                        if paired_parts is not None:
                            _held_rename(vault_root, paired_parts[1], paired_parts[0], rollback=True, kind="directory")
                        if paired_binary is not None and not addressed_binary:
                            _held_rename(vault_root, paired_binary[1], paired_binary[0], rollback=True)
                        _held_rename(vault_root, new_rel, old_rel, rollback=True)
                    except MoveFileError as rollback_error:
                        if move_epoch is not None:
                            graph_sync.provision_rename_recovery(move_epoch)
                        raise RuntimeError(
                            f"move link rewrite failed ({error}); rename rollback also "
                            f"failed: {rollback_error}"
                        ) from error
                    if move_epoch is not None:
                        graph_sync.restore_deletion_epoch(move_epoch)
                    raise
                if _stage is not None:
                    _stage("after-batch")
                heat_moved.append(working_set_heat.observe_commit(heat_move))

            if visible is not None:
                # A page withheld from this mover whose bytes the move does not
                # rewrite stays in the closure for publication, but is never
                # asserted against the move: its standing is the owner's to
                # review.
                rewritten = {
                    write.path.relative_to(vault_root).as_posix() for write in writes
                }
                preflight = dataclasses.replace(
                    preflight,
                    waived_blockers=frozenset(
                        item.after.path
                        for item in preflight.evaluations
                        if not visible(item.after.path) and item.after.path not in rewritten
                    ),
                )
            def mutate_in_store(
                lifecycle_writes: tuple[PlannedWrite, ...],
                required_guards,
                bound_destination: PathGuard,
            ) -> None:
                """Move the page and rewrite its linkers in one store transaction.

                The projector renames the file afterwards. The commit moves the record of the
                original that pairs with the page, and that original and a dot folder of its parts
                are renamed right after the commit; a raw-marked page's parts move in the store. A
                changed source, a link gained since the scan, or a taken destination refuses the
                whole commit with nothing changed.
                The log entry commits with the move; the catalog, prepared as file mode prepares it,
                publishes after ``commit_move`` returns.
                """
                from .vault_store import batch as store_batch
                from .vault_store import binaries
                from .vault_store.write import Expect, PageMove

                nonlocal catalog_target
                bound_destination.recheck(vault_root)
                catalog_target = prepared_catalog(lifecycle_writes)
                entries: list[binaries.Entry] = []
                # A digest-addressed original has no store record: the page move carries its repointed binding.
                if paired_binary is not None and not addressed_binary:
                    entries.append(binaries.Entry(*paired_binary))
                    _materialize_originals(vault_root, originals_context, paired_binary[0])
                if paired_parts is not None and not stored_parts:
                    entries.append(binaries.Entry(*paired_parts, folder=True))
                with store_read.snapshot(vault_root) as view:
                    relocation = binaries.plan(view, entries)
                part_moves = {
                    part: f"{paired_parts[1]}{part[len(paired_parts[0]):]}"
                    for part in stored_parts
                } if paired_parts is not None else {}
                moved = store_batch.Lifecycle(
                    moves=(
                        PageMove(
                            old_rel,
                            new_rel,
                            body=None if moved_source == source else moved_source.encode("utf-8"),
                        ),
                        *(PageMove(part, moved_to) for part, moved_to in part_moves.items()),
                    ),
                    expect={
                        old_rel: Expect(
                            sha256=source_guard.expected_content_hash,
                            linked_from=None if stored_check is None else stored_check.bound,
                        ),
                        new_rel: Expect(absent=True),
                        **{part: Expect(sha256=digest) for part, digest in stored_parts.items()},
                        **{moved_to: Expect(absent=True) for moved_to in part_moves.values()},
                        **relocation.expect(),
                    },
                )
                # The moved page is the move's one piece of work; its link rewrites earn nothing.
                with working_set_heat.primary_paths((new_rel,)):
                    left = _commit_in_store(
                        vault_root,
                        store_writer,
                        relocation,
                        [*lifecycle_writes, *writes, *extra_writes, *log_plan.writes],
                        moved,
                        sources={old_rel, *stored_parts, *relocation.recorded},
                        destinations={new_rel, *part_moves.values(), *(e.dst for e in entries)},
                        required_guards=required_guards,
                        validate_prepared_bindings=_validate_prepared_bindings,
                    )
                warnings.extend(left)

            heat_moved: list[working_set_heat.ObservedCommit | None] = []
            reading.close()
            pointer_writes = record_pointer_writes()
            committed = semantic_writes.commit_move(
                vault_root,
                preflight=preflight,
                mutate=mutate if store_writer is None else mutate_in_store,
            )
            for observed in heat_moved:
                working_set_heat.persist_commit(observed)
            semantic = committed.as_dict()
            if visible is not None:
                with ExitStack() as judging:
                    if store_writer is not None:
                        # The filter reads each page's container: one snapshot of the committed
                        # revision serves them all.
                        judging.enter_context(store_read.request_snapshot(vault_root))
                    semantic = _visible_semantic(semantic, preflight.evaluations, visible)
        except semantic_writes.SemanticWriteError as error:
            raise MoveFileError(code=error.code, reason=error.reason) from error
        except PathGuardError as error:
            raise MoveFileError(code=error.code, reason=error.reason) from error
        except ContentHashMismatchError as error:
            raise MoveFileError(
                code="SOURCE_CHANGED",
                reason="the source changed before publication; read it again and retry.",
            ) from error
        if catalog_target is not None:
            try:
                catalog_publication.publish_markdown_batch(catalog_target)
            except catalog_publication.CatalogPublicationError as error:
                raise MoveFileError(
                    code="GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN",
                    reason=(
                        "the file move committed but its active catalog publication "
                        "did not reach a verified terminal"
                    ),
                ) from error
    else:
        _admit_publication(vault_root, new_rel, None)
        log_rel_no_ext, log_body, log_plan = plan_activity_log()
        unsupported_rel = (
            old_rel if not old_rel.lower().endswith(".md") else new_rel
        )
        if store_writer is not None:
            # Before validation returns, so a plan that validates is a plan that commits.
            catalog_target = store_targets.prepare_catalog(
                vault_root,
                error_class=MoveFileError,
                writes=log_plan.writes,
                removals=(catalog_publication.CatalogRemoval(unsupported_rel, None),),
            )
        if validate_only:
            try:
                source_hash = (stored_original.binary_sha256 if stored_original is not None
                               else hashlib.sha256(reserved_paths.read_generic_bytes(
                                   vault_root, old_rel, physical=True
                               ).data).hexdigest())
            except reserved_paths.ReservedPathLeafError as error:
                raise MoveFileError(
                    "MOVE_FAILED", "move source changed during canonical validation"
                ) from error
            validation = validation_result(
                source_hash=source_hash,
                destination_hash=source_hash,
            )
            return validation.result if preview else validation
        pointer_writes = record_pointer_writes()
        if store_writer is not None:
            # The store form has no epoch and no held rename; the log entry commits with the move.
            reading.close()
            left = _move_binary_in_store(
                vault_root,
                store_writer,
                old_rel=old_rel,
                new_rel=new_rel,
                writes=[*writes, *log_plan.writes],
                originals_context=originals_context,
                validate_prepared_bindings=_validate_prepared_bindings,
            )
            warnings.extend(left)
            store_targets.publish_catalog(
                catalog_target, error_class=MoveFileError, what="the file move committed"
            )
        else:
            try:
                catalog_target = catalog_publication.prepare_catalog_membership_batch(
                    vault_root,
                    writes=log_plan.writes,
                    removals=(
                        catalog_publication.CatalogRemoval(
                            unsupported_rel,
                            None,
                        ),
                    ),
                    now=int(time.time()),
                )
            except catalog_publication.CatalogPublicationError as error:
                raise MoveFileError(
                    code="GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                    reason=str(error),
                ) from error
            if _validate_prepared_bindings is not None:
                _validate_prepared_bindings()
            _materialize_originals(vault_root, originals_context, old_rel)
            move_epoch = graph_sync.prepare_rename_epoch(vault_root, [(old_rel, new_rel)])
            try:
                _held_rename(vault_root, old_rel, new_rel)
            except BaseException:
                if move_epoch is not None:
                    graph_sync.provision_rename_recovery(move_epoch)
                raise
            child_started = False
            try:
                if move_epoch is not None:
                    graph_sync.publish_rename_epoch(move_epoch)
                if writes:
                    # Link rewrites are the move's bookkeeping, nobody's work.
                    with working_set_heat.primary_paths(()):
                        child_started = True
                        batch_atomic_write(
                            writes, vault_root=vault_root,
                            _validate_prepared_bindings=_validate_prepared_bindings,
                        )
            except Exception as e:
                if _move_outcome_is_unproved(e, move_epoch, child_started):
                    if move_epoch is not None:
                        graph_sync.provision_rename_recovery(move_epoch)
                    raise
                log.exception(
                    "move_file: link-update batch failed for %s -> %s", old_rel, new_rel
                )
                try:
                    _held_rename(vault_root, new_rel, old_rel, rollback=True)
                except MoveFileError as rollback_error:
                    if move_epoch is not None:
                        graph_sync.provision_rename_recovery(move_epoch)
                    raise RuntimeError(
                        f"move link rewrite failed ({e}); rename rollback also failed: "
                        f"{rollback_error}"
                    ) from e
                if move_epoch is not None:
                    graph_sync.restore_deletion_epoch(move_epoch)
                raise

    if store_writer is not None:
        # The store transaction holds the move, its rows and its link rewrites; there is no file
        # fan-out, and the projector moves the files.
        from .vault_store import batch as store_batch
        from .vault_store import media as store_media

        moved_binary = paired_binary or (
            None if store_ownership.is_markdown(old_rel) else (old_rel, new_rel)
        )
        if moved_binary is not None:
            # The binary's media rows follow its new path; what file mode drops at the old one goes.
            store_media.after_removal(vault_root, [moved_binary[0]])
            store_media.derive(vault_root, [moved_binary[1]])

        if log_plan.warning:
            warnings.append(log_plan.warning)
        return MoveFileResult(
            old_path=old_rel,
            new_path=new_rel,
            wikilinks_updated=reported_updated,
            path_mentions_updated=reported_mentions,
            files_touched=reported_touched,
            warnings=warnings,
            semantic=semantic,
            pointer_writes=pointer_writes,
            pointer_repointed=[write.reported() for write in (*repoints, *restored)],
            # A bound store filters every caller, so the caller decides who reads the row report,
            # as file mode decides it: the paths it names may include pages withheld from others.
            index=(
                {
                    "operation": "move",
                    # A changed self-link puts the source in `files_touched`; it lives at new_rel.
                    "upsert_reports": [
                        store_batch.row_report(
                            "upsert", [new_rel, *(rel for rel in files_touched if rel != old_rel)]
                        ).as_dict()
                    ],
                    "delete_report": store_batch.row_report("delete", [old_rel]).as_dict(),
                    "reconcile_required": False,
                    "reconcile_guidance": None,
                }
                if store_targets.caller_is_owner(vault_root)
                else None
            ),
        )

    # The filesystem transaction succeeded. Only now notify watcher/sidecars;
    # no derived index observes a move that was subsequently rolled back.
    mutation_context.close()
    from . import file_watcher, index_sync

    watcher_failures: list[str] = []
    try:
        file_watcher.register_self_delete(vault_root, [old_rel])
    except Exception:  # noqa: BLE001 — suppression is independently observed
        log.debug("move delete suppression registration failed", exc_info=True)
        watcher_failures.append("self_delete_registration_failed")
    if semantic is None or source == moved_source:
        try:
            file_watcher.register_self_write(vault_root, [new_abs])
        except Exception:  # noqa: BLE001 — suppression is independently observed
            log.debug("move write suppression registration failed", exc_info=True)
            watcher_failures.append("self_write_registration_failed")

    upsert_reports: list[index_sync.IndexSyncReport] = []
    if batch_fanout_paths:
        if batch_index_reports:
            for report in batch_index_reports:
                upsert_reports.append(
                    report
                    if isinstance(report, index_sync.IndexSyncReport)
                    else index_sync.unverified_upsert_report(
                        vault_root, batch_fanout_paths
                    )
                )
        else:
            upsert_reports.append(
                index_sync.failed_upsert_report(vault_root, batch_fanout_paths)
            )
    if semantic is None or source == moved_source:
        try:
            if new_rel in semantic_states:
                report = index_sync.upsert_after_write(
                    vault_root,
                    [new_abs],
                    semantic_states={new_rel: semantic_states[new_rel]},
                )
            else:
                report = index_sync.upsert_after_write(vault_root, [new_abs])
        except Exception:  # noqa: BLE001 — move remains authoritative
            log.exception("index upsert failed for moved destination %s", new_rel)
            upsert_reports.append(
                index_sync.failed_upsert_report(vault_root, [new_abs])
            )
        else:
            upsert_reports.append(
                report
                if isinstance(report, index_sync.IndexSyncReport)
                else index_sync.unverified_upsert_report(vault_root, [new_abs])
            )
    if moved_parts:
        new_parts = [vault_root / paired_parts[1] / old_part.name for old_part in moved_parts]
        try:
            index_sync.upsert_after_write(vault_root, new_parts)
            index_sync.delete_after_remove(vault_root, [old_part.as_posix() for old_part in moved_parts])
        except Exception:  # noqa: BLE001 — move remains authoritative; reconcile repairs the index
            log.exception("index update failed for the moved parts of %s", new_rel)
            reconcile_parts = True
    try:
        raw_delete_report = index_sync.delete_after_remove(vault_root, [old_rel])
    except Exception:  # noqa: BLE001 — move remains authoritative
        log.exception("index delete failed for moved source %s", old_rel)
        delete_report = index_sync.observed_delete_report(
            [old_rel], degraded=True
        )
    else:
        delete_report = (
            raw_delete_report
            if isinstance(raw_delete_report, index_sync.IndexSyncReport)
            else index_sync.observed_delete_report([old_rel], degraded=False)
        )
    reconcile_required = bool(
        reconcile_parts
        or watcher_failures
        or delete_report.reconcile_required
        or any(report.reconcile_required for report in upsert_reports)
    )
    index_feedback = {
        "operation": "move",
        "upsert_reports": [report.as_dict() for report in upsert_reports],
        "delete_report": delete_report.as_dict(),
        "watcher": {
            "outcome": "degraded" if watcher_failures else "completed",
            "codes": watcher_failures or ["suppression_registered"],
        },
        "reconcile_required": reconcile_required,
        "reconcile_guidance": (
            "Run reconcile to repair observed move fan-out degradation."
            if reconcile_required
            else None
        ),
    }

    log_warning = log_plan.warning
    if catalog_target is None:
        log_warning = write_log_entry(
            vault_root,
            date_iso=today.isoformat(),
            op="move_file",
            rel_path_no_ext=log_rel_no_ext,
            body=log_body,
        )
    if log_warning:
        warnings.append(log_warning)

    return MoveFileResult(
        old_path=old_rel,
        new_path=new_rel,
        wikilinks_updated=reported_updated,
        path_mentions_updated=reported_mentions,
        files_touched=reported_touched,
        warnings=warnings,
        semantic=semantic,
        pointer_writes=pointer_writes,
        pointer_repointed=[write.reported() for write in (*repoints, *restored)],
        # The index outcome covers every rewritten page, so a mover other
        # than the owner receives none.
        index=None if restricted else index_feedback,
    )


def adopt_move(
    vault_root: Path,
    *,
    old_path: str,
    new_path: str,
    source_hash: str,
    content_transform: Callable[[str], str] | None = None,
    extra_writes: tuple[PlannedWrite, ...] = (),
    today: dt.date | None = None,
    _validate_prepared_bindings: Callable[[], None] | None = None,
    exact_content: bool = False,
) -> MoveFileResult:
    """Publish a move whose rename already ran, without renaming.

    Curation adopts a move that a crash cut between its rename and its batch: the page sits at
    `new_path` with the bytes it held at `old_path` (`source_hash`), and `old_path` is absent.
    This commits what the move's batch would have committed: the destination bytes with their
    lineage entry and `content_transform`, and the move's semantic, graph, catalog, index and
    activity-log effects. The guards bind that rename-after state, so a page that changed or
    returned to its old path refuses. Only an unpaired Markdown page in a file-mode vault
    adopts, as only such a move records a curation intent. `exact_content` adopts an inverse
    move, whose `content_transform` alone decides the bytes (`move_file`'s `_exact_content`).
    """
    from . import file_watcher, index_sync

    manager = writer_lease.active_manager()
    with ExitStack() as mutation_context:
        mutation_context.enter_context(manager.mutation_guard(vault_root, operation="move_file"))
        old_rel, new_rel = old_path, new_path
        new_abs = vault_root / new_rel
        try:
            source, source_guard = read_guarded_text(vault_root, new_abs)
        except (OSError, UnicodeDecodeError, PathGuardError) as error:
            raise MoveFileError("SOURCE_CHANGED", "the renamed page is unreadable") from error
        if content_hash(source) != source_hash:
            raise MoveFileError(
                "SOURCE_CHANGED", "the renamed page changed before its move was adopted"
            )
        transform = (
            content_transform or (lambda text: text)
            if exact_content
            else _compose_transforms(
                content_transform, _lineage_transform(old_rel, new_rel, None, False)
            )
        )
        moved_source = transform(source)
        _admit_publication(vault_root, new_rel, moved_source.encode("utf-8"))
        absent_guard = PathGuard.capture(vault_root, old_rel, leaf_policy="absent")
        try:
            preflight = semantic_writes.preflight_move(
                vault_root,
                old_path=old_rel,
                new_path=new_rel,
                source=source,
                moved_source=moved_source,
                source_guard=source_guard,
                destination_guard=absent_guard,
                content_transform=transform,
                adopt=True,
            )
        except semantic_writes.SemanticWriteError as error:
            raise MoveFileError(error.code, error.reason) from error
        semantic_states = {
            item.after.path: semantic_index.from_semantic_page_state(item.after)
            for item in preflight.evaluations
        }
        new_rel_no_ext = new_rel.removesuffix(".md")
        log_plan = plan_log_entry(
            vault_root,
            date_iso=(today or dt.date.today()).isoformat(),
            op="move_file",
            rel_path_no_ext=new_rel_no_ext,
            body=(
                f"Moved {old_rel!r} → {new_rel!r} via exomem Tier 2, adopted after an "
                "interrupted rename. wikilinks_updated=0 across 0 file(s)."
            ),
        )
        catalog_target: catalog_publication.PreparedMarkdownCatalogPublication | None = None
        index_reports: list[object] = []

        def mutate(lifecycle_writes, required_guards, bound_absence: PathGuard) -> None:
            nonlocal catalog_target
            bound_absence.recheck(vault_root)
            destination = PlannedWrite(
                path=new_abs, content=moved_source, expected_hash=source_hash
            )
            catalog_writes = (
                *lifecycle_writes,
                PlannedWrite(path=new_abs, content=moved_source, create_only=True),
                *extra_writes,
                *log_plan.writes,
            )
            try:
                catalog_target = catalog_publication.prepare_catalog_membership_batch(
                    vault_root,
                    writes=catalog_writes,
                    removals=(catalog_publication.CatalogRemoval(old_rel, source_hash),),
                    graph_replacement_provider=lambda: (
                        graph_producer.replacements_for_semantic_transition(
                            vault_root,
                            before_corpus=preflight.before_corpus,
                            after_corpus=preflight.after_corpus,
                            writes=catalog_writes,
                            semantic_states={
                                item.after.path: item.after for item in preflight.evaluations
                            },
                        )
                    ),
                    now=int(time.time()),
                    part_folders={},
                )
            except catalog_publication.CatalogPublicationError as error:
                raise MoveFileError(
                    code="GOVERNANCE_CATALOG_PUBLICATION_BLOCKED", reason=str(error)
                ) from error
            combined = [*lifecycle_writes, destination, *extra_writes]
            if catalog_target is not None:
                combined.extend(log_plan.writes)
            if _validate_prepared_bindings is not None:
                _validate_prepared_bindings()
            epoch = graph_sync.prepare_rename_epoch(vault_root, [(old_rel, new_rel)], adopted=True)
            try:
                if epoch is not None:
                    graph_sync.publish_rename_epoch(epoch)
                batch_atomic_write(
                    combined,
                    vault_root=vault_root,
                    required_guards=required_guards,
                    index_reports=index_reports,
                    _validate_prepared_bindings=_validate_prepared_bindings,
                    semantic_states={
                        rel: semantic_states[rel]
                        for write in combined
                        if (rel := write.path.relative_to(vault_root).as_posix())
                        in semantic_states
                    },
                )
            except BaseException:
                # No rename runs here, so nothing rolls back; the graph repairs from its floor.
                if epoch is not None:
                    graph_sync.provision_rename_recovery(epoch)
                raise

        try:
            committed = semantic_writes.commit_move(vault_root, preflight=preflight, mutate=mutate)
        except semantic_writes.SemanticWriteError as error:
            raise MoveFileError(code=error.code, reason=error.reason) from error
        except PathGuardError as error:
            raise MoveFileError(code=error.code, reason=error.reason) from error
        except ContentHashMismatchError as error:
            raise MoveFileError(
                "SOURCE_CHANGED", "the renamed page changed before its move was adopted"
            ) from error
        if catalog_target is not None:
            try:
                catalog_publication.publish_markdown_batch(catalog_target)
            except catalog_publication.CatalogPublicationError as error:
                raise MoveFileError(
                    code="GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN",
                    reason="the adopted move committed but its catalog publication did not "
                    "reach a verified terminal",
                ) from error
    try:
        file_watcher.register_self_delete(vault_root, [old_rel])
    except Exception:  # noqa: BLE001 — suppression is independently observed
        log.debug("adopted move delete suppression registration failed", exc_info=True)
    try:
        index_sync.delete_after_remove(vault_root, [old_rel])
    except Exception:  # noqa: BLE001 — the move stands; reconcile repairs the index
        log.exception("index delete failed for adopted move source %s", old_rel)
    return MoveFileResult(
        old_path=old_rel,
        new_path=new_rel,
        wikilinks_updated=0,
        files_touched=[],
        warnings=[],
        semantic=committed.as_dict(),
    )


def _commit_in_store(
    vault_root: Path,
    writer,
    relocation,
    writes: list[PlannedWrite],
    lifecycle,
    *,
    sources: set[str],
    destinations: set[str],
    required_guards=(),
    validate_prepared_bindings: Callable[[], None] | None = None,
) -> list[str]:
    """Commit ``writes`` and ``lifecycle`` with the binary moves of ``relocation`` in one store
    transaction, then rename what it moves (``vault_store.binaries``); a refusal moves nothing.

    A stale path among ``sources`` refuses with SOURCE_CHANGED and a taken one among
    ``destinations`` with DEST_EXISTS, as file mode names them. Returns a warning for each file
    that did not move after the commit.
    """
    from .vault_store import batch as store_batch
    from .vault_store import binaries
    from .vault_store.write import StoreConflict

    try:
        with binaries.relocated(vault_root, relocation, writer) as moved:
            store_batch.commit_batch(
                writer,
                writes,
                vault_root=vault_root,
                required_guards=required_guards,
                validate_prepared_bindings=validate_prepared_bindings,
                lifecycle=dataclasses.replace(lifecycle, binary_moves=moved.binary_moves),
            )
    except StoreConflict as conflict:
        if conflict.path in sources:
            raise MoveFileError(
                code="SOURCE_CHANGED",
                reason=(
                    "the source changed before publication; read it again and retry "
                    f"({conflict.reason})."
                ),
            ) from conflict
        if conflict.path in destinations:
            raise MoveFileError(
                code="DEST_EXISTS",
                reason=(
                    f"destination already exists: {conflict.path}. "
                    f"This op refuses to overwrite — pick a different name."
                ),
            ) from conflict
        raise MoveFileError("MOVE_FAILED", "move could not be published") from conflict
    except binaries.Unreadable as error:
        raise MoveFileError(
            *binaries.refusal(error, "MOVE_FAILED", "move could not be published")
        ) from error
    except reserved_paths.ReservedPathLeafError as error:
        raise _rename_error(error) from error
    return [f"{rel}: {binaries.LEFT}" for rel in moved.left]


def _move_binary_in_store(
    vault_root: Path,
    writer,
    *,
    old_rel: str,
    new_rel: str,
    writes: list[PlannedWrite],
    originals_context: ExitStack,
    validate_prepared_bindings: Callable[[], None] | None,
) -> list[str]:
    """Move a binary that pairs with no page: commit its record's move and the rewrites of the
    pages that link to it in one store transaction, then rename it, as file mode renames it and
    writes them. Returns a warning when the file did not move after the commit.
    """
    from .vault_store import batch as store_batch
    from .vault_store import binaries

    # The bytes move with their record, so an evicted original is fetched first.
    _materialize_originals(vault_root, originals_context, old_rel)
    with store_read.snapshot(vault_root) as view:
        relocation = binaries.plan(view, [binaries.Entry(old_rel, new_rel)])
    try:
        # Link rewrites are the move's bookkeeping, nobody's work.
        with working_set_heat.primary_paths(()):
            return _commit_in_store(
                vault_root,
                writer,
                relocation,
                list(writes),
                store_batch.Lifecycle(expect=relocation.expect()),
                sources={old_rel},
                destinations={new_rel},
                validate_prepared_bindings=validate_prepared_bindings,
            )
    except PathGuardError as error:
        raise MoveFileError(code=error.code, reason=error.reason) from error
    except ContentHashMismatchError as error:
        raise MoveFileError(
            code="SOURCE_CHANGED",
            reason="a linking page changed before publication; read it again and retry.",
        ) from error


def _move_outcome_is_unproved(
    error: Exception, epoch: graph_sync.GraphDeletionEpoch | None, child_started: bool
) -> bool:
    # A committed or partial batch must retain its rename; erasing its epoch would hide surviving writes.
    if isinstance(error, BatchWriteError) and (
        error.committed or error.code == "BATCH_ROLLBACK_INCOMPLETE"
    ):
        return True
    return child_started and epoch is not None and not graph_sync.rename_epoch_is_current(epoch)


def _visible_semantic(
    semantic: dict[str, Any], evaluations: Any, visible: Callable[[str], bool]
) -> dict[str, Any]:
    """The move's semantic block over the pages a restricted mover may see."""
    shown = [item for item in evaluations if visible(item.after.path)]
    identities = {item.after.identity for item in shown}
    return {
        **semantic,
        "affected_paths": [item.after.path for item in shown],
        "contract_results": {
            key: value
            for key, value in semantic.get("contract_results", {}).items()
            if key in identities
        },
        "lifecycle_states": {
            key: value
            for key, value in semantic.get("lifecycle_states", {}).items()
            if key in identities
        },
    }


_GRAPH_SYNC_FIELDS = frozenset(
    {"graph_sync", "graph_sync_code", "graph_sync_checkpoint", "graph_sync_remediation"}
)


def restricted_mover_terminal(
    vault_root: Path,
    result: Any,
    *,
    committed: Callable[[Any], Mapping[str, Any]],
) -> Any:
    """A move's terminal as a mover other than the owner receives it.

    Every linking page is rewritten, including pages withheld from the mover,
    and what those rewrites leave behind shapes the answer: a rewrite the
    mover may not see can turn a completed graph update into a pending repair,
    and whether the move wrote any bytes decides whether its leaf comes back
    wrapped in the committed terminal. Such a mover therefore always receives
    the committed terminal (`committed` wraps a bare leaf) and no graph
    outcome, so its answer, in every response detail and on every later
    move, is the same whether or not a withheld page linked the moved one.
    The durable terminal keeps the graph outcome.
    """
    from .governance import egress

    if not isinstance(result, Mapping) or not egress.caller_restricted(vault_root):
        return result
    if "leaf_result" not in result and "new_path" in result and "old_path" in result:
        result = committed(result)

    def _without(value: Mapping[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in value.items() if key not in _GRAPH_SYNC_FIELDS}

    terminal = _without(result)
    leaf = terminal.get("leaf_result")
    if isinstance(leaf, Mapping):
        terminal["leaf_result"] = _without(leaf)
    return terminal


def _rewrite_wikilinks(text: str, old_rel: str, new_rel: str) -> tuple[str, int]:
    return semantic_writes.rewrite_wikilinks_for_move(text, old_rel, new_rel)


_ = walk_vault_md  # imported for parity with other Tier 2 modules
