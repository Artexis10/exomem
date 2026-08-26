"""The f27 replay corpus: an authored episode, and the end-state an expert leaves.

Three properties make this corpus a yardstick rather than a description of a run.

1. **The expectation is a fold, never an observation.** Every turn carries the
   consequences an expert lands after it, in three tiers — ``intent`` (a plan
   item filed from stated intent), ``outcome`` (a record appended from an
   observed event), ``transition`` (an open item's status changed because of an
   outcome) — or nothing at all. :class:`ExpectedEndState` is computed from those
   annotations, so no agent's output can move the target it is compared against.

2. **Turns that land nothing are part of the corpus on purpose.** A tentative
   claim, an elapsed-time remark and a deferral each annotate ``none``, because
   the shipped contract says a tentative claim is never an event and elapsed time
   is never an outcome. A family with only positive turns would score a product
   that wrote something for every utterance as perfect.

3. **No turn names the store or the act of storing.** :data:`STORE_BEARING_RE`
   pins that vocabulary and :func:`assert_no_store_bearing_utterance` runs at
   corpus construction *and* at scenario load. The sibling regex is
   ``_KB_BEARING_RE`` in ``src/exomem/_hooks/exomem_retrieve_nudge.py`` (line 83),
   and it is deliberately **not** reused: it matches ``earlier``, ``previous``,
   ``history`` and ``decision``, which are exactly the ordinary working language
   this family exists to measure. Borrowing it would have refused the corpus for
   being what it is meant to be.

The vocabulary is a generic batch-production workstream — deliverables and the
events that report them — and the scaffold no-leak rule applies to this file.
Nothing here reads a clock, a network or a provider internal, so the corpus is a
pure function of its arguments and a digest over it is stable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType, ModuleType

#: The corpus a scenario names in its expectation ``subject``. The assertions
#: read the fold by this id; they never read the transcript.
CORPUS_ID = "f27-lifecycle-replay-v1"

#: The three tiers a consequence can belong to, in reporting order.
TIERS: tuple[str, ...] = ("intent", "outcome", "transition")

#: The event vocabulary the workstream reports itself in.
EVENT_TYPES: tuple[str, ...] = (
    "produced",
    "approved",
    "delivered",
    "published",
    "rejected",
    "redo-needed",
)

#: The slice-1 collection identities, mirrored rather than imported: the shipped
#: fixture lives under ``tests/`` and a benchmark that imported a test module
#: would make the bench depend on the suite it is measured beside.
PLANNING_COLLECTION_ID = "3f5b6d21-9c4e-4a77-9c2f-6d0b1e2a5c48"
RECORDS_COLLECTION_ID = "5a2c7e10-4b8d-4f31-8a90-1c7e4d9b6f22"
PLANNING_PATH = "Knowledge Base/Planning/Delivery/_collection.md"
RECORDS_PATH = "Knowledge Base/Records/Deliveries/_collection.md"

#: The two collections the seeded vault ships. Anything else in the projected
#: ``collections`` section was created by the replay, which is an extra.
SEEDED_COLLECTION_IDS: frozenset[str] = frozenset(
    {PLANNING_COLLECTION_ID, RECORDS_COLLECTION_ID}
)

#: The parent chain a committed work item hangs from. These are plan items the
#: *seed* wrote, so they are neither expected consequences nor extras.
OUTCOME_TITLE = "Delivery outcome"
INITIATIVE_TITLE = "Delivery initiative"
SEEDED_PLAN_TITLES: tuple[str, ...] = (OUTCOME_TITLE, INITIATIVE_TITLE)

#: The status a work item is filed at, and the statuses the product's own state
#: machine admits. Restated here because the fold has to decide whether a status
#: it did not assign is an extra, and "the product allowed it" is not the test.
FILED_STATUS = "planned"


class StoreBearingUtterance(ValueError):
    """A user turn names the store, or the act of storing. Load-time refusal."""


class CorpusLookupError(LookupError):
    """A scenario named a corpus this module does not define."""


#: The store, and the act of storing. Narrower than the retrieve-nudge's
#: ``_KB_BEARING_RE`` by design — see the module docstring.
STORE_BEARING_RE = re.compile(
    r"\b("
    r"exomem|kb|knowledge\s+base|planning|plan\s+items?|records?|recorded|recording"
    r"|saves?|saved|saving|stores?|stored|storing|tracks?|tracked|tracking"
    r"|remember(?:s|ed|ing)?|captures?|captured|capturing"
    r"|log\s+(?:it|this|that)"
    r"|notes?\s+(?:it|this|that)(?:\s+down)?"
    r"|writes?\s+(?:it|this|that)\s+down"
    r"|files?\s+(?:it|this|that)"
    r")\b",
    re.IGNORECASE,
)


def normalize(value: str) -> str:
    """NFKC, case fold, whitespace collapse. Nothing looser — a miss is a miss."""

    folded = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(folded.split())


def assert_no_store_bearing_utterance(turns: Iterable[tuple[str, str]]) -> None:
    """Refuse the first turn that names the store or the act of storing.

    Names the turn and the matched token, because a refusal that says only
    "something matched" cannot be acted on by whoever wrote the fixture.
    """

    for turn_id, text in turns:
        match = STORE_BEARING_RE.search(text)
        if match is not None:
            raise StoreBearingUtterance(
                f"turn {turn_id!r} contains the store-bearing token "
                f"{match.group(0)!r}: {text!r}; the f27 corpus measures whether "
                "ordinary working language routes on its own, so naming the store "
                "or the act of storing would be teaching the answer"
            )


@dataclass(frozen=True)
class Consequence:
    """One thing an expert lands after a turn."""

    tier: str
    title: str
    event_type: str | None = None
    #: Stated only when the utterance states it; otherwise the agent chooses and
    #: the comparator requires a valid date without comparing its value.
    occurred_on: str | None = None
    status: str | None = None

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"unknown tier {self.tier!r}; the tiers are {TIERS}")
        if self.tier == "outcome" and self.event_type not in EVENT_TYPES:
            raise ValueError(
                f"outcome consequence needs an event type from {EVENT_TYPES}, "
                f"got {self.event_type!r}"
            )
        if self.tier == "transition" and not self.status:
            raise ValueError("transition consequence needs the status it moves to")

    def as_payload(self) -> dict[str, str | None]:
        return {
            "tier": self.tier,
            "title": self.title,
            "event_type": self.event_type,
            "occurred_on": self.occurred_on,
            "status": self.status,
        }


@dataclass(frozen=True)
class ReplayTurn:
    """One user utterance, in ordinary working language, and what it lands."""

    turn_id: str
    text: str
    consequences: tuple[Consequence, ...] = ()
    #: Why this turn lands nothing, when it lands nothing. Recorded rather than
    #: inferred so the corpus can be asserted to contain all three kinds.
    quiet_kind: str | None = None

    @property
    def lands(self) -> str:
        if not self.consequences:
            return "none"
        return "+".join(sorted({consequence.tier for consequence in self.consequences}))

    def as_payload(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "text": self.text,
            "quiet_kind": self.quiet_kind,
            "consequences": [c.as_payload() for c in self.consequences],
        }


@dataclass(frozen=True)
class ExpectedPlanItem:
    """A plan item the fold says must exist, and the statuses it may hold.

    ``filed_status`` is where an expert files it; ``final_status`` is where the
    last transition leaves it. They are the same when no outcome moved it. A
    status outside the pair is one the fold never assigned, which is what makes
    it an extra rather than a coverage miss.
    """

    title: str
    filed_status: str
    final_status: str

    @property
    def assigned_statuses(self) -> frozenset[str]:
        return frozenset({self.filed_status, self.final_status})


@dataclass(frozen=True)
class ExpectedRecord:
    """A record the fold says must exist, keyed on ``(title, event_type)``."""

    title: str
    event_type: str
    occurred_on: str | None = None


@dataclass(frozen=True)
class ExpectedEndState:
    """The fold. Keys are normalised; values keep the corpus's own spelling."""

    plan_items: Mapping[str, ExpectedPlanItem]
    records: Mapping[tuple[str, str], ExpectedRecord]
    transitions: Mapping[str, str]

    def tier_size(self, tier: str) -> int:
        return {
            "intent": len(self.plan_items),
            "outcome": len(self.records),
            "transition": len(self.transitions),
        }[tier]


@dataclass(frozen=True)
class ReplayCorpus:
    """One authored episode plus the end-state folded from its annotations."""

    corpus_id: str
    deliverables: tuple[str, ...]
    turns: tuple[ReplayTurn, ...]
    expected: ExpectedEndState

    @staticmethod
    def normalized(value: str) -> str:
        return normalize(value)

    def turn(self, turn_id: str) -> ReplayTurn:
        for candidate in self.turns:
            if candidate.turn_id == turn_id:
                return candidate
        raise CorpusLookupError(f"no turn {turn_id!r} in corpus {self.corpus_id!r}")

    def as_payload(self) -> dict[str, object]:
        return {
            "corpus_id": self.corpus_id,
            "deliverables": list(self.deliverables),
            "turns": [turn.as_payload() for turn in self.turns],
            "seeded": {
                "planning_manifest": planning_manifest(),
                "records_manifest": records_manifest(),
                "plan_titles": list(SEEDED_PLAN_TITLES),
            },
        }

    def digest(self) -> str:
        payload = json.dumps(self.as_payload(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def replace_turn_text(self, turn_id: str, text: str) -> ReplayCorpus:
        """A variant corpus, for tests that must prove the digest is content-bound."""

        turns = tuple(
            replace(turn, text=text) if turn.turn_id == turn_id else turn
            for turn in self.turns
        )
        if turns == self.turns:
            raise CorpusLookupError(f"no turn {turn_id!r} in corpus {self.corpus_id!r}")
        return build_corpus(self.corpus_id, self.deliverables, turns)


# --------------------------------------------------------------------------
# The episode
# --------------------------------------------------------------------------

#: Six deliverables. Four are taken on during the episode; the fifth and the
#: sixth are named only by turns that land nothing, so a product that files a
#: plan item for a deferral is visible as an extra rather than invisible.
DELIVERABLES: tuple[str, ...] = (
    "Batch layout draft",
    "Colour proof sheet",
    "Assembly checklist",
    "Packing insert",
    "Label sheet",
    "Crate diagram",
)


def _turns() -> tuple[ReplayTurn, ...]:
    return (
        ReplayTurn(
            turn_id="t01-take-on-two",
            text=(
                "This week I'm taking on the Batch layout draft and the "
                "Colour proof sheet for the current run."
            ),
            consequences=(
                Consequence(tier="intent", title="Batch layout draft"),
                Consequence(tier="intent", title="Colour proof sheet"),
            ),
        ),
        ReplayTurn(
            turn_id="t02-take-on-two-more",
            text=(
                "After those two the Assembly checklist and the Packing insert "
                "are mine as well."
            ),
            consequences=(
                Consequence(tier="intent", title="Assembly checklist"),
                Consequence(tier="intent", title="Packing insert"),
            ),
        ),
        ReplayTurn(
            turn_id="t03-first-one-produced",
            text=(
                "The Batch layout draft came off the press this morning and "
                "it is finished."
            ),
            consequences=(
                Consequence(tier="outcome", title="Batch layout draft", event_type="produced"),
                Consequence(tier="transition", title="Batch layout draft", status="completed"),
            ),
        ),
        ReplayTurn(
            turn_id="t04-tentative",
            text="I think the third one might be fine, not sure yet.",
            quiet_kind="tentative",
        ),
        ReplayTurn(
            turn_id="t05-sign-off",
            text="The client signed off on the Colour proof sheet.",
            consequences=(
                Consequence(tier="outcome", title="Colour proof sheet", event_type="approved"),
            ),
        ),
        ReplayTurn(
            turn_id="t06-elapsed-time",
            text="It's been a week since I touched the fifth.",
            quiet_kind="elapsed_time",
        ),
        ReplayTurn(
            turn_id="t07-proof-out",
            text=(
                "The Colour proof sheet went out to the printer this afternoon, "
                "which closes it."
            ),
            consequences=(
                Consequence(tier="outcome", title="Colour proof sheet", event_type="delivered"),
                Consequence(tier="transition", title="Colour proof sheet", status="completed"),
            ),
        ),
        ReplayTurn(
            turn_id="t08-checklist-rejected",
            text=(
                "The Assembly checklist came back rejected on 2026-08-05, the "
                "margins were wrong."
            ),
            consequences=(
                Consequence(
                    tier="outcome",
                    title="Assembly checklist",
                    event_type="rejected",
                    occurred_on="2026-08-05",
                ),
            ),
        ),
        ReplayTurn(
            turn_id="t09-deferral",
            text="I'll do the last two next time.",
            quiet_kind="deferral",
        ),
        ReplayTurn(
            turn_id="t10-insert-out",
            text="The Packing insert is out the door and done.",
            consequences=(
                Consequence(tier="outcome", title="Packing insert", event_type="delivered"),
                Consequence(tier="transition", title="Packing insert", status="completed"),
            ),
        ),
    )


def fold(turns: Iterable[ReplayTurn]) -> ExpectedEndState:
    """Fold per-turn annotations into the end-state an expert session leaves."""

    ordered = tuple(turns)
    plan_items: dict[str, ExpectedPlanItem] = {}
    records: dict[tuple[str, str], ExpectedRecord] = {}
    transitions: dict[str, str] = {}

    for turn in ordered:
        for consequence in turn.consequences:
            key = normalize(consequence.title)
            if consequence.tier == "intent":
                if key in plan_items:
                    raise ValueError(
                        f"turn {turn.turn_id!r} files {consequence.title!r} twice; the "
                        "fold would silently drop one of them"
                    )
                plan_items[key] = ExpectedPlanItem(
                    title=consequence.title,
                    filed_status=FILED_STATUS,
                    final_status=FILED_STATUS,
                )
            elif consequence.tier == "outcome":
                assert consequence.event_type is not None  # enforced by Consequence
                record_key = (key, normalize(consequence.event_type))
                if record_key in records:
                    raise ValueError(
                        f"turn {turn.turn_id!r} repeats the expected event {record_key}; "
                        "occurred_on would become the discriminator, which the corpus "
                        "forbids"
                    )
                records[record_key] = ExpectedRecord(
                    title=consequence.title,
                    event_type=consequence.event_type,
                    occurred_on=consequence.occurred_on,
                )
            else:
                assert consequence.status is not None  # enforced by Consequence
                if key not in plan_items:
                    raise ValueError(
                        f"turn {turn.turn_id!r} moves {consequence.title!r}, which no "
                        "earlier turn filed; a transition needs an open item"
                    )
                transitions[key] = consequence.status
                plan_items[key] = replace(plan_items[key], final_status=consequence.status)

    return ExpectedEndState(
        plan_items=MappingProxyType(dict(plan_items)),
        records=MappingProxyType(dict(records)),
        transitions=MappingProxyType(dict(transitions)),
    )


def build_corpus(
    corpus_id: str, deliverables: tuple[str, ...], turns: tuple[ReplayTurn, ...]
) -> ReplayCorpus:
    """Construct a corpus, running the store-bearing gate before anything else."""

    assert_no_store_bearing_utterance((turn.turn_id, turn.text) for turn in turns)
    named = {normalize(title) for title in deliverables}
    unknown = sorted(
        {
            consequence.title
            for turn in turns
            for consequence in turn.consequences
            if normalize(consequence.title) not in named
        }
    )
    if unknown:
        raise ValueError(
            f"corpus {corpus_id!r} annotates deliverables it never declares: {unknown}"
        )
    expected = fold(turns)
    empty = [tier for tier in TIERS if expected.tier_size(tier) == 0]
    if empty:
        # A tier nothing lands in reports 0/0 and passes for ever. The coverage
        # assertion counts the tiers separately precisely so one of them cannot
        # hide behind the others; an empty one hides behind arithmetic instead.
        raise ValueError(
            f"corpus {corpus_id!r} declares no consequence in tier(s) {empty}; a tier "
            "of size zero would report 0/0 and pass vacuously for ever"
        )
    return ReplayCorpus(
        corpus_id=corpus_id,
        deliverables=deliverables,
        turns=turns,
        expected=expected,
    )


def replay_corpus() -> ReplayCorpus:
    """The f27 episode. A pure function of this module's own literals."""

    return build_corpus(CORPUS_ID, DELIVERABLES, _turns())


def corpus_digest() -> str:
    """The digest a run manifest pins the corpus by."""

    return replay_corpus().digest()


def expected_end_state(corpus_id: str | None) -> ExpectedEndState:
    """The fold a scenario's expectation names, by corpus id."""

    if corpus_id is None:
        raise CorpusLookupError("no corpus id was named by the expectation")
    if corpus_id != CORPUS_ID:
        raise CorpusLookupError(
            f"unknown replay corpus {corpus_id!r}; this module defines {CORPUS_ID!r}"
        )
    return replay_corpus().expected


# --------------------------------------------------------------------------
# The seeded vault
# --------------------------------------------------------------------------


def planning_manifest() -> str:
    """The Delivery plan manifest, keyed on ``title`` exactly as slice 1 ships it."""

    return f"""---
type: collection
exomem_id: {PLANNING_COLLECTION_ID}
title: Delivery plan
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title:
      type: string
      required: true
    kind:
      type: string
    status:
      type: string
    lifecycle:
      type: string
    priority:
      type: string
    commitment:
      type: string
    horizon:
      type: string
    health:
      type: string
    area:
      type: string
    parent:
      type: string
---

Intended deliverables.
"""


def records_manifest() -> str:
    """The Delivery events manifest, joined to the plan on ``title``."""

    return f"""---
type: collection
exomem_id: {RECORDS_COLLECTION_ID}
title: Delivery events
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on, title, event_type]
  fields:
    occurred_on:
      type: date
      required: true
    title:
      type: string
      required: true
    event_type:
      type: string
      required: true
links:
  plans:
    - reference: exomem://memory/{PLANNING_COLLECTION_ID}
      query: {{limit: 50}}
      join:
        title: title
---

Observed delivery events.
"""


class _GraphSyncWatch(logging.Handler):
    """Catch the graph-sync errors the seed provokes instead of losing them.

    ``graph_sync`` reports a stopped rebuild through ``logger.exception``. With
    no logging configured that lands on stderr as a bare traceback beside the
    driver's own output, where it reads as a crash and is attributable to
    nothing. Captured here it becomes a recorded seed warning that travels in
    ``manifest.json``, which is the difference between an unexplained traceback
    and a measurement about the seam.

    The level is WARNING, not ERROR, and that is not a widened net for its own
    sake: the seed silences this logger for its duration (``propagate = False``
    is what keeps the traceback off stderr), so anything below the watch's level
    is deleted rather than merely unrecorded. ``src/exomem/graph_sync.py:3410``
    logs the abandoned-temporary sweep at WARNING with ``exc_info=True`` — an
    ERROR-only watch would trade one traceback on stderr for a silence, which is
    a worse observability position than the one this class was written to fix.

    The empty tuple is not the normal outcome. Measured 2026-08-23 on this host,
    21 of 30 clean seeds emit at least one ``graph rebuild stopped
    checkpoint_sha256=... generation=N`` line — 25 lines over 30 seeds, **all of
    them ERROR**, none WARNING. That message has exactly one call site and it is
    ``logger.exception`` at ``src/exomem/graph_sync.py:2525``; a handler level
    filters what it is offered and cannot relabel a record, so a line's level is
    the product's choice, not this class's. It is routine, it used to reach
    stderr unattributed, and recording it is the point. "This seed was clean"
    therefore means nothing outside that one message class was captured — not
    that nothing was, and not that no ERROR was.

    The level name still travels with each captured line: a reader of
    ``manifest.json`` needs to tell that routine ERROR from the sweep WARNING,
    and a capture that dropped it would be strictly less readable than the stderr
    it replaced.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.captured: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.captured.append(f"{record.levelname} {record.name}: {record.getMessage()}")


def seed_replay_vault(vault_root: Path) -> dict[str, object]:
    """Lay the scaffold, the two collections and the parent chain.

    ``init_vault`` comes first and is not optional: without a schema contract
    under ``Knowledge Base/_Schema/`` the product refuses to resolve the vault at
    all (``exomem.vault.resolve_vault``), so the arm's stdio server never starts
    and every turn answers "No such tool available". Measured 2026-08-23.

    No deliverable items: whether one appears is the measurement. The
    outcome/initiative chain is not decoration — the product refuses a committed
    active work item with no parent initiative, so the chain is what "committed
    work" means here, and an agent that cannot file against it would be failing a
    precondition rather than the family.

    The product's own API does the writing, so the seed is a vault the product
    itself calls valid rather than markdown this module guessed at.

    Laying the scaffold first also all but removes a background rebuild failure
    the seed used to provoke. Measured 2026-08-23 over 226 seeds on this host:
    without ``init_vault`` first, 4 of 36 seeds logged ``GraphProjectionMoved``
    ("the recall projection identity moved across the pass") from the graph-sync
    worker; with it, 1 of 190. Waiting on the product's own
    ``graph_sync.wait_for_current`` between writes was measured too and is
    deliberately NOT used: at a 10 s budget it changed 1/190 to 0/30 while
    costing 20 s per seed (60 of 150 waits ran to timeout, because a freshly
    seeded vault in a process with no drain thread never reports "current"), and
    at every smaller budget down to zero the error rate was the same 0/30. A
    twenty-fold cost for an effect indistinguishable from the ordering change is
    not a quiesce, it is a sleep.

    The residual case is captured rather than printed: see :class:`_GraphSyncWatch`.

    Returns the seeded paths, the parent ``initiative`` reference, and the
    ``warnings`` the seed captured — empty on a clean seed.
    """

    from exomem import planning, records
    from exomem import structured_collections as collections
    from exomem.init import init_vault

    root = Path(vault_root)
    watch = _GraphSyncWatch()
    sync_logger = logging.getLogger("exomem.graph_sync")
    sync_logger.addHandler(watch)
    previous = sync_logger.propagate
    sync_logger.propagate = False
    try:
        return _seed(root, planning, records, collections, init_vault, watch)
    finally:
        sync_logger.removeHandler(watch)
        sync_logger.propagate = previous


def _seed(
    root: Path,
    planning: ModuleType,
    records: ModuleType,
    collections: ModuleType,
    init_vault: Callable[[Path], object],
    watch: "_GraphSyncWatch",
) -> dict[str, object]:
    """The seed itself, with its collaborators passed in so a test can poison it."""

    init_vault(root)
    planning.create_collection(
        root, PLANNING_PATH, planning_manifest(), why="file intended deliverables"
    )
    outcome = planning.add(
        root,
        PLANNING_PATH,
        item={
            "title": OUTCOME_TITLE,
            "kind": "outcome",
            "status": FILED_STATUS,
            "commitment": "committed",
            "horizon": "quarter",
        },
        why="state the intended outcome",
    )
    initiative = planning.add(
        root,
        PLANNING_PATH,
        item={
            "title": INITIATIVE_TITLE,
            "kind": "initiative",
            "status": FILED_STATUS,
            "commitment": "committed",
            "horizon": "quarter",
            "parent": collections.plan_ref(PLANNING_COLLECTION_ID, outcome["plan_id"]),
        },
        why="state the initiative under it",
    )
    records.create_collection(
        root, RECORDS_PATH, records_manifest(), why="log delivery events"
    )
    return {
        "planning": PLANNING_PATH,
        "records": RECORDS_PATH,
        "initiative": collections.plan_ref(
            PLANNING_COLLECTION_ID, str(initiative["plan_id"])
        ),
        "warnings": tuple(watch.captured),
    }


__all__ = [
    "CORPUS_ID",
    "DELIVERABLES",
    "EVENT_TYPES",
    "FILED_STATUS",
    "INITIATIVE_TITLE",
    "OUTCOME_TITLE",
    "PLANNING_COLLECTION_ID",
    "PLANNING_PATH",
    "RECORDS_COLLECTION_ID",
    "RECORDS_PATH",
    "SEEDED_COLLECTION_IDS",
    "SEEDED_PLAN_TITLES",
    "STORE_BEARING_RE",
    "TIERS",
    "Consequence",
    "CorpusLookupError",
    "ExpectedEndState",
    "ExpectedPlanItem",
    "ExpectedRecord",
    "ReplayCorpus",
    "ReplayTurn",
    "StoreBearingUtterance",
    "assert_no_store_bearing_utterance",
    "build_corpus",
    "corpus_digest",
    "expected_end_state",
    "fold",
    "normalize",
    "planning_manifest",
    "records_manifest",
    "replay_corpus",
    "seed_replay_vault",
]
