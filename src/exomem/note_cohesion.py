"""The note-cohesion review: a size step schedules one question for the writing agent.

A compiled page that grows keeps answering more questions until it answers several
independent ones. Whether it does is a judgement about meaning, and the connected agent
makes that judgement: it holds the page and runs on a frontier model. The runtime only
measures size and asks. It never decides what a page is about, never refuses a write,
and never moves anything.

This module owns the whole rule, so the audit check and the write path cannot drift:

- eligibility: a compiled, recall-eligible page that declares no breadth and is not a
  navigation file;
- the size steps, each one a point at which the question is asked once;
- the one question text;
- the finding composer, which both the audit check and the write path call.

The finding states counts and asks the question. It never claims the page is overloaded.
It carries no review partition, so one page is one item, and its `signal_version` is the
step, so a decision binds to the step: a dismissal holds while the page stays within it
and the item reopens when the page reaches the next one.

The write path reads the item's decision from the review-state store rather than from
the due-state projection. It composes the identity with `due_state.entry_for`, the same
composer the projection uses, because composing it differently makes a dismissal
recorded through one surface invisible to the other.

Reserved, not built: a future server-side reading may attach to an item as
`evidence_refs`, a list of `{reading_id, content_hash}`. A reading whose content hash no
longer matches the page is ignored. The item is complete without any reading and nothing
here produces or reads one.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import note_types, recall_policy, temporal
from .find_corpus import NAVIGATION_BASENAMES
from .structure_promotion import BREADTH_TAGS, _terms

if TYPE_CHECKING:
    from .audit import AuditFinding

log = logging.getLogger(__name__)

CATEGORY = "note_cohesion"

#: The `structure_suggestion` kind a compiled write carries while the item is open.
KIND = "note_cohesion"

#: The first step, in semantic units. PROVISIONAL: a cadence parameter, not vocabulary.
#: It is the size of the smallest note the owner confirmed as overloaded in the labelled
#: set, so a lower base would ask about notes no one has shown to need the question.
BASE_STEP_UNITS = 9

#: Each step is the previous one times this, rounded up: 9, 14, 21, 32, 48, 72, ...
#: PROVISIONAL: a cadence parameter, not vocabulary. It bounds how often a coherent page
#: is asked to about log(growth) (six times between 9 and 100 units).
STEP_FACTOR = 1.5

#: The question the writing agent receives. One text, used by the audit finding and the
#: write payload. It names no tool, because the tool names depend on the serving surface;
#: the write payload appends the keep-whole call spelled for that surface.
QUESTION = (
    "Answer this before you reply to the user: does this page answer more than one "
    "independent question? If it answers one, however long, keep it whole and record "
    "that with a dismissal whose reason starts `intentional:` and names the question. "
    "If several questions each have their own material, name them to the user and offer "
    "a split; on the user's yes, propose it through the curation lane and get their exact "
    "confirmation. If the page is a hub by design, add a breadth tag with a governed edit."
)


def step_for(units: int) -> tuple[int, int] | None:
    """``(step, next_step)`` for a unit count, or None below the first step."""
    if units < BASE_STEP_UNITS:
        return None
    step = BASE_STEP_UNITS
    while True:
        following = math.ceil(step * STEP_FACTOR)
        if units < following:
            return step, following
        step = following


def _breadth_declared(frontmatter: Mapping[Any, Any]) -> bool:
    """Whether the page's tags declare it broad on purpose (`structure_promotion.BREADTH_TAGS`)."""
    raw = frontmatter.get("tags")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return False
    return bool(_terms(str(tag) for tag in raw) & BREADTH_TAGS)


def is_candidate(
    rel_path: str,
    *,
    page_type: str | None,
    frontmatter: Mapping[Any, Any],
    type_basis: note_types.Basis,
) -> bool:
    """Whether a page may ever be asked, before its size is known. Pure; no I/O.

    Cheap on purpose: the audit check parses a page only when this holds.
    """
    return (
        type_basis.selects(page_type, note_types.compiled)
        and rel_path.rsplit("/", 1)[-1].casefold() not in NAVIGATION_BASENAMES
        and not _breadth_declared(frontmatter)
    )


def finding(
    vault_root: Path,
    rel_path: str,
    *,
    page_type: str | None,
    frontmatter: Mapping[Any, Any],
    units: int,
    type_basis: note_types.Basis,
) -> AuditFinding | None:
    """The page's `note_cohesion` finding, or None when it is not eligible or below a step."""
    from .audit import AuditFinding

    reached = step_for(units)
    if reached is None or not is_candidate(
        rel_path, page_type=page_type, frontmatter=frontmatter, type_basis=type_basis
    ):
        return None
    if not recall_policy.is_recall_candidate(vault_root, rel_path):
        return None
    step, next_step = reached
    authored = temporal.parse(frontmatter.get("updated") or frontmatter.get("created"))
    # A page with no authored date is due from the day this was computed. The caller's
    # `today` is no help: the projection scans with a far-future date.
    due_since = authored.day if authored is not None else dt.date.today()
    return AuditFinding(
        category=CATEGORY,
        severity="info",
        path=rel_path,
        detail=(
            f"This page holds {units} semantic units and has reached the size step of "
            f"{step} (next step {next_step}). {QUESTION}"
        ),
        meta={
            # The step, so a dismissal holds within it and the item reopens at the next.
            "signal_version": str(step),
            "units": units,
            "step_units": step,
            "next_step_units": next_step,
            "due_since": due_since.isoformat(),
        },
    )


def suggestion_for_state(vault_root: Path, state: Any) -> dict[str, Any] | None:
    """The `structure_suggestion` a committed compiled page carries, or None.

    Present only while the page's item is open: at a step, with no decision recorded
    for that step, and neither the `structural_suggestions` class nor this family
    withheld. Raises when the review state cannot be read; the write path treats a
    raise as "ask nothing", because asking on every write when decisions are
    unreadable would turn the question into noise.
    """
    from . import due_state, envelope, review_state

    basis = state.type_basis or note_types.Basis(vault_root)
    if not is_candidate(
        state.path, page_type=state.page_type, frontmatter=state.frontmatter, type_basis=basis
    ):
        return None
    units = len(state.document.units)
    if step_for(units) is None:
        return None
    if envelope.active().get("structural_suggestions") == "off":
        return None
    reading = finding(
        vault_root,
        state.path,
        page_type=state.page_type,
        frontmatter=state.frontmatter,
        units=units,
        type_basis=basis,
    )
    if reading is None:
        return None
    store = review_state.ReviewStateStore(vault_root)
    payload = store.load()
    if review_state.disposition_for(CATEGORY, payload=payload) != "normal":
        return None
    entry = due_state.entry_for(vault_root, reading)
    if entry is None:
        return None
    if store.effective_state(entry["item_id"], entry["fingerprint"], payload=payload)[0] != "open":
        return None
    meta = reading.meta
    return {
        "kind": KIND,
        "units": meta["units"],
        "step_units": meta["step_units"],
        "next_step_units": meta["next_step_units"],
        "ref": entry["ref"],
        "fingerprint": entry["fingerprint"],
        "question": f"{QUESTION} Keep whole: {_keep_whole_call(entry['ref'], entry['fingerprint'])}",
    }


def _keep_whole_call(ref: str, fingerprint: str) -> str:
    """The dismissal that records a keep-whole answer, spelled for the serving surface.

    Agents follow a call written out in a response far more reliably than a described
    one; the outcome check showed agents ignoring the described form.
    """
    from . import capabilities

    return capabilities.active_call_text(
        "triage_memory",
        {
            "ref": ref,
            "action": "dismiss",
            "expected_fingerprint": fingerprint,
            "why": "intentional: <the one question this page answers>",
        },
    )
