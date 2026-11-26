"""Merge folder splits: siblings whose names are one name after NFC and casefold.

A split folder (`Garden/` beside `garden/`) blocks a whole-vault export with `CASE_COLLISION`.
Writes already land in the split's canonical spelling (`vault.canonical_variant`); this module
folds what is already under the other spellings into it.

`preview` (the dry run) lists every split with its canonical spelling, each move of a file into
it, the files that cannot move and why, and the other spellings that the apply removes once they
are empty. Its `plan_id` binds the moves and those folders. `apply` (`dry_run=false` with that
`plan_id` and a one-line `why`) plans again, refuses a changed plan, then moves each file through
`move_file`, which rewrites the links to it. A file whose move `move_file` refuses is skipped and
listed. The apply then removes each listed spelling that the moves left empty, with a held
`rmdir`, and logs the removal.

The merge is one-way: the bytes are kept, links are rewritten and each moved page's lineage names
its old path, but no operation moves the files back.

Each move scans the vault for the pages that link to the file, so a merge costs the vault's size
times the files it moves. A remote owner's preview and apply therefore each run as a maintenance
job (`maintenance_jobs`): neither is bound to one request, the apply reports its progress and
stops between moves when cancelled, and the job's receipt lists every planned and moved file.

The walk keeps the files an export keeps (`vault_portability.classify_artifact`), so the splits
found here are the ones that block an export.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import access, held_fs, kbdir, maintenance_jobs, request_budget, vault

MODE = "path-variants"
#: The splits, moves and other rows one answer lists; the counts cover all of them, and a job's
#: receipt lists every move and blocked file as entries.
LIST_LIMIT = 50
_WHY_BYTES = 512


@dataclasses.dataclass(frozen=True)
class _Split:
    parent: str
    spellings: tuple[str, ...]
    canonical: str | None
    #: Why the merge leaves this split to the owner, or None when it merges it.
    refusal: str | None = None

    def path(self, name: str) -> str:
        return f"{self.parent}/{name}" if self.parent else name


@contextmanager
def _store_view(root: Path) -> Iterator[Any]:
    from .vault_store import mode, read

    if not mode.store_mode_for_reads(root):
        yield None
        return
    with read.snapshot(root) as view:
        yield view


def _exported(rel: str) -> bool:
    from .vault_portability import ArtifactClass, classify_artifact

    return classify_artifact(rel).artifact_class is not ArtifactClass.DISPOSABLE_RUNTIME


def sibling_names(root: Path, view: Any = None) -> Iterator[tuple[str, list[str], list[str]]]:
    """Each folder of the vault as an export sees it: `(parent, its folders, every name in it)`.

    The names are the files an export keeps and the folders that hold one, plus, in store mode,
    the stored pages and folders below it. A folder that holds only files the export drops (a
    repository's objects, an editor's cache) is no name here, so its splits are not reported or
    merged. The `path_variants` audit and the merge both group these names.
    """
    folders: list[tuple[str, list[str], list[str]]] = []
    live: set[str] = set()
    for directory, subdirs, files in os.walk(root):
        request_budget.checkpoint()
        subdirs.sort()
        parent = Path(directory).relative_to(root).as_posix()
        parent = "" if parent == "." else parent
        kept = [name for name in files if _exported(f"{parent}/{name}" if parent else name)]
        if kept and parent:
            # Every folder above a kept file holds it.
            parts = parent.split("/")
            live.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
        folders.append((parent, list(subdirs), kept))
    for parent, subdirs, kept in folders:
        names = {*kept, *(name for name in subdirs if (f"{parent}/{name}" if parent else name) in live)}
        if view is not None:
            names.update(child.name for child in view.children(parent))
        yield parent, subdirs, sorted(names)


def _splits(root: Path, view: Any) -> list[_Split]:
    """Every sibling group with one `vault_name_key`."""
    found: list[_Split] = []
    for parent, _subdirs, names in sibling_names(root, view):
        groups: dict[str, list[str]] = {}
        for name in names:
            groups.setdefault(vault.vault_name_key(name), []).append(name)
        for spellings in groups.values():
            if len(spellings) < 2:
                continue
            entries = _entries_reader(root, view, parent)
            folders = [name for name in spellings if entries(name) is not None]
            canonical = (
                vault.canonical_variant(root, parent, spellings, entries)
                if len(folders) == len(spellings)
                else None
            )
            if len(folders) < len(spellings):
                refusal = "a file shares its name with another entry; rename one by hand"
            elif not kbdir.is_in_managed_root(f"{parent}/_"):
                # Agents change nothing outside the managed knowledge base.
                refusal = "it is outside the knowledge base; rename one by hand"
            elif canonical is None:
                refusal = "no spelling is a declared top-level folder; rename one by hand"
            else:
                refusal = None
            found.append(_Split(parent, tuple(spellings), canonical, refusal))
    return found


def _entries_reader(root: Path, view: Any, parent: str) -> Callable[[str], set[str] | None]:
    def entries(name: str) -> set[str] | None:
        return vault.folder_entries(root, view, f"{parent}/{name}" if parent else name)

    return entries


def _files_under(root: Path, view: Any, folder: str) -> list[str]:
    """Every file below `folder` that an export keeps, hidden ones included, from the disk and the store.

    A file the export drops is never moved, so a folder that holds one is never emptied.
    """
    files: set[str] = set()
    for directory, _subdirs, names in os.walk(root / folder):
        request_budget.checkpoint()
        base = Path(directory).relative_to(root).as_posix()
        files.update(rel for name in names if _exported(rel := f"{base}/{name}"))
    if view is not None:
        files.update(page.path for page in view.pages(f"{folder}/"))
    return sorted(files)


def _exists(root: Path, view: Any, rel: str) -> bool:
    return os.path.lexists(root / rel) or (view is not None and bool(view.held_pages((rel,))))


def _candidates(root: Path, view: Any, folder: str) -> tuple[list[str], list[dict]]:
    """The files that move out of `folder`, and the hidden files that do not, from one walk.

    A captured artifact moves with its page, so only the page of a pair is a candidate.
    """
    from .move_file import _paired_artifact

    files = _files_under(root, view, folder)
    present = set(files)
    held = (lambda rel: bool(view.held_pages((rel,)))) if view is not None else None
    candidates: list[str] = []
    hidden: list[dict] = []
    for rel in files:
        if Path(rel).name.startswith("."):
            hidden.append({"path": rel, "code": "HIDDEN_FILE", "reason": "move or delete a hidden file by hand"})
            continue
        pair = _paired_artifact(root, rel, held)
        if pair is not None and rel == pair[1] and pair[0] in present:
            continue
        candidates.append(rel)
    return candidates, hidden


def _destination(root: Path, view: Any, split: _Split, name: str, rel: str, planned: set[str]) -> dict:
    """Where `rel` lands in the canonical spelling, or why it cannot move there.

    `planned` holds the name keys of the destinations earlier moves of this plan take.
    """
    rest = rel[len(split.path(name)) + 1 :]
    target = vault.respell_new_path(root, f"{split.path(split.canonical)}/{rest}", replacing=rel)
    checks = [target]
    if rel.lower().endswith(".md") and (root / rel[:-3]).is_file():
        # The page of a captured file: its bytes land beside it.
        checks.append(target[:-3])
    for path in checks:
        if _exists(root, view, path) or vault.vault_name_key(path) in planned:
            return {"path": rel, "code": "DESTINATION_TAKEN", "reason": f"{path} already exists"}
    planned.update(vault.vault_name_key(path) for path in checks)
    return {"from": rel, "to": target}


def _empty_tree(root: Path, view: Any, folder: str) -> list[str] | None:
    """Every folder of a tree that holds no file, deepest first, or None when it holds one."""
    if _files_under(root, view, folder) or any(files for _directory, _subdirs, files in os.walk(root / folder)):
        return None
    folders = [
        Path(directory).relative_to(root).as_posix() for directory, _subdirs, _files in os.walk(root / folder)
    ]
    return sorted(folders, key=lambda rel: (-rel.count("/"), rel))


def _plan(root: Path) -> dict[str, Any]:
    with _store_view(root) as view:
        splits = _splits(root, view)
        moves: list[dict] = []
        blocked: list[dict] = []
        folders: list[str] = []
        listed: list[dict] = []
        folded: list[str] = []
        planned: set[str] = set()
        for split in splits:
            maintenance_jobs.checkpoint("planning")
            if any(split.parent == path or split.parent.startswith(f"{path}/") for path in folded):
                # A split inside a spelling that folds into another is planned with that spelling.
                continue
            row: dict[str, Any] = {"parent": split.parent, "canonical": split.canonical, "variants": []}
            refusal = split.refusal or next(
                (reason for name in split.spellings if (reason := access.writable_reason(root, split.path(name)))),
                None,
            )
            if refusal is not None:
                row["mergeable"] = False
                row["reason"] = refusal
                row["variants"] = [{"name": name} for name in split.spellings]
                listed.append(row)
                continue
            folded.extend(split.path(name) for name in split.spellings if name != split.canonical)
            for name in split.spellings:
                if name == split.canonical:
                    row["variants"].append({"name": name, "canonical": True})
                    continue
                folders.append(split.path(name))
                candidates, hidden = _candidates(root, view, split.path(name))
                blocked.extend(hidden)
                for rel in candidates:
                    unit = _destination(root, view, split, name, rel, planned)
                    if "to" in unit:
                        moves.append(unit)
                    else:
                        blocked.append(unit)
                row["variants"].append({"name": name, "files": len(candidates) + len(hidden)})
            listed.append(row)
    plan_id = maintenance_jobs.plan_identity(
        MODE, {}, [*(("move", unit["from"], unit["to"]) for unit in moves), *(("folder", rel) for rel in folders)]
    )
    return {
        "moves": moves,
        "folders": folders,
        "blocked": blocked,
        "listed": listed,
        "public": {
            "mode": MODE,
            "plan_id": plan_id,
            "split_count": len(splits),
            "splits": listed[:LIST_LIMIT],
            "move_count": len(moves),
            "moves": moves[:LIST_LIMIT],
            "blocked_count": len(blocked),
            "blocked": blocked[:LIST_LIMIT],
            # The other spellings: the apply removes each one that its moves leave empty.
            "folder_count": len(folders),
            "folders": folders[:LIST_LIMIT],
        },
    }


def preview(vault_root: Path) -> dict[str, Any]:
    """Read-only: each split, every move into its canonical spelling, and the folders the merge removes.

    A list longer than `LIST_LIMIT` is cut, and its count says so; a job's receipt lists every move
    and every blocked file as entries.
    """
    root = Path(vault_root)
    plan = _plan(root)
    # A job's receipt drops a long list from the report, so each row is also an entry.
    maintenance_jobs.record_planned((unit["from"], unit["to"]) for unit in plan["moves"])
    maintenance_jobs.record_planned((folder, "removed once empty") for folder in plan["folders"])
    maintenance_jobs.record_planned(
        (
            *((row["path"], f"{row['code']}: {row['reason']}") for row in plan["blocked"]),
            *(
                (f"{row['parent']}/{row['variants'][0]['name']}" if row["parent"] else row["variants"][0]["name"], row["reason"])
                for row in plan["listed"]
                if row.get("mergeable") is False
            ),
        ),
        kind=maintenance_jobs.SKIPPED_PAGE,
    )
    public = dict(plan["public"])
    if plan["moves"] or plan["folders"]:
        public["apply"] = {
            "tool": "maintain_memory",
            "args": {"mode": MODE, "dry_run": False, "plan_id": public["plan_id"], "why": "<reason>"},
        }
    return public


def _validate(plan_id: object, why: object) -> None:
    if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_id):
        raise ValueError("INVALID_ARGUMENTS: path-variants apply requires the preview plan_id")
    if not isinstance(why, str) or not why.strip() or len(why.encode("utf-8")) > _WHY_BYTES or "\n" in why:
        raise ValueError("INVALID_ARGUMENTS: path-variants apply requires a one-line why")


def apply(vault_root: Path, *, plan_id: str, why: str) -> dict[str, Any]:
    """Move every planned file into its canonical spelling, then remove the spellings left empty."""
    _validate(plan_id, why)
    root = Path(vault_root)
    # Planning walks the vault, so it holds no boundary; each move holds it only while it commits.
    plan = _plan(root)
    maintenance_jobs.require_current_plan(MODE, plan_id, plan["public"]["plan_id"], "the folder splits")
    moved: list[dict] = []
    skipped: list[dict] = []
    total = len(plan["moves"])
    for index, unit in enumerate(plan["moves"]):
        maintenance_jobs.checkpoint("moving", done=index, total=total)
        outcome = _move(root, unit)
        if "to" in outcome:
            moved.append(outcome)
            maintenance_jobs.record_paths([outcome["to"]], committed=True)
        else:
            skipped.append(outcome)
            maintenance_jobs.record_planned(
                [(outcome["path"], f"{outcome['code']}: {outcome['reason']}")], kind=maintenance_jobs.SKIPPED_PAGE
            )
    maintenance_jobs.checkpoint("folders", done=total, total=total)
    removed, kept, stopped = _remove_emptied(root, plan["folders"], plan_id, why)
    complete = not skipped and not kept and stopped is None
    outcome = {
        "outcome": "committed" if complete and (moved or removed) else "partial" if moved or removed else "unchanged",
        "moved_count": len(moved),
        "skipped_count": len(skipped),
        "skipped": skipped[:LIST_LIMIT],
        "removed_folders": removed,
        "kept_folders": kept,
        **({"stopped": stopped} if stopped is not None else {}),
    }
    # A job's receipt keeps the compact reply of a write, so the merge's own account goes in its notes.
    maintenance_jobs.note(**outcome)
    return {
        "mode": MODE,
        "plan_id": plan_id,
        **outcome,
        "moved": moved[:LIST_LIMIT],
        # A new preview lists what is left: files that could not move, or a split that remains.
        **({"next": {"tool": "maintain_memory", "args": {"mode": MODE}}} if not complete else {}),
    }


def _remove_emptied(
    root: Path, folders: list[str], plan_id: str, why: str
) -> tuple[list[str], list[dict], dict | None]:
    """Remove each listed spelling that holds no file and is still a split, under the boundary.

    Returns the folders removed, those kept with why, and why removal stopped, if it did.
    """
    removed: list[str] = []
    kept: list[dict] = []
    with maintenance_jobs.chunk_hold(root, "path_variants_commit"), _store_view(root) as view:
        trees: list[list[str]] = []
        for folder in folders:
            if not (root / folder).is_dir():
                continue
            parent, _, name = folder.rpartition("/")
            reason = access.writable_reason(root, folder)
            tree = None if reason else _empty_tree(root, view, folder)
            if tree is None and reason is None and not _files_under(root, view, folder):
                # The export drops these files (an editor's or a system's), so the split no longer
                # blocks it; the merge never deletes a file, so the owner removes the folder.
                reason = "only files an export drops are under it; remove the folder by hand"
            if tree is None:
                kept.append({"path": folder, "reason": reason or "files are still under it"})
            elif not held_fs.name_variants(vault._listed(root / parent if parent else root), name):  # noqa: SLF001
                kept.append({"path": folder, "reason": "it is no longer a split"})
            else:
                trees.append(tree)
        if not trees:
            return removed, kept, None
        if _removal_log(root, plan_id, [tree[-1] for tree in trees], why).warning is not None:
            # The log entry is the record of what was removed; without it nothing is removed.
            return removed, kept, {
                "code": "PATH_VARIANT_AUDIT_UNAVAILABLE",
                "reason": "the vault log is required to record the removal; no folder was removed",
            }
        for tree in trees:
            done = _remove_tree(root, tree)
            removed.extend(done)
            if len(done) < len(tree):
                # Only a write from outside the service can refill a folder checked under the boundary.
                kept.append({"path": tree[-1], "reason": "a file appeared in it during the removal"})
        if removed:
            # The entry names what was removed.
            vault.batch_atomic_write(list(_removal_log(root, plan_id, removed, why).writes), vault_root=root)
    return removed, kept, None


def _remove_tree(root: Path, tree: list[str]) -> list[str]:
    """Remove an empty tree's folders, deepest first, stopping at the first that cannot go."""
    removed: list[str] = []
    acquired = held_fs.acquire(root)
    if not acquired.ok:
        raise ValueError("PATH_GUARD_UNSAFE: held filesystem route is unavailable")
    with acquired.require() as filesystem:
        for rel in tree:
            found = filesystem.parent(rel, access="mutate")
            if not found.ok:
                break
            with found.require() as held:
                if not filesystem.unlink_directory(held).ok:
                    break
            removed.append(rel)
    return removed


def _removal_log(root: Path, plan_id: str, folders: list[str], why: str) -> Any:
    return vault.plan_log_writes(
        root,
        date_iso=dt.date.today().isoformat(),
        op="maintain_memory",
        rel_path_no_ext=folders[-1],
        body="Removed empty path-variant folders "
        + json.dumps(
            {"plan_id": plan_id, "folders": folders, "rationale": why},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        operation_token=f"{MODE}:{plan_id}",
    )


def _previewed(root: Path, unit: dict) -> dict:
    """The unit with its `move_file` preview: the digest the commit passes, or why it is blocked."""
    from .move_file import MoveFileError, move_file

    try:
        result = move_file(
            root, old_path=unit["from"], new_path=unit["to"], update_wikilinks=True, preview=True
        )
    except MoveFileError as error:
        return {**unit, "code": error.code, "reason": error.reason}
    refused = [row for row in result.referrers if row.get("outcome") == "refused"]
    if refused or result.preview_digest is None:
        first = refused[0] if refused else {"code": "MOVE_REFUSED", "cause": "the move cannot commit"}
        return {**unit, "code": first.get("code"), "reason": f"{first.get('path', unit['from'])}: {first.get('cause')}"}
    return {**unit, "digest": result.preview_digest}


def _move(root: Path, unit: dict) -> dict:
    """Preview one move and commit it with that preview's digest: where it landed, or why it did not move."""
    from .move_file import MoveFileError, move_file
    from .pointer_write import PREVIEW_REQUIRED

    for _attempt in (1, 2):
        previewed = _previewed(root, unit)
        if "digest" not in previewed:
            return {"path": unit["from"], "code": previewed["code"], "reason": previewed["reason"]}
        try:
            # The hold waits out another writer's turn instead of failing the move.
            with maintenance_jobs.chunk_hold(root, "path_variants_move"):
                result = move_file(
                    root,
                    old_path=unit["from"],
                    new_path=unit["to"],
                    update_wikilinks=True,
                    preview_digest=previewed["digest"],
                    # Keeps the bytes of each append-only page the move repoints, owner-only.
                    record_repoints=True,
                )
        except MoveFileError as error:
            if error.code == PREVIEW_REQUIRED:
                # A page this move rewrites changed between its preview and its commit.
                continue
            return {"path": unit["from"], "code": error.code, "reason": error.reason}
        return {"from": result.old_path, "to": result.new_path}
    return {"path": unit["from"], "code": PREVIEW_REQUIRED, "reason": "its referrers kept changing; preview again"}
