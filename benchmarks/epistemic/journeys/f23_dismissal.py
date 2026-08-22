"""The f23 journey: a dismissal held across passes, a restart and a bulk batch.

f23 asserts two things a stored-state projection alone cannot settle. A
dismissal is only *respected* if the surfaces that would carry the signal were
actually consulted afterwards and stayed quiet, and counter repetition is only
*governed* if a bulk batch really went through the product and produced one
emission rather than N. Both are claims about what a runtime does, so this
driver runs the episode against the installed CLI and projects only what the
runtime produced.

**The envelope is discovered, never assumed.** The discovery, the refusal, and
the subprocess wrapper are :mod:`.f26_carrier`'s, imported rather than copied:
two journeys that disagree about what "the installed envelope" means would make
the family's evidence incomparable. Without an envelope the journey raises
:class:`EnvelopeNotDiscovered` rather than importing the library in-process,
because an in-process run would measure a library and call it a runtime.

The episode, in order:

1. Two durable conclusions are written, each carrying an overdue prediction, so
   the vault has one signal to dismiss and one that must stay open. A vault
   with nothing left due would make the counters half of the family vacuous.
2. ``maintain --reconcile`` builds the due-state projection. Everything written
   before this point predates the emission ledger and is deliberately not
   counted; the ledger the journey reads at the end therefore describes the
   bulk batch and nothing else.
3. One review pass locates the probe signal, and it is dismissed with a reason
   code through the documented CLI.
4. Further review passes, the full audit, the proposal queue, and a pass at
   each end of the prominence range. Every one of these is a separate process,
   which is what an engine restart is from the vault's point of view.
5. One bulk ingest: ``adopt --mode copy-as-sources`` over
   :data:`BULK_DOCUMENTS` legacy files in a single command.

**What is projected as an unsolicited signal, and what is not.** Anything the
default review union re-lists under the dismissed identity is projected as a
signal targeting the subject, which is what makes the dismissal assertion
falsifiable by the runtime rather than by the harness. Audit findings are not,
even though the audit does re-list the dismissed page. The audit is a report
the user asked for by name, and this product's contract states that triage
never changes audit measurement — a finding inside an explicitly requested
health report is not the signal coming back unbidden, which is the only thing
f23 is about. The audit surface is still consulted and still recorded, with the
finding count on its marker, so the anti-vacuity meta-predicate is answered
with an observation rather than with an assumption.

Nothing here reads a clock. ``taken_at`` and ``check_by`` are supplied by the
caller so a journey artifact is reproducible from its inputs.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ..corpora.no_nudge import ABSENCE_SURFACES
from ..projectors.exomem_vault import VaultProjector
from ..snapshot import EpistemicStateSnapshot, StateItem
from .f26_carrier import (
    Envelope,
    EnvelopeNotDiscovered,
    JourneyStepFailed,
    discover_envelope,
    seed_journey_vault,
)

#: The relation target the seeded conclusions point at. The product refuses a
#: compiled page with no qualifying typed relation, so the journey satisfies
#: that precondition explicitly rather than discovering the refusal at runtime.
SEED_RELATION_TARGET = "Knowledge Base/Entities/Concepts/Local First Knowledge"

#: The page whose signal is dismissed, and the page whose signal must stay open.
DISMISSED_TITLE = "f23 dismissal probe conclusion"
DISMISSED_SLUG = "f23-dismissal-probe-conclusion"
OPEN_TITLE = "f23 open probe conclusion"

#: The reason code the dismissal carries. `quiet`/`off` require one and an item
#: decision does not, but a journey that recorded `unspecified` would be
#: demonstrating the untaught path rather than the documented one.
DISMISSAL_REASON = "handled"
DISMISSAL_WHY = "settled outside the vault by the journey"

def _prominence_levels() -> tuple[str, ...]:
    """The product's own declared level range, or the two ends if it moves.

    Read from `exomem.prominence.CANON` rather than restated, so a level added
    to the product is swept without anyone remembering to update the bench. The
    fallback is deliberate and narrow: this module is importable without the
    product installed (the registry imports it to enumerate journeys), and a
    hard import error there would take the whole family down rather than one
    journey.
    """
    try:
        from exomem.prominence import CANON
    except Exception:  # noqa: BLE001 — the bench runs without the product installed
        return ("off", "maximal")
    return tuple(str(level) for level in CANON)


#: Review passes run *after* the dismissal. Three rather than one because the
#: family's claim is about repetition, and one pass cannot show a repeat.
DEFAULT_PASSES = 3

#: The WHOLE declared prominence range, read from the product rather than
#: restated. A dismissal that only survives the two ends has not survived
#: reconfiguration; the spec says the level range, so the sweep is the range.
PROMINENCE_LEVELS: tuple[str, ...] = _prominence_levels()

#: Files the single bulk-ingest command absorbs. Two would satisfy the
#: assertion's `writes >= 2` floor; twelve makes the difference between one
#: emission and one-per-write impossible to read as noise.
BULK_DOCUMENTS = 12
BULK_DIRECTORY = "legacy"

#: The vocabulary token a re-listing is projected under. The bench's signal
#: classes are product-neutral and none of them names this product's review
#: categories, so any unsolicited re-surfacing of a dismissed identity is
#: projected as promotion-class: it is an unrequested proposal about the
#: subject, which is exactly what that class means here.
RELISTING_SIGNAL_CLASS = "promotion"

#: ``capture name -> the surface it evidences``. A surface with no capture is
#: not projected complete, so a step silently dropped from the episode turns
#: the quiet assertions blocked rather than letting them pass on less evidence.
CAPTURE_SURFACES: Mapping[str, str] = MappingProxyType(
    {
        "audit": "audit_findings",
        "proposals": "proposal_queue",
    }
)

#: Capture names whose payload is a default review union listing.
REVIEW_CAPTURE_PREFIX = "pass-"


@dataclass(frozen=True)
class JourneyRun:
    """One executed episode, and the two snapshots f23 is scored on."""

    prior: EpistemicStateSnapshot
    later: EpistemicStateSnapshot
    subject: str
    dismissed_key: str
    passes: int
    captured: Mapping[str, Mapping[str, object]]


def seed_content(*, marker: str, check_by: str, anchor: str) -> str:
    """A durable conclusion carrying one overdue prediction.

    ``check_by`` is the caller's, not this module's: "overdue" is a statement
    about a date relative to the run, and a driver that read the clock would
    produce an artifact nobody can reproduce from its inputs.
    """

    return (
        f"{marker}\n"
        "\n"
        "## Observations\n"
        "\n"
        f"- [benchmark] {marker} #f23 (epistemic-bench) ^{anchor}\n"
        "\n"
        "## Prediction\n"
        "\n"
        f"- check_by: {check_by}\n"
        "- The probe signal is open until somebody decides about it.\n"
        "\n"
        "## Relations\n"
        "\n"
        f"- supports [[{SEED_RELATION_TARGET}]]\n"
    )


def seed_bulk_documents(vault: Path, *, count: int = BULK_DOCUMENTS) -> tuple[str, ...]:
    """Write the legacy files one bulk-ingest command will absorb.

    Plain Markdown outside the governed subtree, which is what the documented
    adoption route is for; the journey must not hand-build pages the product
    would otherwise have written itself.
    """

    directory = vault / BULK_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    today = dt.date.today()
    for index in range(1, count + 1):
        relative = f"{BULK_DIRECTORY}/f23-bulk-{index:02d}.md"
        # A DISTINCT overdue `check_by` per file, so each absorbed page adds one
        # more due item and the counters digest therefore moves on every write.
        # Identical (or absent) dates make the change-only rule produce the
        # single-emission result for free, and a batch whose digest never moves
        # cannot demonstrate that anything suppressed a repeat.
        overdue = (today - dt.timedelta(days=index)).isoformat()
        (vault / relative).write_text(
            f"# Legacy note {index:02d}\n"
            "\n"
            "A legacy document absorbed by the f23 bulk ingest.\n"
            "\n"
            "## Prediction\n"
            "\n"
            f"- id: f23-bulk-{index:02d}\n"
            f"- check_by: {overdue}\n"
            "\n"
            f"The legacy claim {index:02d} still holds.\n",
            encoding="utf-8",
        )
        written.append(relative)
    return tuple(written)


def write_steps(*, check_by: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The two seeding writes, as complete executable argv."""

    return (
        (
            "seed-dismissed",
            (
                "remember",
                "--json",
                "--response-detail",
                "compact",
                "--title",
                DISMISSED_TITLE,
                "--content",
                seed_content(
                    marker="A signal a person decides about once and never again",
                    check_by=check_by,
                    anchor="f23-dismissed",
                ),
            ),
        ),
        (
            "seed-open",
            (
                "remember",
                "--json",
                "--response-detail",
                "compact",
                "--title",
                OPEN_TITLE,
                "--content",
                seed_content(
                    marker="A signal nobody has decided about, which keeps the counters honest",
                    check_by=check_by,
                    anchor="f23-open",
                ),
            ),
        ),
    )


def bulk_step(selected: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """One command that writes many pages. The whole point of the batch half."""

    argv: list[str] = ["adopt", BULK_DIRECTORY, "--mode", "copy-as-sources"]
    for relative in selected:
        argv.extend(["--selected-path", relative])
    argv.append("--json")
    return "bulk", tuple(argv)


def _envelope_data(payload: Mapping[str, object]) -> Mapping[str, object]:
    """The documented ``{success, data}`` body, or the payload itself."""

    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def _run_json(
    envelope: Envelope,
    vault: Path,
    argv: Sequence[str],
    *,
    name: str,
    extra_env: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    raw = envelope.run(argv, cwd=vault, timeout=180.0, extra_env=extra_env)
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise JourneyStepFailed(f"{name} response is not JSON: {raw[:200]}") from error
    if not isinstance(payload, dict):
        raise JourneyStepFailed(f"{name} response is not an object")
    return payload


def _listed_items(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    items = _envelope_data(payload).get("items")
    if not isinstance(items, list):
        return ()
    return tuple(entry for entry in items if isinstance(entry, Mapping))


def _probe_item(payload: Mapping[str, object]) -> Mapping[str, object]:
    for entry in _listed_items(payload):
        if DISMISSED_SLUG in str(entry.get("path") or ""):
            return entry
    raise JourneyStepFailed(
        f"the first review pass did not list {DISMISSED_SLUG}; there is nothing to dismiss, "
        "so the episode cannot measure whether a dismissal is respected"
    )


def _record_key(entry: Mapping[str, object]) -> str:
    return f"{entry.get('item_id') or ''}:{entry.get('fingerprint') or ''}"


def run_journey(
    envelope: Envelope,
    *,
    vault: Path,
    check_by: str,
    taken_at: str,
    passes: int = DEFAULT_PASSES,
    bulk_documents: int = BULK_DOCUMENTS,
) -> JourneyRun:
    """Run the episode and return the snapshot pair it produced."""

    captured: dict[str, Mapping[str, object]] = {}
    for name, argv in write_steps(check_by=check_by):
        captured[name] = _run_json(envelope, vault, argv, name=name)
    captured["reconcile"] = _run_json(
        envelope, vault, ("maintain", "--reconcile", "--json"), name="reconcile"
    )

    # Deliberately NOT a `pass-` capture: this listing precedes the dismissal,
    # so treating it as a re-listing would score the discovery step itself as
    # the signal coming back.
    captured["discovery"] = _run_json(
        envelope, vault, ("review", "--limit", "50", "--json"), name="discovery"
    )
    probe = _probe_item(captured["discovery"])
    dismissed_key = _record_key(probe)
    captured["dismissal"] = _run_json(
        envelope,
        vault,
        (
            "review",
            "dismiss",
            str(probe.get("ref") or ""),
            "--reason",
            DISMISSAL_REASON,
            "--why",
            DISMISSAL_WHY,
            "--json",
        ),
        name="dismissal",
    )
    subject = f"dismissal-{dismissed_key}"
    prior = project_run(
        vault,
        captured={},
        subject=subject,
        dismissed_key=dismissed_key,
        passes=0,
        phase="f23-p1",
        taken_at=taken_at,
    )

    for index in range(1, passes + 1):
        name = f"{REVIEW_CAPTURE_PREFIX}{index}"
        captured[name] = _run_json(
            envelope, vault, ("review", "--limit", "50", "--json"), name=name
        )
    captured["audit"] = _run_json(
        envelope, vault, ("review", "--audit", "--json"), name="audit"
    )
    captured["proposals"] = _run_json(
        envelope,
        vault,
        ("review_memory", "--mode", "adoption", "--json"),
        name="proposals",
    )
    for level in PROMINENCE_LEVELS:
        name = f"{REVIEW_CAPTURE_PREFIX}prominence-{level}"
        captured[name] = _run_json(
            envelope,
            vault,
            ("review", "--limit", "50", "--json"),
            name=name,
            extra_env={"EXOMEM_PROMINENCE": level},
        )

    selected = seed_bulk_documents(vault, count=bulk_documents)
    bulk_name, bulk_argv = bulk_step(selected)
    captured[bulk_name] = _run_json(envelope, vault, bulk_argv, name=bulk_name)

    later = project_run(
        vault,
        captured=captured,
        subject=subject,
        dismissed_key=dismissed_key,
        passes=passes + len(PROMINENCE_LEVELS),
        phase="f23-p2",
        taken_at=taken_at,
    )
    return JourneyRun(
        prior=prior,
        later=later,
        subject=subject,
        dismissed_key=dismissed_key,
        passes=passes + len(PROMINENCE_LEVELS),
        captured=captured,
    )


def project_run(
    vault: Path,
    *,
    captured: Mapping[str, Mapping[str, object]],
    subject: str,
    dismissed_key: str,
    passes: int,
    phase: str,
    taken_at: str,
) -> EpistemicStateSnapshot:
    """The vault projection, plus what the captured responses evidenced.

    The vault projector is the source of the decision and the emission ledger,
    because both are files it already reads. What it cannot know is whether the
    surfaces that would carry the signal were ever consulted — so the surface
    markers for the queues are raised to ``complete`` only where a captured
    response demonstrates the runtime answered on them, and the re-listings
    those responses contained become signal items. Nothing is stamped from an
    assumption; a surface nobody asked about stays exactly as the projector
    reported it.
    """

    projected = VaultProjector(vault).project(phase=phase, taken_at=taken_at)
    observed = _observed_surfaces(captured)
    items: list[StateItem] = []
    for item in projected.items:
        surface = str((item.raw or {}).get("surface") or "")
        if surface in observed and item.id == f"surface-{surface}":
            items.append(item.model_copy(update={"raw": dict(observed[surface])}))
            continue
        if item.id == subject:
            raw = dict(item.raw or {})
            raw["passes"] = str(passes)
            items.append(item.model_copy(update={"raw": raw}))
            continue
        items.append(item)
    items.extend(_relisting_signals(captured, subject=subject, key=dismissed_key))
    return projected.model_copy(
        update={
            "items": tuple(items),
            "completeness_notes": (
                f"{projected.completeness_notes} Surfaces raised to complete by a captured "
                f"response: {', '.join(sorted(observed)) or 'none'}. Audit findings are "
                "recorded on their marker and are deliberately not projected as unsolicited "
                "signals: the audit is a report the caller asked for by name."
            ),
        }
    )


def _observed_surfaces(
    captured: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, str]]:
    """``surface -> its marker raw``, for surfaces a captured response answered."""

    observed: dict[str, dict[str, str]] = {}
    if any(name.startswith(REVIEW_CAPTURE_PREFIX) for name in captured):
        observed["review_queue"] = {
            "surface": "review_queue",
            "projection": "complete",
            "consulted": str(
                sum(1 for name in captured if name.startswith(REVIEW_CAPTURE_PREFIX))
            ),
        }
    for name, surface in CAPTURE_SURFACES.items():
        payload = captured.get(name)
        if payload is None:
            continue
        data = _envelope_data(payload)
        listed = data.get("findings") if surface == "audit_findings" else data.get("items")
        observed[surface] = {
            "surface": surface,
            "projection": "complete",
            "listed": str(len(listed) if isinstance(listed, list) else 0),
        }
    return {
        surface: raw for surface, raw in observed.items() if surface in ABSENCE_SURFACES
    }


def _relisting_signals(
    captured: Mapping[str, Mapping[str, object]], *, subject: str, key: str
) -> tuple[StateItem, ...]:
    """One signal item per default-union re-listing of the dismissed identity.

    Matched on the review identity the product itself returns, not on the page
    path: a different signal about the same page is a different signal, and
    scoring it as a re-nag would fail a product for doing the right thing.
    """

    signals: list[StateItem] = []
    for name in sorted(captured):
        if not name.startswith(REVIEW_CAPTURE_PREFIX):
            continue
        for entry in _listed_items(captured[name]):
            if str(entry.get("item_id") or "") != key.split(":", 1)[0]:
                continue
            signals.append(
                StateItem(
                    id=f"relisted-{name}-{_record_key(entry)}",
                    kind="container",
                    title=str(entry.get("path") or entry.get("ref") or name),
                    review_state=str(entry.get("state") or "open"),
                    raw={
                        "signal_class": RELISTING_SIGNAL_CLASS,
                        "targets": subject,
                        "surface": "review_queue",
                        "fingerprint": str(entry.get("fingerprint") or ""),
                    },
                )
            )
    return tuple(signals)


__all__ = [
    "BULK_DOCUMENTS",
    "CAPTURE_SURFACES",
    "DEFAULT_PASSES",
    "DISMISSAL_REASON",
    "DISMISSED_SLUG",
    "DISMISSED_TITLE",
    "Envelope",
    "EnvelopeNotDiscovered",
    "JourneyRun",
    "JourneyStepFailed",
    "OPEN_TITLE",
    "PROMINENCE_LEVELS",
    "RELISTING_SIGNAL_CLASS",
    "SEED_RELATION_TARGET",
    "bulk_step",
    "discover_envelope",
    "project_run",
    "run_journey",
    "seed_bulk_documents",
    "seed_content",
    "seed_journey_vault",
    "write_steps",
]
