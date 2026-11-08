"""The leaf plumbing both fold step kinds share: trash a file only while identical bytes stay held elsewhere.

Curation refuses `move`, `delete` and `recover` because a rename that stops half-way
cannot be told apart from one that an outside actor reversed. A fold never needs that
proof: it moves a file to the vault trash only after it has re-verified, immediately
before the move, that identical bytes are held elsewhere (the history entry of a recap
revision, the canonical page's original of a duplicate). A move whose state is later
unknown therefore cannot lose content, and resume decides from the live state alone:

- bytes at the sealed trash identity and none in place: the move committed;
- bytes in place and none at the identity: it did not;
- neither, or both: the run blocks with `CURATION_OUTCOME_UNCERTAIN`.

The trash writer (`delete_file`) keeps its append-only refusal for every other caller. A
fold reaches past it only through `Placement`, which fixes the trash identity and names
the verifier that runs before the file moves.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

IN_PLACE: Final = "in-place"
MOVED: Final = "moved"


@dataclass(frozen=True)
class Placement:
    """The one closed bypass of the trash writer's append-only refusal.

    `trash_path` is the sealed identity the file must land at, and `verify` re-proves,
    just before the move, that identical bytes remain held elsewhere. It raises to refuse.
    """

    trash_path: str
    verify: Callable[[], None]


def _hash_at(vault_root: Path, path: str) -> str | None:
    """The content hash of the file at `path`, or None when nothing is there."""
    from . import curation

    if curation._guarded_absent(vault_root, path):  # noqa: SLF001 - curation's own guarded reader
        return None
    return curation._guarded_text(vault_root, path, stale=False)[1]  # noqa: SLF001


def state(vault_root: Path, *, path: str, trash_path: str, expected_hash: str) -> str:
    """Whether the move of `path` to `trash_path` has not happened (`IN_PLACE`) or has (`MOVED`).

    A file that changed since it was sealed is a stale binding, not an unknown state.
    """
    from . import curation

    in_place = _hash_at(vault_root, path)
    trashed = _hash_at(vault_root, trash_path)
    if in_place == expected_hash and trashed is None:
        return IN_PLACE
    if in_place is None and trashed == expected_hash:
        return MOVED
    if trashed is None and in_place is not None:
        raise curation._error("CURATION_BINDING_STALE", f"{path!r} changed since it was sealed")  # noqa: SLF001
    raise curation._error(  # noqa: SLF001
        "CURATION_OUTCOME_UNCERTAIN", f"{path!r} and its trash identity do not name one outcome"
    )


def restore_from_trash(
    vault_root: Path, *, path: str, trash_path: str, expected_hash: str
) -> bool:
    """Put the exact bytes at `trash_path` back at `path`; False when they already stood there.

    This is the undo of `move_to_trash`: it restores only the bytes that were sealed, so
    nothing a later writer put at `path` is replaced (a taken `path` is a stale binding).
    """
    from . import curation
    from . import recover_from_trash as recover_module

    if state(vault_root, path=path, trash_path=trash_path, expected_hash=expected_hash) == IN_PLACE:
        return False
    try:
        recover_module.recover_from_trash(
            vault_root, trash_path=trash_path, restore_path=path, allow_curated=False, _fold=True
        )
    except recover_module.RecoverError as error:
        raise curation._error(error.code, error.reason) from error  # noqa: SLF001
    if state(vault_root, path=path, trash_path=trash_path, expected_hash=expected_hash) != IN_PLACE:
        raise curation._error(  # noqa: SLF001
            "CURATION_OUTCOME_UNCERTAIN", f"{path!r} was not restored from its sealed trash identity"
        )
    return True


def move_to_trash(
    vault_root: Path,
    *,
    path: str,
    trash_path: str,
    expected_hash: str,
    verify: Callable[[], None],
    now: dt.datetime,
    expected_dead_inbound: Sequence[str] = (),
) -> bool:
    """Trash `path` at its sealed identity once `verify` passes; False when it already stood there.

    `verify` runs again inside the trash writer, immediately before the rename, so a copy
    that vanished after the caller's own check still stops the move.
    """
    from . import curation
    from . import delete_file as delete_module

    if state(vault_root, path=path, trash_path=trash_path, expected_hash=expected_hash) == MOVED:
        return False
    try:
        delete_module.delete_file(
            vault_root,
            path=path,
            confirm=True,
            # A superseded revision is part of its chain by design, and `expected_dead_inbound` names
            # the episode's own pages: any other inbound link would have pinned the file.
            force_superseded=True,
            expected_dead_inbound=list(expected_dead_inbound),
            now=now,
            expected_hash=expected_hash,
            _fold=Placement(trash_path, verify),
        )
    except delete_module.DeleteFileError as error:
        raise curation._error(error.code, error.reason) from error  # noqa: SLF001
    if state(vault_root, path=path, trash_path=trash_path, expected_hash=expected_hash) != MOVED:
        raise curation._error(  # noqa: SLF001
            "CURATION_OUTCOME_UNCERTAIN", f"{path!r} did not reach its sealed trash identity"
        )
    return True
