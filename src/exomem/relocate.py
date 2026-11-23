"""Relocate a set of pages to a project and to the names their titles give, as one sealed plan.

The caller names the pages, or one review item. This module derives the whole repair, reading
only, and seals it through the curation lane:

- a page whose path changes gets a `move`, and its `fields` patch rewrites the page's project;
- a page that stays where it is gets a pointer correction of its project;
- each Source that a relocated page cites gains the new project key. It loses the old key only
  when no page outside the set that still carries that key cites it;
- the `repoint-links` steps come last and rewrite every link to a moved page. A plan with more
  referrers than one step rewrites in a short hold splits them by referring page.

The plan is sealed with `derived_by="relocate"`. Preparing it runs under the request budget: a
call that runs short keeps the steps it prepared and answers `preparing`, and the same call made
again goes on from them. The person approves one fingerprint, apply runs the steps under the
request budget, and one compensation plan undoes them. The page-field
registry's `carries` attribute says which fields hold project keys. The note writer's own
placement says where a page lands and what it is named (`note.planned_destination`,
`vault.resolve_filename_slug`). Nothing here branches on a vocabulary entry.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

from . import curation, memory_refs, note_types, page_fields, project_keys, repoint_links, vault
from .cli_ops import OpError

#: The most pages one relocation names: a bound on the work its derivation reads.
MAX_PAGES: Final[int] = curation.MAX_DERIVED_STEPS // 2
#: How many pages a proposal drops for a leaf refusal, preparing again each time, before it fails.
MAX_EXCLUSIONS: Final[int] = 3
#: The most referring pages one repoint step rewrites. A step holds the vault for its whole batch,
#: so a plan with more referrers splits them by referring page into steps of this size.
#: Measured 2026-11-23 on a laptop disk under WSL, corpus cache on: 100 pages relocated in a
#: 3,000-page vault split 451 referring pages into 19 steps, and no step held the vault past 3.0 s.
REFERRERS_PER_STEP: Final[int] = 25
#: How often a review item's batch is halved when its plan is too large to seal (200 pages
#: reach 12).
MAX_HALVINGS: Final[int] = 4

_CARRIES = page_fields.CARRIES_PROJECT_KEY
#: The refusal code of a page whose planned path cannot be derived, which name drift reports too.
NO_DESTINATION: Final[str] = "RELOCATE_NO_DESTINATION"
#: How long a paused preparation waits for the same call to go on from it. The calls of one
#: proposal follow each other at once (`next_action`), so an older one was abandoned.
PREPARATION_TTL_SECONDS: Final[float] = 900.0
#: Where paused preparations wait, in the vault's state directory: they are a cache of work that
#: the same call can redo, not vault content.
PREPARATIONS_DIRNAME: Final[str] = "relocate-preparations"
#: The estimate of what a sealed plan holds for each moved page, each page that links to one and
#: each link between them, besides the link lines that a rewrite seals twice (`_fitting`).
#: Measured 2026-11-23 on rename batches of a generated 3,000-page vault: a move seals about 1.7
#: to 2.0 KB with its summary row, a referring page about 600 bytes and a link about 300. The
#: page figure leaves room for paths longer than the measured ones.
SEALED_BYTES_PER_PAGE: Final[int] = 2200
SEALED_BYTES_PER_REFERRER: Final[int] = 650
SEALED_BYTES_PER_LINK: Final[int] = 300
#: The share of the sealed-plan limit that a review item's batch is sized to fill, so that a
#: batch the estimate under-counts still seals and halving stays the exception.
SEALED_HEADROOM: Final[float] = 0.75


def _error(code: str, reason: str) -> curation.CurationError:
    return curation.CurationError(code, reason)


@dataclass(frozen=True)
class _Page:
    path: str
    text: str
    frontmatter: Mapping[str, Any]
    note_type: note_types.NoteType | None
    #: The title the page's own bytes give, which its name follows.
    title: str | None = None


@dataclass
class _Derived:
    steps: list[dict[str, Any]] = field(default_factory=list)
    #: The page each step serves, so a refused step drops that page.
    step_pages: dict[str, str] = field(default_factory=dict)
    moves: list[dict[str, str]] = field(default_factory=list)
    corrected: list[str] = field(default_factory=list)
    gaining: list[str] = field(default_factory=list)
    losing: list[dict[str, str]] = field(default_factory=list)
    keeping: list[dict[str, Any]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    unchecked: list[dict[str, str]] = field(default_factory=list)
    #: The Sources whose correction steps the plan holds, by step.
    source_steps: set[str] = field(default_factory=set)
    #: Whether the catalogue could not answer which pages name a moved page's path in prose.
    mentions_unchecked: bool = False
    #: Pages of a review item's batch left for its next batch, because the whole batch was too
    #: large to seal.
    deferred: list[str] = field(default_factory=list)


def propose(
    vault_root: Path,
    *,
    pages: Sequence[str] | None = None,
    review_ref: str | None = None,
    to_project: str | None = None,
    folder: str | None = None,
    from_project: str | None = None,
) -> dict[str, Any]:
    """Derive and seal the plan that relocates these pages, and answer with its grouped summary.

    A call that runs short of time before the plan seals answers `preparing` with its progress,
    and keeps the steps it prepared for the same call made again (`_keep_paused`).
    """
    root = Path(vault_root)
    raw = _requested(root, pages, review_ref)
    if to_project is None and (folder is not None or from_project is not None):
        raise _error(
            "INVALID_RELOCATION", "folder and from_project apply only to a relocation with to_project"
        )
    registry, registers = _destination_registry(root, to_project, folder)
    selected, refused = _select(root, raw)
    #: The Sources a leaf refused at preparation: each keeps its keys and is listed as refused.
    held_sources: set[str] = set()
    deferred: list[str] = []
    exclusions = halvings = 0
    budget = curation._call_budget()  # noqa: SLF001 -- the lane's one request deadline
    preparation_key = _preparation_key(
        root,
        {
            "pages": list(pages) if pages is not None else None,
            "review_ref": review_ref,
            "to_project": to_project,
            "folder": folder,
            "from_project": from_project,
        }
    )
    preparation = curation.Preparation()
    paused = _paused(root, preparation_key)
    resumed = paused is not None and set(paused["selected"]) <= {item.path for item in selected}
    if paused is not None and resumed:
        # The same call goes on from the preparation it paused, with the batch it chose then.
        chosen = set(paused["selected"])
        selected = [item for item in selected if item.path in chosen]
        refused = list(paused["refused"])
        deferred = list(paused["deferred"])
        held_sources = set(paused["held_sources"])
        exclusions, halvings = int(paused["exclusions"]), int(paused["halvings"])
        preparation = curation.Preparation(
            steps=list(paused["steps"]), registry_ids=dict(paused["registry_ids"])
        )
    elif review_ref is not None:
        # The server chose this batch, so it sizes the batch to what one sealed plan holds.
        fits = _fitting(root, selected)
        deferred.extend(item.path for item in selected[fits:])
        selected = selected[:fits]

    def remember(rows: Sequence[dict[str, str]]) -> None:
        """Tell the review item that its rename batch could not take these pages.

        Only a rename-only relocation by `review_ref` reports: that is the call the item offers,
        and its family skips the pages until they change (`attention.record_refused`).
        """
        if review_ref is not None and to_project is None and rows:
            from . import attention

            attention.record_refused(root, review_ref, rows)

    def leave_out(error: curation.CurationError, derived: _Derived) -> bool:
        """Leave out of a review item's batch the pages whose move or referrer rewrite was refused.

        The server chose that batch, so it shrinks, as a batch too large to seal is halved. The
        refusal spends no exclusion: each pass drops at least one page, so the loop ends. Pages
        the caller named refuse instead, and a refused Source correction keeps its own rule.
        """
        nonlocal selected
        if review_ref is None or error.step_id in derived.source_steps:
            return False
        named = set(error.pages)
        if not named and error.step_id in derived.step_pages:
            named = {derived.step_pages[error.step_id]}
        rows = [
            _refusal(item.path, error.code, error.reason)
            for item in selected
            if item.path in named
        ]
        if not rows:
            return False
        refused.extend(rows)
        selected = [item for item in selected if item.path not in named]
        remember(rows)
        return True

    if not resumed:
        remember(refused)

    def shrink(error: curation.CurationError) -> bool:
        """Halve a review item's batch that is too large to seal.

        The server chose that batch, so it shrinks; pages the caller named refuse instead, because
        dropping them would change the request.
        """
        nonlocal selected, halvings
        if (
            error.code != "CURATION_PLAN_TOO_LARGE"
            or review_ref is None
            or len(selected) < 2
            or halvings == MAX_HALVINGS
        ):
            return False
        keep = len(selected) // 2
        deferred.extend(item.path for item in selected[keep:])
        selected = selected[:keep]
        halvings += 1
        return True

    def out_of_time(slowest: float) -> bool:
        """Whether this call stops before another step or the seal, by apply's rule (`_advance`).

        A call with no preparation key can keep nothing, so it prepares to the seal in one call.
        """
        from . import request_budget

        if preparation_key is None:
            return False
        return not budget.can_afford(slowest + request_budget.DELIVERY_RESERVE_SECONDS)

    def pause(derived: _Derived) -> dict[str, Any]:
        """Keep what this call prepared for the same call to go on from, and say how far it got."""
        _keep_paused(
            root,
            preparation_key,
            {
                "selected": [item.path for item in selected],
                "refused": refused,
                "deferred": deferred,
                "held_sources": sorted(held_sources),
                "exclusions": exclusions,
                "halvings": halvings,
                "registry_ids": preparation.registry_ids,
                "steps": preparation.steps,
            },
        )
        return {
            "action": "relocate",
            "mutated": False,
            "sealed": False,
            "phase": "preparing",
            "progress": {
                "steps": len(derived.steps),
                "prepared": len(preparation.steps),
                "this_call": preparation.this_call,
            },
            "next_action": "relocate",
        }

    # Each pass either returns, raises, pauses, spends one bounded exclusion or halving, or leaves
    # at least one page of a review item's batch out. A pass keeps the steps an earlier pass
    # prepared while they open its plan unchanged (`curation.Preparation`).
    try:
        while True:
            derived = _derive(
                root,
                selected,
                to_project=to_project,
                from_project=from_project,
                folder=folder,
                registry=registry,
                registers=registers,
                held_sources=held_sources,
            )
            derived.refused[:0] = refused
            derived.deferred = list(deferred)
            if not derived.steps:
                _drop_paused(root, preparation_key)
                summary = _summary(derived, to_project=to_project, registers=False)
                return {"action": "relocate", "mutated": False, "sealed": False, "summary": summary}
            writes_key = registers and any(
                step["kind"] == "edit" or "fields" in step["args"] for step in derived.steps
            )
            if writes_key and folder and not any(
                "project_folders" in step["args"] for step in derived.steps
            ):
                raise _error(
                    "RELOCATE_FOLDER_NEEDS_MOVE",
                    f"no page moves into project {to_project!r}, so no step can register its "
                    "folder; omit folder to register the default one",
                )
            plan = {"version": 1, "title": _title(derived, to_project), "steps": derived.steps}
            try:
                prepared = curation.prepare_proposal(
                    root, plan, derived_by="relocate", preparation=preparation, stop=out_of_time
                )
            except curation.PreparationPaused:
                return pause(derived)
            except curation.CurationError as error:
                if shrink(error) or leave_out(error, derived):
                    continue
                page = derived.step_pages.get(error.step_id or "")
                if page is None or exclusions == MAX_EXCLUSIONS:
                    raise
                exclusions += 1
                refused.append({"page": page, "code": error.code, "reason": error.reason})
                if error.step_id in derived.source_steps:
                    held_sources.add(page)
                else:
                    selected = [item for item in selected if item.path != page]
                continue
            if preparation.this_call and out_of_time(preparation.slowest):
                # Every step is prepared; the same call made again seals at once.
                return pause(derived)
            summary = _summary(
                derived,
                to_project=to_project,
                registers=writes_key,
                referrers=sum(
                    curation.rewritten_referrers(step, binding)
                    for step, binding in zip(
                        prepared.plan["steps"], prepared.binding_manifest, strict=True
                    )
                ),
            )
            # The plan seals the summary it is approved by, so a later preview shows the same groups.
            try:
                sealed = curation.seal_proposal(root, dataclasses.replace(prepared, summary=summary))
            except curation.CurationError as error:
                if shrink(error):
                    continue
                raise
            _drop_paused(root, preparation_key)
            response = {key: value for key, value in sealed.items() if key != "plan"}
            return {
                **response,
                "action": "relocate",
                "sealed": True,
                "steps": len(derived.steps),
                "summary": summary,
                "next_action": "apply",
            }
    except Exception:
        # A refusal ends the proposal: the same call made again derives it afresh.
        _drop_paused(root, preparation_key)
        raise


def _preparation_key(root: Path, request: Mapping[str, Any]) -> str | None:
    """The paused preparation's name: the call's arguments and who asks (`reader_key`), else
    None, so a preparation is kept only for callers that would prepare the same steps."""
    reader = reader_key(root)
    if reader is None:
        return None
    identity = {"request": dict(request), "reader": reader}
    return hashlib.sha256(curation.canonical_json_bytes(identity)).hexdigest()[:32]


def reader_key(root: Path) -> str | None:
    """Who asks, as one key for relocation state kept between calls; None when it cannot be read.

    The audience, the authorization session, the purpose and the connector ceiling decide which
    pages and referrers a caller sees and writes; a session's grants can raise its audience's
    policy ceiling, as the served due-state cache also keys. The ceiling is
    `connector_boundary.admission_identity`: the denied scopes, the configuration revision and
    the registry assignment. The writable scopes need no part of
    their own: the host configuration makes each writable scope exclusive to one client, so
    callers denied the same scopes write the same ones. A preparation or a refusal kept under one
    key is read only under the same key, and a caller whose ceiling cannot be read keeps nothing.
    """
    from .governance import connector_boundary
    from .governance.principal import effective_principal

    who = effective_principal()
    try:
        denied, revision, assignment = connector_boundary.admission_identity(root, who)
    except connector_boundary.BoundaryUnavailable:
        return None
    identity = {
        "audience": who.audience_id,
        "session": getattr(who, "authorization_session_id", None),
        "purpose": who.purpose,
        "denied": sorted(denied),
        "revision": revision,
        "assignment": assignment,
    }
    return hashlib.sha256(curation.canonical_json_bytes(identity)).hexdigest()


def _paused_path(root: Path, key: str) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(root) / PREPARATIONS_DIRNAME / f"{key}.json"


def _paused(root: Path, key: str | None) -> dict[str, Any] | None:
    """The preparation that this call paused, else None.

    An unreadable one is not used. An expired one is deleted, since it holds page bytes.
    """
    import json

    if key is None:
        return None
    path = _paused_path(root, key)
    try:
        if time.time() - path.stat().st_mtime > PREPARATION_TTL_SECONDS:
            path.unlink(missing_ok=True)
            return None
        held = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return held if isinstance(held, dict) and held.get("version") == 1 else None


def _keep_paused(root: Path, key: str, state: Mapping[str, Any]) -> None:
    """Keep a paused preparation, and drop the ones older than `PREPARATION_TTL_SECONDS`."""
    from . import due_state, state_paths

    state_paths.ensure_vault_state_dir(root)
    path = _paused_path(root, key)
    path.parent.mkdir(exist_ok=True)
    for other in path.parent.glob("*.json"):
        try:
            if time.time() - other.stat().st_mtime > PREPARATION_TTL_SECONDS:
                other.unlink()
        except OSError:
            continue
    due_state._replace_json(path, {"version": 1, **state})  # noqa: SLF001 -- its atomic writer


def _drop_paused(root: Path, key: str | None) -> None:
    if key is None:
        return
    try:
        _paused_path(root, key).unlink(missing_ok=True)
    except OSError:
        return


def _fitting(root: Path, selected: Sequence[_Page]) -> int:
    """How many pages, from the head of a review item's batch, one sealed plan holds.

    The estimate reads one link index: a sealed plan grows with each moved page, each page that
    links to one, each link between them and each link line that a rewrite seals with its undo.
    At least one page is kept, so a page above the limit alone still refuses as before.
    """
    from .governance import egress

    paths = [item.path for item in selected]
    inbound = vault.find_inbound_wikilinks_many(
        root, paths, visible=egress.visible_page_filter(root)
    )
    room = curation.MAX_SEALED_BYTES * SEALED_HEADROOM
    used = 0
    referrers: set[str] = set()
    lines: set[tuple[str, int]] = set()
    for count, path in enumerate(paths):
        cost = SEALED_BYTES_PER_PAGE
        for hit in inbound.get(path, ()):
            cost += SEALED_BYTES_PER_LINK
            if hit.path not in referrers:
                referrers.add(hit.path)
                cost += SEALED_BYTES_PER_REFERRER
            if (hit.path, hit.line_number) not in lines:
                lines.add((hit.path, hit.line_number))
                cost += 2 * len(hit.context.encode("utf-8"))
        if count and used + cost > room:
            return count
        used += cost
    return len(paths)


def _requested(root: Path, pages: Sequence[str] | None, review_ref: str | None) -> list[str]:
    if (pages is None) == (review_ref is None):
        raise _error("INVALID_RELOCATION", "a relocation names pages or one review_ref, not both")
    if review_ref is not None:
        covered = curation._pages_of_review_item(root, str(review_ref))  # noqa: SLF001
        if covered is None:
            raise _error(
                "RELOCATE_REVIEW_ITEM_UNKNOWN", "review_ref names no current review item on a page"
            )
        if not covered:
            raise _error(
                "RELOCATE_REVIEW_ITEM_EMPTY", "the review item names no page to relocate now"
            )
        return covered
    if (
        not isinstance(pages, list)
        or not 1 <= len(pages) <= MAX_PAGES
        or not all(isinstance(page, str) and page for page in pages)
    ):
        raise _error(
            "INVALID_RELOCATION", f"pages must list 1 to {MAX_PAGES} page refs or vault paths"
        )
    return list(pages)


def _destination_registry(
    root: Path, to_project: str | None, folder: str | None
) -> tuple[project_keys.ProjectRegistry | None, bool]:
    """The project registry as the plan leaves it, and whether the plan registers `to_project`."""
    if to_project is None:
        return None, False
    if not page_fields.fields_carrying(root, _CARRIES):
        raise _error(
            "RELOCATE_NO_PROJECT_FIELD", "this vault retired every field that carries a project key"
        )
    if folder is not None:
        try:
            project_keys.require_portable_folder(folder)
        except project_keys.InvalidProjectFolderError as error:
            raise _error("INVALID_RELOCATION", str(error)) from error
    try:
        key_plan = project_keys.plan_project_keys(
            root, [to_project], folders={to_project: folder} if folder else None
        )
    except project_keys.ProjectKeyTypoError as error:
        raise _error("PROJECT_KEY_TYPO", str(error)) from error
    except ValueError as error:
        raise _error("INVALID_PROJECT_KEY", str(error)) from error
    registers = to_project in key_plan.introduced_keys
    kept = key_plan.registry.folder_for(to_project) or ""
    if folder and not registers and kept.casefold() != folder.casefold():
        raise _error(
            "RELOCATE_FOLDER_FIXED",
            f"project {to_project!r} already keeps its notes in {kept!r}; omit folder",
        )
    return key_plan.registry, registers


def _select(root: Path, raw: Sequence[str]) -> tuple[list[_Page], list[dict[str, str]]]:
    """Each named page once, read as it is now; a page the lane cannot change is refused by name.

    Every memory ref among the names resolves over one identity census.
    """
    basis = note_types.Basis(root)
    resolve = memory_refs.identifier_resolver_read_only(root, raw)
    selected: list[_Page] = []
    refused: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in raw:
        try:
            path, text = curation._read_page(root, value, resolve=resolve)  # noqa: SLF001
        except curation.CurationError as error:
            refused.append({"page": value, "code": error.code, "reason": error.reason})
            continue
        if path in seen:
            continue
        seen.add(path)
        curated = vault.in_curated_tree(path) if vault.in_append_only_tree(path) is None else None
        if curated:
            refused.append(
                {
                    "page": path,
                    "code": "CURATED_PROTECTED",
                    "reason": f"{path} is in curated tree {curated!r}; relocate it there",
                }
            )
            continue
        frontmatter, body, _raw = vault.parse_frontmatter(text)
        try:
            note_type = basis.resolve(frontmatter.get("type")).require()
        except OpError as error:
            refused.append({"page": path, "code": error.code, "reason": str(error)})
            continue
        selected.append(
            _Page(path, text, frontmatter, note_type, vault.authored_title(frontmatter, body))
        )
    return selected, refused


def _derive(
    root: Path,
    selected: Sequence[_Page],
    *,
    to_project: str | None,
    from_project: str | None,
    folder: str | None,
    registry: project_keys.ProjectRegistry | None,
    registers: bool,
    held_sources: set[str],
) -> _Derived:
    """The plan's steps for these pages, with what each group of the summary lists."""
    from . import find

    derived = _Derived()
    carrying = page_fields.fields_carrying(root, _CARRIES)
    lists = page_fields.list_fields(root)
    # The one writer snapshot that the title check and the Source rule read.
    snapshot = functools.cache(lambda: find.writer_resolver_snapshot(root))
    names = Names(root, resolver=snapshot)
    moves: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    #: Each relocated page and the keys it gives up, for the Source rule.
    relocated: dict[str, tuple[_Page, frozenset[str]]] = {}
    why = f"Relocate to project {to_project}." if to_project else "Rename to the page title."
    for page in selected:
        values = page_fields.carried(root, page.frontmatter, _CARRIES)
        changes: dict[str, Any] = {}
        dropped: frozenset[str] = frozenset()
        if to_project is not None:
            held = {key for value in values.values() for key in page_fields.carried_keys(value)}
            if from_project is not None:
                if from_project not in held:
                    derived.refused.append(_refusal(
                        page.path, "RELOCATE_NOT_IN_PROJECT",
                        f"the page does not carry project {from_project!r}",
                    ))
                    continue
                dropped = frozenset({from_project})
            elif len(held - {to_project}) > 1:
                derived.refused.append(_refusal(
                    page.path, "RELOCATE_AMBIGUOUS_PROJECT",
                    "the page carries several projects; name the one it leaves as from_project",
                ))
                continue
            else:
                dropped = frozenset(held - {to_project})
            rewritten = _rewritten(values, carrying, lists, drop=dropped, add=to_project)
            if isinstance(rewritten, str):
                derived.refused.append(_refusal(
                    page.path, rewritten,
                    "the page keeps another project in a single-value field and has no list to add "
                    f"{to_project!r} to",
                ))
                continue
            changes = rewritten
        after = {**values, **changes}
        alone = {key for value in after.values() for key in page_fields.carried_keys(value)}
        try:
            target = planned_path(
                root,
                page.path,
                page.note_type,
                page.title,
                names,
                project=to_project if alone == {to_project} else None,
                registry=registry,
            )
        except (OpError, ValueError) as error:
            derived.refused.append(_refusal(page.path, NO_DESTINATION, str(error)))
            continue
        if target is None and len(changes) > 1:
            derived.refused.append(_refusal(
                page.path, "RELOCATE_MULTIPLE_FIELDS",
                f"the page needs {len(changes)} fields corrected in place; correct them one at a time",
            ))
            continue
        if target is not None:
            args: dict[str, Any] = {"old_path": page.path, "new_path": target}
            if changes:
                args["fields"] = changes
                if registers and folder:
                    args["project_folders"] = {to_project: folder}
            moves.append({"page": page.path, "args": args})
            derived.moves.append({"from": page.path, "to": target})
        elif changes:
            ((name, value),) = changes.items()
            corrections.append({"page": page.path, "args": _correction(page, name, value, why)})
            derived.corrected.append(page.path)
        else:
            derived.unchanged.append(page.path)
            continue
        if changes:
            relocated[page.path] = (page, dropped)
    sources = (
        _source_corrections(
            root, relocated, {page.path for page in selected}, to_project, carrying, lists, why,
            derived, snapshot=snapshot, held=held_sources,
        )
        if to_project is not None and relocated
        else []
    )
    for prefix, rows in (("move", moves), ("edit", corrections), ("source", sources)):
        for ordinal, row in enumerate(rows, start=1):
            step_id = f"{prefix}-{ordinal:04d}"
            kind = "move" if prefix == "move" else "edit"
            derived.steps.append({"step_id": step_id, "kind": kind, "args": row["args"]})
            derived.step_pages[step_id] = row["page"]
            if prefix == "source":
                derived.source_steps.add(step_id)
    if moves:
        derived.steps.extend(
            _repoint_steps(
                root, [(row["args"]["old_path"], row["args"]["new_path"]) for row in moves], derived
            )
        )
    return derived


def _repoint_steps(
    root: Path, moves: Sequence[tuple[str, str]], derived: _Derived
) -> list[dict[str, Any]]:
    """The link rewrite: one step, or steps of `REFERRERS_PER_STEP` referring pages each.

    Each referring page is named by exactly one step, so a page's rewrite never spans two holds.
    """
    referrers, complete = repoint_links.referrers(root, moves)
    derived.mentions_unchecked = not complete
    if len(referrers) <= REFERRERS_PER_STEP:
        return [{"step_id": "repoint", "kind": curation.REPOINT_STEP_KIND, "args": {}}]
    return [
        {
            "step_id": f"repoint-{ordinal:04d}",
            "kind": curation.REPOINT_STEP_KIND,
            "args": {"referrers": referrers[start : start + REFERRERS_PER_STEP]},
        }
        for ordinal, start in enumerate(range(0, len(referrers), REFERRERS_PER_STEP), start=1)
    ]


def _refusal(page: str, code: str, reason: str) -> dict[str, str]:
    return {"page": page, "code": code, "reason": reason}


def _correction(page: _Page, name: str, value: Any, why: str) -> dict[str, Any]:
    return {
        "path": page.path,
        "why": why,
        "operation": {
            "kind": "patch_frontmatter",
            "field": name,
            "value": value,
            "expected_hash": vault.content_hash(page.text),
        },
    }


def _rewritten(
    values: Mapping[str, Any],
    carrying: Sequence[str],
    lists: frozenset[str],
    *,
    drop: frozenset[str],
    add: str,
) -> dict[str, Any] | str:
    """The carrying fields that change when a page gives up `drop` and gains `add`.

    A dropped key becomes `add` where it stood. A page that does not hold `add` afterwards gains it
    in its first list field, else its first empty field, else the first carrying field it lacks,
    in the shape the registry gives that field (`lists`). A page whose every carrying field holds
    another key singly cannot gain it, and the answer is that refusal's code.
    """
    changes: dict[str, Any] = {}
    for name, value in values.items():
        if isinstance(value, list):
            out: list[Any] = []
            for item in value:
                item = add if isinstance(item, str) and item in drop else item
                if item == add and add in out:
                    continue
                out.append(item)
            if out != value:
                changes[name] = out
        elif isinstance(value, str) and value in drop:
            changes[name] = add
    after = {**values, **changes}
    if add in {key for value in after.values() for key in page_fields.carried_keys(value)}:
        return changes
    held_lists = [name for name, value in after.items() if isinstance(value, list)]
    empty = [name for name, value in after.items() if not page_fields.carried_keys(value)]
    absent = [name for name in carrying if name not in values]
    if held_lists:
        changes[held_lists[0]] = [*after[held_lists[0]], add]
    elif empty or absent:
        name = (empty or absent)[0]
        changes[name] = [add] if name in lists else add
    else:
        return "RELOCATE_SINGLE_VALUE_FIELD"
    return changes


class Names:
    """What a rename may not take, for one derivation or one scan over an unchanged vault.

    A live file or a prior claim in the target folder (each folder listed once), another live
    page's title, and a name this derivation already gave to an earlier page.
    """

    def __init__(self, root: Path, *, resolver: Callable[[], Any] | None = None) -> None:
        from . import title_occupancy

        self.root = Path(root)
        try:
            self.titles: Any = title_occupancy.TitleCheck(self.root, None, resolver=resolver)
        except ValueError:
            # The vault's naming style cannot be read; each page's own naming then refuses it.
            self.titles = None
        self.claimed: set[str] = set()
        self.folders: dict[Path, Callable[[str], bool]] = {}


def renamed_by_title(path: str, note_type: note_types.NoteType | None) -> bool:
    """Whether a relocation names this page by its title.

    The types whose file name the writer builds from the title (`titled_name`) are in scope, less
    the captured Sources, which keep their dated capture names. A navigation page keeps its name
    whatever its type: links, and the name-drift item's anchor, find it by that name.
    """
    from . import fold_leaf

    return (
        note_type is not None
        and note_types.titled_name(note_type)
        and not note_types.captured_source(note_type)
        and not fold_leaf.is_navigation(path)
    )


def title_stem(
    root: Path, path: str, note_type: note_types.NoteType, title: str | None
) -> str | None:
    """The file stem the vault's writer gives this page's title now, date prefix included.

    None when the title can name no file: no title, or one with no character a file name can
    hold. Such a page keeps its name, because any other name would be made up.
    """
    from . import layout

    usable = vault.corrected_title_name(title).name if title else ""
    if not usable:
        return None
    # `slug` is the closed stem kind that builds a name from the slug alone, with no date.
    prefix = layout.date_prefix(PurePosixPath(path).stem) if note_type.stem != "slug" else ""
    if vault.fit_filename(usable, date_prefix=prefix) is None:
        return None
    name, _warnings = vault.resolve_filename_slug(
        title or "", None, vault_root=root, date_prefix=prefix
    )
    return prefix + name if name else None


def planned_path(
    root: Path,
    path: str,
    note_type: note_types.NoteType | None,
    title: str | None,
    names: Names,
    *,
    project: str | None = None,
    registry: project_keys.ProjectRegistry | None = None,
) -> str | None:
    """Where a relocation puts this page now, or None when it stays where it is.

    The one rule for a page's planned path: a relocation derives its moves with it, and the
    name-drift review asks it whether a rename-only relocation would move a page.

    In scope are the pages that `renamed_by_title` names. The name is the one the
    vault's writer gives the page's title, after the date prefix that a dated type keeps. The
    folder changes only for a compiled type that a project partitions, and only when the page
    belongs to `project` alone afterwards; an entity is renamed where it is. A name that a file,
    a prior claim, a name given earlier in this derivation or another live page's title holds
    gets the writer's number, and the page's own name stays free for it, so a page that holds
    its numbered name keeps it. A name that differs only in case is the same file on a case-blind
    volume, so it stays too.
    """
    from . import note

    if note_type is None or not renamed_by_title(path, note_type):
        return None
    current = PurePosixPath(path)
    stem = title_stem(root, path, note_type, title) or current.stem
    parent = current.parent.as_posix()
    # `project` is the closed partition value of a type whose folder its project splits.
    if project is not None and note_types.compiled(note_type) and note_type.partition == "project":
        planned = note.planned_destination(
            root,
            note_type=note_type.key,
            title=title or stem,
            project=project,
            project_registry=registry,
        )
        parent = PurePosixPath(planned).parent.as_posix()
    target = f"{parent}/{stem}{current.suffix}"
    if vault.vault_name_key(target) == vault.vault_name_key(path):
        return None
    free = vault.unique_path(
        root / parent,
        stem,
        current.suffix,
        vault_root=root,
        # A stem that an earlier page of this derivation takes counts as held, as a live title
        # does. The page's own title does not: its bare links already reach it.
        title_held=lambda candidate: vault.vault_name_key(f"{parent}/{candidate}{current.suffix}")
        in names.claimed
        or (names.titles is not None and names.titles.held(candidate, excluding=path)),
        excluding=path,
        folders=names.folders,
    )
    target = free.relative_to(root).as_posix()
    if vault.vault_name_key(target) == vault.vault_name_key(path):
        return None
    names.claimed.add(vault.vault_name_key(target))
    return target


def _source_corrections(
    root: Path,
    relocated: Mapping[str, tuple[_Page, frozenset[str]]],
    selected: set[str],
    to_project: str,
    carrying: Sequence[str],
    lists: frozenset[str],
    why: str,
    derived: _Derived,
    *,
    snapshot: Callable[[], Any],
    held: set[str],
) -> list[dict[str, Any]]:
    """A pointer correction for each Source that a relocated page cites and whose keys change.

    A Source in `held` was refused by its leaf at an earlier preparation: it keeps its keys.
    """
    from . import source_closure

    resolver = snapshot()
    cited: dict[str, dict[str, Any]] = {}
    for path, (page, dropped) in relocated.items():
        try:
            inspection = source_closure.inspect_source_closure(root, page.text, resolver=resolver)
        except (OSError, ValueError, OpError) as error:
            derived.unchecked.append({"page": path, "reason": str(error)})
            continue
        for item in inspection.resolved:
            if item.path in selected or item.path in held:
                continue
            entry = cited.setdefault(item.path, {"text": item.source, "dropped": set()})
            entry["dropped"] |= dropped
    if not cited:
        return []
    # A citer may still link a Source by an earlier path, which resolves to it through its lineage.
    priors = {path: vault.lineage_link_targets(root, path) for path in cited}
    inbound = vault.find_inbound_wikilinks_many(
        root, list(dict.fromkeys([*cited, *(prior for found in priors.values() for prior in found)]))
    )
    closure_fields = [
        name
        for name, writer in page_fields.writers(root).items()
        if writer == page_fields.WRITER_CLOSURE
    ]
    keys_of: dict[str, set[str] | None] = {}

    def keys(citer: str) -> set[str] | None:
        """The project keys a citer carries, or None when it cannot be read (it then keeps all)."""
        if citer not in keys_of:
            try:
                _path, text = curation._read_page(root, citer)  # noqa: SLF001
            except curation.CurationError:
                keys_of[citer] = None
            else:
                front = vault.parse_frontmatter(text)[0]
                keys_of[citer] = {
                    key
                    for value in page_fields.carried(root, front, _CARRIES).values()
                    for key in page_fields.carried_keys(value)
                }
        return keys_of[citer]

    corrections: list[dict[str, Any]] = []
    for path, entry in cited.items():
        front = vault.parse_frontmatter(entry["text"])[0]
        citers = {
            link.path for target in (path, *priors[path]) for link in inbound.get(target, ())
        }
        for name in closure_fields:
            for value in page_fields.carried_keys(front.get(name)):
                located = source_closure.locate(root, value, resolver=resolver)
                if located is not None:
                    citers.add(located)
        outside = citers - set(relocated) - {path}
        still_held: set[str] = set()
        for citer in outside:
            carried = keys(citer)
            still_held |= set(entry["dropped"]) if carried is None else carried
        dropped = frozenset(key for key in entry["dropped"] if key not in still_held)
        values = page_fields.carried(root, front, _CARRIES)
        before = {key for value in values.values() for key in page_fields.carried_keys(value)}
        rewritten = _rewritten(values, carrying, lists, drop=dropped, add=to_project)
        if isinstance(rewritten, str):
            derived.refused.append(_refusal(
                path, rewritten,
                f"the Source keeps another project in a single-value field and has no list to add "
                f"{to_project!r} to",
            ))
            continue
        if len(rewritten) > 1:
            derived.refused.append(_refusal(
                path, "RELOCATE_MULTIPLE_FIELDS",
                f"the Source needs {len(rewritten)} fields corrected; correct them one at a time",
            ))
            continue
        kept = sorted((set(entry["dropped"]) - dropped) & before)
        if kept:
            derived.keeping.append(
                {"path": path, "keeps": kept, "other_citers": len(outside)}
            )
        if not rewritten:
            continue
        if to_project not in before:
            derived.gaining.append(path)
        for key in sorted(dropped & before):
            derived.losing.append({"path": path, "key": key})
        ((name, value),) = rewritten.items()
        page = _Page(path, entry["text"], front, None)
        corrections.append({"page": path, "args": _correction(page, name, value, why)})
    return corrections


def _title(derived: _Derived, to_project: str | None) -> str:
    pages = len(derived.moves) + len(derived.corrected)
    if to_project is None:
        return f"Rename {pages} pages to their titles"
    return f"Relocate {pages} pages to project {to_project}"


def _summary(
    derived: _Derived,
    *,
    to_project: str | None,
    registers: bool,
    referrers: int = 0,
) -> dict[str, Any]:
    """The grouped summary the person approves: never the plan itself, which may be large."""
    summary: dict[str, Any] = {
        "moves": curation.summary_group(derived.moves),
        "corrected_in_place": curation.summary_group(derived.corrected),
        "sources_gaining_key": curation.summary_group(derived.gaining),
        "sources_losing_key": curation.summary_group(derived.losing),
        "sources_keeping_old_key": curation.summary_group(derived.keeping),
        "referrers_rewritten": referrers,
        "unchanged": curation.summary_group(derived.unchanged),
        "refused": curation.summary_group(derived.refused),
    }
    if derived.unchecked:
        summary["sources_unchecked"] = curation.summary_group(derived.unchecked)
    if derived.deferred:
        summary["deferred_to_next_batch"] = curation.summary_group(derived.deferred)
    if derived.mentions_unchecked:
        from . import path_mentions

        summary["warnings"] = [path_mentions.NOT_CHECKED]
    if registers:
        summary["registers_project"] = {
            "key": to_project,
            "note": "The first move registers this project key. An undo keeps the key registered.",
        }
    return summary
