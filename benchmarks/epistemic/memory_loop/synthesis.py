"""Named-synthesis versus minor-refinement pair (close-memory-loop task 1.6).

Both episodes start from the same frozen pre-capture world: five narrower
notes about one invented product, each owning one design decision. They are
a matched pair:

* ``synthesis`` -- an ordinary product discussion names a broader operating
  thesis that all five decisions follow from. It answers its own future
  question, so it earns a focused first-class home on the first pass, related
  to the antecedents by truthful typed links, while the narrower notes keep
  their scope (a backlink is fine; the thesis restated on them is not).
* ``refinement`` -- an in-scope detail of one antecedent, with a coined
  label, belongs on that antecedent. No new page, however quotable the label.

Actor input and the pre-capture world are frozen and digested before any
effect; the evaluator side (candidates, expectations, later-use anchors) is
never part of :func:`actor_view`. This public pair is synthetic. The exact
private replay of the episode that motivated it runs only locally, binds its
own original input and snapshot, and is never scored against this fixture:
:func:`void_reasons` refuses any record that is not a synthetic-fixture run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import observation as obs
from .contract import (
    Admissible,
    BuiltWorld,
    Candidate,
    CaptureCheck,
    EdgeCount,
    EntityIntact,
    Expectation,
    FixtureError,
    LaterUse,
    Mentions,
    NewPages,
    NoNewEdge,
    NoNewMention,
    NoteSeed,
    PreCapture,
    Result,
    Select,
    VaultState,
    build_world,
    check_expectations,
    selects_of,
    semantics_fingerprint,
    sha256_json,
    validate_candidates,
)

FIXTURE_SET_ID = "memory-loop-synthesis-pair-v1"
#: Where the exact private replay lives: nowhere in this repository.
EXACT_PRIVATE_REPLAY = "local only; binds its own original input and snapshot; never scored here"

INVENTED_NAMES: tuple[str, ...] = ("Tallyloom",)
ORDINARY_CAPITALIZED: tuple[str, ...] = (
    "Conflict",
    "Export",
    "Offline",
    "Plugin",
    "Small",
    "Telemetry",
    "That",
    "When",
    "We",
    "How",
    "I",
    "It",
    "Keeping",
    "Markdown",
)

ANTECEDENT_KEYS: tuple[str, ...] = (
    "ant_offline",
    "ant_conflicts",
    "ant_plugins",
    "ant_export",
    "ant_telemetry",
)

WORLD = PreCapture(
    notes=(
        NoteSeed(
            key="ant_offline",
            title="Offline edits in Tallyloom",
            slug="offline-edits-in-tallyloom",
            observation="Tallyloom keeps every edit on the device and syncs only when the owner reconnects.",
            category="design",
        ),
        NoteSeed(
            key="ant_conflicts",
            title="Conflict handling in Tallyloom",
            slug="conflict-handling-in-tallyloom",
            observation="When two devices disagree, Tallyloom keeps the latest human edit and archives the other.",
            category="design",
        ),
        NoteSeed(
            key="ant_plugins",
            title="Plugin sandbox in Tallyloom",
            slug="plugin-sandbox-in-tallyloom",
            observation="Tallyloom plugins can read and write only inside the workspace folder.",
            category="design",
        ),
        NoteSeed(
            key="ant_export",
            title="Export format in Tallyloom",
            slug="export-format-in-tallyloom",
            observation="Tallyloom exports plain Markdown files with front matter, one file per note.",
            category="design",
        ),
        NoteSeed(
            key="ant_telemetry",
            title="Telemetry in Tallyloom",
            slug="telemetry-in-tallyloom",
            observation="Tallyloom sends no usage data unless the owner opts in, and then only weekly aggregates.",
            category="design",
        ),
    ),
)


@dataclass(frozen=True)
class Episode:
    episode_id: str
    turns: tuple[str, ...]
    later_turn: str


EPISODES: tuple[Episode, ...] = (
    Episode(
        episode_id="synthesis",
        turns=(
            "I've been thinking about why Tallyloom keeps making the same kind of call. Keeping "
            "edits on the device until the owner reconnects, keeping the human's edit when devices "
            "disagree, boxing plugins into the workspace folder, exporting plain Markdown, and "
            "making telemetry opt-in are all one idea: the owner keeps custody of their notes at "
            "every step, and the app is only ever a guest. I've started calling it the "
            "owner-custody principle. It's the test new features have to pass: if a feature needs "
            "notes to leave the owner's custody, it has to justify that openly.",
        ),
        later_turn="We're weighing a hosted sync server for Tallyloom. How should we think about it?",
    ),
    Episode(
        episode_id="refinement",
        turns=(
            "Small detail on Tallyloom's offline edits: queued changes now wait up to 30 days "
            "before the app warns that sync is overdue. I've been calling that the thirty-day queue.",
        ),
        later_turn="How long can Tallyloom hold offline edits before it complains?",
    ),
)

# --------------------------------------------------------------------------- #
# Evaluator side
# --------------------------------------------------------------------------- #

_THESIS = Select(kind="page", tokens=("custody",), created_only=True)
#: Relations no synthesis bears to the decisions it explains.
_UNTRUTHFUL_SYNTHESIS_LINKS = ("supersedes", "duplicates", "contradicts", "causes", "caused_by", "blocks")
_THESIS_MARKERS = ("custody", "custodian", "guest")
#: The truthful typed links between a synthesis and the decisions it explains,
#: by direction: the thesis is derived from or evidenced by each decision, and
#: each decision supports, implements, refines or specializes the thesis.
SYNTHESIS_LINKS = (
    Admissible(relation="derived_from", direction="forward"),
    Admissible(relation="evidenced_by", direction="forward"),
    Admissible(relation="supports", direction="reverse"),
    Admissible(relation="implements", direction="reverse"),
    Admissible(relation="refines", direction="reverse"),
    Admissible(relation="extends", direction="reverse"),
)
#: Every antecedent is named in the discussion; a tolerance of one missed
#: link is allowed, declared here rather than hidden in a threshold.
LINKED_AT_LEAST = 4
#: The recurring product may be promoted to an entity during either episode;
#: that is not fragmentation of the thesis or of the refinement.
_TOLERATED_NEW_TITLES = ("Tallyloom",)


@dataclass(frozen=True)
class EpisodeExpectations:
    episode_id: str
    candidates: tuple[Candidate, ...]
    expectations: tuple[Expectation, ...]
    later_use: LaterUse


MANIFEST: tuple[EpisodeExpectations, ...] = (
    EpisodeExpectations(
        episode_id="synthesis",
        candidates=(
            Candidate(
                key="thesis",
                statement=(
                    "The owner-custody principle: the owner keeps custody of their notes at every "
                    "step and the app is a guest; a feature that moves notes out of custody must "
                    "justify it openly."
                ),
                homes=("new:owner-custody",),
                routes=("focused_note", "entity"),
                dispositions=("routed",),
                provenance="direct",
                checked_by=(
                    "synthesis/focused-home",
                    "synthesis/thesis-on-home",
                    "synthesis/only-the-thesis-is-new",
                )
                + tuple(f"synthesis/{key}-keeps-scope" for key in ANTECEDENT_KEYS)
                + tuple(f"synthesis/{key}-intact" for key in ANTECEDENT_KEYS),
            ),
            Candidate(
                key="thesis-links",
                statement="The thesis relates to each of the five decisions it explains.",
                homes=("new:owner-custody",),
                routes=("focused_note", "relation_only"),
                dispositions=("routed",),
                provenance="direct",
                checked_by=("synthesis/linked-to-the-antecedents", "synthesis/no-untruthful-link"),
            ),
        ),
        expectations=(
            NewPages(
                key="synthesis/only-the-thesis-is-new",
                polarity="positive",
                exactly=1,
                tolerate=_TOLERATED_NEW_TITLES,
                reason=(
                    "The thesis is one focused home (a note or a concept entity), not split across "
                    "pages. Promoting the recurring product to an entity is not counted."
                ),
            ),
            Mentions(
                key="synthesis/focused-home",
                polarity="positive",
                select=_THESIS,
                groups=(("custody",),),
                reason="The named thesis gets its own focused home: exactly one created page named for it.",
            ),
            Mentions(
                key="synthesis/thesis-on-home",
                polarity="positive",
                select=_THESIS,
                groups=(("guest", "custodian", "custody"), ("justify", "justification", "justified")),
                reason="The home carries the thesis and the test it sets for features.",
            ),
            EdgeCount(
                key="synthesis/linked-to-the-antecedents",
                polarity="positive",
                source=_THESIS,
                targets=tuple(Select(key=key) for key in ANTECEDENT_KEYS),
                admissible=SYNTHESIS_LINKS,
                at_least=LINKED_AT_LEAST,
                reason="Truthful typed links connect the thesis to the decisions it explains.",
            ),
            NoNewEdge(
                key="synthesis/no-untruthful-link",
                polarity="negative",
                relations=_UNTRUTHFUL_SYNTHESIS_LINKS,
                reason="A synthesis neither replaces, duplicates, contradicts nor causes its antecedents.",
            ),
        )
        + tuple(
            NoNewMention(
                key=f"synthesis/{key}-keeps-scope",
                polarity="negative",
                select=Select(key=key),
                markers=_THESIS_MARKERS,
                ignore_links=True,
                reason="A narrower page may gain a link to the thesis, never the thesis itself.",
            )
            for key in ANTECEDENT_KEYS
        )
        + tuple(
            EntityIntact(
                key=f"synthesis/{key}-intact",
                polarity="positive",
                entity=key,
                reason="The antecedent stays a live page with its own title.",
            )
            for key in ANTECEDENT_KEYS
        ),
        later_use=LaterUse(
            useful=(
                "Apply the owner-custody principle: a hosted sync server takes notes out of the "
                "owner's custody, so it must justify that openly, as offline edits, sandboxed "
                "plugins, plain export and opt-in telemetry do not."
            ),
            wrong=("A hosted sync server needs no special justification.",),
        ),
    ),
    EpisodeExpectations(
        episode_id="refinement",
        candidates=(
            Candidate(
                key="queue-window",
                statement="Queued offline changes wait up to 30 days before the sync-overdue warning (the 'thirty-day queue').",
                homes=("existing:ant_offline",),
                routes=("existing_page", "semantic_unit"),
                dispositions=("routed",),
                provenance="direct",
                checked_by=(
                    "refinement/on-its-existing-home",
                    "refinement/no-new-page",
                    "refinement/antecedent-intact",
                )
                + tuple(f"refinement/{key}-untouched" for key in ANTECEDENT_KEYS[1:]),
            ),
        ),
        expectations=(
            Mentions(
                key="refinement/on-its-existing-home",
                polarity="positive",
                select=Select(key="ant_offline"),
                groups=(("30 days", "thirty days", "30-day", "thirty-day"),),
                reason="The detail refines the offline-edits decision it belongs to.",
            ),
            NewPages(
                key="refinement/no-new-page",
                polarity="negative",
                exactly=0,
                tolerate=_TOLERATED_NEW_TITLES,
                reason="A coined label for an in-scope detail does not manufacture a page or an entity.",
            ),
            EntityIntact(
                key="refinement/antecedent-intact",
                polarity="positive",
                entity="ant_offline",
                reason="The existing home stays the same page.",
            ),
        )
        + tuple(
            NoNewMention(
                key=f"refinement/{key}-untouched",
                polarity="negative",
                select=Select(key=key),
                markers=("30 days", "thirty", "30-day", "queue"),
                reason="Other decisions' scopes do not own the offline queue window.",
            )
            for key in ANTECEDENT_KEYS[1:]
        ),
        later_use=LaterUse(
            useful="Queued offline edits wait up to 30 days before Tallyloom warns that sync is overdue.",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# Frozen pins (re-pinned deliberately; the tests refuse drift)
# --------------------------------------------------------------------------- #

FIXTURE_SET_SHA256 = "7cac6de143f4682a7fcd4f9a2c56f923a38a6fe2f0dee42deac5f128bf2bede8"
ACTOR_SHA256: dict[str, str] = {
    "synthesis": "50c776148b7dfd7ca3a0a517ed190bd7bb54208cf0e5fdf8fafeb7ddd95fb007",
    "refinement": "5fbcfa6f5c51314b623643ea95ff53bae666eab07080c61c9114842883efb230",
}
EVALUATOR_SHA256: dict[str, str] = {
    "synthesis": "3e0f0f8e04079cdc4d240f2cf73ea46c3530ca0ff1af941e7b5eebb166078003",
    "refinement": "3d428824c7b2bb686250f805bb7641cc869625849986b2f1cd131ce6516cbe6d",
}
PRE_CAPTURE_SHA256 = "80e33a8646d7bd509563be44f442fbfe31aa61f05c14fc42c18cf01e6a3ba41d"

# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #


def episode(episode_id: str) -> Episode:
    for item in EPISODES:
        if item.episode_id == episode_id:
            return item
    raise FixtureError(f"unknown episode: {episode_id}")


def expectations_for(episode_id: str) -> EpisodeExpectations:
    for item in MANIFEST:
        if item.episode_id == episode_id:
            return item
    raise FixtureError(f"unknown episode: {episode_id}")


def actor_view(episode_id: str) -> dict[str, Any]:
    item = episode(episode_id)
    return {"episode_id": item.episode_id, "turns": list(item.turns), "later_turn": item.later_turn}


def actor_sha256(episode_id: str) -> str:
    return sha256_json(actor_view(episode_id))


def pre_capture_spec_sha256() -> str:
    return sha256_json(WORLD)


def evaluator_sha256(episode_id: str | None = None) -> str:
    evaluator = MANIFEST if episode_id is None else expectations_for(episode_id)
    return sha256_json({"semantics": semantics_fingerprint(), "evaluator": evaluator})


def fixture_set_sha256() -> str:
    return sha256_json(
        {
            "fixture_set": FIXTURE_SET_ID,
            "actor": [actor_view(item.episode_id) for item in EPISODES],
            "world": WORLD,
            "evaluator": evaluator_sha256(),
        }
    )


def frozen(episode_id: str) -> obs.Frozen:
    """The module's pins for one episode, which a run binds to before any effect."""

    item = episode(episode_id)
    return obs.Frozen(
        fixture_id=f"{FIXTURE_SET_ID}/{episode_id}",
        actor_sha256=ACTOR_SHA256[episode_id],
        pre_capture_sha256=PRE_CAPTURE_SHA256,
        evaluator_sha256=EVALUATOR_SHA256[episode_id],
        turns_sha256=obs.turns_sha256(item.turns),
        later_turn_sha256=obs.text_sha256(item.later_turn),
        shipped_prompt_sha256=obs.shipped_prompt_sha256(),
    )


def build_pre_capture(root: Path) -> BuiltWorld:
    """Both episodes share this one frozen world; each run starts from a fresh build."""

    return build_world(root, WORLD, world_id=FIXTURE_SET_ID)


def check_capture(episode_id: str, world: BuiltWorld, before: VaultState, after: VaultState) -> CaptureCheck:
    return check_expectations(
        expectations_for(episode_id).expectations, world=world.key_to_path, before=before, after=after
    )


def void_reasons(record: obs.NoNudgeObservation, *, episode_id: str) -> tuple[str, ...]:
    """Why a record cannot be scored against this public synthetic pair; an
    exact private replay binds its own original input and snapshot and stays
    local."""

    return obs.void_reasons(record, frozen(episode_id))


def first_pass_focus(record: obs.NoNudgeObservation, world: BuiltWorld) -> Result:
    """The thesis's first destination decision is a new focused home, made after
    inspecting at least one antecedent, and no effect wrote the thesis onto an
    antecedent before it.

    The thesis candidate is recognised by the agent's own text naming custody.
    """

    antecedents = {world.key_to_path[key] for key in ANTECEDENT_KEYS}
    thesis_keys = {
        decision.candidate_key
        for decision in record.agent_decisions
        if decision.candidate_key and "custody" in f"{decision.text} {decision.title or ''}".casefold()
    }
    destinations = sorted(
        (
            decision
            for decision in record.agent_decisions
            if decision.phase == "destination" and decision.candidate_key in thesis_keys
        ),
        key=lambda decision: decision.seq,
    )
    if not destinations:
        return Result("synthesis/first-pass-focus", "positive", "fail", "no destination decision names the thesis")
    first = destinations[0]
    early_effects = [
        effect.path
        for effect in record.leaf_effects
        if effect.candidate_key in thesis_keys and effect.path in antecedents and effect.seq < first.seq
    ]
    inspected = sorted(set(first.alternatives) & antecedents)
    ok = first.route == "focused_note" and first.title is not None and bool(inspected) and not early_effects
    return Result(
        "synthesis/first-pass-focus",
        "positive",
        "pass" if ok else "fail",
        f"first route {first.route}; antecedents inspected {len(inspected)}/5; early effects {early_effects}",
    )


def assert_manifest_consistent() -> None:
    if [item.episode_id for item in EPISODES] != [item.episode_id for item in MANIFEST]:
        raise FixtureError("episodes and expectations must pair one to one, in order")
    world_keys = set(WORLD.keys())
    if set(ANTECEDENT_KEYS) != world_keys or len(ANTECEDENT_KEYS) != 5:
        raise FixtureError("the pair's world is exactly five narrower antecedents")
    for item in MANIFEST:
        keys = [expectation.key for expectation in item.expectations]
        if len(keys) != len(set(keys)):
            raise FixtureError(f"{item.episode_id}: expectation keys must be unique")
        validate_candidates(item.candidates, world_keys=world_keys, expectation_keys=keys)
        enforced = {check for candidate in item.candidates for check in candidate.checked_by}
        if enforced != set(keys):
            raise FixtureError(f"{item.episode_id}: untied expectations {sorted(set(keys) ^ enforced)}")
        for expectation in item.expectations:
            for select in selects_of(expectation):
                if select.key is not None and select.key not in world_keys:
                    raise FixtureError(f"{expectation.key} selects {select.key}, absent from the world")
