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

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .vault import (
    PlannedWrite,
    VaultPathError,
    batch_atomic_write,
    find_inbound_wikilinks,
    in_append_only_tree,
    in_curated_tree,
    resolve_under_vault,
    walk_vault_md,
    write_log_entry,
)


log = logging.getLogger(__name__)

_WIKILINK_PATTERN = re.compile(r"\[\[([^\]\|\n]+?)(\|[^\]\n]*)?\]\]")


@dataclass
class MoveFileResult:
    old_path: str
    new_path: str
    wikilinks_updated: int
    files_touched: list[str]
    warnings: list[str]

    def as_dict(self) -> dict:
        return {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "wikilinks_updated": self.wikilinks_updated,
            "files_touched": self.files_touched,
            "warnings": self.warnings,
        }


@dataclass
class MoveFileError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def move_file(
    vault_root: Path,
    *,
    old_path: str,
    new_path: str,
    update_wikilinks: bool = True,
    allow_curated: bool = False,
    today: dt.date | None = None,
) -> MoveFileResult:
    try:
        old_abs, old_rel = resolve_under_vault(
            vault_root, old_path, must_exist=True, must_be_file=True
        )
    except VaultPathError as e:
        raise MoveFileError(code=e.code, reason=e.reason) from e
    try:
        new_abs, new_rel = resolve_under_vault(vault_root, new_path)
    except VaultPathError as e:
        raise MoveFileError(code=e.code, reason=e.reason) from e

    if new_abs.exists():
        raise MoveFileError(
            code="DEST_EXISTS",
            reason=(
                f"destination already exists: {new_rel}. "
                f"This op refuses to overwrite — pick a different name."
            ),
        )

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
    if not intra_append:
        if src_append:
            raise MoveFileError(
                code="APPEND_ONLY",
                reason=(
                    f"{old_rel} is in {src_append}/ which is append-only "
                    f"(SKILL.md rule 2). Moves OUT of {src_append}/ are "
                    f"forbidden; relocation WITHIN {src_append}/ is allowed."
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

    # Scan inbound links BEFORE the move, while the old path still exists.
    inbound = find_inbound_wikilinks(vault_root, old_rel) if update_wikilinks else []

    new_abs.parent.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    files_touched: list[str] = []
    wikilinks_updated = 0

    # Stage all writes: the move itself + every link-updated file.
    writes: list[PlannedWrite] = []
    if update_wikilinks and inbound:
        files_to_rewrite = sorted({hit.path for hit in inbound})
        for rel in files_to_rewrite:
            try:
                abs_file = (vault_root / rel).resolve()
                abs_file.relative_to(vault_root.resolve())
            except (ValueError, OSError):
                continue
            try:
                text = abs_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            new_text, n_changed = _rewrite_wikilinks(text, old_rel, new_rel)
            if n_changed > 0:
                writes.append(PlannedWrite(path=abs_file, content=new_text))
                files_touched.append(rel)
                wikilinks_updated += n_changed

    # Read old contents, then write to new location; then unlink old.
    # We can't easily roll the move itself into batch_atomic_write since it's a
    # rename. Strategy: write new file (as if creating it), then unlink old.
    # If the unlink fails partway, the worst case is two copies — caller can
    # reconcile.
    old_contents = old_abs.read_text(encoding="utf-8")
    writes.append(PlannedWrite(path=new_abs, content=old_contents))

    try:
        batch_atomic_write(writes, vault_root=vault_root)
    except Exception as e:
        log.exception("move_file: link-update batch failed for %s -> %s", old_rel, new_rel)
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    # Register the self-authored removal BEFORE the unlink so the watcher's
    # delete event for the old path is dropped (we purge its sidecar rows
    # below; the new path was registered by batch_atomic_write above).
    try:
        from . import file_watcher
        file_watcher.register_self_delete(vault_root, [old_rel])
    except Exception:  # noqa: BLE001 — suppression is best-effort
        log.debug("self-delete suppression registration failed", exc_info=True)

    try:
        old_abs.unlink()
    except OSError as e:
        log.exception("move_file: copy succeeded but old unlink failed: %s", old_rel)
        warnings.append(
            f"new file written but could not remove old {old_rel!r}: {e}. "
            f"Delete it manually on the desk."
        )
    else:
        # Old path is gone; purge its embedding sidecar rows. The new path
        # was already re-indexed by batch_atomic_write above.
        try:
            from . import embeddings
            embeddings.delete_after_remove(vault_root, [old_rel])
        except Exception:  # noqa: BLE001 — embeddings are best-effort
            log.exception(
                "embedding delete failed for moved %s; sidecar may be stale", old_rel
            )

    # If we just moved a file out of `_trash/`, its `.meta.json` sidecar (if
    # any) is now an orphan. Drop the sidecar — recovery is "removed from
    # trash," not "trash entry that points nowhere." For trash → trash
    # moves we leave it alone (the sidecar is still valid).
    parts = old_rel.split("/")
    moved_out_of_trash = (
        len(parts) >= 2 and parts[0] == "Knowledge Base" and parts[1] == "_trash"
    )
    new_parts = new_rel.split("/")
    moved_into_trash = (
        len(new_parts) >= 2 and new_parts[0] == "Knowledge Base"
        and new_parts[1] == "_trash"
    )
    if moved_out_of_trash and not moved_into_trash:
        sidecar = old_abs.parent / f"{old_abs.name}.meta.json"
        if sidecar.exists():
            try:
                sidecar.unlink()
                warnings.append(
                    f"removed orphan trash sidecar: {sidecar.name}"
                )
            except OSError as e:
                warnings.append(
                    f"recovered file but could not remove orphan sidecar "
                    f"{sidecar.name!r}: {e}"
                )

    today = today or dt.date.today()
    date_iso = today.isoformat()
    new_rel_no_ext = new_rel.removesuffix(".md") if new_rel.endswith(".md") else new_rel
    log_body = (
        f"Moved {old_rel!r} → {new_rel!r} via exomem Tier 2. "
        f"wikilinks_updated={wikilinks_updated} across {len(files_touched)} file(s)."
    )
    if src_curated or dst_curated:
        log_body += f" allow_curated=true (tree: {src_curated or dst_curated})."
    log_warning = write_log_entry(
        vault_root,
        date_iso=date_iso,
        op="move_file",
        rel_path_no_ext=new_rel_no_ext,
        body=log_body,
    )
    if log_warning:
        warnings.append(log_warning)

    return MoveFileResult(
        old_path=old_rel,
        new_path=new_rel,
        wikilinks_updated=wikilinks_updated,
        files_touched=files_touched,
        warnings=warnings,
    )


def _rewrite_wikilinks(text: str, old_rel: str, new_rel: str) -> tuple[str, int]:
    """Rewrite [[old]]/[[old.md]]/[[basename]] → [[new]] in `text`.

    Only rewrites the bare basename form when the basename in the link
    matches the OLD file's basename; this matches the resolution that
    find_inbound_wikilinks performs.
    """
    old_no_ext = old_rel.removesuffix(".md")
    new_no_ext = new_rel.removesuffix(".md")
    old_full = old_no_ext if old_no_ext.startswith("Knowledge Base/") else "Knowledge Base/" + old_no_ext
    new_full = new_no_ext if new_no_ext.startswith("Knowledge Base/") else "Knowledge Base/" + new_no_ext
    old_stripped = old_full.removeprefix("Knowledge Base/")
    new_stripped = new_full.removeprefix("Knowledge Base/")
    old_basename = old_no_ext.rsplit("/", 1)[-1]
    new_basename = new_no_ext.rsplit("/", 1)[-1]

    n = 0

    def repl(m: re.Match) -> str:
        nonlocal n
        target = m.group(1).strip()
        alias = m.group(2) or ""
        # Split off `#anchor` so refs like `[[Knowledge Base/Foo#section]]`
        # match the path part. The anchor is preserved verbatim in the
        # rewrite so the intra-page jump still resolves at the new location.
        if "#" in target:
            target_path, anchor = target.split("#", 1)
            target_path = target_path.rstrip()
            anchor_suffix = "#" + anchor
        else:
            target_path = target
            anchor_suffix = ""
        target_no_ext = target_path.removesuffix(".md")
        if target_no_ext == old_full or target_no_ext == old_stripped:
            n += 1
            # Preserve whether the link was full-form or stripped-form, and
            # carry the anchor through unchanged.
            if target_path.startswith("Knowledge Base/"):
                return f"[[{new_full}{anchor_suffix}{alias}]]"
            return f"[[{new_stripped}{anchor_suffix}{alias}]]"
        if "/" not in target_no_ext and target_no_ext == old_basename:
            n += 1
            # Basename links rewrite to the new basename (still bare-form).
            return f"[[{new_basename}{anchor_suffix}{alias}]]"
        return m.group(0)

    new_text = _WIKILINK_PATTERN.sub(repl, text)
    return new_text, n


_ = walk_vault_md  # imported for parity with other Tier 2 modules
