"""Name drift: a page whose file name is not the name its title would get today.

Until 0.186.0 a new page took a slug name; since then a vault's `filename_style` decides, and
the default is the title. A retitle never renamed a page either. Both leave pages whose file
name no longer follows their title. This module is the one place that finds them:

- the rule: a page drifts when its file name differs from the name that the creation path,
  `vault.resolve_filename_slug`, gives its frontmatter title today. There is no second naming
  rule here. A ` (n)` collision suffix on an otherwise matching name is not drift;
- the scope: the types that `note_types.titled_name` says are named by their title, less the
  captured Sources, which keep their dated names. Episodes, Evidence and Records are outside
  `titled_name` already;
- a page with no usable title is counted apart and is never proposed for a rename;
- the batch resolver, the review finding and the retitle suggestion, so that the audit check,
  the read operation and the write path cannot disagree.

It measures and proposes. It never renames: `curation.relocate` does, on the owner's yes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from . import layout, note_types, vault
from .find_corpus import NAVIGATION_BASENAMES

if TYPE_CHECKING:
    from .audit import AuditFinding

CATEGORY = "name_drift"

#: The `structure_suggestion` kind that a retitling write carries.
KIND = "rename"

#: How many pages one proposed rename batch holds. PROVISIONAL: a cadence parameter, not
#: vocabulary. It is the operator's preview size; `curation.relocate` caps a batch itself.
BATCH_PAGES = 200

#: Closed verdicts of `judge`.
IN_STEP = "in_step"
DRIFTED = "drifted"
NO_USABLE_TITLE = "no_usable_title"

#: The collision numbers a name can end with: `-n` as `vault.numbered_stem` adds it on creation,
#: and ` (n)` as `layout.allocate` adds it in a relayout. Both are formats that code fixes.
_COLLISION_SUFFIX = re.compile(r"(?:-| \()(\d+)\)?$")


@dataclass(frozen=True, slots=True)
class Page:
    """One judged page. `planned` is the file name before any vault-wide collision number."""

    path: str
    verdict: str
    title: str = ""
    current: str = ""
    planned: str = ""


@dataclass(frozen=True, slots=True)
class Scan:
    """The judged pages of one corpus. `drifted` and `untitled` are sorted by path."""

    drifted: tuple[Page, ...]
    untitled: tuple[Page, ...]
    examined: int


def _in_scope(page_type: object, basis: note_types.Basis) -> note_types.NoteType | None:
    """The page's note type when its file name is built from its title, else None.

    The registry attribute decides, never a type key. A captured Source is dated and keeps its
    capture name, so it is out although `titled_name` includes its role.
    """
    note_type = basis.resolve(page_type).note_type
    if note_type is None or not note_types.titled_name(note_type):
        return None
    return None if note_types.captured_source(note_type) else note_type


def _frontmatter_title(frontmatter: Mapping[Any, Any]) -> str:
    title = frontmatter.get("title")
    return title.strip() if isinstance(title, str) else ""


def _date_prefix(note_type: note_types.NoteType, current_stem: str) -> str:
    """The date prefix a dated type's name keeps: its page's own date is not known to this rule, so the
    name's current prefix stands, the same reading `layout.place` takes."""
    # `stem` takes the closed values that the registry schema fixes (`note_types.STEMS`), so this is not a vocabulary.
    dated = note_type.stem == "dated" or note_type.stem == "started"
    return layout.date_prefix(current_stem) if dated else ""


def planned_name(
    vault_root: Path | None, title: str, *, current_stem: str, note_type: note_types.NoteType
) -> str | None:
    """The file stem the creation path gives `title`, before any collision number; None without a usable title."""
    prefix = _date_prefix(note_type, current_stem)
    usable = vault.corrected_title_name(title).name
    # The creation path falls back to a slug for a title that no file name can hold; such a
    # page has no usable title here, whatever the style, and is never proposed for a rename.
    if not usable or vault.fit_filename(usable, date_prefix=prefix) is None:
        return None
    stem, _warnings = vault.resolve_filename_slug(title, None, vault_root=vault_root, date_prefix=prefix)
    return stem


def _numbered_names(planned: str, prefix: str, number: int) -> set[str]:
    """The names `planned` takes at collision `number`, in each form that a page can carry."""
    names = {vault.numbered_stem(prefix + planned, number).removeprefix(prefix)}
    fitted = vault.fit_filename(planned, date_prefix=prefix, number=number)
    if fitted is not None:
        names.add(fitted[0].removeprefix(prefix))
    return names


def judge(
    vault_root: Path | None,
    rel_path: str,
    *,
    page_type: object,
    frontmatter: Mapping[Any, Any],
    basis: note_types.Basis,
) -> Page | None:
    """The page's verdict, or None when its name is not built from its title."""
    note_type = _in_scope(page_type, basis)
    name = PurePosixPath(rel_path).name
    if (
        note_type is None
        or not name.endswith(".md")
        or name.casefold() in NAVIGATION_BASENAMES
    ):
        return None
    stem = name.removesuffix(".md")
    title = _frontmatter_title(frontmatter)
    planned = planned_name(vault_root, title, current_stem=stem, note_type=note_type)
    if planned is None:
        return Page(rel_path, NO_USABLE_TITLE, title, name)
    names = {planned}
    suffix = _COLLISION_SUFFIX.search(stem)
    if suffix is not None and int(suffix.group(1)) > 1:
        names |= _numbered_names(planned, _date_prefix(note_type, stem), int(suffix.group(1)))
    matches = unicodedata.normalize("NFC", stem) in {unicodedata.normalize("NFC", name) for name in names}
    return Page(rel_path, IN_STEP if matches else DRIFTED, title, name, planned + ".md")


def scan(vault_root: Path, pages: Iterable[Any]) -> Scan:
    """Judge every page of a corpus. `pages` are parsed pages with `rel_path` and `frontmatter`."""
    basis = note_types.Basis(vault_root)
    drifted: list[Page] = []
    untitled: list[Page] = []
    examined = 0
    for page in pages:
        verdict = judge(
            vault_root,
            page.rel_path,
            page_type=page.frontmatter.get("type"),
            frontmatter=page.frontmatter,
            basis=basis,
        )
        if verdict is None:
            continue
        examined += 1
        if verdict.verdict == DRIFTED:
            drifted.append(verdict)
        elif verdict.verdict == NO_USABLE_TITLE:
            untitled.append(verdict)
    key = lambda item: item.path  # noqa: E731 -- one sort key for both lists
    return Scan(tuple(sorted(drifted, key=key)), tuple(sorted(untitled, key=key)), examined)


def next_batch(drift: Scan, limit: int = BATCH_PAGES, *, after: str | None = None) -> list[Page]:
    """The next `limit` drifted pages after path `after`, in path order. Pages with no usable title never appear."""
    start = 0
    if after is not None:
        start = next((i for i, page in enumerate(drift.drifted) if page.path > after), len(drift.drifted))
    return list(drift.drifted[start : start + max(limit, 0)])


def pages_for_review(vault_root: Path) -> list[str]:
    """The next batch's paths, read now: what a relocation by this family's `review_ref` renames.

    The scan is fresh, so a batch already renamed is never proposed again even before the
    review row is rebuilt. A stopped scan raises the budget refusal: an unknown batch is never empty.
    """
    from . import audit as audit_module
    from .vault import kb_root

    pages = audit_module._parse_all(kb_root(vault_root), vault_root)  # noqa: SLF001 -- the audit's own corpus read
    return [page.path for page in next_batch(scan(vault_root, pages))]


def drift_signal(drift: Scan) -> str:
    """A short identity of the drifted set and the names planned for it: it changes when either does."""
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


def _summary(count: int, untitled: int) -> str:
    noun = "page has a file name" if count == 1 else "pages have file names"
    sentence = f"{count} {noun} that no longer follow their titles."
    if untitled:
        sentence += f" {untitled} more have no usable title and are left as they are."
    return sentence


def finding(vault_root: Path, drift: Scan) -> AuditFinding | None:
    """The one review finding for the vault, or None while no page drifts.

    It is anchored on the first drifted page, so one vault holds one item. Its `signal_version`
    changes with the drifted set, so a decision binds to the set that was shown. Its
    text carries the call that renames the next batch, with the item's own reference.
    """
    from . import due_state
    from .audit import AuditFinding

    if not drift.drifted:
        return None
    anchor = drift.drifted[0].path
    meta = {
        "signal_version": drift_signal(drift),
        "review_partition": CATEGORY,
        "drifted": len(drift.drifted),
        "untitled": len(drift.untitled),
        "batch_pages": BATCH_PAGES,
        # No authored date: the drift is due from the start, and the placeholder sorts it last among its own rows.
        "due_since": dt.date.min.isoformat(),
    }
    reading = AuditFinding(
        category=CATEGORY, severity="info", path=anchor, detail="", meta=meta
    )
    entry = due_state.entry_for(vault_root, reading)
    ref = entry["ref"] if entry is not None else None
    call = relocate_call(review_ref=ref) if ref else relocate_call()
    meta["review_ref"], meta["call"] = ref, call
    reading.detail = (
        f"{_summary(len(drift.drifted), len(drift.untitled))} Propose the next batch of "
        f"{min(BATCH_PAGES, len(drift.drifted))} to the user as one preview, then apply it on their yes: {call}"
    )
    return reading


def suggestion_after_write(vault_root: Path, state: Any, *, previous: Any = None) -> dict[str, Any] | None:
    """The `structure_suggestion` of a write that retitled a page so that its name drifts, else None.

    Only a changed title asks. The plan is made from the new title in memory: the write path
    scans nothing. The user's own edit is the approval, so the suggestion carries the call and
    no question. May raise; the write path treats a raise as "suggest nothing".
    """
    from . import envelope, review_state

    if previous is None or _frontmatter_title(previous.frontmatter) == _frontmatter_title(state.frontmatter):
        return None
    if envelope.active().get("structural_suggestions") == "off":
        return None
    basis = state.type_basis or note_types.Basis(vault_root)
    verdict = judge(
        vault_root, state.path, page_type=state.page_type, frontmatter=state.frontmatter, basis=basis
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


def listing(vault_root: Path, *, cursor: str | None = None, limit: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    """One page of the pages whose names follow no title, for the read operation.

    The scan is one pass over the admitted corpus through the cached frontmatter reads, never
    a query per page. When the budget stops that pass, the answer says the scan is incomplete and
    gives no total: an unknown count is never 0. Rows are drifted pages and pages with no
    usable title, in path order; `cursor` is the last path of the previous page.
    """
    from . import audit as audit_module
    from . import request_budget, review_state
    from .vault import kb_root

    limit = max(1, min(limit, MAX_PAGE_SIZE))
    try:
        pages = audit_module._parse_all(kb_root(vault_root), vault_root)
    except request_budget.ReadBudgetExhausted:
        return {
            "outcome": "The scan stopped before it finished, so the number of pages with stale names is unknown. Ask again.",
            "complete": False,
            "total_drifted": None,
            "no_usable_title": None,
            "pages": [],
        }
    drift = scan(vault_root, pages)
    rows = sorted([*drift.drifted, *drift.untitled], key=lambda page: page.path)
    if cursor is not None:
        rows = [page for page in rows if page.path > cursor]
    shown = rows[:limit]
    refs = review_state.refs_for_paths(vault_root, [page.path for page in shown]) if shown else {}
    found = finding(vault_root, drift)
    result: dict[str, Any] = {
        "outcome": _summary(len(drift.drifted), len(drift.untitled)) if drift.drifted or drift.untitled
        else "Every page's file name follows its title.",
        "complete": True,
        "total_drifted": len(drift.drifted),
        "no_usable_title": len(drift.untitled),
        "pages": [
            {
                "ref": refs[page.path],
                "state": page.verdict,
                "title": page.title,
                "current_name": page.current,
                **({"planned_name": page.planned} if page.verdict == DRIFTED else {}),
            }
            for page in shown
        ],
    }
    if found is not None:
        result["rename_next_batch"] = found.meta["call"]
    if len(rows) > len(shown):
        result["next_cursor"] = shown[-1].path
    return result
