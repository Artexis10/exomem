"""The held-items operations of the review and repair tools.

The review tool lists held items with the `held` kind. The repair tool resolves one with the
`held-resolve` kind. Both are adapters over `vault_store.held`, which owns the list, the two
resolutions and their rules. An adapter checks its arguments, hides what the caller may not read,
calls the store module and shapes one answer. Each answer leads with a sentence that the agent can
repeat to the person as it stands. Hashes, revisions and the folder of a saved copy stay in a
`diagnostics` part.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# `cli_ops` imports the operation entries, which read the names below, so this module takes
# `OpError` from it only inside the two operations.

#: The tools that carry these two operations. A later rename of either tool is one edit here.
REVIEW_TOOL = "read_review"
REPAIR_TOOL = "repair_memory"
LIST_MODE = "held"
RESOLVE_MODE = "held-resolve"

INVALID = "HELD_ITEMS_INVALID"
_DEFAULT_LIMIT = 25
_MAX_LIMIT = 100
# The arguments that held-resolve needs, each with the step that supplies it: a closed set.
_REQUIRED = {  # nosemgrep: ep-word-set -- the closed argument names of held-resolve
    "path": "Pass the held item's path, as the held list returns it.",
    "choice": "Pass choice keep-mine or keep-store.",
    "item_version": f"Read the item with {REVIEW_TOOL} kind {LIST_MODE} and pass its item_version.",
    "why": "Say in `why` what the person or owner asked for.",
}
#: The fields of `held-resolve`: each is required, and the operation entries read them from here.
RESOLVE_FIELDS = tuple(_REQUIRED)


def _may_see(vault_root: Path, rel: str) -> bool:
    """Whether this caller may read the page at ``rel`` in full: a held item is its business.

    The release level comes from the path alone. The level that compares the page's bytes on disk
    would hide every held item, since a held edit has file bytes that differ from the stored page
    and a held delete has no file.
    """
    from .governance import egress
    from .governance.principal import effective_principal

    level = egress.release_level_for_path_only(vault_root, rel, principal=effective_principal())
    return level >= egress.LEVEL_FULL


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def _row(item: Any) -> dict[str, Any]:
    return {
        "path": item.path,
        "kind": item.kind,
        "detected_at": item.detected_at,
        "file": {
            "exists": item.file.exists,
            "sha256": item.file.sha256,
            "size": item.file.size,
            **({"unreadable": item.file.unreadable} if item.file.unreadable else {}),
        },
        "store": None
        if item.store is None
        else {"revision": item.store.revision, "sha256": item.store.sha256},
        "resolvable": item.resolvable,
        "keep_mine_possible": item.keep_mine_possible,
        "choices": list(item.choices),
        "item_version": item.item_version,
        "diagnostics": {
            "held_store_revision": item.held_store_revision,
            "held_sha256": item.held_sha256,
        },
    }


def listing(vault_root: Path, limit: int | None = None) -> dict[str, Any]:
    """Every held item that this caller may read, oldest first, with the choices that resolve it."""
    from .cli_ops import OpError
    from .vault_store import held, mode

    cap = _DEFAULT_LIMIT if limit is None else limit
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= _MAX_LIMIT:
        raise OpError(
            INVALID,
            f"limit {limit!r} is not a whole number from 1 to {_MAX_LIMIT}.",
            f"Pass a limit from 1 to {_MAX_LIMIT}, or omit it to list {_DEFAULT_LIMIT}.",
        )
    root = Path(vault_root)
    visible = [item for item in held.list_held(root) if _may_see(root, item.path)]
    shown = visible[:cap]
    # An item the caller may not read is left out without a count, as the release contract omits it.
    if not visible:
        sentence = (
            "Nothing is held: no outside edit or delete waits for a choice."
            if mode.store_mode_active(root)
            else "This vault is not in store mode, so it holds no held items."
        )
    else:
        binaries = sum(not item.resolvable for item in visible)
        pages = len(visible) - binaries
        parts = []
        if pages:
            parts.append(
                f"{_count(pages, 'file', 'files')} changed outside Exomem and "
                f"{'waits' if pages == 1 else 'wait'} for a choice. "
                f"Keep the person's file with {held.KEEP_MINE}, "
                f"or keep Exomem's page with {held.KEEP_STORE}."
            )
        if binaries:
            parts.append(
                f"{_count(binaries, 'binary file is', 'binary files are')} held. "
                "Binaries cannot be resolved here yet and stay held "
                "until a later release adds that."
            )
        if len(shown) < len(visible):
            parts.append(f"Showing {len(shown)} of {len(visible)}.")
        sentence = " ".join(parts)
    return {
        "kind": LIST_MODE,
        "outcome": sentence,
        "returned": len(shown),
        "total": len(visible),
        "truncated": len(shown) < len(visible),
        "resolve_with": (
            f"{REPAIR_TOOL} kind {RESOLVE_MODE} with path, one of the item's choices, "
            "the item's item_version and why. "
            f"{held.KEEP_STORE} saves the person's file first, so no bytes are lost."
        ),
        "items": [_row(item) for item in shown],
    }


def resolve(
    vault_root: Path,
    path: str | None,
    choice: str | None,
    item_version: str | None,
    why: str | None,
) -> dict[str, Any]:
    """Resolve one held item. A refusal changes nothing and names what to change."""
    from .cli_ops import OpError
    from .vault_store import held

    supplied = {"path": path, "choice": choice, "item_version": item_version, "why": why}
    for name, hint in _REQUIRED.items():
        value = supplied[name]
        if not isinstance(value, str) or not value.strip():
            raise OpError(INVALID, f"{name} is required to resolve a held item.", hint)
    assert path is not None and choice is not None and item_version is not None and why is not None
    root = Path(vault_root)
    if not _may_see(root, path):
        # A page this caller may not read answers as an item that is not held: it reveals nothing.
        raise held.nothing_held(path)
    result = held.resolve_held(root, path, choice, why=why, seen=item_version)
    diagnostics: dict[str, Any] = {"revision": result.revision}
    copy = None
    if result.exported_to is not None:
        from . import state_paths

        copy = result.exported_to.relative_to(state_paths.vault_state_dir(root)).as_posix()
        diagnostics["saved_copy_sha256"] = result.exported_sha256
    if choice == held.KEEP_MINE:
        sentence = (
            f"Kept the person's file as the stored page of {path}. "
            "Exomem writes nothing to the file."
            if result.person_file_existed
            else f"Removed {path} from the store, as the person deleted its file."
        )
    elif copy is not None:
        sentence = (
            f"Kept Exomem's page for {path}. The person's file is saved first as {copy} in this "
            "vault's state folder, outside the vault, and Exomem now writes its page to the file."
        )
    else:
        sentence = (
            f"Kept Exomem's page for {path}. "
            "The person had deleted its file, so Exomem writes the page back."
        )
    return {
        "outcome": sentence,
        "operation": RESOLVE_MODE,
        "choice": choice,
        "path": path,
        **({"saved_copy": copy} if copy is not None else {}),
        "diagnostics": diagnostics,
    }
