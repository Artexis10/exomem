"""The `conversation` benchmark group (``context-activation-conversation-v1``).

It measures thread-aware compilation: whether an activation packet compiled
from the current turn PLUS a bounded view of the conversation (`focus`,
`recent`, `refs`) serves the right anchors, and never the wrong ones. The group
is the pre-registration the OpenSpec change `add-thread-aware-compilation`
requires: fixtures, gold, poison and expected behaviour are authored and
digest-pinned BEFORE any scored run, and the expectations describe the
POST-change compiler. A run against a compiler that lacks the mechanism is
expected to fail the cases that need it; that failure is the control.

It is a SIBLING, never an extension. The English set
(:mod:`epistemic.corpora.context_activation`) is digest-pinned and stays
byte-identical; this set has its own case ids (``V<n>`` and their twins
``W<n>``), its own digest and its own corpus. It reuses that module's product
writers, so every page goes through the canonical note, entity, resource and
Records writers. The governed page is withheld from the restricted audience by
a scope and a ceiling-0 rule written the way the governance tests write them.

Populations (all invented names; nothing here comes from a real vault):

* ``V1``-``V12`` rich single turns of two to five sentences, at least four
  spanning two domains (two anchors of different kinds; two hubs the turn
  names compete by design, which is today's contract).
* ``V13``-``V25`` multi-turn conversations of three to six earlier entries
  plus the current turn: promotion, tie-break, anaphoric carry, topic switch,
  drowning, and a refs-only conversation.
* ``V26``, ``V27`` attachment cases: a nearly content-free turn plus a
  fixture-authored `focus` of cues as a vision layer would read them, and one
  twin whose cues name nothing in the corpus.
* ``V28``/``W28`` the withheld-versus-absent pair, scored for byte identity.
* One negative twin for every rich and multi-turn case.

Every case pre-registers gold, poison, must-include and must-exclude facts,
the expected packet status, the expected ``generation.carried_by`` and the
expected ``origin`` of every gold anchor. Gold, poison and ``refs`` name
LOGICAL KEYS, never vault paths: :func:`build_corpus` renders the corpus and
returns the key -> path map, and the runner resolves keys at run time.

Arm semantics (the design table): (a) turn only, the mechanism-removal
control; (b) turn + ``recent`` + ``refs``, as a hook sends them; (c) turn +
``focus``, as a remote agent sends it; (d) all three. A case's ``arms`` name
the arms it is scored on (arm (a) is always among them); its base expectation
holds for every scored arm unless ``arm_expectations`` overrides fields for
one. The expectation on arm (a) is the case's TARGET, evaluated against a
request with no conversation: that is what makes arm (a) the control.

How a gold anchor is served is fixed by the case's expectation: at status
``resolved`` on the anchor, except a case with ``expected_carried_by``
``conversation``, whose single gold anchor is carried at status ``partial``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from epistemic.corpora.context_activation import (
    EXPECTED_STATUSES,
    FixtureError,
    _compiled_note,
    _corpus_hash,
    _create_records_collection,
    _entity,
    _governed_resource,
    _records_manifest,
    find_normalized_leaks,
    find_verbatim_leaks,
)

FIXTURE_SET_ID = "context-activation-conversation-v1"
CORPUS_ID = "context-activation-conversation-corpus-v1"

ARMS: tuple[str, ...] = ("a", "b", "c", "d")
GROUPS: tuple[str, ...] = ("rich_turn", "multi_turn", "attachment", "withheld")
CALLERS: tuple[str, ...] = ("owner", "restricted")
#: `generation.carried_by` values a case may pre-register (None: nothing carried).
CARRIED_BY: tuple[str | None, ...] = (None, "conversation")
#: The origin labels of a served anchor (spec: "Focus is resolved as part of
#: the current turn"). `agent_choice` is deliberately absent: a `focus` origin
#: is never the agent's `anchor` choice.
ORIGINS: tuple[str, ...] = ("turn", "focus", "turn_and_focus", "conversation")
#: `pronoun_negative`: an ordinary turn that carries a pronoun, demonstrative
#: or temporal deictic pointing back at nothing, sent with an anaphoric-carry
#: case's conversation. It must never be carried (ruling C1 on #1463). These
#: are extra negatives, not the case's one twin.
TWIN_MODES: tuple[str, ...] = ("unrelated_conversation", "empty_turn", "pronoun_negative")
_ARM_OVERRIDE_FIELDS = frozenset({"status", "carried_by", "origin", "disambiguated_by"})

POSITIVE_KINDS: frozenset[str] = frozenset(
    {
        "rich_single",
        "rich_two_domain",
        "promotion",
        "tie_break",
        "anaphoric_carry",
        "topic_switch",
        "drowning",
        "refs_only",
        "attachment",
        "withheld_ref",
    }
)
KINDS: tuple[str, ...] = (
    *sorted(POSITIVE_KINDS),
    *(f"{kind}_twin" for kind in sorted(POSITIVE_KINDS - {"withheld_ref"})),
    "absent_ref",
)

#: The audience the governed page is withheld from.
RESTRICTED_AUDIENCE = "external"
WITHHELD_KEY = "r_osprey_ledger"
_SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FC1"
_RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FC2"
_WITHHELD_PATH = "Knowledge Base/Products/Osprey escrow ledger.md"


@dataclass(frozen=True)
class ConversationCase:
    """One authored case or twin. Editing any field moves the digest.

    ``conversation``: ``None`` or a dict with optional ``focus`` (one line),
    ``recent`` (a list of ``{role, text}``, oldest first) and ``refs`` (logical
    page keys, resolved to vault paths at run time).

    ``gold``: logical keys the packet must serve. ``poison``: keys that must
    never be served (resolved, listed under ambiguity, carried, a unit, a
    pointer or a current-state entry). ``must_include`` / ``must_exclude``:
    fact phrases that a served packet must / must not contain. ``expected_*``
    are the target behaviour; ``arm_expectations`` overrides them for one arm
    (a dict of arm -> {status, carried_by, origin, disambiguated_by}).
    ``expected_origin`` is a tuple of (gold key, origin) pairs.
    """

    case_id: str
    pairs_with: str | None
    group: str
    kind: str
    turn: str
    conversation: dict | None
    gold: tuple[str, ...]
    poison: tuple[str, ...]
    must_include: tuple[str, ...]
    must_exclude: tuple[str, ...]
    expected_status: str
    expected_carried_by: str | None
    expected_origin: tuple[tuple[str, str], ...]
    arms: tuple[str, ...] = ARMS
    expected_disambiguated_by: str | None = None
    arm_expectations: dict | None = None
    caller: str = "owner"
    twin_mode: str | None = None

    def __post_init__(self) -> None:
        if self.group not in GROUPS:
            raise FixtureError(f"{self.case_id}: unknown group {self.group!r}")
        if self.kind not in KINDS:
            raise FixtureError(f"{self.case_id}: unknown kind {self.kind!r}")
        if self.expected_status not in EXPECTED_STATUSES:
            raise FixtureError(f"{self.case_id}: unknown expected status {self.expected_status!r}")
        if self.expected_carried_by not in CARRIED_BY:
            raise FixtureError(f"{self.case_id}: unknown expected carried_by {self.expected_carried_by!r}")
        for key, origin in self.expected_origin:
            if origin not in ORIGINS:
                raise FixtureError(f"{self.case_id}: unknown origin {origin!r} for {key!r}")
        if self.caller not in CALLERS:
            raise FixtureError(f"{self.case_id}: unknown caller {self.caller!r}")
        if self.twin_mode is not None and self.twin_mode not in TWIN_MODES:
            raise FixtureError(f"{self.case_id}: unknown twin mode {self.twin_mode!r}")
        if not set(self.arms) <= set(ARMS) or "a" not in self.arms:
            raise FixtureError(f"{self.case_id}: arms must be drawn from a-d and include the control arm a")
        for arm, override in (self.arm_expectations or {}).items():
            if arm not in ARMS or not set(override) <= _ARM_OVERRIDE_FIELDS:
                raise FixtureError(f"{self.case_id}: malformed arm expectation for {arm!r}")
        if not self.turn.strip():
            raise FixtureError(f"{self.case_id}: turn must not be empty")

    def expectation(self, arm: str) -> dict:
        """The effective expectation on one arm: the base, then the arm's overrides."""

        if arm not in ARMS:
            raise FixtureError(f"unknown arm {arm!r}")
        base = {
            "status": self.expected_status,
            "carried_by": self.expected_carried_by,
            "origin": dict(self.expected_origin),
            "disambiguated_by": self.expected_disambiguated_by,
        }
        base.update((self.arm_expectations or {}).get(arm, {}))
        return base


# --------------------------------------------------------------------------- #
# The corpus content. Every name is invented.
# --------------------------------------------------------------------------- #

#: (key, domain, name, slug, summary, tags)
ENTITIES: tuple[tuple[str, str, str, str, str, tuple[str, ...]], ...] = (
    ("e_ottilie_marsh", "people", "Ottilie Marsh", "ottilie-marsh",
     "Programme lead who scored the entries and prepares the follow-up cycle.", ("programme",)),
    ("e_bastien_quill", "people", "Bastien Quill", "bastien-quill",
     "Lead surveyor who trains new volunteers on the tide tables.", ("survey",)),
    ("e_perpetua_holt", "people", "Perpetua Holt", "perpetua-holt",
     "Treasurer who approves any spend above five hundred before it is booked.", ("money",)),
    ("e_lysander_crake", "people", "Lysander Crake", "lysander-crake",
     "Contractor who repairs the slate roofs and quotes in fixed lump sums.", ("building",)),
    ("e_wilhelmina_pryce", "people", "Wilhelmina Pryce", "wilhelmina-pryce",
     "Teacher who pilots new units with her Thursday class before they are adopted.", ("teaching",)),
    ("e_corvin_ashdown", "people", "Corvin Ashdown", "corvin-ashdown",
     "Recruiter who screens the first-round candidates by phone.", ("recruiting",)),
    ("e_marigold_tenby", "people", "Marigold Tenby", "marigold-tenby",
     "Volunteer coordinator who keeps the marshal roster and the radio list.", ("volunteers",)),
)

ENTITY_NAMES: tuple[str, ...] = tuple(row[2] for row in ENTITIES)

#: (key, domain, title, slug, observation, tags): hubs, i.e. notes filed under
#: the `hub` category. A hub's anchor alias is its title.
HUBS: tuple[tuple[str, str, str, str, str, tuple[str, ...]], ...] = (
    ("h_harbor_lantern", "finance", "Harbor Lantern budget", "harbor-lantern-budget",
     "The Harbor Lantern budget caps the signal-repair spend at forty thousand and reserves a tenth for contingencies.",
     ("hub", "finance")),
    ("h_kestrel", "people", "Kestrel hiring plan", "kestrel-hiring-plan",
     "The Kestrel hiring plan holds two analyst seats open until the March review and prefers internal moves first.",
     ("hub", "hiring")),
    ("h_tidewater", "finance", "Tidewater grant", "tidewater-grant",
     "The Tidewater grant pays out in three tranches, and the second tranche needs the interim report filed first.",
     ("hub", "funding")),
    ("h_brindle", "operations", "Brindle Court renovation", "brindle-court-renovation",
     "The Brindle Court renovation is waiting on the roofer's quote before the scaffolding goes up.",
     ("hub", "building")),
    ("h_nightjar", "operations", "Nightjar migration", "nightjar-migration",
     "The Nightjar migration moves the booking system to the new host in two cutover waves, weekend nights only.",
     ("hub", "systems")),
    ("h_copperfield", "finance", "Copperfield audit", "copperfield-audit",
     "The Copperfield audit samples forty invoices per quarter and flags anything without a purchase order.",
     ("hub", "finance")),
    ("h_copperfield_depot", "operations", "Copperfield depot roster", "copperfield-depot-roster",
     "The Copperfield depot roster rotates the night shift every third week and posts changes on Fridays.",
     ("hub", "depot")),
    ("h_saltmarsh", "research", "Saltmarsh survey", "saltmarsh-survey",
     "The Saltmarsh survey counts wading birds along six fixed transects at low tide each fortnight.",
     ("hub", "wildlife")),
    ("h_wrenfield", "education", "Wrenfield curriculum", "wrenfield-curriculum",
     "The Wrenfield curriculum drops the third-term project and adds a shorter portfolio task instead.",
     ("hub", "teaching")),
    ("h_larkspur", "community", "Larkspur festival", "larkspur-festival",
     "The Larkspur festival needs eight volunteer marshals for the Saturday parade route.",
     ("hub", "events")),
    ("h_ironbark", "logistics", "Ironbark relocation", "ironbark-relocation",
     "The Ironbark relocation books the removal van for the first week of June and packs the archive last.",
     ("hub", "moving")),
    ("h_ironbark_lease", "logistics", "Ironbark lease renewal", "ironbark-lease-renewal",
     "The Ironbark lease renewal falls due in September and needs the landlord's countersignature.",
     ("hub", "lease")),
    ("h_mistral", "finance", "Mistral pricing review", "mistral-pricing-review",
     "The Mistral pricing review compares the annual plan against monthly billing and recommends a ten percent discount.",
     ("hub", "pricing")),
    ("h_quarrel", "logistics", "Quarrel Bay ferry", "quarrel-bay-ferry",
     "The Quarrel Bay ferry runs a reduced timetable on Sundays and skips the noon crossing in winter.",
     ("hub", "transport")),
)

#: (key, domain, vault path, title, observation, tags): curated resources.
RESOURCES: tuple[tuple[str, str, str, str, str, tuple[str, ...]], ...] = (
    ("r_cormorant_winch", "operations", "Knowledge Base/Products/Cormorant winch.md", "Cormorant winch",
     "The Cormorant winch hauls the pontoon out each autumn; grease the drum before every haul.",
     ("resource", "harbour")),
    ("r_pennywhistle_plotter", "research", "Knowledge Base/Products/Pennywhistle plotter.md", "Pennywhistle plotter",
     "The Pennywhistle plotter prints survey sheets on waterproof paper and jams on damp rolls.",
     ("resource", "survey")),
    (WITHHELD_KEY, "finance", _WITHHELD_PATH, "Osprey escrow ledger",
     "The Osprey escrow ledger holds the retention deposits and is reconciled on the last working day.",
     ("resource", "ledger")),
)

#: Records collections: (key, domain, manifest path, exomem id, title, claims, items).
RECORDS: tuple[tuple[str, str, str, str, str, tuple[str, ...], tuple[tuple[str, dict[str, object]], ...]], ...] = (
    (
        "c_pontoon_haul",
        "operations",
        "Knowledge Base/Records/Pontoon Haul/_collection.md",
        "71000000-0000-4000-8000-000000000001",
        "Pontoon haul log",
        ("pontoon haul", "haul log"),
        (
            ("71000000-0000-4000-8000-000000000011",
             {"observed_on": "2026-08-04", "subject": "pontoon-haul", "state": "drum regreased"}),
            ("71000000-0000-4000-8000-000000000012",
             {"observed_on": "2026-09-08", "subject": "pontoon-haul", "state": "cable inspected"}),
        ),
    ),
)

#: (key, title, slug, category, observation, tags): ordinary notes, none of them
#: an anchor. `f_spring_round` is the retrieval-carry trap: its words are the
#: incidental words of an anaphoric turn.
FILLERS: tuple[tuple[str, str, str, str, str, tuple[str, ...]], ...] = (
    ("f_spring_round", "Spring round scoring sheet", "spring-round-scoring-sheet", "fact",
     "The spring round scoring sheet lists every entrant's results and the weights that were applied to the next round.",
     ("scoring",)),
    ("f_van_tyres", "Van tyre pressures", "van-tyre-pressures", "fact",
     "Run the van tyres at thirty-six psi when loaded and thirty-two when empty.", ("vehicles",)),
    ("f_tea_rota", "Tea rota", "tea-rota", "fact",
     "Whoever opens the workshop fills the urn; the biscuit tin is restocked on Fridays.", ("office",)),
    ("f_kiln_schedule", "Kiln firing schedule", "kiln-firing-schedule", "fact",
     "Bisque firings run on Mondays and glaze firings on Thursdays, with a slow cool overnight.", ("ceramics",)),
    ("f_bike_locks", "Bike lock codes", "bike-lock-codes", "fact",
     "The shed padlock code changes on the first of every month and lives in the sealed envelope.", ("office",)),
    ("f_recycling", "Recycling collection days", "recycling-collection-days", "fact",
     "Glass goes out on alternate Tuesdays and cardboard must be flattened or the crew leaves it.", ("house",)),
    ("f_coffee", "Coffee grinder settings", "coffee-grinder-settings", "fact",
     "Setting nine suits the filter brewer and setting three suits espresso.", ("kitchen",)),
    ("f_board_games", "Board game night", "board-game-night", "fact",
     "Hosts pick two medium-weight games and newcomers get the short rules summary first.", ("games",)),
    ("f_wifi", "Wi-Fi mesh node", "wifi-mesh-node", "technique",
     "The hallway node drops after firmware updates, so restart the main unit before the satellite.", ("network",)),
    ("f_chess", "Chess opening repertoire", "chess-opening-repertoire", "fact",
     "Play the Caro-Kann against e4 and the Queen's Gambit Declined against d4.", ("chess",)),
    ("f_podcasts", "Podcast queue", "podcast-queue", "fact",
     "Queue the history series for the commute and the interview shows for weekend chores.", ("media",)),
    ("f_museum", "Museum membership", "museum-membership", "fact",
     "The family membership covers two adults and three children and includes exhibition previews.", ("family",)),
    ("f_keyboards", "Keyboard switch comparison", "keyboard-switch-comparison", "finding",
     "Tactile switches suit all-day typing; linear ones felt too light on the test board.", ("computers",)),
    ("f_adapters", "Travel adapter checklist", "travel-adapter-checklist", "fact",
     "Pack two universal adapters and the short extension lead because hotel sockets hide behind the bed.", ("travel",)),
    ("f_cobbler", "Shoe repair", "shoe-repair", "fact",
     "The cobbler on the high street resoles leather boots in a week and does heels while you wait.", ("errands",)),
    ("f_library", "Library card renewal", "library-card-renewal", "fact",
     "Cards renew online every two years and the reservation limit is twelve items.", ("books",)),
    ("f_gifts", "Gift ideas", "gift-ideas", "fact",
     "He is into model rockets and adventure novels this year, so avoid anything with small batteries.", ("family",)),
    ("f_insurance", "Home insurance renewal", "home-insurance-renewal", "action",
     "The renewal quote rose by a fifth, so compare two brokers before the policy rolls over in November.", ("money",)),
    ("f_tram", "Tram pass top-up", "tram-pass-top-up", "fact",
     "The monthly pass is cheaper after sixteen journeys and should be topped up before the first working day.", ("travel",)),
    ("f_inks", "Fountain pen inks", "fountain-pen-inks", "finding",
     "Blue-black iron gall ink suits the journal and the brown ink feathers on cheap paper.", ("stationery",)),
    ("f_crosswords", "Crossword solving tips", "crossword-solving-tips", "fact",
     "Start with the anagrams and the short clues, because the setter hides abbreviations in the long ones.", ("games",)),
    ("f_sweep", "Chimney sweep appointment", "chimney-sweep-appointment", "action",
     "The sweep comes every September before the wood burner is lit, so book the visit in July.", ("house",)),
    ("f_spices", "Spice rack inventory", "spice-rack-inventory", "fact",
     "Smoked paprika and cumin run out first, and whole spices keep far longer than ground ones.", ("kitchen",)),
    ("f_rsvps", "Party RSVP list", "party-rsvp-list", "fact",
     "Thirty-two guests confirmed and three still owe a reply about the vegetarian option.", ("family",)),
    ("f_soup", "Lentil soup batch", "lentil-soup-batch", "technique",
     "A double batch of red lentil soup fills eight tubs and keeps in the freezer for three months.", ("kitchen",)),
    ("f_alarms", "Smoke alarm checks", "smoke-alarm-checks", "technique",
     "Test every alarm on the first of the month and swap the batteries each spring.", ("house",)),
    ("f_stamps", "Stamp collection catalogue", "stamp-collection-catalogue", "fact",
     "The album is sorted by country and duplicates go in the envelope for the club auction.", ("hobby",)),
    ("f_solar", "Solar panel output", "solar-panel-output", "finding",
     "Summer days produce around eighteen kilowatt hours and the darkest months fall below four.", ("house",)),
    ("f_filters", "Email filter rules", "email-filter-rules", "fact",
     "Newsletters skip the inbox and invoices from the energy supplier go to the bills folder.", ("computers",)),
    ("f_permit", "Residents parking permit", "residents-parking-permit", "fact",
     "The permit renews in March and visitor vouchers come in books of ten.", ("errands",)),
)

#: Every page's logical key -> its anchor kind (`note` for a page that is not an anchor).
KEY_KINDS: dict[str, str] = {
    **{row[0]: "entity" for row in ENTITIES},
    **{row[0]: "hub" for row in HUBS},
    **{row[0]: "resource" for row in RESOURCES},
    **{row[0]: "records_collection" for row in RECORDS},
    **{row[0]: "note" for row in FILLERS},
}
#: Every page's logical key -> the domain it belongs to.
KEY_DOMAINS: dict[str, str] = {
    **{row[0]: row[1] for row in ENTITIES},
    **{row[0]: row[1] for row in HUBS},
    **{row[0]: row[1] for row in RESOURCES},
    **{row[0]: row[1] for row in RECORDS},
    **{row[0]: "misc" for row in FILLERS},
}
_TITLES: dict[str, str] = {
    **{row[0]: row[2] for row in ENTITIES},
    **{row[0]: row[2] for row in HUBS},
    **{row[0]: row[3] for row in RESOURCES},
    **{row[0]: row[4] for row in RECORDS},
    **{row[0]: row[1] for row in FILLERS},
}


def title_of(key: str) -> str:
    """The page title (an anchor's own name) for a logical key."""

    try:
        return _TITLES[key]
    except KeyError as error:
        raise FixtureError(f"unknown corpus key {key!r}") from error


# --------------------------------------------------------------------------- #
# The cases
# --------------------------------------------------------------------------- #

_ABSENT = {"status": "unresolved", "carried_by": None, "origin": {}, "disambiguated_by": None}


def _user(text: str) -> dict:
    return {"role": "user", "text": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "text": text}


def _case(
    case_id: str,
    kind: str,
    turn: str,
    *,
    group: str,
    status: str,
    conversation: dict | None = None,
    gold: tuple[str, ...] = (),
    poison: tuple[str, ...] = (),
    must_include: tuple[str, ...] = (),
    must_exclude: tuple[str, ...] = (),
    carried_by: str | None = None,
    origin: dict[str, str] | None = None,
    arms: tuple[str, ...] = ARMS,
    disambiguated_by: str | None = None,
    arm_expectations: dict | None = None,
    pairs_with: str | None = None,
    caller: str = "owner",
    twin_mode: str | None = None,
) -> ConversationCase:
    return ConversationCase(
        case_id=case_id,
        pairs_with=pairs_with,
        group=group,
        kind=kind,
        turn=turn,
        conversation=conversation,
        gold=gold,
        poison=poison,
        must_include=must_include,
        must_exclude=must_exclude,
        expected_status=status,
        expected_carried_by=carried_by,
        expected_origin=tuple((origin or {}).items()),
        arms=arms,
        expected_disambiguated_by=disambiguated_by,
        arm_expectations=arm_expectations,
        caller=caller,
        twin_mode=twin_mode,
    )


def _twin(case: ConversationCase, case_id: str, mode: str, **values) -> ConversationCase:
    """The negative twin of a case. By default it expects nothing served (status
    ``unresolved``, no gold) and guards what the case serves; ``values`` override."""

    arguments: dict = {"status": "unresolved", "arms": case.arms}
    arguments.update(values)
    turn = arguments.pop("turn", case.turn)
    conversation = arguments.pop("conversation", case.conversation)
    return _case(
        case_id,
        f"{case.kind}_twin",
        turn,
        group=arguments.pop("group", case.group),
        conversation=conversation,
        pairs_with=case.case_id,
        twin_mode=mode,
        **arguments,
    )


#: The focus a content-free twin carries after the post-hoc correction (see
#: design.md): it names nothing in the corpus, so the twin tests a content-free
#: turn and not the ruled "focus is current-turn evidence" semantics.
_QUIET_FOCUS = "closing pleasantries"


def _quiet(case: ConversationCase) -> dict:
    """The case's conversation with its focus replaced by one that names nothing."""
    return {**(case.conversation or {}), "focus": _QUIET_FOCUS}


# -- Rich single turns: two to five sentences, no conversation ---------------- #

_RICH = (
    _case(
        "V1",
        "rich_single",
        "I have been going through the numbers all morning and the totals still do not add up. "
        "Before the trustees meet on Friday, pull together what the Harbor Lantern budget says about "
        "contingency and repair spend. I mostly want to know whether the reserve survives the second quarter.",
        group="rich_turn",
        gold=("h_harbor_lantern",),
        poison=("h_copperfield",),
        must_include=("caps the signal-repair spend at forty thousand",),
        must_exclude=("samples forty invoices per quarter",),
        status="resolved",
        origin={"h_harbor_lantern": "turn"},
    ),
    _case(
        "V2",
        "rich_single",
        "The roofer finally answered and the quote is higher than I hoped. "
        "I need a plain summary of where the Brindle Court renovation stands and what it is waiting on. "
        "Then we can decide whether to push the scaffolding date.",
        group="rich_turn",
        gold=("h_brindle",),
        poison=("e_lysander_crake",),
        must_include=("waiting on the roofer's quote",),
        must_exclude=("quotes in fixed lump sums",),
        status="resolved",
        origin={"h_brindle": "turn"},
    ),
    _case(
        "V3",
        "rich_two_domain",
        "Bastien Quill is back from the estuary and wants the volunteer rota sorted. "
        "Can you check what the Saltmarsh survey says about the transects before I reply to him? "
        "I would like to answer in one go.",
        group="rich_turn",
        gold=("e_bastien_quill", "h_saltmarsh"),
        poison=("r_pennywhistle_plotter",),
        must_include=("trains new volunteers on the tide tables", "six fixed transects"),
        must_exclude=("jams on damp rolls",),
        status="resolved",
        origin={"e_bastien_quill": "turn", "h_saltmarsh": "turn"},
    ),
    _case(
        "V4",
        "rich_single",
        "Wilhelmina Pryce sent over a draft unit yesterday evening. "
        "It has three pages of activities and a long reading list, and I have not opened it yet. "
        "What did she say she wanted to trial with her class this term?",
        group="rich_turn",
        gold=("e_wilhelmina_pryce",),
        poison=("h_wrenfield",),
        must_include=("pilots new units with her Thursday class",),
        must_exclude=("adds a shorter portfolio task",),
        status="resolved",
        origin={"e_wilhelmina_pryce": "turn"},
    ),
    _case(
        "V5",
        "rich_two_domain",
        "We need to settle the Mistral pricing review before the Cormorant winch goes out for its autumn service. "
        "The drum needs greasing first, which changes when we can free up the boat. "
        "Give me the pricing conclusion and the winch constraint together.",
        group="rich_turn",
        gold=("h_mistral", "r_cormorant_winch"),
        poison=("h_quarrel",),
        must_include=("recommends a ten percent discount", "grease the drum before every haul"),
        must_exclude=("skips the noon crossing in winter",),
        status="resolved",
        origin={"h_mistral": "turn", "r_cormorant_winch": "turn"},
    ),
    _case(
        "V6",
        "rich_two_domain",
        "Perpetua Holt asked about the last two entries in the Pontoon haul log before she signs off the maintenance spend. "
        "Pull the latest state from the log and tell me what she has to approve next. "
        "I would rather she hears it from me than from the yard.",
        group="rich_turn",
        gold=("e_perpetua_holt", "c_pontoon_haul"),
        poison=("h_harbor_lantern",),
        must_include=("approves any spend above five hundred", "cable inspected"),
        must_exclude=("caps the signal-repair spend at forty thousand",),
        status="resolved",
        origin={"e_perpetua_holt": "turn", "c_pontoon_haul": "turn"},
    ),
    _case(
        "V7",
        "rich_single",
        "The hosting people want a date and the cutover checklist is only half written. "
        "Tell me what the Nightjar migration says about the booking system and about the weekend nights. "
        "Keep it short, because I am calling them at noon.",
        group="rich_turn",
        gold=("h_nightjar",),
        poison=("h_brindle",),
        must_include=("two cutover waves",),
        must_exclude=("waiting on the roofer's quote",),
        status="resolved",
        origin={"h_nightjar": "turn"},
    ),
    _case(
        "V8",
        "rich_two_domain",
        "Marigold Tenby is sorting the marshal roster and I still owe her an answer about the Larkspur festival. "
        "Could you tell me how many marshals the parade route needs? "
        "She wants to close the list tonight.",
        group="rich_turn",
        gold=("e_marigold_tenby", "h_larkspur"),
        poison=("h_quarrel",),
        must_include=("keeps the marshal roster and the radio list", "eight volunteer marshals"),
        must_exclude=("skips the noon crossing in winter",),
        status="resolved",
        origin={"e_marigold_tenby": "turn", "h_larkspur": "turn"},
    ),
    _case(
        "V9",
        "rich_single",
        "Two candidates dropped out over the weekend and the shortlist looks thin. "
        "Remind me what the Kestrel hiring plan says about the analyst seats and about internal moves. "
        "I would like to decide before the March review comes around.",
        group="rich_turn",
        gold=("h_kestrel",),
        poison=("e_corvin_ashdown",),
        must_include=("holds two analyst seats open until the March review",),
        must_exclude=("screens the first-round candidates by phone",),
        status="resolved",
        origin={"h_kestrel": "turn"},
    ),
    _case(
        "V10",
        "rich_two_domain",
        "Corvin Ashdown asked whether the Pennywhistle plotter can print the candidate packs on waterproof paper. "
        "The last time we tried, the roll jammed halfway through the run. "
        "Tell me what we know before I answer him.",
        group="rich_turn",
        gold=("e_corvin_ashdown", "r_pennywhistle_plotter"),
        poison=("e_bastien_quill",),
        must_include=("screens the first-round candidates by phone", "jams on damp rolls"),
        must_exclude=("trains new volunteers on the tide tables",),
        status="resolved",
        origin={"e_corvin_ashdown": "turn", "r_pennywhistle_plotter": "turn"},
    ),
    _case(
        "V11",
        "rich_single",
        "The ferry timetable changed again and half of the crossings are gone. "
        "What does the Quarrel Bay ferry note say about Sundays and about the winter timetable? "
        "I am trying to decide whether the visit is still worth it.",
        group="rich_turn",
        gold=("h_quarrel",),
        poison=("h_nightjar",),
        must_include=("skips the noon crossing in winter",),
        must_exclude=("two cutover waves",),
        status="resolved",
        origin={"h_quarrel": "turn"},
    ),
    _case(
        "V12",
        "rich_single",
        "Ottilie Marsh emailed the entrants overnight and three of them replied straight away. "
        "I have not read any of it and I would like the short version. "
        "Who is she waiting on, and what does she want from us?",
        group="rich_turn",
        gold=("e_ottilie_marsh",),
        poison=("h_tidewater",),
        must_include=("scored the entries and prepares the follow-up cycle",),
        must_exclude=("second tranche needs the interim report",),
        status="resolved",
        origin={"e_ottilie_marsh": "turn"},
    ),
)

_RICH_TWINS = (
    _twin(
        _RICH[0], "W1", "empty_turn",
        turn="I have been going through my notes all morning and the plan for the weekend still does not settle. "
        "Before the others arrive on Friday, pull together whatever suits everyone and sounds easy. "
        "I mostly want to know whether the picnic survives the rain.",
        poison=("h_harbor_lantern",), must_exclude=("caps the signal-repair spend at forty thousand",),
    ),
    _twin(
        _RICH[1], "W2", "empty_turn",
        turn="The neighbours finally answered and the offer is better than I hoped. "
        "I need a plain summary of what a good afternoon would look like and what it is waiting on. "
        "Then we can decide whether to move the date.",
        poison=("h_brindle",), must_include=(), must_exclude=("waiting on the roofer's quote",),
    ),
    _twin(
        _RICH[2], "W3", "empty_turn",
        turn="Someone is back from the coast and wants the volunteer list sorted. "
        "Can you check what the notes say about the route before I reply? "
        "I would like to answer in one go.",
        poison=("e_bastien_quill", "h_saltmarsh"),
        must_exclude=("trains new volunteers on the tide tables", "six fixed transects"),
    ),
    _twin(
        _RICH[3], "W4", "empty_turn",
        turn="A draft came over yesterday evening. "
        "It has three pages of activities and a long reading list, and I have not opened it yet. "
        "What did the sender say they wanted to trial this term?",
        poison=("e_wilhelmina_pryce",), must_exclude=("pilots new units with her Thursday class",),
    ),
    _twin(
        _RICH[4], "W5", "empty_turn",
        turn="We need to settle the dinner menu before the weekend goes out on us. "
        "The oven needs cleaning first, which changes when we can start cooking. "
        "Give me the shopping list and the timing together.",
        poison=("h_mistral", "r_cormorant_winch"),
        must_exclude=("recommends a ten percent discount", "grease the drum before every haul"),
    ),
    _twin(
        _RICH[5], "W6", "empty_turn",
        turn="Someone asked about the last two entries in the shopping list before they sign off the weekend spend. "
        "Pull the latest quantities from the list and tell me what they have to buy next. "
        "I would rather they hear it from me than from the shop.",
        poison=("e_perpetua_holt", "c_pontoon_haul"),
        must_exclude=("approves any spend above five hundred", "cable inspected"),
    ),
    _twin(
        _RICH[6], "W7", "empty_turn",
        turn="The caterers want a date and the guest list is only half written. "
        "Tell me what the invitation says about the food and about the evening. "
        "Keep it short, because I am calling them at noon.",
        poison=("h_nightjar",), must_exclude=("two cutover waves",),
    ),
    _twin(
        _RICH[7], "W8", "empty_turn",
        turn="A friend is sorting the guest rota and I still owe an answer about the street party. "
        "Could you tell me how many chairs the tables need? "
        "The list closes tonight.",
        poison=("e_marigold_tenby", "h_larkspur"),
        must_exclude=("keeps the marshal roster and the radio list", "eight volunteer marshals"),
    ),
    _twin(
        _RICH[8], "W9", "empty_turn",
        turn="Two friends dropped out over the weekend and the outing looks thin. "
        "Remind me what the forecast says about the afternoon and about the trains. "
        "I would like to decide before the week comes around.",
        poison=("h_kestrel",), must_exclude=("holds two analyst seats open until the March review",),
    ),
    _twin(
        _RICH[9], "W10", "empty_turn",
        turn="A neighbour asked whether the printer can make the invitations on thick card. "
        "The last time we tried, the paper jammed halfway through the run. "
        "Tell me what we know before I answer.",
        poison=("e_corvin_ashdown", "r_pennywhistle_plotter"),
        must_exclude=("screens the first-round candidates by phone", "jams on damp rolls"),
    ),
    _twin(
        _RICH[10], "W11", "empty_turn",
        turn="The bus timetable changed again and half of the journeys are gone. "
        "What does the leaflet say about Sundays and about the winter service? "
        "I am trying to decide whether the trip is still worth it.",
        poison=("h_quarrel",), must_exclude=("skips the noon crossing in winter",),
    ),
    _twin(
        _RICH[11], "W12", "empty_turn",
        turn="A cousin emailed everyone overnight and three of them replied straight away. "
        "I have not read any of it and I would like the short version. "
        "Who is waiting on whom, and what do they want from us?",
        poison=("e_ottilie_marsh",), must_exclude=("scored the entries and prepares the follow-up cycle",),
    ),
)

# -- Multi-turn conversations ------------------------------------------------- #

_UNRELATED = {
    "focus": "the van and its tyres",
    "recent": [
        _user("Which pressure did we settle on for the van tyres when it is fully loaded?"),
        _assistant("Thirty-six psi loaded and thirty-two empty, as the note says."),
        _user("Good, and who is filling the urn on Monday?"),
    ],
}

_PROMOTION_1 = _case(
    "V13",
    "promotion",
    "Perpetua Holt has to approve the spend before the money moves, "
    "and I keep forgetting what the Tidewater people promised us.",
    group="multi_turn",
    conversation={
        "focus": "Tidewater grant tranche timing",
        "recent": [
            _user("The Tidewater grant is the one the panel approved in three tranches, right?"),
            _assistant("Yes, the Tidewater grant pays out in tranches and each release needs its own sign-off."),
            _user("Then I should ask about the second release before the month ends."),
            _assistant("Agreed. Do you want a reminder drafted for the treasurer?"),
        ],
    },
    gold=("e_perpetua_holt", "h_tidewater"),
    poison=("h_harbor_lantern",),
    must_include=("approves any spend above five hundred", "second tranche needs the interim report"),
    must_exclude=("caps the signal-repair spend at forty thousand",),
    status="resolved",
    origin={"e_perpetua_holt": "turn", "h_tidewater": "turn"},
    arm_expectations={
        "c": {"origin": {"e_perpetua_holt": "turn", "h_tidewater": "turn_and_focus"}},
        "d": {"origin": {"e_perpetua_holt": "turn", "h_tidewater": "turn_and_focus"}},
    },
)
_PROMOTION_2 = _case(
    "V14",
    "promotion",
    "Lysander Crake needs to price the slate roof before we go ahead with the Brindle side of things this month.",
    group="multi_turn",
    conversation={
        "focus": "the Brindle Court renovation quote",
        "recent": [
            _user("Where do we stand on the Brindle Court renovation?"),
            _assistant("It is waiting on the roofer's quote before the scaffolding goes up."),
            _user("Then the quote is the next step."),
        ],
        "refs": ["h_brindle"],
    },
    gold=("e_lysander_crake", "h_brindle"),
    poison=("h_nightjar",),
    must_include=("quotes in fixed lump sums", "waiting on the roofer's quote"),
    must_exclude=("two cutover waves",),
    status="resolved",
    origin={"e_lysander_crake": "turn", "h_brindle": "turn"},
    arm_expectations={
        "c": {"origin": {"e_lysander_crake": "turn", "h_brindle": "turn_and_focus"}},
        "d": {"origin": {"e_lysander_crake": "turn", "h_brindle": "turn_and_focus"}},
    },
)
_TIE_1 = _case(
    "V15",
    "tie_break",
    "Where did we land on Copperfield, the invoices or the night shift? I need to answer today.",
    group="multi_turn",
    conversation={
        "recent": [
            _user("Let me see the Copperfield audit findings first."),
            _assistant("As I recall, the Copperfield audit samples forty invoices every quarter."),
            _user("Good, that is the one I meant."),
        ],
    },
    gold=("h_copperfield",),
    poison=("h_copperfield_depot",),
    must_include=("samples forty invoices per quarter",),
    must_exclude=("rotates the night shift every third week",),
    status="resolved",
    origin={"h_copperfield": "turn"},
    arms=("a", "b", "d"),
    disambiguated_by="conversation",
)
_TIE_2 = _case(
    "V16",
    "tie_break",
    "What is the position on Ironbark, the countersignature or the removal van? The landlord keeps asking.",
    group="multi_turn",
    conversation={
        "recent": [
            _user("The landlord cares about the lease, so start with the Ironbark lease renewal."),
            _assistant("Yes, the Ironbark lease renewal is due in September."),
            _user("Right, that is what I need to know about."),
        ],
    },
    gold=("h_ironbark_lease",),
    poison=("h_ironbark",),
    must_include=("needs the landlord's countersignature",),
    must_exclude=("packs the archive last",),
    status="resolved",
    origin={"h_ironbark_lease": "turn"},
    arms=("a", "b", "d"),
    disambiguated_by="conversation",
)
_CARRY_1 = _case(
    "V17",
    "anaphoric_carry",
    "Given everything above, how did her results compare with the spring round, "
    "and should we change anything before the next one?",
    group="multi_turn",
    conversation={
        "focus": "Ottilie Marsh and the scoring notes",
        "recent": [
            _user("I want to go over Ottilie Marsh's scoring notes before Friday."),
            _assistant("Sure. Do you want the scores only, or her comments as well?"),
            _user("Comments as well, and anything unusual."),
            _assistant("Understood. I will start with the entries she flagged."),
        ],
    },
    gold=("e_ottilie_marsh",),
    poison=("f_spring_round",),
    must_include=("scored the entries and prepares the follow-up cycle",),
    must_exclude=("weights that were applied to the next round",),
    status="partial",
    carried_by="conversation",
    origin={"e_ottilie_marsh": "conversation"},
    arms=("a", "b", "c", "d"),
    arm_expectations={
        "c": {"status": "resolved", "carried_by": None, "origin": {"e_ottilie_marsh": "focus"}},
        "d": {"status": "resolved", "carried_by": None, "origin": {"e_ottilie_marsh": "focus"}},
    },
)
_CARRY_2 = _case(
    "V18",
    "anaphoric_carry",
    "Is she the right person to chase that up, or should I ask someone else to take it on?",
    group="multi_turn",
    conversation={
        "focus": "Marigold Tenby and the parade roster",
        "recent": [
            _user("Marigold Tenby has the marshal roster ready for the parade."),
            _assistant("Good. Eight marshals are confirmed and the radio list is next."),
            _user("Nobody has checked the radio list yet."),
        ],
    },
    gold=("e_marigold_tenby",),
    poison=("h_larkspur",),
    must_include=("keeps the marshal roster and the radio list",),
    must_exclude=("eight volunteer marshals",),
    status="partial",
    carried_by="conversation",
    origin={"e_marigold_tenby": "conversation"},
    arms=("a", "b", "c", "d"),
    arm_expectations={
        "c": {"status": "resolved", "carried_by": None, "origin": {"e_marigold_tenby": "focus"}},
        "d": {"status": "resolved", "carried_by": None, "origin": {"e_marigold_tenby": "focus"}},
    },
)
_SWITCH_1 = _case(
    "V19",
    "topic_switch",
    "What are the risks with that, and who is affected by it?",
    group="multi_turn",
    conversation={
        "focus": "the Kestrel hiring plan risks",
        "recent": [
            _user("Let's start with the Harbor Lantern budget and the repair spend."),
            _assistant("Understood. The Harbor Lantern budget caps the repair spend and reserves a tenth."),
            _user("Fine, that is settled. Actually, park it and look at the Kestrel hiring plan instead."),
            _assistant("Switching to the Kestrel hiring plan. Two analyst seats are held open."),
        ],
    },
    gold=("h_kestrel",),
    poison=("h_harbor_lantern",),
    must_include=("holds two analyst seats open until the March review",),
    must_exclude=("caps the signal-repair spend at forty thousand",),
    status="partial",
    carried_by="conversation",
    origin={"h_kestrel": "conversation"},
    arm_expectations={
        "c": {"status": "resolved", "carried_by": None, "origin": {"h_kestrel": "focus"}},
        "d": {"status": "resolved", "carried_by": None, "origin": {"h_kestrel": "focus"}},
    },
)
_SWITCH_2 = _case(
    "V20",
    "topic_switch",
    "Who could take that on, and would it be too much for them?",
    group="multi_turn",
    conversation={
        "focus": "the Larkspur festival marshals",
        "recent": [
            _user("Bastien Quill is late with the tide tables again."),
            _assistant("I can nudge him. Do you want a short message?"),
            _user("Leave that for now. Tell me where the Larkspur festival stands."),
            _assistant("The Larkspur festival still needs its marshals."),
        ],
    },
    gold=("h_larkspur",),
    poison=("e_bastien_quill",),
    must_include=("eight volunteer marshals",),
    must_exclude=("trains new volunteers on the tide tables",),
    status="partial",
    carried_by="conversation",
    origin={"h_larkspur": "conversation"},
    arm_expectations={
        "c": {"status": "resolved", "carried_by": None, "origin": {"h_larkspur": "focus"}},
        "d": {"status": "resolved", "carried_by": None, "origin": {"h_larkspur": "focus"}},
    },
)
_SWITCH_3 = _case(
    "V21",
    "topic_switch",
    "Is it safe to use this week, or should we book the inspector first?",
    group="multi_turn",
    conversation={
        "focus": "the Cormorant winch condition",
        "recent": [
            _user("Start with the Nightjar migration and the weekend cutover."),
            _assistant("Two cutover waves are planned, weekend nights only."),
            _user("Good. Now the Cormorant winch: has anybody greased it?"),
            _assistant("The drum is greased before every haul."),
        ],
    },
    gold=("r_cormorant_winch",),
    poison=("h_nightjar",),
    must_include=("grease the drum before every haul",),
    must_exclude=("two cutover waves",),
    status="partial",
    carried_by="conversation",
    origin={"r_cormorant_winch": "conversation"},
    arm_expectations={
        "c": {"status": "resolved", "carried_by": None, "origin": {"r_cormorant_winch": "focus"}},
        "d": {"status": "resolved", "carried_by": None, "origin": {"r_cormorant_winch": "focus"}},
    },
)
_DROWN_1 = _case(
    "V22",
    "drowning",
    "Wilhelmina Pryce sent the new unit yesterday; summarise where Wilhelmina Pryce stands before Monday.",
    group="multi_turn",
    conversation={
        "focus": "Wilhelmina Pryce and her new unit",
        "recent": [
            _user("Let's finish the Harbor Lantern budget before anything else."),
            _assistant("Sure. The Harbor Lantern budget caps the signal-repair spend."),
            _user("And the contingency line in the Harbor Lantern budget, is it a tenth?"),
            _assistant("Yes, a tenth of the Harbor Lantern budget is reserved."),
            _user("Then the Harbor Lantern budget is basically done, apart from the repair spend."),
            _assistant("Agreed. I will note the Harbor Lantern budget as ready for review."),
        ],
    },
    gold=("e_wilhelmina_pryce",),
    poison=("h_harbor_lantern",),
    must_include=("pilots new units with her Thursday class",),
    must_exclude=("caps the signal-repair spend at forty thousand",),
    status="resolved",
    origin={"e_wilhelmina_pryce": "turn"},
    arm_expectations={
        "c": {"origin": {"e_wilhelmina_pryce": "turn_and_focus"}},
        "d": {"origin": {"e_wilhelmina_pryce": "turn_and_focus"}},
    },
)
_DROWN_2 = _case(
    "V23",
    "drowning",
    "Book the Cormorant winch for Thursday and tell me when the drum was last greased.",
    group="multi_turn",
    conversation={
        "focus": "the Cormorant winch booking",
        "recent": [
            _user("Walk me through the Kestrel hiring plan one more time."),
            _assistant("The Kestrel hiring plan holds seats open and prefers internal moves."),
            _user("Which seats does the Kestrel hiring plan hold open, exactly?"),
            _assistant("The Kestrel hiring plan holds two analyst seats until the review."),
            _user("So the Kestrel hiring plan leaves us short until March."),
            _assistant("Yes, the Kestrel hiring plan is the constraint until March."),
        ],
    },
    gold=("r_cormorant_winch",),
    poison=("h_kestrel",),
    must_include=("grease the drum before every haul",),
    must_exclude=("holds two analyst seats open until the March review",),
    status="resolved",
    origin={"r_cormorant_winch": "turn"},
    arm_expectations={
        "c": {"origin": {"r_cormorant_winch": "turn_and_focus"}},
        "d": {"origin": {"r_cormorant_winch": "turn_and_focus"}},
    },
)
_DROWN_3 = _case(
    "V24",
    "drowning",
    "Perpetua Holt has asked for the receipts, so list what Perpetua Holt needs to approve each quarter.",
    group="multi_turn",
    conversation={
        "focus": "Perpetua Holt approvals",
        "recent": [
            _user("Give me the Saltmarsh survey counts for the last fortnight."),
            _assistant("The Saltmarsh survey counts wading birds along six transects."),
            _user("Were all six transects of the Saltmarsh survey walked at low tide?"),
            _assistant("The Saltmarsh survey walks them at low tide each fortnight."),
            _user("Then the Saltmarsh survey is on schedule, and I will say so."),
            _assistant("Agreed. I will record the Saltmarsh survey as on schedule."),
        ],
    },
    gold=("e_perpetua_holt",),
    poison=("h_saltmarsh",),
    must_include=("approves any spend above five hundred",),
    must_exclude=("six fixed transects",),
    status="resolved",
    origin={"e_perpetua_holt": "turn"},
    arm_expectations={
        "c": {"origin": {"e_perpetua_holt": "turn_and_focus"}},
        "d": {"origin": {"e_perpetua_holt": "turn_and_focus"}},
    },
)
_REFS_ONLY = _case(
    "V25",
    "refs_only",
    "Could you go through that again and tell me what I should do about it?",
    group="multi_turn",
    conversation={"refs": ["h_saltmarsh"]},
    poison=("h_saltmarsh",),
    must_exclude=("six fixed transects",),
    status="unresolved",
    arms=("a", "b", "d"),
)

_MULTI = (
    _PROMOTION_1, _PROMOTION_2, _TIE_1, _TIE_2, _CARRY_1, _CARRY_2,
    _SWITCH_1, _SWITCH_2, _SWITCH_3, _DROWN_1, _DROWN_2, _DROWN_3, _REFS_ONLY,
)

_MULTI_TWINS = (
    _twin(
        _PROMOTION_1, "W13", "unrelated_conversation",
        conversation=_UNRELATED,
        gold=("e_perpetua_holt",), poison=("h_tidewater",),
        must_include=("approves any spend above five hundred",),
        must_exclude=("second tranche needs the interim report",),
        status="resolved", origin={"e_perpetua_holt": "turn"},
    ),
    _twin(
        _PROMOTION_2, "W14", "unrelated_conversation",
        conversation=_UNRELATED,
        gold=("e_lysander_crake",), poison=("h_brindle",),
        must_include=("quotes in fixed lump sums",),
        must_exclude=("waiting on the roofer's quote",),
        status="resolved", origin={"e_lysander_crake": "turn"},
    ),
    _twin(
        _TIE_1, "W15", "unrelated_conversation",
        conversation={
            "recent": [
                _user("Which pressure did we settle on for the van tyres when it is fully loaded?"),
                _assistant("Thirty-six psi loaded and thirty-two empty, as the note says."),
                _user("Good, and who is filling the urn on Monday?"),
            ],
        },
        status="ambiguous", arms=("a", "b", "d"),
    ),
    _twin(
        _TIE_2, "W16", "unrelated_conversation",
        conversation={
            "recent": [
                _user("Which pressure did we settle on for the van tyres when it is fully loaded?"),
                _assistant("Thirty-six psi loaded and thirty-two empty, as the note says."),
                _user("Good, and who is filling the urn on Monday?"),
            ],
        },
        status="ambiguous", arms=("a", "b", "d"),
    ),
    _twin(
        _CARRY_1, "W17", "empty_turn",
        conversation=_quiet(_CARRY_1),
        turn="Thanks, all done for today.",
        poison=("e_ottilie_marsh", "f_spring_round"),
        must_exclude=("scored the entries and prepares the follow-up cycle", "weights that were applied to the next round"),
    ),
    _twin(
        _CARRY_2, "W18", "empty_turn",
        conversation=_quiet(_CARRY_2),
        turn="Good morning, I am back at my desk.",
        poison=("e_marigold_tenby",),
        must_exclude=("keeps the marshal roster and the radio list",),
    ),
    _twin(
        _SWITCH_1, "W19", "empty_turn",
        conversation=_quiet(_SWITCH_1),
        turn="Enough for now, talk later.",
        poison=("h_kestrel", "h_harbor_lantern"),
        must_exclude=("holds two analyst seats open until the March review", "caps the signal-repair spend at forty thousand"),
    ),
    _twin(
        _SWITCH_2, "W20", "empty_turn",
        conversation=_quiet(_SWITCH_2),
        turn="Right, I am heading out for lunch.",
        poison=("h_larkspur", "e_bastien_quill"),
        must_exclude=("eight volunteer marshals", "trains new volunteers on the tide tables"),
    ),
    _twin(
        _SWITCH_3, "W21", "empty_turn",
        conversation=_quiet(_SWITCH_3),
        turn="Lovely, see you after the break.",
        poison=("r_cormorant_winch", "h_nightjar"),
        must_exclude=("grease the drum before every haul", "two cutover waves"),
    ),
    _twin(
        _DROWN_1, "W22", "empty_turn",
        conversation=_quiet(_DROWN_1),
        turn="Anyway, what a grey afternoon.",
        poison=("h_harbor_lantern", "e_wilhelmina_pryce"),
        must_exclude=("caps the signal-repair spend at forty thousand", "pilots new units with her Thursday class"),
    ),
    _twin(
        _DROWN_2, "W23", "empty_turn",
        conversation=_quiet(_DROWN_2),
        turn="Fine, I will look in on Monday.",
        poison=("h_kestrel", "r_cormorant_winch"),
        must_exclude=("holds two analyst seats open until the March review", "grease the drum before every haul"),
    ),
    _twin(
        _DROWN_3, "W24", "empty_turn",
        conversation=_quiet(_DROWN_3),
        turn="Cheers, there is plenty of chat for one day.",
        poison=("h_saltmarsh", "e_perpetua_holt"),
        must_exclude=("six fixed transects", "approves any spend above five hundred"),
    ),
    _twin(
        _REFS_ONLY, "W25", "empty_turn",
        turn="Which day of the week is the parish meeting held?",
        poison=("h_saltmarsh",), must_exclude=("six fixed transects",),
    ),
)

# -- Pronoun-bearing negatives of the anaphoric carry (ruling C1 on #1463) ----- #
# Ordinary turns that carry "it", "that", "this", "next" or "other" but point
# back at nothing: an expletive "it", a complementiser or relative "that",
# temporal deixis, a same-turn antecedent, a closing acknowledgement. Each is
# sent with its carry case's earlier turns (the focus names nothing), and must
# serve nothing from them on every arm.


def _pronoun_negative(case: ConversationCase, case_id: str, turn: str) -> ConversationCase:
    return _twin(
        case, case_id, "pronoun_negative",
        conversation=_quiet(case),
        turn=turn,
        poison=case.gold,
        must_exclude=case.must_include,
    )


_PRONOUN_NEGATIVES = (
    *(
        _pronoun_negative(_CARRY_1, f"N{index}", turn)
        for index, turn in enumerate(
            (
                "Is it possible to install Python 3.13 on my laptop?",
                "It looks like rain later, should I bring an umbrella?",
                "It's raining again, what should I cook for dinner?",
                "What time is it in Tokyo right now?",
                "Would it be okay to swap the rice for quinoa in the recipe?",
                "It takes forty minutes to walk to the station.",
                "Let's switch to the grocery list, can you make it shorter?",
                "I bought a new kettle yesterday and it already leaks.",
                "I think that we should buy groceries on the way home.",
                "I know that tomatoes are technically a fruit.",
                "The other day I went hiking in the hills.",
            ),
            start=1,
        )
    ),
    *(
        _pronoun_negative(_CARRY_2, f"N{index}", turn)
        for index, turn in enumerate(
            (
                "It seems that the library closes early on Sundays.",
                "It turns out the bakery on the corner does gluten-free bread.",
                "It's hard to say which laptop is better for music.",
                "My sister said that the museum is free on Sundays.",
                "I hope that the weather holds for the picnic.",
                "I'm sure that the train leaves at nine.",
                "The recipe that my aunt sent needs two eggs.",
                "What should I cook this week for dinner?",
                "These days I mostly read on the train.",
                "Next week I want to try a new running route.",
                "Thanks, that's all for today.",
            ),
            start=12,
        )
    ),
)

# -- Attachments: a content-free turn plus cue-only focus ---------------------- #

_ATTACHMENT_1 = _case(
    "V26",
    "attachment",
    "thoughts on this?",
    group="attachment",
    conversation={"focus": "Ottilie Marsh, Tidewater grant"},
    gold=("e_ottilie_marsh", "h_tidewater"),
    poison=("h_harbor_lantern",),
    must_include=("scored the entries and prepares the follow-up cycle", "second tranche needs the interim report"),
    must_exclude=("caps the signal-repair spend at forty thousand",),
    status="resolved",
    origin={"e_ottilie_marsh": "focus", "h_tidewater": "focus"},
    arms=("a", "c", "d"),
    arm_expectations={"a": dict(_ABSENT)},
)
_ATTACHMENT_2 = _case(
    "V27",
    "attachment",
    "any comments on these two?",
    group="attachment",
    conversation={"focus": "Nightjar migration, Lysander Crake"},
    gold=("h_nightjar", "e_lysander_crake"),
    poison=("h_brindle",),
    must_include=("two cutover waves", "quotes in fixed lump sums"),
    must_exclude=("waiting on the roofer's quote",),
    status="resolved",
    origin={"h_nightjar": "focus", "e_lysander_crake": "focus"},
    arms=("a", "c", "d"),
    arm_expectations={"a": dict(_ABSENT)},
)
# Added post hoc (design.md, "Post-hoc fixture correction"): a content-free turn
# whose focus DOES name an earlier subject. `focus` is current-turn evidence by
# ruling, so arms (c) and (d) resolve it and arm (a) abstains. It keeps that
# ruling pinned now that twins W17 to W24 carry a focus that names nothing.
_FOCUS_NAMES_SUBJECT = _case(
    "V29",
    "attachment",
    "Thanks, all done for today.",
    group="attachment",
    conversation={"focus": "Ottilie Marsh and the scoring notes"},
    gold=("e_ottilie_marsh",),
    poison=("f_spring_round",),
    must_include=("scored the entries and prepares the follow-up cycle",),
    must_exclude=("weights that were applied to the next round",),
    status="resolved",
    origin={"e_ottilie_marsh": "focus"},
    arms=("a", "c", "d"),
    arm_expectations={"a": dict(_ABSENT)},
)
_ATTACHMENT_TWIN = _twin(
    _ATTACHMENT_1, "W26", "unrelated_conversation",
    conversation={"focus": "Dunmere sketch, Velcott brief"},
    poison=("e_ottilie_marsh", "h_tidewater"),
    must_exclude=("scored the entries and prepares the follow-up cycle", "second tranche needs the interim report"),
    arms=("a", "c", "d"),
)

# -- The withheld-versus-absent pair (a restricted caller, byte identity) ------ #

_WITHHELD = _case(
    "V28",
    "withheld_ref",
    "Given all of that, what should we change before the next one?",
    group="withheld",
    conversation={
        "recent": [
            _user("Compare the Kestrel hiring plan against the Osprey escrow ledger before the meeting."),
            _assistant("Will do. I will start with the seats and then the deposits."),
            _user("Also keep the retention deposits in mind."),
        ],
        "refs": ["h_kestrel", WITHHELD_KEY],
    },
    gold=("h_kestrel",),
    poison=(WITHHELD_KEY,),
    must_include=("holds two analyst seats open until the March review",),
    must_exclude=("reconciled on the last working day",),
    status="partial",
    carried_by="conversation",
    origin={"h_kestrel": "conversation"},
    arms=("a", "b", "d"),
    pairs_with="W28",
    caller="restricted",
)
_ABSENT_REF = _case(
    "W28",
    "absent_ref",
    "Given all of that, what should we change before the next one?",
    group="withheld",
    conversation={
        "recent": [
            _user("Compare the Kestrel hiring plan against the Plover escrow ledger before the meeting."),
            _assistant("Will do. I will start with the seats and then the deposits."),
            _user("Also keep the retention deposits in mind."),
        ],
        "refs": ["h_kestrel"],
    },
    gold=("h_kestrel",),
    poison=(WITHHELD_KEY,),
    must_include=("holds two analyst seats open until the March review",),
    must_exclude=("reconciled on the last working day",),
    status="partial",
    carried_by="conversation",
    origin={"h_kestrel": "conversation"},
    arms=("a", "b", "d"),
    pairs_with="V28",
    caller="restricted",
)

CASES: tuple[ConversationCase, ...] = (
    *_RICH,
    *_MULTI,
    _ATTACHMENT_1,
    _ATTACHMENT_2,
    _FOCUS_NAMES_SUBJECT,
    _WITHHELD,
    *_RICH_TWINS,
    *_MULTI_TWINS,
    _ATTACHMENT_TWIN,
    _ABSENT_REF,
    *_PRONOUN_NEGATIVES,
)
CASE_IDS: tuple[str, ...] = tuple(case.case_id for case in CASES)


# --------------------------------------------------------------------------- #
# Arms, digest, manifest checks
# --------------------------------------------------------------------------- #

#: Every text a corpus page must never contain verbatim or near-verbatim: the
#: turns, each earlier entry and each `focus` line.
LEAK_CHECKED_TEXTS: tuple[str, ...] = tuple(
    dict.fromkeys(
        text
        for case in CASES
        for text in (
            case.turn,
            *(entry["text"] for entry in ((case.conversation or {}).get("recent") or ())),
            (case.conversation or {}).get("focus") or "",
        )
        if text.strip()
    )
)


def case_by_id(case_id: str) -> ConversationCase:
    for case in CASES:
        if case.case_id == case_id:
            return case
    raise FixtureError(f"unknown conversation case id {case_id!r}")


def conversation_for_arm(
    case: ConversationCase, arm: str, key_to_ref: dict[str, str]
) -> dict | None:
    """The `conversation` argument the case sends on one arm, with each logical
    ref key resolved to the page's canonical ref (an entity's is its
    ``exomem://memory/<id>`` handle, not its path; the runner reads it from the
    activation index).

    (a) none; (b) `recent` and `refs`; (c) `focus`; (d) all three. ``None``
    when the arm carries nothing, which is a request without `conversation`.
    """

    if arm not in ARMS:
        raise FixtureError(f"unknown arm {arm!r}")
    conversation = case.conversation
    if arm == "a" or not conversation:
        return None
    request: dict = {}
    if arm in ("c", "d") and conversation.get("focus"):
        request["focus"] = conversation["focus"]
    if arm in ("b", "d"):
        if conversation.get("recent"):
            request["recent"] = [dict(entry) for entry in conversation["recent"]]
        if conversation.get("refs"):
            try:
                request["refs"] = [key_to_ref[key] for key in conversation["refs"]]
            except KeyError as error:
                raise FixtureError(f"{case.case_id}: ref {error.args[0]!r} names no corpus page") from error
    return request or None


def fixture_set_digest(cases: Iterable[ConversationCase] = CASES) -> str:
    """Stable sha256 over every field of every case, ordered by case id."""

    rows = sorted(
        ({field.name: getattr(case, field.name) for field in fields(ConversationCase)} for case in cases),
        key=lambda row: row["case_id"],
    )
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def assert_manifest_consistent(cases: tuple[ConversationCase, ...] = CASES) -> None:
    """Refuse a duplicate id, an undeclared key, overlapping gold and poison, a
    twin that pairs with the wrong kind, and a rich or multi-turn case without
    exactly one twin."""

    ids = [case.case_id for case in cases]
    if len(set(ids)) != len(ids):
        raise FixtureError("duplicate case_id in the conversation set")
    by_id = {case.case_id: case for case in cases}
    for case in cases:
        referenced = [
            *case.gold,
            *case.poison,
            *(key for key, _origin in case.expected_origin),
            *((case.conversation or {}).get("refs") or ()),
        ]
        unknown = [key for key in referenced if key not in KEY_KINDS]
        if unknown:
            raise FixtureError(f"{case.case_id}: keys {unknown} name no corpus page")
        if not set(case.gold).isdisjoint(case.poison):
            raise FixtureError(f"{case.case_id}: gold and poison overlap")
        if case.kind == "absent_ref" or case.kind == "withheld_ref":
            partner = by_id.get(case.pairs_with or "")
            if partner is None or partner.pairs_with != case.case_id or {case.kind, partner.kind} != {
                "absent_ref",
                "withheld_ref",
            }:
                raise FixtureError(f"{case.case_id}: the withheld and absent cases must pair with each other")
            continue
        if case.kind.endswith("_twin"):
            positive = by_id.get(case.pairs_with or "")
            if positive is None or f"{positive.kind}_twin" != case.kind:
                raise FixtureError(f"{case.case_id}: pairs_with must name a {case.kind.removesuffix('_twin')} case")
            if positive.group != case.group:
                raise FixtureError(f"{case.case_id}: a twin must share its case's group")
            if case.twin_mode is None:
                raise FixtureError(f"{case.case_id}: a twin declares its twin_mode")
        elif case.pairs_with is not None:
            raise FixtureError(f"{case.case_id}: only a twin pairs with a case")
    for case in cases:
        if case.group in ("rich_turn", "multi_turn") and case.kind in POSITIVE_KINDS:
            twins = [
                twin
                for twin in cases
                if twin.pairs_with == case.case_id
                and twin.kind == f"{case.kind}_twin"
                and twin.twin_mode != "pronoun_negative"
            ]
            if len(twins) != 1:
                raise FixtureError(f"{case.case_id}: a case needs exactly one negative twin")
    for case in cases:
        if case.twin_mode == "pronoun_negative" and case.kind != "anaphoric_carry_twin":
            raise FixtureError(f"{case.case_id}: a pronoun negative pairs with an anaphoric-carry case")
    if any(case.group == "attachment" and case.kind == "attachment" for case in cases) and not any(
        case.kind == "attachment_twin" for case in cases
    ):
        raise FixtureError("the attachment cases need a twin whose cues name nothing")


# --------------------------------------------------------------------------- #
# The corpus build
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConversationCorpusManifest:
    corpus_id: str
    fixture_set_digest: str
    key_to_path: dict[str, str]
    #: Exact canonical bytes (the English set's ``corpus_hash``). It moves with
    #: every write receipt the product writers mint, so two builds differ.
    corpus_hash: str
    #: The same pages with those receipts (minted ids, audit trailers) removed:
    #: identical for every build of one fixture set.
    logical_hash: str


_RECORDS_FIELDS = (
    "    observed_on:\n"
    "      type: date\n"
    "      required: true\n"
    "    subject:\n"
    "      type: string\n"
    "      required: true\n"
    "    state:\n"
    "      type: string"
)


def _withhold_governed_page(root: Path) -> None:
    """Withhold the one governed page from the restricted audience.

    Policy is authored as the governance tests author it (a scope naming the
    page and a ceiling-0 rule for the restricted audience, as YAML under the
    vault's governance directory, which is what the compiled policy loads).
    The authoring tool's propose-and-commit is not used: its commit verifies a
    catalogue composite that a cold fixture vault does not serve, and it refuses
    ("prepared composite does not match") before writing anything.
    """

    directory = root / "Knowledge Base" / "_Governance"
    for relative, text in (
        (
            "scopes/osprey-ledger.yaml",
            "governance_version: 1\n"
            f"id: {_SCOPE_ID}\n"
            "name: Osprey escrow ledger\n"
            f'paths: ["{_WITHHELD_PATH}"]\n',
        ),
        (
            "rules/osprey-ledger-restricted.yaml",
            "governance_version: 1\n"
            f"id: {_RULE_ID}\n"
            f'scope_ids: ["{_SCOPE_ID}"]\n'
            f"audience: {RESTRICTED_AUDIENCE}\n"
            "ceiling: 0\n",
        ),
    ):
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _logical_corpus_hash(root: Path, key_to_path: dict[str, str]) -> str:
    """Digest the corpus with the product writers' own write receipts removed."""

    pages: list[tuple[str, str]] = []
    for path in sorted((root / "Knowledge Base").rglob("*")):
        relative = path.relative_to(root).as_posix()
        if (
            not path.is_file()
            or path.suffix.lower() not in {".md", ".yaml", ".yml"}
            or path.name in {"index.md", "log.md"}
            or relative.startswith("Knowledge Base/_Schema/")
        ):
            continue
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^exomem_id:\s*[^\n]+\n", "", text)
        text = re.sub(r"(?m)^(?:created|updated):\s*[^\n]+\n", "", text)
        text = re.sub(r"(?m)^(?:plan|record)_audit:\s*[^\n]+\n", "", text)
        text = re.sub(r"(?m)^# exomem-(?:plan|record)-audit:\s*[^\n]+\n", "", text)
        text = re.sub(
            r"(?m)^<!-- exomem-item-presentation:v1 [^\n]+ -->\n", "<!-- exomem-item-presentation:v1 -->\n", text
        )
        pages.append((relative, text))
    payload = {
        "corpus_id": CORPUS_ID,
        "fixture_set_digest": fixture_set_digest(),
        "key_to_path": dict(sorted(key_to_path.items())),
        "pages": pages,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _build_corpus_in_process(root: Path) -> ConversationCorpusManifest:
    from exomem.init import init_vault

    root = Path(root)
    init_vault(root)
    key_to_path: dict[str, str] = {}
    for key, _domain, name, slug, summary, tags in ENTITIES:
        key_to_path[key] = _entity(root, name=name, slug=slug, summary=summary, tags=tags)
    for key, _domain, title, slug, observation, tags in HUBS:
        key_to_path[key] = _compiled_note(
            root, title=title, slug=slug, observation=observation, category="hub", tags=tags
        )
    for key, _domain, path, title, observation, tags in RESOURCES:
        key_to_path[key] = _governed_resource(root, path=path, title=title, observation=observation, tags=tags)
    for key, _domain, manifest_path, exomem_id, title, claims, items in RECORDS:
        key_to_path[key] = _create_records_collection(
            root,
            manifest_path=manifest_path,
            manifest_text=_records_manifest(
                exomem_id=exomem_id,
                title=title,
                source="Items",
                claims=claims,
                fields=_RECORDS_FIELDS,
                natural_key="observed_on, subject",
            ),
            items=items,
        )
    for key, title, slug, category, observation, tags in FILLERS:
        key_to_path[key] = _compiled_note(
            root, title=title, slug=slug, observation=observation, category=category, tags=tags
        )
    _withhold_governed_page(root)

    leaks = find_verbatim_leaks(root, turns=LEAK_CHECKED_TEXTS) + find_normalized_leaks(
        root, turns=LEAK_CHECKED_TEXTS
    )
    if leaks:
        raise FixtureError(f"conversation fixture text leaked into the corpus: {leaks!r}")
    return ConversationCorpusManifest(
        corpus_id=CORPUS_ID,
        fixture_set_digest=fixture_set_digest(),
        key_to_path=dict(sorted(key_to_path.items())),
        corpus_hash=_corpus_hash(root),
        logical_hash=_logical_corpus_hash(root, key_to_path),
    )


def _run_isolated_build_request(request_path: str, result_path: str) -> None:
    """Child-process entry point for one isolated corpus build."""

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    manifest = _build_corpus_in_process(Path(request["root"]))
    Path(result_path).write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def build_corpus(root: Path, *, timeout: float = 900.0) -> ConversationCorpusManifest:
    """Build the corpus in a child with private state, leases, config and logs,
    exactly as the English set's ``build_corpus`` does: the caller's environment
    and singletons are never rebound, and a writer refusal fails the build."""

    root = Path(root).resolve()
    repository = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix="exomem-context-activation-conversation-") as runtime_raw:
        runtime = Path(runtime_raw)
        directories = {
            name: runtime / name for name in ("state", "xdg-state", "logs", "call-ledger", "writer-lease", "tmp")
        }
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True)
        request, result = runtime / "request.json", runtime / "result.json"
        request.write_text(json.dumps({"root": str(root)}), encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("EXOMEM_")}
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    path
                    for path in (
                        str(repository / "src"),
                        str(repository / "benchmarks"),
                        environment.get("PYTHONPATH", ""),
                    )
                    if path
                ),
                "EXOMEM_STATE_ROOT": str(directories["state"]),
                "EXOMEM_CONFIG_PATH": str(runtime / "config.json"),
                "EXOMEM_LOG_DIR": str(directories["logs"]),
                "EXOMEM_CALL_LEDGER_DIR": str(directories["call-ledger"]),
                "EXOMEM_WRITER_LEASE_STATE_DIR": str(directories["writer-lease"]),
                "EXOMEM_LEASE_COORDINATOR_DB": str(runtime / "lease-coordinator.sqlite"),
                "EXOMEM_VAULT_PATH": str(root),
                "EXOMEM_DISABLE_EMBEDDINGS": "1",
                "EXOMEM_DISABLE_GRAPH_DRAIN": "1",
                "EXOMEM_DISABLE_GRAPH_SCHEDULING": "1",
                "XDG_STATE_HOME": str(directories["xdg-state"]),
                "TMPDIR": str(directories["tmp"]),
            }
        )
        code = (
            "from epistemic.corpora.context_activation_conversation import _run_isolated_build_request; "
            f"_run_isolated_build_request({str(request)!r}, {str(result)!r})"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise FixtureError(f"isolated conversation corpus build exceeded {timeout:.0f} seconds") from error
        if completed.returncode != 0:
            detail = completed.stderr[-4000:].strip() or completed.stdout[-4000:].strip()
            raise FixtureError(
                f"isolated conversation corpus build failed with exit {completed.returncode}: {detail}"
            )
        if not result.is_file():
            raise FixtureError("isolated conversation corpus build returned no manifest")
        return ConversationCorpusManifest(**json.loads(result.read_text(encoding="utf-8")))


__all__ = [
    "ARMS",
    "CALLERS",
    "CARRIED_BY",
    "CASES",
    "CASE_IDS",
    "CORPUS_ID",
    "ENTITIES",
    "ENTITY_NAMES",
    "FILLERS",
    "FIXTURE_SET_ID",
    "GROUPS",
    "HUBS",
    "KEY_DOMAINS",
    "KEY_KINDS",
    "KINDS",
    "LEAK_CHECKED_TEXTS",
    "ORIGINS",
    "POSITIVE_KINDS",
    "RECORDS",
    "RESOURCES",
    "RESTRICTED_AUDIENCE",
    "TWIN_MODES",
    "WITHHELD_KEY",
    "ConversationCase",
    "ConversationCorpusManifest",
    "FixtureError",
    "assert_manifest_consistent",
    "build_corpus",
    "case_by_id",
    "conversation_for_arm",
    "fixture_set_digest",
    "title_of",
]
