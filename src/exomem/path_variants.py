"""Merge folder splits: siblings whose names are one name after NFC and casefold.

A split folder (`Garden/` beside `garden/`) blocks a whole-vault export with `CASE_COLLISION`.
Writes already land in the split's canonical spelling (`vault.canonical_variant`); this module
folds what is already under the other spellings into it.

`preview` lists every split, its canonical spelling, the files still under each other spelling,
and the other spellings that are left as empty folders. It plans one bounded batch of moves, each
whose `plan_id` binds its moves and the empty folders; the preview also shows, while time allows,
the links each move rewrites. `apply` plans again, refuses a changed plan, then previews and moves
the batch one file at a time through `move_file`, which rewrites links and refuses a move whose
referrers changed since its own preview. A file whose move `move_file` refuses is skipped and
listed. Both calls stop early when the request budget runs short and say what is left. The merge is one-way: the bytes are kept, links are rewritten and each moved page's
lineage names its old path, but no operation moves the files back. It then removes the listed empty
folders, which only a held `rmdir` of a still-empty folder can do. Preview again for the next batch.

A plan resolves destinations only until its batch is full and previews no move, so its cost
follows the batch, not the size of the split. A move's preview scans the vault for its referrers,
so a call's moves are bounded by time as well as by the batch.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import held_fs, request_budget, vault, writer_lease
from .audit import VAULT_WALK_SKIP_DIRS

MODE = "path-variants"
#: The files one apply moves. Each move scans the referrers of the file it moves, and the apply
#: previews the batch again before it moves it, so the batch bounds one call's work.
BATCH_UNITS = 16
#: The splits and blocked files one answer lists; the counts cover all of them.
LIST_LIMIT = 50
#: The candidates one plan examines to fill its batch. Each costs a destination, so a split whose
#: leading files are all blocked still answers in bounded time and lists them.
EXAMINE_LIMIT = 4 * BATCH_UNITS
#: The time a move is assumed to take before one has been measured in this call, in seconds.
FIRST_MOVE_RESERVE = 2.0
_WHY_BYTES = 512
_STALE = "STALE_PATH_VARIANT_PLAN: the vault changed since the preview; preview again"


@dataclasses.dataclass(frozen=True)
class _Split:
    parent: str
    spellings: tuple[str, ...]
    canonical: str | None

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


def _visible(name: str) -> bool:
    return not name.startswith(".") and name not in VAULT_WALK_SKIP_DIRS


def _splits(root: Path, view: Any) -> list[_Split]:
    """Every sibling group with one `vault_name_key`, walked as the `path_variants` audit walks."""
    found: list[_Split] = []
    for directory, subdirs, files in os.walk(root):
        request_budget.checkpoint()
        subdirs[:] = sorted(name for name in subdirs if _visible(name))
        parent = Path(directory).relative_to(root).as_posix()
        parent = "" if parent == "." else parent
        names = {*subdirs, *(name for name in files if not name.startswith("."))}
        if view is not None:
            names.update(child.name for child in view.children(parent) if _visible(child.name))
        groups: dict[str, list[str]] = {}
        for name in sorted(names):
            groups.setdefault(vault.vault_name_key(name), []).append(name)
        for spellings in groups.values():
            if len(spellings) < 2:
                continue
            entries = _entries_reader(root, view, parent)
            folders = [name for name in spellings if entries(name) is not None]
            canonical = (
                vault.canonical_variant(root, spellings, entries)
                if len(folders) == len(spellings)
                else None
            )
            found.append(_Split(parent, tuple(spellings), canonical))
    return found


def _entries_reader(root: Path, view: Any, parent: str) -> Callable[[str], set[str] | None]:
    def entries(name: str) -> set[str] | None:
        return vault.folder_entries(root, view, f"{parent}/{name}" if parent else name)

    return entries


def _files_under(root: Path, view: Any, folder: str) -> list[str]:
    """Every file below `folder`, hidden ones included, from the disk and the store."""
    files: set[str] = set()
    for directory, _subdirs, names in os.walk(root / folder):
        request_budget.checkpoint()
        base = Path(directory).relative_to(root).as_posix()
        files.update(f"{base}/{name}" for name in names)
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


def _destination(root: Path, view: Any, split: _Split, name: str, rel: str) -> dict:
    """Where `rel` lands in the canonical spelling, or why it cannot move there."""
    rest = rel[len(split.path(name)) + 1 :]
    target = vault.respell_new_path(root, f"{split.path(split.canonical)}/{rest}", replacing=rel)
    checks = [target]
    if rel.lower().endswith(".md") and (root / rel[:-3]).is_file():
        # The page of a captured file: its bytes land beside it.
        checks.append(target[:-3])
    for path in checks:
        if _exists(root, view, path):
            return {"path": rel, "code": "DESTINATION_TAKEN", "reason": f"{path} already exists"}
    return {"from": rel, "to": target}


def _empty_tree(root: Path, view: Any, folder: str) -> list[str] | None:
    """Every folder of a tree that holds no file, deepest first, or None when it holds one."""
    if _files_under(root, view, folder):
        return None
    folders = [
        Path(directory).relative_to(root).as_posix() for directory, _subdirs, _files in os.walk(root / folder)
    ]
    return sorted(folders, key=lambda rel: (-rel.count("/"), rel))


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
    return {
        **unit,
        "digest": result.preview_digest,
        "referrers_rewritten": len(result.files_touched),
    }


def _plan(root: Path) -> dict[str, Any]:
    with _store_view(root) as view:
        splits = _splits(root, view)
        queued: list[tuple[_Split, str, str]] = []
        blocked: list[dict] = []
        empty: list[str] = []
        listed: list[dict] = []
        folded: list[str] = []
        for split in splits:
            if any(split.parent == path or split.parent.startswith(f"{path}/") for path in folded):
                # A split inside a spelling that folds into another is planned with that spelling.
                continue
            folded.extend(split.path(name) for name in split.spellings if name != split.canonical and split.canonical)
            row: dict[str, Any] = {"parent": split.parent, "canonical": split.canonical, "variants": []}
            if split.canonical is None:
                row["mergeable"] = False
                row["reason"] = "a file shares its name with another entry; rename one by hand"
            for name in split.spellings:
                if split.canonical is None or name == split.canonical:
                    row["variants"].append({"name": name, **({"canonical": True} if name == split.canonical else {})})
                    continue
                tree = _empty_tree(root, view, split.path(name))
                if tree is not None:
                    empty.extend(tree)
                    row["variants"].append({"name": name, "files": 0, "empty": True})
                    continue
                candidates, hidden = _candidates(root, view, split.path(name))
                queued.extend((split, name, rel) for rel in candidates)
                blocked.extend(hidden)
                row["variants"].append({"name": name, "files": len(candidates) + len(hidden)})
            listed.append(row)
        batch: list[dict] = []
        examined = 0
        for split, name, rel in queued:
            if len(batch) == BATCH_UNITS or examined == EXAMINE_LIMIT:
                break
            examined += 1
            unit = _destination(root, view, split, name, rel)
            if "to" in unit:
                batch.append(unit)
            else:
                blocked.append({"path": rel, "code": unit["code"], "reason": unit["reason"]})
    plan_id = hashlib.sha256(
        json.dumps(
            {"batch": batch, "empty": empty},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    examined_blocked = examined - len(batch)
    return {
        "batch": batch,
        "empty": empty,
        "public": {
            "mode": MODE,
            "plan_id": plan_id,
            "split_count": len(splits),
            "splits": listed[:LIST_LIMIT],
            "splits_truncated": len(listed) > LIST_LIMIT,
            # Files still to move, less those this plan examined and found blocked; a file past the
            # examined ones may yet be blocked when a later plan reaches it.
            "files_pending": len(queued) - examined_blocked,
            "files_examined": examined,
            "batch": [{"from": unit["from"], "to": unit["to"]} for unit in batch],
            "empty_folders": empty[:LIST_LIMIT],
            "empty_folder_count": len(empty),
            "blocked": blocked[:LIST_LIMIT],
            "blocked_count": len(blocked),
        },
    }


class _Pace:
    """Whether another move still fits the request budget, from the slowest move so far."""

    def __init__(self) -> None:
        self.slowest = FIRST_MOVE_RESERVE

    def fits(self) -> bool:
        return request_budget.can_afford(2 * self.slowest)

    @contextmanager
    def timed(self) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.slowest = max(self.slowest, time.monotonic() - started)


def preview(vault_root: Path) -> dict[str, Any]:
    """Read-only: each split, the next batch of moves into its canonical spelling, and empty folders.

    Each move of the batch is previewed while time allows: the pages whose links it rewrites, or
    why `move_file` would refuse it (the apply then skips it). A move left unpreviewed says so.
    """
    root = Path(vault_root)
    plan = _plan(root)
    public = dict(plan["public"])
    pace = _Pace()
    rows = []
    for unit in plan["batch"]:
        if not pace.fits():
            rows.append({**unit, "previewed": False})
            continue
        with pace.timed():
            previewed = _previewed(root, unit)
        if "digest" in previewed:
            rows.append({**unit, "previewed": True, "referrers_rewritten": previewed["referrers_rewritten"]})
        else:
            rows.append({**unit, "previewed": True, "refused": {"code": previewed["code"], "reason": previewed["reason"]}})
    public["batch"] = rows
    if plan["batch"] or plan["empty"]:
        public["apply"] = {
            "tool": "maintain_memory",
            "args": {"mode": MODE, "apply": True, "plan_id": public["plan_id"], "why": "<reason>"},
        }
    return public


def _validate(plan_id: object, why: object) -> None:
    if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_id):
        raise ValueError("INVALID_ARGUMENTS: path-variants apply requires the preview plan_id")
    if not isinstance(why, str) or not why.strip() or len(why.encode("utf-8")) > _WHY_BYTES or "\n" in why:
        raise ValueError("INVALID_ARGUMENTS: path-variants apply requires a one-line why")


def _remove_empty(root: Path, folders: list[str]) -> list[str]:
    """Remove the listed folders, deepest first, or none: each must still be empty but for listed folders."""
    listed = set(folders)
    for rel in folders:
        names = vault._listed(root / rel) if (root / rel).is_dir() else None  # noqa: SLF001
        if names is None or any(f"{rel}/{name}" not in listed for name in names):
            raise ValueError(_STALE)
    removed: list[str] = []
    acquired = held_fs.acquire(root)
    if not acquired.ok:
        raise ValueError("PATH_GUARD_UNSAFE: held filesystem route is unavailable")
    with acquired.require() as filesystem:
        for rel in folders:
            request_budget.checkpoint()
            found = filesystem.parent(rel, access="mutate")
            # Every folder was checked empty under the mutation boundary, so only a write from
            # outside the service can fail one here; the folders removed so far are reported.
            if not found.ok:
                break
            with found.require() as held:
                if not filesystem.unlink_directory(held).ok:
                    break
            removed.append(rel)
    return removed


def _split_spellings(folders: list[str]) -> list[str]:
    """The listed folders that are a split's own spelling: those whose parent is not listed too."""
    listed = set(folders)
    return [rel for rel in folders if rel.rpartition("/")[0] not in listed]


def apply(vault_root: Path, *, plan_id: str, why: str) -> dict[str, Any]:
    """Move one confirmed batch into canonical spellings and remove the listed empty folders."""
    from .move_file import MoveFileError, move_file
    from .pointer_write import PREVIEW_REQUIRED

    _validate(plan_id, why)
    root = Path(vault_root)
    # Planning walks the vault, so it runs before any boundary; each move takes the boundary itself
    # and refuses when its referrers changed since its own preview.
    plan = _plan(root)
    if plan["public"]["plan_id"] != plan_id:
        raise ValueError(_STALE)
    moved: list[dict] = []
    skipped: list[dict] = []
    stopped: dict[str, str] | None = None
    pace = _Pace()
    for unit in plan["batch"]:
        if not pace.fits():
            stopped = {"path": unit["from"], "code": "BUDGET_SHORT", "reason": "the call ran short of time; preview again"}
            break
        with pace.timed():
            outcome = _move(root, unit, move_file, MoveFileError, PREVIEW_REQUIRED)
        if "to" in outcome and "code" not in outcome:
            moved.append(outcome)
        else:
            skipped.append(outcome)
    removed: list[str] = []
    if stopped is None and plan["empty"]:
        with writer_lease.active_manager().mutation_guard(
            root,
            request_id=writer_lease.active_mutation_request_id(),
            operation="path_variants_commit",
            holder_kind="command",
        ):
            for rel in _split_spellings(plan["empty"]):
                parent, _, name = rel.rpartition("/")
                if not held_fs.name_variants(vault._listed(root / parent), name):  # noqa: SLF001
                    raise ValueError(_STALE)
            if _removal_log(root, plan_id, plan["empty"], why).warning is not None:
                # The log entry is the record of what was removed; without it nothing is removed.
                raise ValueError(
                    "PATH_VARIANT_AUDIT_UNAVAILABLE: the vault log is required to record the removal"
                )
            removed = _remove_empty(root, plan["empty"])
            if removed:
                # The entry names what was removed, which a write from outside the service can cut short.
                vault.batch_atomic_write(list(_removal_log(root, plan_id, removed, why).writes), vault_root=root)
    remaining = plan["public"]["files_pending"] - len(moved)
    outcome = "committed" if moved or removed else "unchanged"
    if stopped is not None:
        outcome = "partial"
    return {
        "mode": MODE,
        "outcome": outcome,
        "plan_id": plan_id,
        "moved": moved,
        "skipped": skipped,
        "removed_folders": removed,
        **({"stopped": stopped} if stopped is not None else {}),
        "files_remaining": remaining,
        # A new preview lists what remains: more files, or the folders the moves emptied.
        **({"next": {"tool": "maintain_memory", "args": {"mode": MODE}}} if moved or stopped else {}),
    }


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


def _move(root: Path, unit: dict, move_file: Callable, error_type: type, preview_required: str) -> dict:
    """Preview one move and commit it with that preview's digest: where it landed, or why it did not move."""
    for _attempt in (1, 2):
        previewed = _previewed(root, {"from": unit["from"], "to": unit["to"]})
        if "digest" not in previewed:
            return {"path": unit["from"], "code": previewed["code"], "reason": previewed["reason"]}
        try:
            result = move_file(
                root,
                old_path=unit["from"],
                new_path=unit["to"],
                update_wikilinks=True,
                preview_digest=previewed["digest"],
                # Keeps the bytes of each append-only page the move repoints, owner-only.
                record_repoints=True,
            )
        except error_type as error:
            if error.code == preview_required:
                # A page this move rewrites changed between its preview and its commit.
                continue
            return {"path": unit["from"], "code": error.code, "reason": error.reason}
        return {"from": result.old_path, "to": result.new_path}
    return {"path": unit["from"], "code": preview_required, "reason": "its referrers kept changing; preview again"}
