"""A curation plan's `repoint-links` steps: the rewrite of the referrers of the plan's moves.

The plan's `move` steps move bytes and lineage only. The repoint steps run last. A step without
`referrers` rewrites every referrer of the moves; a step with `referrers` rewrites only those pages,
so a plan can split the rewrite by referring page, each page in one step (`referrers`). At proposal
a step seals each page it rewrites with its exact before hash, the hash of the bytes it rewrites the
page to, and one outcome as a move preview gives it. At execution it re-derives each rewrite from
the sealed bytes and commits every rewrite and every append-only pointer repoint in one batch, which
carries the step's witness. Its compensation returns each page it rewrote to its sealed pre-step
bytes, and each append-only page through the pointer write's revert.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import fold_leaf, move_file, page_fields, path_mentions, pointer_write, semantic_writes, vault
from .semantic_writes import rewrite_wikilinks_for_move

#: The call a refusal names as the one whose approval covers the repoints: the plan approval.
_APPROVAL_CALL = "the curation plan's preview and approval"


def _view(vault_root: Path) -> tuple[Any, Any]:
    """The caller's view and write admission, by the move's own rule (`move_file.mover_view`).

    A restricted caller never reads, rewrites or lists a page it cannot see (`MoverView.scope`);
    such a page keeps its old link, which resolves through the moved page's lineage.
    """
    from .governance import connector_boundary

    return move_file.mover_view(vault_root).scope, connector_boundary.effect_admission(vault_root)


def _rewrite(
    text: str,
    moves: Sequence[tuple[str, str]],
    renames: Sequence[path_mentions.Rename],
    *,
    body_immutable: bool,
) -> tuple[str, int, int, int]:
    """`text` with every link to a moved page renamed, then its prose mentions, unless its body is
    immutable; with the link, written mention and kept mention counts."""
    changes = 0
    for old_rel, new_rel in moves:
        text, count = rewrite_wikilinks_for_move(text, old_rel, new_rel)
        changes += count
    text, mentions = path_mentions.rewrite(text, renames, apply=not body_immutable)
    if body_immutable:
        return text, changes, 0, mentions
    return text, changes, mentions, 0


def _needed(
    texts: Mapping[str, str],
    moves: Sequence[tuple[str, str]],
    renames: Sequence[path_mentions.Rename],
) -> tuple[list[tuple[str, str]], tuple[path_mentions.Rename, ...]]:
    """The moves and renames that change or count in at least one of `texts`, in plan order.

    A step that names its referrers seals only these, so a plan split by referring page does not
    seal every move once per step. An entry that counts nothing leaves the text as it was, so the
    narrowed lists rewrite each page to the same bytes; a page that they would not rewrite to the
    same bytes keeps every entry.
    """
    used_moves: set[int] = set()
    used_renames: set[int] = set()
    for rel, text in texts.items():
        immutable = _append_only(rel)
        for index, (old_rel, new_rel) in enumerate(moves):
            text, count = rewrite_wikilinks_for_move(text, old_rel, new_rel)
            if count:
                used_moves.add(index)
        for index, rename in enumerate(renames):
            text, count = path_mentions.rewrite(text, (rename,), apply=not immutable)
            if count:
                used_renames.add(index)
    narrowed = [move for index, move in enumerate(moves) if index in used_moves]
    narrowed_renames = tuple(rename for index, rename in enumerate(renames) if index in used_renames)
    for rel, text in texts.items():
        immutable = _append_only(rel)
        if _rewrite(text, narrowed, narrowed_renames, body_immutable=immutable) != _rewrite(
            text, moves, renames, body_immutable=immutable
        ):
            return list(moves), tuple(renames)
    return narrowed, narrowed_renames


def _append_only(rel: str) -> bool:
    """Whether the page is captured content, which changes only through the pointer write.

    A generated index inside an append-only tree is not captured content, so it is rewritten as
    a writable page (`move_file.plan_inbound_rewrites`).
    """
    generated_index = rel.rsplit("/", 1)[-1] == "index.md"
    return vault.in_append_only_tree(rel) is not None and not generated_index


def referrers(vault_root: Path, moves: Sequence[tuple[str, str]]) -> tuple[list[str], bool]:
    """Every page the caller sees that links to or names an old path of `moves`, at the path the
    moves leave it, sorted; and whether the catalogue answered the prose-mention lookup."""
    root = Path(vault_root)
    visible, _writable = _view(root)
    location = dict(moves)
    # One index read and one catalogue proof serve every move: a lookup per move walks the vault.
    inbound = vault.find_inbound_wikilinks_many(root, list(location), visible=visible)
    found = {hit.path for hits in inbound.values() for hit in hits}
    mentions = path_mentions.find_pages(root, list(location))
    found.update(mentions.pages)
    if visible is not None:
        found = {rel for rel in found if visible(rel)}
    return sorted({location.get(rel, rel) for rel in found}), mentions.complete


def prepare(
    vault_root: Path,
    moves: Sequence[tuple[str, str]],
    projected: Mapping[str, str],
    *,
    only: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Seal the referrers of `moves`, in plan order, as the plan leaves them.

    `projected` holds the exact bytes of each page that an earlier step of the plan leaves, at the
    path where that step leaves it: a referrer among them is read and bound there. `only` names the
    referrers this step rewrites, at the paths the moves leave them; without it the step finds and
    rewrites every referrer. Each named page is bound, rewritten or not, so the step's witness
    covers exactly its pages, and the step seals only the moves those pages use (`_needed`). A
    referrer whose change the pointer write refuses refuses the plan.
    """
    root = Path(vault_root)
    visible, writable = _view(root)
    location = dict(moves)
    complete = True
    if only is None:
        found, complete = referrers(root, moves)
    else:
        # A named page is judged by the unsplit step's rule: the caller sees it where it stood.
        origin = {new: old for old, new in moves}
        found = sorted(
            rel for rel in set(only) if visible is None or visible(origin.get(rel, rel))
        )
    renames = path_mentions.renames(root, list(moves))
    texts: dict[str, str] = {}
    for rel in found:
        if rel in projected:
            texts[rel] = projected[rel]
            continue
        try:
            texts[rel], _guard = vault.read_guarded_text(root, root / rel)
        except (OSError, UnicodeDecodeError, vault.PathGuardError):
            continue
    if only is not None:
        moves, renames = _needed(texts, moves, renames)
        location = dict(moves)
    pointers = page_fields.pointer_fields(root)
    matcher = pointer_write.page_matcher(root, location)
    # A page the step leaves unchanged resolves an old link through the moved page's lineage,
    # which only a Markdown page with a frontmatter block records.
    unchanged = (
        move_file.KEPT
        if all(move_file.records_lineage(root, old, new, undo=False) for old, new in moves)
        else move_file.KEPT_DANGLING
    )
    pages: dict[str, dict[str, Any]] = {}
    outcomes: list[dict[str, str]] = []
    repoints: list[pointer_write.PointerWrite] = []
    refused: list[pointer_write.PointerWriteError] = []
    # Each live page the step rewrites in place, whose lifecycle record the step carries.
    carried: dict[str, str] = {}
    for rel, text in texts.items():
        append_only = _append_only(rel)
        after, changes, written, kept = _rewrite(text, moves, renames, body_immutable=append_only)
        if not (changes or written) or not writable(rel):
            if changes or written or kept:
                outcomes.append({"path": rel, "outcome": unchanged})
            if only is not None:
                held = vault.content_hash(text)
                pages[rel] = {"path": rel, "before_hash": held, "after_hash": held, "append_only": append_only}
            continue
        if append_only:
            try:
                repoints.append(_repoint(root, rel, text, after, pointers, location, matcher))
            except pointer_write.PointerWriteError as error:
                outcomes.append({"path": rel, "outcome": move_file.REFUSED, "code": error.code})
                refused.append(error)
                continue
        outcomes.append(
            {"path": rel, "outcome": move_file.REPOINTED if append_only else move_file.REWRITTEN}
        )
        row: dict[str, Any] = {
            "path": rel,
            "before_hash": vault.content_hash(text),
            "after_hash": vault.content_hash(after),
            "append_only": append_only,
        }
        if not append_only:
            # The writable page's own undo; an append-only page reverts from its pointer record.
            row["restore"] = _delta(text, after)
            if rel not in projected:
                carried[rel] = text
        pages[rel] = row
    if refused:
        raise fold_leaf.refuse(refused[0].code, refused[0].reason)
    try:
        semantic_writes.require_lifecycle_settled(root, carried)
    except semantic_writes.SemanticWriteError as error:
        raise fold_leaf.refuse(error.code, error.reason) from error
    store_rows = move_file.store_rows_outcome(root, unchanged)
    # Each moved page is bound as the plan leaves it, rewritten or not, so the witness of the one
    # step that rewrites every referrer states where the plan's pages stand. A step that names its
    # referrers binds only those: another step of the plan may rewrite a moved page.
    for _old_rel, new_rel in moves if only is None else ():
        if new_rel not in pages and new_rel in projected:
            held = vault.content_hash(projected[new_rel])
            pages[new_rel] = {
                "path": new_rel, "before_hash": held, "after_hash": held, "append_only": False,
            }
    if only is not None and not pages:
        raise fold_leaf.refuse("CURATION_BINDING_STALE", "no page the step names can be read")
    return {
        "moves": [list(move) for move in moves],
        # The mention rewrite reads the old folders once, here: at execution the moves have
        # emptied them, and the sealed bytes must be re-derived from the same names.
        "renames": [[rename.old, rename.new, list(rename.longer)] for rename in renames],
        "pages": [pages[rel] for rel in sorted(pages)],
        "referrers": outcomes,
        **({"store_rows": store_rows} if store_rows is not None else {}),
        "preview_digest": pointer_write.preview_digest(location, repoints),
        **({"mentions_unchecked": True} if not complete else {}),
    }


def commit(vault_root: Path, prepared: Mapping[str, Any], *, operation: str) -> dict[str, Any]:
    """Re-derive each sealed rewrite from the sealed bytes and commit them all in one batch.

    The plan's moves have committed, so an old link target names the page at its new path. Each
    append-only page changes only through the pointer write, recorded under `operation`, and the
    plan approval approves it: a repoint that the sealed digest does not list refuses.
    """
    root = Path(vault_root)
    moves = [(str(old_rel), str(new_rel)) for old_rel, new_rel in prepared["moves"]]
    location = dict(moves)
    renames = tuple(
        path_mentions.Rename(str(old), str(new), tuple(longer))
        for old, new, longer in prepared["renames"]
    )
    pointers = page_fields.pointer_fields(root)
    matcher = pointer_write.page_matcher(root, location, settled=True)
    writes: list[vault.PlannedWrite] = []
    repoints: list[pointer_write.PointerWrite] = []
    for row in prepared["pages"]:
        rel = str(row["path"])
        text, guard = _sealed_text(root, rel, row["before_hash"])
        if row["before_hash"] == row["after_hash"]:
            continue
        append_only = _append_only(rel)
        after, _changes, _written, _kept = _rewrite(
            text, moves, renames, body_immutable=append_only
        )
        if vault.content_hash(after) != row["after_hash"]:
            raise fold_leaf.refuse(
                "CURATION_BINDING_STALE", f"{rel!r} no longer rewrites to its sealed bytes"
            )
        if append_only:
            try:
                repoints.append(_repoint(root, rel, text, after, pointers, location, matcher))
            except pointer_write.PointerWriteError as error:
                raise fold_leaf.refuse(error.code, error.reason) from error
        writes.append(
            vault.PlannedWrite(root / rel, after, guard=guard, expected_hash=row["before_hash"])
        )
    try:
        pointer_write.require_preview(
            location, repoints, prepared.get("preview_digest"), preview_call=_APPROVAL_CALL
        )
    except pointer_write.PointerWriteError as error:
        raise fold_leaf.refuse(error.code, error.reason) from error
    records = [(write, pointer_write.new_record_id()) for write in repoints]
    with pointer_write.recording(root, records, operation=operation):
        _commit(root, writes)
    return {
        "path": str(prepared["pages"][0]["path"]),
        "rewritten": len(writes) - len(records),
        "pointer_repointed": len(records),
    }


def restorations(
    vault_root: Path, material: Mapping[str, Any], text_of: Callable[[str], str]
) -> list[tuple[str, str, str, pointer_write.PointerWrite | None]]:
    """Each page a sealed step changed, with the text it holds now and its pre-step text.

    `material` names the step's operation id and its changed page rows; `text_of` reads a page as
    the caller's plan leaves it. A writable page reverses its sealed line delta. An append-only page
    reverts through the record that the step's pointer write left under that operation id, and
    comes with the revert write to record. A page off its sealed after hash refuses as stale.
    """
    root = Path(vault_root)
    try:
        records = pointer_write.operation_records(root, material.get("operation"))
    except pointer_write.PointerWriteError as error:
        raise fold_leaf.refuse("CURATION_COMPENSATION_UNAVAILABLE", error.reason) from error
    restored: list[tuple[str, str, str, pointer_write.PointerWrite | None]] = []
    for row in material["pages"]:
        rel = str(row["path"])
        current = text_of(rel)
        if vault.content_hash(current) != row["after_hash"]:
            raise fold_leaf.refuse(
                "CURATION_BINDING_STALE", f"{rel!r} changed since the step it reverses"
            )
        revert: pointer_write.PointerWrite | None = None
        if row.get("append_only"):
            record_id = records.get(rel)
            if record_id is None:
                raise fold_leaf.refuse(
                    "CURATION_COMPENSATION_UNAVAILABLE",
                    f"{rel!r} has no pointer-write record of the step to revert",
                )
            try:
                revert = pointer_write.revert_write(
                    pointer_write.load_record(root, record_id), record_id, current, path=rel
                )
            except pointer_write.PointerWriteError as error:
                code = (
                    "CURATION_COMPENSATION_UNAVAILABLE"
                    if error.code == "POINTER_RECORD_UNKNOWN"
                    else "CURATION_BINDING_STALE"
                )
                raise fold_leaf.refuse(code, error.reason) from error
            before = revert.after
        else:
            before = _apply(current, row["restore"], forward=False, path=rel)
        if vault.content_hash(before) != row["before_hash"]:
            raise fold_leaf.refuse(
                "CURATION_BINDING_STALE", f"{rel!r} no longer returns to its sealed pre-step bytes"
            )
        restored.append((rel, current, before, revert))
    return restored


def restore(
    vault_root: Path, material: Mapping[str, Any], *, operation: str
) -> dict[str, Any]:
    """Return each page a sealed step changed to its pre-step bytes, in one batch.

    The step's append-only pages revert through their pointer records, and each revert is
    recorded under `operation`.
    """
    root = Path(vault_root)
    guards: dict[str, Any] = {}

    def text_of(rel: str) -> str:
        try:
            text, guards[rel] = vault.read_guarded_text(root, root / rel)
        except (OSError, UnicodeDecodeError, vault.PathGuardError) as error:
            raise fold_leaf.refuse("CURATION_BINDING_STALE", f"{rel!r} is unavailable") from error
        return text

    writes: list[vault.PlannedWrite] = []
    reverts: list[tuple[pointer_write.PointerWrite, str]] = []
    for rel, current, before, revert in restorations(root, material, text_of):
        if revert is not None:
            reverts.append((revert, pointer_write.new_record_id()))
        writes.append(
            vault.PlannedWrite(
                root / rel, before, guard=guards[rel], expected_hash=vault.content_hash(current)
            )
        )
    with pointer_write.recording(root, reverts, operation=operation):
        _commit(root, writes)
    return {"path": str(material["pages"][0]["path"]), "restored": len(writes)}


def _repoint(
    root: Path,
    rel: str,
    before: str,
    after: str,
    pointers: Collection[str],
    location: Mapping[str, str],
    matcher: Callable[[str, str], bool],
) -> pointer_write.PointerWrite:
    """The proven pointer write of an append-only referrer: only link spans that name a moved page."""
    return pointer_write.plan(
        root,
        rel,
        before,
        after,
        pointer_write.Declared(fields=pointers, moves=location),
        reason="repoint-links of a curation plan",
        same_page=matcher,
    )


def _delta(before: str, after: str) -> list[list[Any]]:
    from . import duplicate_fold

    return duplicate_fold._delta(before, after)  # noqa: SLF001 - the lane's shared line delta


def _apply(text: str, delta: Sequence[Sequence[Any]], *, forward: bool, path: str) -> str:
    from . import duplicate_fold

    return duplicate_fold._apply(text, delta, forward=forward, path=path)  # noqa: SLF001


def _sealed_text(root: Path, rel: str, expected: str) -> tuple[str, Any]:
    """The page's text and guard, or `CURATION_BINDING_STALE` when it no longer holds `expected`."""
    try:
        text, guard = vault.read_guarded_text(root, root / rel)
    except (OSError, UnicodeDecodeError, vault.PathGuardError) as error:
        raise fold_leaf.refuse("CURATION_BINDING_STALE", f"{rel!r} is unavailable") from error
    if vault.content_hash(text) != expected:
        raise fold_leaf.refuse(
            "CURATION_BINDING_STALE", f"{rel!r} changed since the step was sealed"
        )
    return text, guard


def _commit(root: Path, writes: Collection[vault.PlannedWrite]) -> None:
    """Commit the writes in one batch with the step's witness, or the witness alone.

    Each rewritten page's relation-review lifecycle record moves with its bytes in the same
    batch, as a move's own link rewrites do, so the page's next governed write finds the record
    binding it and any reviewed-none decision still in force.
    """
    from . import add, preserve

    if not writes:
        fold_leaf.commit_witness(root)
        return
    pages = tuple(writes)
    intents: list[object] = []
    committed: list[Path] = []

    def mutate(lifecycle: tuple[vault.PlannedWrite, ...], guards: tuple[Any, ...]) -> None:
        written = preserve.commit_media_sidecar_writes(
            root,
            (*lifecycle, *pages),
            post_commit_fanout=False,
            publication_intents_out=intents,
            required_guards=guards,
        )
        assert isinstance(written, list)  # no deferred graph completion was asked for
        committed.extend(written)

    try:
        semantic_writes.commit_rewrites(
            root, preflight=semantic_writes.preflight_rewrites(root, pages), mutate=mutate
        )
    except (vault.ContentHashMismatchError, vault.PathGuardError) as error:
        raise fold_leaf.refuse(
            "CURATION_BINDING_STALE", "a page changed while the step wrote it"
        ) from error
    except semantic_writes.SemanticWriteError as error:
        code = "CURATION_BINDING_STALE" if error.code == "STALE_SEMANTIC_WRITE" else error.code
        raise fold_leaf.refuse(code, error.reason) from error
    written = {write.path for write in pages}
    add.deferred_fanout(
        root,
        [path for path in committed if path in written],
        created_paths=[],
        publication_intents=intents,
    )
