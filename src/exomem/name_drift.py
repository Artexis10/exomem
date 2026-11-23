"""Name drift: a page whose file name is not the one a relocation would give it today.

Until 0.186.0 a new page took a slug name; since then a vault's `filename_style` decides, and
the default is the title. A retitle never renamed a page either. Both leave pages whose file
name no longer follows their title. This module finds them and proposes the renames:

- the rule: a page drifts when a rename-only `curation.relocate` would move it. This module asks
  `relocate.planned_path`, the rule that relocation derives its moves with, so a renamed page
  stops drifting and the backlog closes by construction. Scope, title, date prefix, case and
  collision numbers are all decided there;
- a page with no usable title, and a page whose rename this caller cannot plan, are counted
  apart and never proposed;
- the one review item for the vault, anchored on the top index so that its own renames never
  move its identity, with the call that renames the next batch;
- the bounded listing and the retitle suggestion.

It measures and proposes. It never renames: `curation.relocate` does, on the owner's yes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable
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
    #: Why the page's rename cannot be planned, for a `refused` page.
    reason: str = ""


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
    inspected, and no rename moves it (`judge` leaves navigation pages out).
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
    """The page's verdict, or None when no relocation names it by its title.

    A navigation page keeps its name whatever its type: links and this family's anchor find it by
    that name.
    """
    from . import relocate
    from .cli_ops import OpError
    from .find_corpus import NAVIGATION_BASENAMES

    if note_type is None or not relocate.renamed_by_title(note_type):
        return None
    if PurePosixPath(rel_path).name.lower() in NAVIGATION_BASENAMES:
        return None
    current = PurePosixPath(rel_path).name
    shown = title or ""
    try:
        if relocate.title_stem(vault_root, rel_path, note_type, title) is None:
            return Page(rel_path, NO_USABLE_TITLE, shown, current)
        target = relocate.planned_path(vault_root, rel_path, note_type, title, names)
    except (OpError, ValueError) as error:
        return Page(rel_path, REFUSED, shown, current, reason=str(error))
    if target is None:
        return Page(rel_path, IN_STEP, shown, current)
    return Page(rel_path, DRIFTED, shown, current, PurePosixPath(target).name)


def scan(vault_root: Path, pages: Iterable[Any]) -> Scan:
    """Judge every page of a corpus, in path order, as one relocation of them all would.

    `pages` are parsed pages with `rel_path`, `frontmatter` and `body`. The loop checks the
    request's read budget on every page, so a scan that cannot finish raises the budget refusal
    instead of answering with a partial count.
    """
    from . import find, relocate, request_budget, vault

    root = Path(vault_root)
    basis = note_types.Basis(root)
    names = relocate.Names(root, resolver=lambda: find.writer_resolver_snapshot(root))
    drifted: list[Page] = []
    untitled: list[Page] = []
    refused: list[Page] = []
    for page in sorted(pages, key=lambda page: page.rel_path):
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
            drifted.append(verdict)
        elif verdict.verdict == NO_USABLE_TITLE:
            untitled.append(verdict)
        else:
            refused.append(verdict)
    return Scan(tuple(drifted), tuple(untitled), tuple(refused))


def _corpus_scan(vault_root: Path) -> Scan:
    """One scan of the admitted corpus, through the audit's own cached corpus read."""
    from . import audit as audit_module
    from .vault import kb_root

    pages = audit_module._parse_all(kb_root(vault_root), vault_root)  # noqa: SLF001 -- the audit's own corpus read
    return scan(vault_root, pages)


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
    """This vault's one name-drift review ref, composed as the projection composes every item's."""
    from . import due_state

    entry = due_state.entry_for(vault_root, _reading({}))
    return str(entry["ref"]) if entry is not None else None


def pages_for_ref(vault_root: Path, review_ref: str) -> list[str] | None:
    """The next batch when `review_ref` is this vault's name-drift item, else None.

    One scan: the item's identity is its anchor and family, so the ref is recognised without the
    whole-vault review passes. A caller that may not see the anchor gets None, as for a ref
    that names no item.
    """
    from . import review_state
    from .governance import egress

    try:
        wanted = review_state.parse_review_ref(review_ref)
        own = item_ref(vault_root)
    except ValueError:
        return None
    if own is None or review_state.parse_review_ref(own) != wanted:
        return None
    visible = egress.visible_page_filter(Path(vault_root))
    if visible is not None and not visible(anchor_path()):
        return None
    return pages_for_review(vault_root)


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


def _summary(drifted: int, untitled: int, refused: int) -> str:
    noun = "page has a file name" if drifted == 1 else "pages have file names"
    sentence = f"{drifted} {noun} that no longer follow their titles."
    if untitled:
        sentence += f" {untitled} more have no usable title and are left as they are."
    if refused:
        sentence += f" {refused} more cannot be renamed by this caller and are left as they are."
    return sentence


#: The counts a finding is built from, which the due-state row stores (`stored_finding`).
# nosemgrep: ep-word-set -- the meta fields that `finding` writes, not a vocabulary.
_COUNTS = ("signal_version", "drifted", "untitled", "refused")


def _finding_from(vault_root: Path, counts: dict[str, Any]) -> AuditFinding:
    """The review finding for these counts, with the item's reference and its call spelled now."""
    reading = _reading({**counts, "batch_pages": BATCH_PAGES})
    ref = item_ref(vault_root)
    call = relocate_call(review_ref=ref) if ref else relocate_call()
    assert reading.meta is not None
    reading.meta["review_ref"], reading.meta["call"] = ref, call
    drifted = int(counts["drifted"])
    reading.detail = (
        f"{_summary(drifted, int(counts.get('untitled') or 0), int(counts.get('refused') or 0))} "
        f"Propose the next batch of {min(BATCH_PAGES, drifted)} to the user as one preview, then "
        f"apply it on their yes: {call}"
    )
    return reading


def finding(vault_root: Path, drift: Scan) -> AuditFinding | None:
    """The one review finding for the vault, or None while no page drifts.

    Its `signal_version` changes with the drifted set, so a decision binds to the set that was
    shown. Its text carries the call that renames the next batch, with the item's own reference.
    """
    if not drift.drifted:
        return None
    return _finding_from(
        vault_root,
        {
            "signal_version": drift_signal(drift),
            "drifted": len(drift.drifted),
            "untitled": len(drift.untitled),
            "refused": len(drift.refused),
        },
    )


def stored_finding(vault_root: Path, meta: dict[str, Any]) -> AuditFinding | None:
    """The finding rebuilt from a stored row's meta, with no scan; None when it holds no drift."""
    if not meta.get("drifted"):
        return None
    return _finding_from(vault_root, {key: meta.get(key) for key in _COUNTS})


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
            "pages": [],
        }
    rows = sorted([*drift.drifted, *drift.untitled, *drift.refused], key=lambda page: page.path)
    if cursor is not None:
        rows = [page for page in rows if page.path > cursor]
    shown = rows[:limit]
    refs = review_state.refs_for_paths(vault_root, [page.path for page in shown]) if shown else {}
    found = finding(vault_root, drift)
    result: dict[str, Any] = {
        "outcome": _summary(len(drift.drifted), len(drift.untitled), len(drift.refused))
        if drift.drifted or drift.untitled or drift.refused
        else "Every page's file name follows its title.",
        "complete": True,
        "total_drifted": len(drift.drifted),
        "no_usable_title": len(drift.untitled),
        "refused": len(drift.refused),
        "pages": [
            {
                "ref": refs[page.path],
                "state": page.verdict,
                "title": page.title,
                "current_name": page.current,
                **({"planned_name": page.planned} if page.verdict == DRIFTED else {}),
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
