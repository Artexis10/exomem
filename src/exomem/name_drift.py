"""Name drift: a page whose file name is not the one a relocation would give it today.

Until 0.186.0 a new page took a slug name; since then a vault's `filename_style` decides, and
the default is the title. A retitle never renamed a page either. Both leave pages whose file
name no longer follows their title. This module finds them and proposes the renames:

- the rule: a page drifts when a rename-only `curation.relocate` would move it. This module asks
  `relocate.planned_path`, the rule that relocation derives its moves with, so a renamed page
  stops drifting and the backlog closes by construction. Scope, title, date prefix, case and
  collision numbers are all decided there;
- a page with no usable title, a page whose rename this caller cannot plan, and a page whose
  rename the item's call was refused for, are counted apart and never proposed. A refusal is
  remembered beside the due-state projection, for the callers who see what its caller saw,
  until the page or a page linking to it changes;
- the one review item for the vault, anchored on the top index so that its own renames never
  move its identity, with the call that renames the next batch;
- the bounded listing and the retitle suggestion.

It measures and proposes. It never renames: `curation.relocate` does, on the owner's yes.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import hashlib
import json
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from . import note_types

if TYPE_CHECKING:
    from .audit import AuditFinding
    from .relocate import Names

CATEGORY = "name_drift"

#: The `structure_suggestion` kind that a retitling write carries.
KIND = "rename"

#: How many pages one proposed rename batch holds. PROVISIONAL: a cadence parameter, not
#: vocabulary. It is the operator's preview size; `curation.relocate` halves a batch it cannot seal.
BATCH_PAGES = 200

#: The file in the vault's state directory, beside the due-state projection, that remembers the
#: pages a rename of the item's batch was refused for. A reconcile and a row refresh rewrite only
#: the projection, so the memory outlives both. Losing the file costs one refused preparation per
#: page, which records the refusal again.
REFUSALS_FILENAME = "name-drift-refusals.json"
#: How many readers keep their refusals; the least recently recorded is dropped. A configuration
#: edit gives every reader a new key (`relocate.reader_key`) and orphans the old one, so without a
#: bound the file grows with each edit. A dropped reader's pages cost one refused preparation each.
REFUSAL_READERS = 16
_REFUSALS_LOCK = threading.Lock()

#: Closed verdicts of `judge`.
IN_STEP = "in_step"
DRIFTED = "drifted"
NO_USABLE_TITLE = "no_usable_title"
REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class Page:
    """One judged page. `planned` is the file name a rename-only relocation gives it now."""

    path: str
    verdict: str
    title: str = ""
    current: str = ""
    planned: str = ""
    #: Why the page's rename cannot be planned or was refused, for a `refused` page.
    reason: str = ""
    #: The refusal's code, for a `refused` page.
    code: str = ""


@dataclass(frozen=True, slots=True)
class Scan:
    """The judged pages of one corpus, each tuple sorted by path."""

    drifted: tuple[Page, ...]
    untitled: tuple[Page, ...]
    refused: tuple[Page, ...]


def anchor_path() -> str:
    """Where the vault's one item is anchored: the top index, as the index's own count check is.

    The item stands for the whole vault. An anchor on a drifted page would move with that page's
    own rename and take the item's identity with it. The top index is a page, so the item can be
    inspected, and no rename moves it (`relocate.renamed_by_title` leaves navigation pages out).
    """
    from .kbdir import managed_join

    return managed_join("index.md")


def judge(
    vault_root: Path,
    rel_path: str,
    *,
    note_type: note_types.NoteType | None,
    title: str | None,
    names: Names,
) -> Page | None:
    """The page's verdict, or None when no relocation names it by its title."""
    from . import relocate
    from .cli_ops import OpError

    if not relocate.renamed_by_title(rel_path, note_type):
        return None
    current = PurePosixPath(rel_path).name
    shown = title or ""
    try:
        if relocate.title_stem(vault_root, rel_path, note_type, title) is None:
            return Page(rel_path, NO_USABLE_TITLE, shown, current)
        target = relocate.planned_path(vault_root, rel_path, note_type, title, names)
    except (OpError, ValueError) as error:
        return Page(
            rel_path, REFUSED, shown, current, reason=str(error), code=relocate.NO_DESTINATION
        )
    if target is None:
        return Page(rel_path, IN_STEP, shown, current)
    return Page(rel_path, DRIFTED, shown, current, PurePosixPath(target).name)


def scan(vault_root: Path, pages: Iterable[Any]) -> Scan:
    """Judge every page of a corpus, in path order, as one relocation of them all would.

    `pages` are parsed pages with `rel_path`, `frontmatter`, `body` and `snapshot_hash`. The loop
    checks the request's read budget on every page, so a scan that cannot finish raises the budget
    refusal instead of answering with a partial count. A drifted page whose rename the item's call
    was refused for, and which has not changed since, counts as refused (`record_refused`); the
    scan reads that memory and prepares nothing.
    """
    from . import find, foreground_priority, relocate, request_budget, vault

    root = Path(vault_root)
    basis = note_types.Basis(root)
    names = relocate.Names(root, resolver=lambda: find.writer_resolver_snapshot(root))
    ordered = sorted(pages, key=lambda page: page.rel_path)
    remembered = _refusals(root)
    hashes = {page.rel_path: page.snapshot_hash for page in ordered} if remembered else {}
    drifted: list[Page] = []
    untitled: list[Page] = []
    refused: list[Page] = []
    for page in foreground_priority.yielding_in_bulk(ordered):
        request_budget.checkpoint()
        verdict = judge(
            root,
            page.rel_path,
            note_type=basis.resolve(page.frontmatter.get("type")).note_type,
            title=vault.authored_title(page.frontmatter, page.body),
            names=names,
        )
        if verdict is None or verdict.verdict == IN_STEP:
            continue
        if verdict.verdict == DRIFTED:
            held = _held(remembered.get(page.rel_path), page.rel_path, hashes)
            if held is None:
                drifted.append(verdict)
            else:
                # The batch leaves a held page where it is, so a later page may take its name.
                planned = PurePosixPath(page.rel_path).with_name(verdict.planned).as_posix()
                names.claimed.discard(vault.vault_name_key(planned))
                held_code = str(held["code"])
                refused.append(dataclasses.replace(verdict, verdict=REFUSED, code=held_code))
        elif verdict.verdict == NO_USABLE_TITLE:
            untitled.append(verdict)
        else:
            refused.append(verdict)
    return Scan(tuple(drifted), tuple(untitled), tuple(refused))


def _refusals_path(vault_root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / REFUSALS_FILENAME


def _refusals(vault_root: Path) -> dict[str, dict[str, Any]]:
    """The refusals remembered for this caller (`relocate.reader_key`), by page path.

    None are read when the file is absent or cannot be read, or the caller's key cannot be.
    """
    from . import relocate

    reader = relocate.reader_key(vault_root)
    pages = _stored_refusals(vault_root).get(reader) if reader is not None else None
    return dict(pages) if pages else {}


def _stored_refusals(vault_root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Every remembered refusal, by reader and then page path."""
    try:
        payload = json.loads(_refusals_path(vault_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    readers = payload.get("readers") if isinstance(payload, dict) else None
    if not isinstance(readers, dict):
        return {}
    return {
        str(reader): {
            str(path): entry for path, entry in pages.items() if isinstance(entry, dict)
        }
        for reader, pages in readers.items()
        if isinstance(pages, dict)
    }


def _held(
    entry: dict[str, Any] | None, path: str, hashes: Mapping[str, str | None]
) -> dict[str, Any] | None:
    """The remembered refusal of `path` while it still applies, else None.

    It applies while the page has the bytes it was refused with, and each recorded referrer has
    its bytes then too. The entry is this caller's and records only referrers that its scan read
    (`record_refused`), so a recorded referrer that this scan does not hold was deleted or moved:
    a change, which lets the page back in.
    """
    if entry is None or not entry.get("hash") or entry.get("hash") != hashes.get(path):
        return None
    referrers = entry.get("referrers")
    if not isinstance(referrers, dict):
        return None
    if any(hashes.get(rel) != held for rel, held in referrers.items()):
        return None
    return entry


def record_refused(
    vault_root: Path, review_ref: str, refused: Sequence[Mapping[str, str]]
) -> bool:
    """Remember the pages a rename of this item's batch was refused for; whether the ref is ours.

    Each row names a `page` and its refusal `code`. The memory binds the page's exact bytes and
    those of each page that links to it, so the scan proposes the page again once it or one of
    those referrers changes. Both hashes come from the corpus read that the scan compares them
    with (`_corpus`). A linking page outside it, such as one outside the managed root, is not
    bound, because the scan could never tell its deletion from its absence. The memory is kept
    under the caller's key (`relocate.reader_key`), and only a scan under the same key reads it;
    a caller without a key records nothing. It keeps no reason text: a reason can name pages that
    another audience may not see. The row is refreshed, so the item closes when every remaining
    drifted page is refused.
    """
    from . import due_state, relocate, state_paths, vault
    from .governance import egress

    if not _owns(vault_root, review_ref):
        return False
    root = Path(vault_root)
    reader = relocate.reader_key(root)
    if reader is None:
        return True
    paths = sorted({str(row["page"]) for row in refused})
    inbound = vault.find_inbound_wikilinks_many(
        root, paths, visible=egress.visible_page_filter(root)
    )
    wanted = {*paths, *(link.path for links in inbound.values() for link in links)}
    hashes = {
        page.rel_path: page.snapshot_hash
        for page in _corpus(root, select=lambda rel, _content: rel in wanted)
        if page.snapshot_hash
    }
    entries: dict[str, dict[str, Any]] = {}
    for row in refused:
        path = str(row["page"])
        if path not in hashes:
            continue
        referrers = {
            link.path: hashes[link.path]
            for link in inbound.get(path, ())
            if link.path != path and link.path in hashes
        }
        entries[path] = {"code": str(row["code"]), "hash": hashes[path], "referrers": referrers}
    if not entries:
        return True
    with _REFUSALS_LOCK:
        readers = _stored_refusals(root)
        # The reader recorded last goes last, so the bound drops the least recently recorded.
        pages = {**readers.pop(reader, {}), **entries}
        kept = [*readers.items(), (reader, pages)][-REFUSAL_READERS:]
        state_paths.ensure_vault_state_dir(root)
        # The projection's own atomic replace: a reader never sees half a file.
        due_state._replace_json(  # noqa: SLF001
            _refusals_path(root), {"version": 1, "readers": dict(kept)}
        )
    due_state.schedule_name_drift_refresh(root)
    return True


def _corpus(
    vault_root: Path, *, select: Callable[[str, bytes | None], bool] | None = None
) -> list[Any]:
    """The admitted corpus that a scan reads, through the audit's own cached corpus read.

    `select` keeps only the vault-relative paths it takes, admitted as the whole read admits them.
    """
    from . import audit as audit_module
    from .vault import kb_root

    return audit_module._parse_all(kb_root(vault_root), vault_root, select=select)  # noqa: SLF001 -- the audit's own corpus read


def _corpus_scan(vault_root: Path) -> Scan:
    """One scan of the admitted corpus."""
    return scan(vault_root, _corpus(vault_root))


def pages_for_review(vault_root: Path) -> list[str]:
    """The next batch's paths, read now: what a relocation by this family's `review_ref` renames.

    The scan is fresh, so a batch already renamed is never proposed again even before the
    review row is rebuilt. A stopped scan raises the budget refusal: an unknown batch is never
    empty.
    """
    return [page.path for page in _corpus_scan(vault_root).drifted[:BATCH_PAGES]]


def _reading(meta: dict[str, Any]) -> AuditFinding:
    from .audit import AuditFinding

    return AuditFinding(
        category=CATEGORY,
        severity="info",
        path=anchor_path(),
        detail="",
        meta={
            "review_partition": CATEGORY,
            # No authored date: the drift is due from the start, and the placeholder sorts it
            # last among its own rows.
            "due_since": dt.date.min.isoformat(),
            **meta,
        },
    )


def item_ref(vault_root: Path) -> str | None:
    """This vault's one name-drift review ref, composed as the projection composes every item's.

    None while the vault has no top index: the due-state block serves no row whose page is
    missing (`due_state._page_exists`), so an item there would carry a ref that no listing shows.
    """
    from . import due_state

    if not due_state._page_exists(Path(vault_root), anchor_path()):  # noqa: SLF001 -- its drop rule
        return None
    entry = due_state.entry_for(vault_root, _reading({}))
    return str(entry["ref"]) if entry is not None else None


def _owns(vault_root: Path, review_ref: str) -> bool:
    """Whether `review_ref` names this vault's name-drift item for this caller.

    The item's identity is its anchor and family, so the ref is recognised without the
    whole-vault review passes. A caller that may not see the anchor is answered no, as for a ref
    that names no item.
    """
    from . import review_state
    from .governance import egress

    try:
        wanted = review_state.parse_review_ref(review_ref)
        own = item_ref(vault_root)
    except ValueError:
        return False
    if own is None or review_state.parse_review_ref(own) != wanted:
        return False
    visible = egress.visible_page_filter(Path(vault_root))
    return visible is None or visible(anchor_path())


def pages_for_ref(vault_root: Path, review_ref: str) -> list[str] | None:
    """The next batch when `review_ref` is this vault's name-drift item, else None."""
    return pages_for_review(vault_root) if _owns(vault_root, review_ref) else None


def drift_signal(drift: Scan) -> str:
    """A short identity of the drifted set and its planned names: it changes when either does."""
    digest = hashlib.sha256()
    for page in drift.drifted:
        digest.update(f"{page.path}\0{page.planned}\n".encode())
    return digest.hexdigest()[:16]


def relocate_call(**arguments: Any) -> str:
    """The `curation.relocate` call, spelled for the serving surface."""
    from . import capabilities

    return capabilities.active_call_text(
        "maintain_memory", {"mode": "curation", "curation_action": "relocate", **arguments}
    )


def _refused_codes(drift: Scan) -> dict[str, int]:
    """How many refused pages each refusal code holds, by code."""
    return dict(sorted(collections.Counter(page.code for page in drift.refused).items()))


def _summary(drifted: int, untitled: int, refused: int, codes: Mapping[str, int]) -> str:
    if drifted == 1:
        sentence = "1 page has a file name that no longer follows its title."
    else:
        sentence = f"{drifted} pages have file names that no longer follow their titles."
    if untitled:
        sentence += (
            " 1 more has no usable title and is left as it is."
            if untitled == 1
            else f" {untitled} more have no usable title and are left as they are."
        )
    if refused:
        by_code = ", ".join(f"{code} {count}" for code, count in codes.items())
        sentence += (
            f" 1 more cannot be renamed now and is left as it is ({by_code})."
            if refused == 1
            else f" {refused} more cannot be renamed now and are left as they are ({by_code})."
        )
    return sentence


#: The meta fields a finding is built from, which the due-state row stores (`stored_finding`).
#: The scan time is `scanned_at`, not `as_of`: a listing's `as_of` names the held review pass it
#: read, and an item must not carry that name.
# nosemgrep: ep-word-set -- the meta fields that `finding` writes, not a vocabulary.
_STORED = ("signal_version", "drifted", "untitled", "refused", "refused_codes", "scanned_at")


def _finding_from(vault_root: Path, fields: dict[str, Any], ref: str) -> AuditFinding:
    """The review finding for these stored fields, with the item's call spelled now.

    The counts are those of the scan made at `scanned_at`, which the text states: a finding rebuilt
    from the stored row is as old as that row.
    """
    reading = _reading({**fields, "batch_pages": BATCH_PAGES})
    call = relocate_call(review_ref=ref)
    assert reading.meta is not None
    reading.meta["review_ref"], reading.meta["call"] = ref, call
    drifted = int(fields["drifted"])
    summary = _summary(
        drifted,
        int(fields.get("untitled") or 0),
        int(fields.get("refused") or 0),
        fields.get("refused_codes") or {},
    )
    as_of = f"As of {fields['scanned_at']}, " if fields.get("scanned_at") else ""
    reading.detail = (
        f"{as_of}{summary} Propose the next batch of up to {min(BATCH_PAGES, drifted)} to the "
        f"user as one preview, then apply it on their yes: {call}"
    )
    return reading


def finding(vault_root: Path, drift: Scan) -> AuditFinding | None:
    """The one review finding for the vault, or None while no page drifts or no top index exists.

    Its `signal_version` changes with the drifted set, so a decision binds to the set that was
    shown. Its text carries the call that renames the next batch, with the item's own reference.
    The pages left as they are count apart, so the item closes when every remaining drifted page
    is refused.
    """
    ref = item_ref(vault_root) if drift.drifted else None
    if ref is None:
        return None
    return _finding_from(
        vault_root,
        {
            "signal_version": drift_signal(drift),
            "drifted": len(drift.drifted),
            "untitled": len(drift.untitled),
            "refused": len(drift.refused),
            "refused_codes": _refused_codes(drift),
            "scanned_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
        },
        ref,
    )


def stored_finding(vault_root: Path, meta: dict[str, Any]) -> AuditFinding | None:
    """The finding rebuilt from a stored row's meta, with no scan; None when it holds no drift."""
    ref = item_ref(vault_root) if meta.get("drifted") else None
    if ref is None:
        return None
    return _finding_from(vault_root, {key: meta.get(key) for key in _STORED}, ref)


def suggestion_after_write(
    vault_root: Path, state: Any, *, previous: Any = None
) -> dict[str, Any] | None:
    """The `structure_suggestion` of a write that retitled a page so its name drifts, else None.

    Only a changed title asks. The plan is made from the new title in memory with the shared
    rule, which lists one folder and asks the resident title index: the write path scans
    nothing. The user's own edit is the approval, so the suggestion carries the call and no
    question. May raise; the write path treats a raise as "suggest nothing".
    """
    from . import envelope, relocate, review_state, vault

    if previous is None:
        return None
    title = vault.authored_title(state.frontmatter, state.body)
    if title == vault.authored_title(previous.frontmatter, previous.body):
        return None
    if envelope.active().get("structural_suggestions") == "off":
        return None
    basis = state.type_basis or note_types.Basis(vault_root)
    verdict = judge(
        vault_root,
        state.path,
        note_type=basis.resolve(state.page_type).note_type,
        title=title,
        names=relocate.Names(vault_root),
    )
    if verdict is None or verdict.verdict != DRIFTED:
        return None
    ref = review_state.refs_for_paths(vault_root, [state.path])[state.path]
    return {
        "kind": KIND,
        "ref": ref,
        "current_name": verdict.current,
        "planned_name": verdict.planned,
        "call": relocate_call(pages=[ref]),
        "outcome": (
            f"The title changed, so the file name {verdict.current!r} no longer follows it. "
            f"Rename it to {verdict.planned!r}; the user's edit is the approval."
        ),
    }


#: Rows a listing page returns at most. A bound on the answer, not on the scan.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


def listing(
    vault_root: Path, *, cursor: str | None = None, limit: int = DEFAULT_PAGE_SIZE
) -> dict[str, Any]:
    """One page of the pages whose names follow no title, for the read operation.

    The scan is one pass over the admitted corpus through the cached frontmatter reads, never
    a query per page. When the budget stops that pass, the answer says the scan is incomplete and
    gives no total: an unknown count is never 0. Rows are drifted pages and the pages left as
    they are, in path order; `cursor` is the last path of the previous page.
    """
    from . import request_budget, review_state

    limit = max(1, min(limit, MAX_PAGE_SIZE))
    try:
        drift = _corpus_scan(vault_root)
    except request_budget.ReadBudgetExhausted:
        return {
            "outcome": (
                "The scan stopped before it finished, so the number of pages with stale names"
                " is unknown. Ask again."
            ),
            "complete": False,
            "total_drifted": None,
            "no_usable_title": None,
            "refused": None,
            "refused_codes": None,
            "pages": [],
        }
    rows = sorted([*drift.drifted, *drift.untitled, *drift.refused], key=lambda page: page.path)
    if cursor is not None:
        rows = [page for page in rows if page.path > cursor]
    shown = rows[:limit]
    refs = review_state.refs_for_paths(vault_root, [page.path for page in shown]) if shown else {}
    found = finding(vault_root, drift)
    codes = _refused_codes(drift)
    result: dict[str, Any] = {
        "outcome": _summary(len(drift.drifted), len(drift.untitled), len(drift.refused), codes)
        if drift.drifted or drift.untitled or drift.refused
        else "Every page's file name follows its title.",
        "complete": True,
        "total_drifted": len(drift.drifted),
        "no_usable_title": len(drift.untitled),
        "refused": len(drift.refused),
        "refused_codes": codes,
        "pages": [
            {
                "ref": refs[page.path],
                "state": page.verdict,
                "title": page.title,
                "current_name": page.current,
                **({"planned_name": page.planned} if page.verdict == DRIFTED else {}),
                **({"code": page.code} if page.code else {}),
                **({"reason": page.reason} if page.reason else {}),
            }
            for page in shown
        ],
    }
    if found is not None:
        result["rename_next_batch"] = found.meta["call"]
    if len(rows) > len(shown):
        result["next_cursor"] = shown[-1].path
    return result
