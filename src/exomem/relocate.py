"""Relocate a set of pages to a project and to the names their titles give, as one sealed plan.

The caller names the pages, or one review item. This module derives the whole repair, reading
only, and seals it through the curation lane:

- a page whose path changes gets a `move`, and its `fields` patch rewrites the page's project;
- a page that stays where it is gets a pointer correction of its project;
- each Source that a relocated page cites gains the new project key. It loses the old key only
  when no page outside the set that still carries that key cites it;
- the `repoint-links` steps come last and rewrite every link to a moved page. A plan with more
  referrers than one step rewrites in a short hold splits them by referring page.

The plan is sealed with `derived_by="relocate"`. The person approves one fingerprint, apply runs
the steps under the request budget, and one compensation plan undoes them. The page-field
registry's `carries` attribute says which fields hold project keys. The note writer's own
placement says where a page lands and what it is named (`note.planned_destination`,
`vault.resolve_filename_slug`). Nothing here branches on a vocabulary entry.
"""

from __future__ import annotations

import dataclasses
import functools
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

_CARRIES = page_fields.CARRIES_PROJECT_KEY


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


def propose(
    vault_root: Path,
    *,
    pages: Sequence[str] | None = None,
    review_ref: str | None = None,
    to_project: str | None = None,
    folder: str | None = None,
    from_project: str | None = None,
) -> dict[str, Any]:
    """Derive and seal the plan that relocates these pages, and answer with its grouped summary."""
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
    for _attempt in range(MAX_EXCLUSIONS + 1):
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
        if not derived.steps:
            summary = _summary(derived, to_project=to_project, registers=False)
            return {"action": "relocate", "mutated": False, "sealed": False, "summary": summary}
        writes_key = registers and any(
            step["kind"] == "edit" or "fields" in step["args"] for step in derived.steps
        )
        if writes_key and folder and not any("project_folders" in step["args"] for step in derived.steps):
            raise _error(
                "RELOCATE_FOLDER_NEEDS_MOVE",
                f"no page moves into project {to_project!r}, so no step can register its folder; "
                "omit folder to register the default one",
            )
        plan = {"version": 1, "title": _title(derived, to_project), "steps": derived.steps}
        try:
            prepared = curation.prepare_proposal(root, plan, derived_by="relocate")
        except curation.CurationError as error:
            page = derived.step_pages.get(error.step_id or "")
            if page is None or _attempt == MAX_EXCLUSIONS:
                raise
            refused.append({"page": page, "code": error.code, "reason": error.reason})
            if error.step_id in derived.source_steps:
                held_sources.add(page)
            else:
                selected = [item for item in selected if item.path != page]
            continue
        summary = _summary(
            derived,
            to_project=to_project,
            registers=writes_key,
            referrers=sum(
                curation.rewritten_referrers(step, binding)
                for step, binding in zip(prepared.plan["steps"], prepared.binding_manifest, strict=True)
            ),
        )
        # The plan seals the summary it is approved by, so a later preview shows the same groups.
        sealed = curation.seal_proposal(root, dataclasses.replace(prepared, summary=summary))
        response = {key: value for key, value in sealed.items() if key != "plan"}
        return {
            **response,
            "action": "relocate",
            "sealed": True,
            "steps": len(derived.steps),
            "summary": summary,
            "next_action": "apply",
        }
    raise AssertionError("unreachable: the last attempt returns or raises")


def _requested(root: Path, pages: Sequence[str] | None, review_ref: str | None) -> list[str]:
    if (pages is None) == (review_ref is None):
        raise _error("INVALID_RELOCATION", "a relocation names pages or one review_ref, not both")
    if review_ref is not None:
        anchor = curation._page_of_review_item(root, str(review_ref))  # noqa: SLF001
        if anchor is None:
            raise _error(
                "RELOCATE_REVIEW_ITEM_UNKNOWN", "review_ref names no current review item on a page"
            )
        return [anchor]
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
    from . import find, title_occupancy

    derived = _Derived()
    carrying = page_fields.fields_carrying(root, _CARRIES)
    lists = page_fields.list_fields(root)
    # The one writer snapshot that the title check and the Source rule read.
    snapshot = functools.cache(lambda: find.writer_resolver_snapshot(root))
    try:
        titles: title_occupancy.TitleCheck | None = title_occupancy.TitleCheck(
            root, None, resolver=snapshot
        )
    except ValueError:
        # The vault's naming style cannot be read; each page's own naming then refuses it.
        titles = None
    moves: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    #: Each relocated page and the keys it gives up, for the Source rule.
    relocated: dict[str, tuple[_Page, frozenset[str]]] = {}
    claimed: set[str] = set()
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
            target = _destination(
                root, page, to_project if alone == {to_project} else None, registry, claimed, titles
            )
        except (OpError, ValueError) as error:
            derived.refused.append(_refusal(page.path, "RELOCATE_NO_DESTINATION", str(error)))
            continue
        if target is None and len(changes) > 1:
            derived.refused.append(_refusal(
                page.path, "RELOCATE_MULTIPLE_FIELDS",
                f"the page needs {len(changes)} fields corrected in place; correct them one at a time",
            ))
            continue
        if target is not None:
            claimed.add(vault.vault_name_key(target))
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


def _destination(
    root: Path,
    page: _Page,
    project: str | None,
    registry: project_keys.ProjectRegistry | None,
    claimed: set[str],
    titles: Any,
) -> str | None:
    """Where the note writer would put this compiled page now, or None when it stays.

    The name is the one the vault's writer gives the page's title, after the date prefix that a
    dated type keeps. The folder changes only for a type that a project partitions, and only when
    the page belongs to `project` alone afterwards. Entities and Sources never move. A name that
    a file, a moved page's lineage, an earlier move of this plan or another live page's title
    holds gets the writer's number, so the page never takes over another page's bare links.
    """
    from . import layout, note

    definition = page.note_type
    if definition is None or not note_types.compiled(definition):
        return None
    current = PurePosixPath(page.path)
    title = page.title
    stem = current.stem
    if title:
        # `slug` is the closed stem kind that builds a name from the slug alone, with no date.
        prefix = layout.date_prefix(current.stem) if definition.stem != "slug" else ""
        name, _warnings = vault.resolve_filename_slug(
            title, None, vault_root=root, date_prefix=prefix
        )
        if name:
            stem = prefix + name
    parent = current.parent.as_posix()
    # `project` is the closed partition value of a type whose folder its project splits.
    if project is not None and definition.partition == "project":
        planned = note.planned_destination(
            root,
            note_type=definition.key,
            title=title or stem,
            project=project,
            project_registry=registry,
        )
        parent = PurePosixPath(planned).parent.as_posix()
    target = f"{parent}/{stem}{current.suffix}"
    # A name that differs only in case is the same file on a case-blind volume: it stays.
    if vault.vault_name_key(target) == vault.vault_name_key(page.path):
        return None
    free = vault.unique_path(
        root / parent,
        stem,
        current.suffix,
        vault_root=root,
        # A stem that an earlier move of this plan takes counts as held, as a live title does. The
        # page's own title does not: its bare links already reach it.
        title_held=lambda candidate: vault.vault_name_key(f"{parent}/{candidate}{current.suffix}")
        in claimed
        or (titles is not None and titles.held(candidate, excluding=page.path)),
    )
    target = free.relative_to(root).as_posix()
    return None if vault.vault_name_key(target) == vault.vault_name_key(page.path) else target


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
    if derived.mentions_unchecked:
        from . import path_mentions

        summary["warnings"] = [path_mentions.NOT_CHECKED]
    if registers:
        summary["registers_project"] = {
            "key": to_project,
            "note": "The first move registers this project key. An undo keeps the key registered.",
        }
    return summary
