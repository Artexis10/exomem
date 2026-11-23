"""Release verdicts that a store-mode write's corpus admission keeps from one request to the next.

Each store-mode save admits its whole corpus before it commits (`semantic_contract`), under the
store thread's bound writer, where no verdict is published (`published_admission.Admission`). So
each save decided every page again. This memo keeps a page's verdict while every input of it holds:

- The key names every input that is not a page's bytes: the caller, its purpose and connector
  ceiling, the policy and its grants, the tombstones and the registry assignment
  (`egress.release_walk_filter`). A verdict is found only under the key it was decided with.
- An entry names the hash of the page's stored bytes and of each other page whose bytes its decision
  reads (`page_admission.scope_parents`). A verdict is used only while every one of those hashes is
  the current one in the request's pinned store snapshot.
- A page whose decision reads anything else is never kept: a page that the store does not hold, a
  container claim, a bridge, a collection-relevant path, a parent with parents of its own, and a RAW
  release that reads its companion's or its original's bytes. Each request decides those again.
- A verdict whose decision met an input it could not read is never kept, so a transient withhold
  lasts one request.

It stores verdicts and hashes, never page bytes.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from pathlib import Path

from .. import find_corpus
from . import page_admission, raw_protection
from .principal import RequestPrincipal

#: Verdicts kept under one key. The bound sits above the 50,000-page case, so the load target never evicts.
_ENTRIES_MAX = 65_536
#: Keys kept at once, across vaults: a few callers or policies that alternate keep their verdicts,
#: and a key that no request uses is dropped first.
_KEYS_MAX = 4

#: `(vault root, key)` to path to `(allowed, page hash, ((parent path, parent hash or None), ...))`.
_KEPT: OrderedDict[tuple[str, Hashable], OrderedDict[str, tuple[bool, str, tuple]]] = OrderedDict()
_LOCK = threading.Lock()


def forget(vault_root: Path | None = None) -> None:
    """Drop the verdicts kept for one vault, or for every vault; the next request decides them again."""
    with _LOCK:
        if vault_root is None:
            _KEPT.clear()
            return
        for slot in [slot for slot in _KEPT if slot[0] == str(vault_root)]:
            del _KEPT[slot]


def decide(
    root: Path, key: Hashable, paths: list[str],
    decide_many: Callable[[list[str]], tuple[dict[str, bool], frozenset[str]]], who: RequestPrincipal,
) -> dict[str, bool]:
    """Whether each of `paths` is admitted: a kept verdict where its hashes are current, else `decide_many`'s.

    `decide_many` returns its verdicts and the paths whose decision met an input it could not read.
    Call it inside a pinned store request (`egress.release_decision_context`), so the hashes read here
    are those of the bytes that `decide_many` decides. Outside one it decides every path.
    """
    from ..vault_store import read

    if read.request_memo(root) is None:
        return decide_many(paths)[0]
    slot = (str(root), key)
    with _LOCK:
        kept = _KEPT.get(slot)
        known = {} if kept is None else {path: entry for path in paths if (entry := kept.get(path)) is not None}
    # Every path asked and every parent named is read, so a path missing here is one the store does not hold.
    with read.snapshot(root) as view:
        current = view.page_hashes([*paths, *(parent for entry in known.values() for parent, _ in entry[2])])
    answers, missed = {}, []
    for path in paths:
        entry = known.get(path)
        if (entry is not None and current.get(path) == entry[1]
                and all(current.get(parent) == digest for parent, digest in entry[2])):
            answers[path] = entry[0]
        else:
            missed.append(path)
    fresh, unavailable = decide_many(missed) if missed else ({}, frozenset())
    keepable = [path for path in missed if path in current and path not in unavailable]
    found = _dependencies(root, keepable, who) if keepable else {}
    with _LOCK:
        kept = _KEPT.get(slot)
        if kept is None:
            kept = _KEPT[slot] = OrderedDict()
        _KEPT.move_to_end(slot)
        for path in answers:
            if path in kept:
                kept.move_to_end(path)
        for path in missed:
            parents = found.get(path)
            if parents is None:
                kept.pop(path, None)
            else:
                kept[path] = (fresh[path], current[path], parents)
                kept.move_to_end(path)
        while len(kept) > _ENTRIES_MAX:
            kept.popitem(last=False)
        while len(_KEPT) > _KEYS_MAX:
            _KEPT.popitem(last=False)
    answers.update(fresh)
    return {path: answers[path] for path in paths}


def _dependencies(root: Path, paths: list[str], who: RequestPrincipal) -> dict[str, tuple]:
    """The pages of `paths` whose verdict reads only the key, their own bytes and the parents named,
    each with `((parent path, parent hash or None), ...)`. `paths` are pages that the store holds."""
    from ..vault_store import read

    simple = page_admission.proven_simple(root, paths)
    found: dict[str, tuple] = dict.fromkeys(simple, ())
    claims = {}
    for path, parents in _scope_parents(root, [path for path in paths if path not in simple]).items():
        # A RAW release that resolves bytes reads its companion and its original, which no hash here names.
        if raw_protection.marked(path) and raw_protection.permits(root, path, who, resolve_bytes=False) is None:
            continue
        claims[path] = parents
    named = list(dict.fromkeys(parent for parents in claims.values() for parent in parents))
    current: dict[str, str] = {}
    leaves: set[str] = set()
    if named:
        with read.snapshot(root) as view:
            current = view.page_hashes(named)
        present = [parent for parent in named if parent in current]
        leaves.update(page_admission.proven_simple(root, present))
        leaves.update(parent for parent, own in _scope_parents(root, [p for p in present if p not in leaves]).items()
                      if own == ())
    for path, parents in claims.items():
        # A parent's absence is a fact of the store only where the store answers for that path.
        if all(read.serves(root, parent) and (parent not in current or parent in leaves) for parent in parents):
            found[path] = tuple((parent, current.get(parent)) for parent in parents)
    return found


def _scope_parents(root: Path, paths: list[str]) -> dict[str, tuple[str, ...]]:
    """`page_admission.scope_parents` of each Markdown page of `paths` that has them, from its stored bytes."""
    from ..vault_store import ownership, read

    pages = [path for path in paths if ownership.is_markdown(path)]
    if not pages:
        return {}
    with read.snapshot(root) as view:
        bodies = view.bodies(pages)
    found = {}
    for path, raw in bodies.items():
        # Parsed as the stored facts were (`page_admission.derive_files`).
        page = find_corpus.parse_page(root / path, 0, root, content=raw, resolved_relative=path)
        parents = page_admission.scope_parents(path, raw, page)
        if parents is not None:
            found[path] = parents
    return found
