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
import re
import threading
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from . import find_corpus, freshness, memory_refs, originals, vault

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
    """The SHA-256 of the bytes at `path`, or None when nothing is there. A binary original has no text form."""
    from . import curation

    if curation._guarded_absent(vault_root, path):  # noqa: SLF001 - curation's own guarded reader
        return None
    return curation._guarded_digest(vault_root, path, stale=False)  # noqa: SLF001


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


# ---------------------------------------------------------------- what the two fold kinds share


def refuse(code: str, reason: str):
    """A curation refusal; the fold modules raise through this one constructor."""
    from . import curation

    return curation._error(code, reason)  # noqa: SLF001 - the lane's own error constructor


def invalid(where: str, why: str):
    return refuse("INVALID_CURATION_PLAN", f"{where} {why}")


def require_authority(vault_root: Path) -> None:
    """The whole-namespace content authority that source-identity lookup needs, as a curation refusal.

    A fold reads every page of the vault, whatever the caller may see, because a reference that
    the caller cannot see still cites the page being retired.
    """
    from . import source_capture

    try:
        source_capture.require_authority(vault_root)
    except source_capture.SourceCaptureError as error:
        code, _, reason = str(error).partition(": ")
        raise refuse(code, reason) from error


def has_authority(vault_root: Path) -> bool:
    """Whether the current caller holds the whole-namespace authority that `require_authority` demands."""
    from . import curation

    try:
        require_authority(vault_root)
    except curation.CurationError:
        return False
    return True


def text_at(vault_root: Path, path: str, expected_hash: str) -> str:
    """The text of the page at `path`, refusing as stale unless it still hashes to `expected_hash`."""
    from . import curation

    text, actual = curation._guarded_text(vault_root, path, stale=True)  # noqa: SLF001
    if actual != expected_hash:
        raise refuse("CURATION_BINDING_STALE", f"{path!r} changed since it was reviewed")
    return text


def page_arg(value: Any, where: str) -> dict[str, str]:
    """A step's page argument: exactly a governed `path` and the reviewed `expected_hash`."""
    from . import curation

    # nosemgrep: ep-word-set -- the fold steps' page argument fields, fixed by their schemas.
    if not isinstance(value, Mapping) or set(value) != {"path", "expected_hash"}:
        raise invalid(where, "must be an object of exactly path and expected_hash")
    if not originals.is_sha256(value["expected_hash"]):
        raise invalid(where, "needs the reviewed expected_hash")
    path = curation.normalize_target_path(value["path"], field=f"{where}.path", allow_fold=True)
    return {"path": path, "expected_hash": value["expected_hash"]}


def is_navigation(path: str) -> bool:
    """Whether `path` is a generated listing (`index.md`, `log.md`): a row there lists a page, it never cites it."""
    return PurePosixPath(path).name.lower() in find_corpus.NAVIGATION_BASENAMES


def listings_of(vault_root: Path, paths: Collection[str]) -> dict[str, list[str]]:
    """Each of `paths` mapped to the generated listings that name it, from one read of the inbound links.

    A trash move leaves a listing's row to age out of the capped list. Read every path before the first
    move: a process without a live registry rebuilds its inbound index whenever a page moves.
    """
    inbound = vault.find_inbound_wikilinks_many(vault_root, list(paths))
    return {path: sorted({link.path for link in links if is_navigation(link.path)}) for path, links in inbound.items()}


def listing_pages(vault_root: Path, path: str) -> list[str]:
    """The generated listings that name `path`."""
    return listings_of(vault_root, [path])[path]


#: The closed form of a memory reference, as the reference scheme fixes it.
# Sound under C6: it checks the shape of a protocol token (scheme prefix plus a UUID), not what anyone meant.
MEMORY_REFERENCE = re.compile(re.escape(memory_refs.REF_PREFIX) + r"([0-9a-fA-F-]{36})")


#: What each vault page cited when it was last read, by page and the stat signature it was read at.
#: A repeat census stats every page and reads again only the pages whose signature moved, so the vault
#: mutation boundary is held for a stat walk, not a read of every page. The first census reads every page.
_CITED: dict[str, dict[str, tuple[freshness.FileSignature, frozenset[str]]]] = {}
_CITED_LOCK = threading.Lock()


def _identities_in(page: Path) -> frozenset[str] | None:
    """The memory identities a page cites, or None when it cannot be read as text."""
    try:
        text = page.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return frozenset(
        identity
        for match in MEMORY_REFERENCE.finditer(text)
        if (identity := memory_refs.normalize_id(match.group(1)))
    )


def _disk_signatures(vault_root: Path) -> dict[str, freshness.FileSignature]:
    """The stat signature of every page the vault walk yields: one stat per page and no read."""
    signatures: dict[str, freshness.FileSignature] = {}
    for page in vault.walk_vault_md(vault_root):
        try:
            signatures[str(page)] = freshness.stat_signature(page)
        except OSError:
            continue  # the page went away after the walk listed it
    return signatures


def _settle_inbound_index(vault_root: Path, signatures: Mapping[str, freshness.FileSignature]) -> None:
    """Withdraw the resident inbound-link index when the freshness registry lags the disk.

    The index is keyed to the registry, and a page written outside the service stays unknown to both until
    the watcher reports it. A lookup after a withdrawal reads the pages, so a citer that the registry has
    not seen still pins. A registry that agrees with the disk keeps the index, and a process without a live
    registry already stats the disk for its key.
    """
    registered = freshness.triple(vault_root, "vault")
    if registered is not None and registered != freshness.triple_from_entries(signatures.items()):
        vault.evict_inbound_index(vault_root)


def _citations(
    vault_root: Path, signatures: Mapping[str, freshness.FileSignature]
) -> Iterator[tuple[str, frozenset[str]]]:
    """Each vault page, as a path string, and the memory identities it cites, over the whole vault.

    The census does not trust the freshness registry: a citation it missed would let a fold trash the page
    it cites. `signatures` was read from the disk before any page, so the bytes are never older than the
    signature they are held under. A page that cannot be read is answered as citing nothing and never held,
    so a transient failure cannot hide a citation until the page changes.
    """
    root = str(Path(vault_root).resolve())
    with _CITED_LOCK:
        known = _CITED.get(root, {})
    held: dict[str, tuple[freshness.FileSignature, frozenset[str]]] = {}
    for location, signature in signatures.items():
        entry = known.get(location)
        if entry is None or entry[0] != signature:
            cited = _identities_in(Path(location))
            if cited is None:
                yield location, frozenset()
                continue
            entry = (signature, cited)
        held[location] = entry
        yield location, entry[1]
    with _CITED_LOCK:
        _CITED[root] = held


def _pages_citing(
    vault_root: Path, identities: Collection[str], signatures: Mapping[str, freshness.FileSignature]
) -> dict[str, set[str]]:
    """Each memory identity mapped to the vault-relative pages that cite it, whatever their caller or class."""
    citing: dict[str, set[str]] = {identity: set() for identity in identities}
    for location, cited in _citations(vault_root, signatures):
        for identity in cited & citing.keys():
            if (relative := freshness.registry_rel(vault_root, location)) is not None:
                citing[identity].add(relative)
    return citing


def referencing_pages(
    vault_root: Path, targets: Mapping[str, str | None], own: frozenset[str]
) -> dict[str, list[str]]:
    """Each target path mapped to the live pages outside `own` that cite it, in path order.

    `targets` maps a path to the memory identity of its page, or None. Two evidences, both read over the
    whole vault with no caller filter: a wikilink (which is how a `sources:` citation is written) and a
    memory reference to the target's identity. Any live page counts, whatever its lifecycle class: a
    superseded or retired page still cites what it cited. A generated listing never cites.

    One stat of every page settles both evidences against the disk, so a page written outside the service
    and not yet reported by the watcher still counts.
    """
    signatures = _disk_signatures(vault_root)
    _settle_inbound_index(vault_root, signatures)
    named: dict[str, set[str]] = {target: set() for target in targets}
    for target, links in vault.find_inbound_wikilinks_many(vault_root, list(targets)).items():
        named[target].update(link.path for link in links if link.path not in own and not is_navigation(link.path))
    identities = {
        identity: target for target, ref in targets.items() if (identity := memory_refs.normalize_id(ref))
    }
    if identities:
        for identity, pages in _pages_citing(vault_root, identities, signatures).items():
            named[identities[identity]].update(page for page in pages if page not in own)
    return {target: sorted(pages) for target, pages in named.items()}


def ingested_entries(value: Any) -> list[str]:
    """The `ingested_into` entries, flattened: an unquoted `[[x]]` parses as a nested list."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list | tuple):
        return [entry for item in value for entry in ingested_entries(item)]
    return []


def commit_witness(vault_root: Path) -> None:
    """Commit the step's witness, last: it states the finished effect, so no earlier batch may carry it."""
    from . import curation_witness

    pending = curation_witness.pending_for(vault_root)
    if pending is None:
        raise refuse("CURATION_OUTCOME_UNCERTAIN", "the fold has no witness to commit")
    pending.ready = True
    vault.batch_atomic_write([], vault_root=vault_root, post_commit_fanout=False)
