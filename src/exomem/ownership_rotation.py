"""The ownership rotation: which pages were asked, and which pages the next day's slice asks.

`ownership_review` asks a page whether it belongs in its current home. This module decides
when. A page is asked at most once per signal: when it is created in a project, when its
project key or title changes, and when the daily rotation reaches it. It keeps two private
state files beside the due-state projection:

- the asked ledger, `.ownership-asked.jsonl`: one appended line per page asked, which is
  the only record that an item exists. It is keyed by the page's identity (`page_key`), so
  a page that moves or is renamed stays asked and its item reopens at its new home. It is
  append-only so a write adds one line and never rewrites the file, and it is its own
  file because the projection is derived and rebuilt while an asked page is a question
  that was put to the user;
- the rotation file, `.ownership-rotation.json`: the day the rotation last ran, the
  order in which each project's pages are asked, and how many eligible pages were never
  asked. Only the rotation writes it, once a day.

The rotation asks at most `rotation_daily` never-asked pages a day, and takes projects in
turn so that one large project cannot hold the whole slice. Within a project it asks the
page farthest from its project's centre first. The distance is the cosine distance
between the page's mean semantic-unit vector and the mean of the project's other pages.
It orders the rotation and never excludes a page from it: a page the distance ranks low
is still asked in time. A project with fewer than three eligible pages, or without
vectors for them, has no distance, and its pages are asked in authored-date order.

The distances are computed here, off the write path, by one path-filtered paginated read
of the vector sweep per project whose membership changed since the last run. The write
path only appends to the ledger.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .find_types import ParsedPage

log = logging.getLogger(__name__)

ASKED_FILENAME = ".ownership-asked.jsonl"
ROTATION_FILENAME = ".ownership-rotation.json"
ROTATION_VERSION = 1

ENV_ROTATION_DAILY = "EXOMEM_OWNERSHIP_ROTATION_DAILY"
#: Never-asked pages the rotation asks about in one day. PROVISIONAL: a cadence parameter,
#: not vocabulary; `rotation_daily()` reads the owner's setting, and 0 turns the rotation off.
DEFAULT_ROTATION_DAILY = 3

#: A project needs this many pages with vectors before a page has "the other pages" to
#: be far from. PROVISIONAL: a cadence parameter, not vocabulary.
MIN_PAGES_FOR_DISTANCE = 3

#: Pages per vector read, which bounds the unit vectors held in memory at once and the
#: variables in one `IN (...)` query.
_PAGES_PER_VECTOR_READ = 500

_LOCK = threading.Lock()
_ROTATION_LOCK = threading.Lock()


def rotation_daily() -> int:
    """The owner's daily slice, from `EXOMEM_OWNERSHIP_ROTATION_DAILY`; 0 turns it off."""
    raw = os.environ.get(ENV_ROTATION_DAILY)
    if raw is None:
        return DEFAULT_ROTATION_DAILY
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("invalid %s=%r; using %d", ENV_ROTATION_DAILY, raw, DEFAULT_ROTATION_DAILY)
        return DEFAULT_ROTATION_DAILY


# --------------------------------------------------------------------------
# the asked ledger
# --------------------------------------------------------------------------


def asked_path(vault_root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ASKED_FILENAME


def rotation_path(vault_root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ROTATION_FILENAME


def page_key(rel_path: str, frontmatter: Mapping[Any, Any]) -> str:
    """The ledger's identity for a page: its `exomem_id`, else its vault-relative path.

    The one place the identity is read, for the write path, the audit check and the
    rotation alike. The id survives a move or a rename, so a dismissed page that moves to
    another folder is still the page that was asked, and its item reopens there. A page
    without an id can only be told by its path, and re-enters as never-asked when moved.
    An id is a UUID and a path ends in a file extension, so the two cannot collide.
    """
    from . import memory_refs

    return memory_refs.normalize_id(frontmatter.get("exomem_id")) or rel_path


@dataclass(slots=True)
class _Ledger:
    """The ledger as last read: the file it came from, how far, and the pages asked."""

    inode: int
    offset: int
    asked: dict[str, dt.date]


_LEDGERS: dict[str, _Ledger] = {}

#: Most vaults one process keeps in these memos. One process can serve several vaults, and a
#: vault's memo holds its whole ledger or order, so the oldest entry leaves first.
_MEMO_CAP = 8


def _remember(memo: dict[str, Any], key: str, value: Any) -> None:
    """Store `value` under `key`, keeping at most `_MEMO_CAP` entries. Call under `_LOCK`."""
    if key not in memo and len(memo) >= _MEMO_CAP:
        memo.pop(next(iter(memo)))
    memo[key] = value


def asked(vault_root: Path) -> Mapping[str, dt.date]:
    """The pages that were asked, by `page_key`, each with the day it was first asked.

    Read incrementally: the file only ever grows, so a later call parses the lines that
    were appended since the last one. The returned mapping is never changed afterwards;
    an append yields a new one. A missing or unreadable ledger reads as nothing asked,
    which costs an advisory and never a write.
    """
    path = asked_path(vault_root)
    key = str(path)
    try:
        info = path.stat()
    except OSError:
        with _LOCK:
            _LEDGERS.pop(key, None)
        return {}
    with _LOCK:
        ledger = _LEDGERS.get(key)
        if ledger is None or ledger.inode != info.st_ino or info.st_size < ledger.offset:
            ledger = _Ledger(info.st_ino, 0, {})
        if info.st_size == ledger.offset:
            _remember(_LEDGERS, key, ledger)
            return ledger.asked
        try:
            with path.open("rb") as handle:
                handle.seek(ledger.offset)
                data = handle.read(info.st_size - ledger.offset)
        except OSError:
            return ledger.asked
        # Only whole lines count: a torn tail is read again when its newline lands.
        whole = data[: data.rfind(b"\n") + 1]
        merged = dict(ledger.asked)
        for line in whole.splitlines():
            try:
                row = json.loads(line)
                day = dt.date.fromisoformat(row["on"])
                merged.setdefault(str(row["key"]), day)
            except (ValueError, KeyError, TypeError):
                continue
        ledger = _Ledger(info.st_ino, ledger.offset + len(whole), merged)
        _remember(_LEDGERS, key, ledger)
        return ledger.asked


def mark_asked(vault_root: Path, keys: Iterable[str], *, on: dt.date) -> list[str]:
    """Record that these pages (by `page_key`) were asked, and return the ones not asked before.

    One append of whole lines. Not synced to disk: a lost tail only means the rotation
    asks that page again, which is the cheap direction to be wrong in.
    """
    current = asked(vault_root)
    fresh = [key for key in dict.fromkeys(str(k) for k in keys) if key and key not in current]
    if not fresh:
        return []
    from . import state_paths

    state_paths.ensure_vault_state_dir(vault_root)
    lines = "".join(
        json.dumps({"key": key, "on": on.isoformat()}, ensure_ascii=False, separators=(",", ":"))
        + "\n"
        for key in fresh
    )
    with _LOCK, asked_path(vault_root).open("ab") as handle:
        handle.write(lines.encode("utf-8"))
        handle.flush()
    return fresh


# --------------------------------------------------------------------------
# the rotation state
# --------------------------------------------------------------------------

_STATE_MEMO: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def state(vault_root: Path) -> dict[str, Any]:
    """The rotation file as last written, or an empty state. Parsed once per change."""
    path = rotation_path(vault_root)
    key = str(path)
    try:
        info = path.stat()
        token = (info.st_mtime_ns, info.st_size)
    except OSError:
        return {}
    with _LOCK:
        hit = _STATE_MEMO.get(key)
    if hit is not None and hit[0] == token:
        return hit[1]
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != ROTATION_VERSION:
        return {}
    with _LOCK:
        _remember(_STATE_MEMO, key, (token, payload))
    return payload


def _save_state(vault_root: Path, payload: dict[str, Any]) -> None:
    """Replace the rotation file atomically, with the writer the due-state files use."""
    from . import due_state, state_paths

    state_paths.ensure_vault_state_dir(vault_root)
    due_state._replace_json(rotation_path(vault_root), payload)


def is_due(vault_root: Path, today: dt.date) -> bool:
    """Whether the rotation has work today: it is on, and it has not run today."""
    return rotation_daily() > 0 and rotated_on(vault_root) != today


def rotated_on(vault_root: Path) -> dt.date | None:
    """The day the rotation last ran, or None when it never has."""
    try:
        return dt.date.fromisoformat(str(state(vault_root).get("rotated_on")))
    except ValueError:
        return None


def never_asked_report(vault_root: Path) -> dict[str, Any] | None:
    """How many eligible pages were never asked, and the day that was counted. None before any run.

    A count taken at the last rotation, not a live one: pages written or retired since are
    not in it, so it always carries its day.
    """
    payload = state(vault_root)
    count = payload.get("never_asked")
    counted_on = payload.get("counted_on")
    if type(count) is not int or not isinstance(counted_on, str):
        return None
    return {"count": count, "as_of": counted_on}


# --------------------------------------------------------------------------
# ordering a project
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Member:
    """One eligible page of a project, with the date that orders a project without a distance.

    `path` locates the page's vectors; `key` is the page's identity in the ledger and in
    the stored order, so a page that moves within its project keeps its place.
    """

    path: str
    authored: dt.date | None
    key: str


def membership_token(members: Iterable[Member]) -> str:
    """The identity of a project's membership: changes when a page joins or leaves."""
    encoded = "\0".join(sorted(member.key for member in members)).encode("utf-8", "surrogatepass")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _by_authored(members: Iterable[Member]) -> list[str]:
    ordered = sorted(members, key=lambda m: (m.authored or dt.date.max, m.path))
    return [member.key for member in ordered]


def order_project(members: list[Member], means: Mapping[str, np.ndarray]) -> list[str]:
    """The project's pages, by `page_key`, in the order they are asked.

    Farthest first, where a page's distance is 1 minus the cosine between its mean vector
    and the sum of the project's other pages' mean vectors (the sum has the mean's
    direction, and cosine ignores length). A page without a vector follows the pages with
    one, in authored-date order, so no eligible page is ever left out.
    """
    with_vector = [member for member in members if member.path in means]
    if min(len(members), len(with_vector)) < MIN_PAGES_FOR_DISTANCE:
        return _by_authored(members)
    matrix = np.stack([means[member.path] for member in with_vector])
    others = matrix.sum(axis=0) - matrix
    scale = np.linalg.norm(matrix, axis=1) * np.linalg.norm(others, axis=1)
    cosine = np.divide(
        (matrix * others).sum(axis=1), scale, out=np.zeros(len(with_vector)), where=scale > 0
    )
    distance = 1.0 - cosine
    far_first = sorted(range(len(with_vector)), key=lambda i: (-distance[i], with_vector[i].path))
    ordered = [with_vector[i].key for i in far_first]
    ordered += _by_authored(member for member in members if member.path not in means)
    return ordered


def page_means(vault_root: Path, paths: list[str]) -> dict[str, np.ndarray]:
    """Each page's mean unit vector, read in pages of the vector sweep. Empty when unreadable.

    Reads only these pages' rows, `_PAGES_PER_VECTOR_READ` at a time, so a project is read
    whole without holding the corpus's vectors. Rebuildable derived data: a failed read
    leaves the project without a distance, never without a rotation.
    """
    from . import embeddings

    means: dict[str, np.ndarray] = {}
    try:
        index = embeddings.get_embedding_index(vault_root)
        for start in range(0, len(paths), _PAGES_PER_VECTOR_READ):
            grouped = index.all_semantic_unit_vectors(
                paths=paths[start : start + _PAGES_PER_VECTOR_READ]
            )
            for path, rows in grouped.items():
                matrix = np.stack([row.vector for row in rows]).astype(np.float64)
                norms = np.linalg.norm(matrix, axis=1)
                matrix = matrix[norms > 0] / norms[norms > 0, None]
                if len(matrix):
                    means[path] = matrix.mean(axis=0)
    except Exception:  # noqa: BLE001 - a missing or mismatched vector space is not a failure
        log.debug("ownership rotation could not read page vectors", exc_info=True)
        return {}
    return means


# --------------------------------------------------------------------------
# the daily rotation
# --------------------------------------------------------------------------


def eligible_members(vault_root: Path, pages: Iterable[ParsedPage]) -> dict[str, list[Member]]:
    """The eligible pages of each project, from the corpus the caller parsed."""
    from . import lifecycle_statuses, note_types, ownership_review, temporal

    type_basis = note_types.Basis(vault_root)
    status_basis = lifecycle_statuses.Basis(vault_root)
    projects: dict[str, list[Member]] = {}
    for page in pages:
        subject = ownership_review.subject_of(
            vault_root,
            page.rel_path,
            page_type=page.page_type,
            frontmatter=page.frontmatter,
            title=page.title,
            type_basis=type_basis,
            status_basis=status_basis,
        )
        if subject is None:
            continue
        fm = page.frontmatter
        authored = temporal.parse(fm.get("updated") or fm.get("created"))
        projects.setdefault(subject.project, []).append(
            Member(
                page.rel_path,
                authored.day if authored is not None else None,
                page_key(page.rel_path, fm),
            )
        )
    return projects


def _take_in_turn(
    orders: Mapping[str, list[str]],
    already: Mapping[str, Any],
    *,
    limit: int,
    after: str | None,
) -> tuple[list[str], str | None]:
    """Up to `limit` never-asked pages, one project at a time in a ring, and where to resume.

    The ring starts after the project that supplied the last page of the previous run, so a
    vault with more projects than the daily slice still reaches every project.
    """
    keys = sorted(orders)
    if after in keys:
        cut = keys.index(after) + 1
        keys = keys[cut:] + keys[:cut]
    queues = {key: (page for page in orders[key] if page not in already) for key in keys}
    picked: list[str] = []
    last: str | None = None
    live = list(keys)
    while live and len(picked) < limit:
        remaining = []
        for key in live:
            page = next(queues[key], None)
            if page is None:
                continue
            picked.append(page)
            last = key
            remaining.append(key)
            if len(picked) == limit:
                break
        live = remaining
    return picked, last


def rotate(
    vault_root: Path,
    pages: Iterable[ParsedPage],
    *,
    today: dt.date,
    limit: int | None = None,
    read_means: Callable[[Path, list[str]], Mapping[str, np.ndarray]] | None = None,
) -> list[str]:
    """Ask about today's never-asked pages once per day, and return the `page_key`s newly asked.

    Idempotent within a day. Refreshes the order of only those projects whose membership
    changed since the last run, then takes the slice in turn. The caller opens the returned
    pages' items in the projection.
    """
    # The day's guard (`rotated_on`) is read at the top and written at the end, so two
    # overlapping runs, as a read and a rebuild start together on a cold vault, would each
    # take a full slice. The second waits here, then finds the day taken.
    with _ROTATION_LOCK:
        return _rotate_locked(
            vault_root, pages, today=today, limit=limit, read_means=read_means
        )


def _rotate_locked(
    vault_root: Path,
    pages: Iterable[ParsedPage],
    *,
    today: dt.date,
    limit: int | None,
    read_means: Callable[[Path, list[str]], Mapping[str, np.ndarray]] | None,
) -> list[str]:
    limit = rotation_daily() if limit is None else limit
    previous = state(vault_root)
    if limit <= 0 or previous.get("rotated_on") == today.isoformat():
        return []
    read_means = read_means or page_means
    members = eligible_members(vault_root, pages)
    kept = previous.get("projects")
    if not isinstance(kept, dict):
        kept = {}
    projects: dict[str, dict[str, Any]] = {}
    for project, group in members.items():
        token = membership_token(group)
        before = kept.get(project)
        if isinstance(before, dict) and before.get("token") == token:
            projects[project] = before
            continue
        means = read_means(vault_root, [member.path for member in group])
        projects[project] = {"token": token, "order": order_project(group, means)}
    orders = {project: row["order"] for project, row in projects.items()}
    already = asked(vault_root)
    picked, last = _take_in_turn(
        orders, already, limit=limit, after=previous.get("next_project")
    )
    # The day guard is saved before the ledger append. When the save fails nothing was asked, so
    # the next serve tries again; when the append fails after it, the day asks too few pages. A
    # failure either way never asks more than `rotation_daily` in a day.
    chosen = set(picked)
    remaining = sum(
        1 for order in orders.values() for key in order if key not in already and key not in chosen
    )
    _save_state(
        vault_root,
        {
            "version": ROTATION_VERSION,
            "rotated_on": today.isoformat(),
            "counted_on": today.isoformat(),
            "never_asked": remaining,
            "next_project": last,
            "projects": projects,
        },
    )
    newly = mark_asked(vault_root, picked, on=today)
    return newly
