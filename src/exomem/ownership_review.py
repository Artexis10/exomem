"""The ownership review: ask the connected agent whether a page belongs in its current home.

A page's project is an agent's choice that nothing re-examines. Whether a page serves its
project's own goal is a judgement about meaning, and the connected agent makes it: it
holds the page and runs on a frontier model. The runtime only decides when to ask. It
never decides what a page is about, never claims that a page is misfiled, carries no
score, never refuses a write and never moves anything.

This module owns the family's rule, so the audit check and the write path cannot drift:

- eligibility: the pages `note_cohesion` may ask about, with one project key;
- the signal version, which binds a decision to the page's project key, home folder and
  title, so that body growth never reopens a decision and a change to any of the three
  always does;
- the question, whose test and deferral rule are read from the semantic authoring
  routes, so a better-worded test needs no code change;
- the finding composer, which both the audit check and the write path call.

When an item exists is `ownership_rotation`'s job: a page is asked when it is created in
a project, when its project key or title changes, and when the daily rotation reaches it.
A page that was never asked has no item.

The write path reads the item's decision from the review-state store and composes the
identity with `due_state.entry_for`, the same composer the projection uses, because
composing it differently makes a dismissal recorded through one surface invisible to the
other.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from . import find_corpus, note_cohesion, note_types, semantic_authoring

if TYPE_CHECKING:
    from . import lifecycle_statuses
    from .audit import AuditFinding

log = logging.getLogger(__name__)

CATEGORY = "ownership_review"

#: The `structure_suggestion` kind a write carries while the page's item is open. A closed
#: protocol token that `mutation_terminal` implements a projector for, not vault vocabulary.
KIND = "ownership"

#: The answers, in the words the connected agent follows. The ownership test and the
#: deferral rule are guidance in `semantic_authoring`; what stays here is the call
#: mechanics, which depend on the serving surface. `{keep}` is the dismissal that keeps the
#: page where it is.
ANSWERS = (
    "Do not change the page when you answer. "
    "Keep (the page serves the project's goal): {keep}. "
    "Abstain: dismiss with a reason that starts `deferred:`, or make no call. "
    "Move, new_project or split: tell the user, and on their yes propose it through the "
    "curation lane. Register a new project through the project-key route first, link the "
    "related projects' notes with a typed relation, and get the user's exact confirmation of "
    "the previewed plan."
)


class Subject(NamedTuple):
    """What a decision binds to: the page's project key, home folder and title."""

    project: str
    folder: str
    title: str


def project_key(frontmatter: Mapping[Any, Any]) -> str | None:
    """The page's one project key, or None when it names none or several.

    A page that names several projects has no single home to question, and the project
    key is the home the question is about.
    """
    keys = {key.strip() for key in find_corpus.all_projects(dict(frontmatter))}
    keys.discard("")
    return next(iter(keys)) if len(keys) == 1 else None


def folder_of(rel_path: str) -> str:
    """The page's home folder: its vault-relative parent directory."""
    return rel_path.rpartition("/")[0]


def signal_version(subject: Subject) -> str:
    """The identity a decision binds to. Changes when the project key, folder or title does."""
    encoded = "\0".join(subject).encode("utf-8", "surrogatepass")
    return hashlib.sha256(encoded).hexdigest()[:16]


def is_candidate(
    rel_path: str,
    *,
    page_type: str | None,
    frontmatter: Mapping[Any, Any],
    type_basis: note_types.Basis,
) -> bool:
    """Whether a page may ever be asked: the cohesion candidates that carry a project key.

    Pure; no I/O. Sources and Evidence are not compiled pages, and a hub or snapshot spans
    projects by design, so neither is asked. Reuses `note_cohesion`'s rule so that both
    families ask about the same pages.
    """
    return project_key(frontmatter) is not None and note_cohesion.is_candidate(
        rel_path, page_type=page_type, frontmatter=frontmatter, type_basis=type_basis
    )


def subject_of(
    vault_root: Path,
    rel_path: str,
    *,
    page_type: str | None,
    frontmatter: Mapping[Any, Any],
    title: str,
    type_basis: note_types.Basis,
    status_basis: lifecycle_statuses.Basis | None = None,
) -> Subject | None:
    """The page's subject, or None when the page is not eligible to be asked."""
    if not is_candidate(
        rel_path, page_type=page_type, frontmatter=frontmatter, type_basis=type_basis
    ) or not note_cohesion.is_askable(
        vault_root, rel_path, frontmatter=frontmatter, status_basis=status_basis
    ):
        return None
    project = project_key(frontmatter)
    return Subject(project, folder_of(rel_path), title) if project is not None else None


def question(*, keep: str) -> str:
    """The question text: lead, ownership test, deferral rule, then the answers.

    The first three are guidance read from the served contract, so the item detail and the
    write-time question carry exactly what a client reads in the bootstrap.
    """
    routes = semantic_authoring.AUTHORING_CONTRACT.routes
    return " ".join(
        (
            routes["ownership_question"],
            routes["ownership_test"],
            routes["ownership_deferral"],
            ANSWERS.format(keep=keep),
        )
    )


#: How many placing steps a plan question names before it counts the rest. A plan holds at most
#: 64 steps, and the question stays under the 400-token bound that gate 5 measures.
PLAN_NAMED_STEPS = 12

PLAN_ANSWERS = (
    "This plan places notes with these projects, by step id: {named}. "
    "Before the user confirms it, check each step's project against the ownership test. "
    "If a step's project is wrong, propose a corrected plan; the question changes no step."
)


def plan_question(vault_root: Path, steps: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The one ownership question a curation preview carries, or None.

    A `create-note` step places a new note and a `move` step with a `project` field
    reassigns one. Each such step for a compiled note is named by its step id with the
    project it assigns, so a split's children are asked about together, once. Reads the
    plan's steps and each moved page's frontmatter; it never changes the plan, so the
    plan fingerprint is the same with or without the question.
    """
    from . import promotions
    from . import vault as vault_module

    basis = note_types.Basis(vault_root)
    placed: list[tuple[str, str]] = []
    for step in steps:
        args = step.get("args") or {}
        kind = step.get("kind")
        if kind == "create-note":
            note_type = args.get("note_type")
            project = project_key(args)
            compiled = note_type is None or basis.selects(note_type, note_types.compiled)
            if project is not None and compiled:
                placed.append((str(step["step_id"]), project))
        elif kind == "move":
            # A page the caller may not see is never read: its project would reach the caller
            # through the question. The preview already answers a withheld page as a missing one.
            if not promotions.page_visible(vault_root, str(args["old_path"])):
                continue
            try:
                text = (vault_root / str(args["old_path"])).read_text(encoding="utf-8")
                frontmatter = vault_module.parse_frontmatter(text)[0]
            except (OSError, ValueError, KeyError):
                continue
            if not note_cohesion.is_candidate(
                str(args["old_path"]),
                page_type=frontmatter.get("type"),
                frontmatter=frontmatter,
                type_basis=basis,
            ):
                continue
            fields = args.get("fields") or {}
            reassigns = bool({"project", "projects"} & fields.keys())
            assigned = project_key(fields if reassigns else frontmatter)
            if assigned is not None:
                placed.append((str(step["step_id"]), assigned))
    if not placed:
        return None
    named = ", ".join(f"{step_id} ({project})" for step_id, project in placed[:PLAN_NAMED_STEPS])
    if len(placed) > PLAN_NAMED_STEPS:
        named += f", and {len(placed) - PLAN_NAMED_STEPS} more"
    routes = semantic_authoring.AUTHORING_CONTRACT.routes
    return {
        "kind": KIND,
        "steps": [{"step_id": step_id, "project": project} for step_id, project in placed],
        "question": " ".join(
            (
                routes["ownership_question"],
                routes["ownership_test"],
                routes["ownership_deferral"],
                PLAN_ANSWERS.format(named=named),
            )
        ),
    }


def preview_question(vault_root: Path, steps: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The question a curation preview carries, or None. Never raises.

    A quiet ownership family or `structural_suggestions: off` withholds it, as on a write. A
    failure to read the review state or a page asks nothing: the question must not block a
    preview, so the preview arrives without it.
    """
    from . import envelope, review_state

    try:
        if envelope.active().get("structural_suggestions") == "off":
            return None
        payload = review_state.ReviewStateStore(vault_root).load_shared()
        if review_state.disposition_for(CATEGORY, payload=payload) != "normal":
            return None
        return plan_question(vault_root, steps)
    except Exception:  # noqa: BLE001 -- the preview never fails for an optional question
        log.warning("ownership question left out of a preview", exc_info=True)
        return None


def keep_call(item_id: str = "<id>") -> str:
    """The dismissal that records a keep answer, spelled for the serving surface.

    Agents follow a call written out in a response far more reliably than a described one.
    """
    from . import capabilities

    return capabilities.active_call_text(
        "triage_memory",
        {
            "item_id": item_id,
            "action": "dismiss",
            "why": "intentional: <the project goal this page serves>",
        },
    )


def finding(
    rel_path: str,
    *,
    subject: Subject,
    asked_on: dt.date,
    keep: str | None = None,
) -> AuditFinding:
    """The page's `ownership_review` finding. The caller has proved the page eligible.

    The detail asks and never claims the page is misfiled. It is due from the day the page
    was asked about, because nothing about the page's own dates says when the question
    became worth asking. `keep` lets a bulk caller spell the call once for every page.
    """
    from .audit import AuditFinding

    return AuditFinding(
        category=CATEGORY,
        severity="info",
        path=rel_path,
        detail=question(keep=keep if keep is not None else keep_call()),
        meta={
            # Project key, folder and title: a dismissal holds while all three stay and
            # the item reopens when any changes. The body is deliberately not in it.
            "signal_version": signal_version(subject),
            # One item per page with its own id, so a decision on another page-level
            # family never settles this question, and the reverse.
            "review_partition": CATEGORY,
            "project": subject.project,
            "home_folder": subject.folder,
            "due_since": asked_on.isoformat(),
        },
    )


def suggestion_after_write(
    vault_root: Path,
    state: Any,
    *,
    created: bool = False,
    previous: Any = None,
) -> dict[str, Any] | None:
    """The `structure_suggestion` a committed page carries, or None. May raise.

    Opens the page's item when this write is a trigger: a creation, or an edit that changed
    the project key or the title (`previous` is the page before it). The ledger append
    carries no vector and scans no project. Then returns the question while the item is
    open: asked, with no decision recorded for its signal, and neither the
    `structural_suggestions` class nor this family withheld. The write path treats a raise
    as "ask nothing", because asking on every write when decisions are unreadable would
    turn the question into noise.
    """
    from . import due_state, envelope, ownership_rotation, review_ids, review_state

    basis = state.type_basis or note_types.Basis(vault_root)
    subject = subject_of(
        vault_root,
        state.path,
        page_type=state.page_type,
        frontmatter=state.frontmatter,
        title=state.title,
        type_basis=basis,
    )
    if subject is None:
        return None
    key = ownership_rotation.page_key(state.path, state.frontmatter)
    if created or (
        previous is not None
        and (project_key(previous.frontmatter), previous.title)
        != (subject.project, subject.title)
    ):
        ownership_rotation.mark_asked(vault_root, [key], on=dt.date.today())
    asked_on = ownership_rotation.asked(vault_root).get(key)
    if asked_on is None:
        return None
    if envelope.active().get("structural_suggestions") == "off":
        return None
    store = review_state.ReviewStateStore(vault_root)
    payload = store.load_shared()
    if review_state.disposition_for(CATEGORY, payload=payload) != "normal":
        return None
    reading = finding(state.path, subject=subject, asked_on=asked_on)
    entry = due_state.entry_for(vault_root, reading)
    if entry is None:
        return None
    if store.effective_state(entry["item_id"], entry["fingerprint"], payload=payload)[0] != "open":
        return None
    ref, fingerprint = entry["ref"], entry["fingerprint"]
    return {
        "kind": KIND,
        "ref": ref,
        "fingerprint": fingerprint,
        "question": question(
            keep=keep_call(review_ids.item_id({"ref": ref, "fingerprint": fingerprint}))
        ),
    }
