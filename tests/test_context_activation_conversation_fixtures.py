"""The pre-registered `conversation` benchmark group (thread-aware compilation, S0).

`context-activation-conversation-v1` is a SIBLING of the digest-pinned English
set and of the multilingual set: its own cases, its own corpus, its own digest.
These tests pin the manifest (every population the OpenSpec requirement names,
a negative twin for every case, gold/poison/must-include/must-exclude and the
expected status, `carried_by` and `origin` on every case) and the built corpus
(invented names only, no fixture text verbatim in any page, one page governed
and withheld from a restricted audience). They import only the corpus and the
product's own index: no scorer, no model.

The digest is pinned as a literal in this file. It is authored and committed
before the group's first scored run; a later fixture edit voids the runs that
reference the old digest instead of rescoring them.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from epistemic.corpora import context_activation as english_set
from epistemic.corpora.context_activation_conversation import (
    ARMS,
    CALLERS,
    CASE_IDS,
    CASES,
    CORPUS_ID,
    ENTITY_NAMES,
    FIXTURE_SET_ID,
    KEY_DOMAINS,
    KEY_KINDS,
    LEAK_CHECKED_TEXTS,
    ORIGINS,
    POSITIVE_KINDS,
    WITHHELD_KEY,
    ConversationCase,
    FixtureError,
    assert_manifest_consistent,
    build_corpus,
    case_by_id,
    conversation_for_arm,
    fixture_set_digest,
    title_of,
)

from exomem import working_set_index
from exomem.public_artifact_privacy import assert_public_artifacts_clean

pytestmark = pytest.mark.timeout(600)

#: The English set's digest. The conversation group must never edit that module.
ENGLISH_SET_DIGEST = "a49d85f49b18c2ce8f0349933ed01ceb4fb5ca176dca066700c9c2f93605426f"

#: The pinned digest of this group. Editing any fixture field, gold list
#: included, changes it, and that voids every run manifest that names the old one.
CONVERSATION_SET_DIGEST = "f8cad6d57da87d8c29767e76d7d71a968ada27f182594d0fbeb4798a2d56b59b"

RICH = [case for case in CASES if case.group == "rich_turn" and case.kind in POSITIVE_KINDS]
MULTI = [case for case in CASES if case.group == "multi_turn" and case.kind in POSITIVE_KINDS]
ATTACHMENTS = [
    case
    for case in CASES
    if case.group == "attachment" and case.kind in POSITIVE_KINDS and case.case_id != "V29"
]
PROJECT_WORDS = re.compile(r"\b(work|personal|project|projects|alpha|beta)\b", re.IGNORECASE)
ANAPHORS = re.compile(
    r"\b(he|she|it|they|him|her|his|hers|its|their|theirs|them|that|this|those|these|"
    r"the former|the latter|(?:first|second|third|fourth|last) (?:one|option))\b",
    re.IGNORECASE,
)


def _sentences(text: str) -> int:
    return len([part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part])


def _twins_of(case: ConversationCase) -> list[ConversationCase]:
    """The case's one negative twin (the pronoun-bearing negatives are extra)."""
    return [
        item
        for item in CASES
        if item.pairs_with == case.case_id
        and item.kind == f"{case.kind}_twin"
        and item.twin_mode != "pronoun_negative"
    ]


PRONOUN_NEGATIVES = [case for case in CASES if case.twin_mode == "pronoun_negative"]


def _with(case: ConversationCase, **change) -> tuple[ConversationCase, ...]:
    edited = dataclasses.replace(case, **change)
    return tuple(edited if item.case_id == case.case_id else item for item in CASES)


# --------------------------------------------------------------------------- #
# The manifest (pure)
# --------------------------------------------------------------------------- #


def test_the_set_is_a_sibling_and_leaves_the_english_set_byte_identical() -> None:
    assert FIXTURE_SET_ID == "context-activation-conversation-v1"
    assert FIXTURE_SET_ID != english_set.FIXTURE_SET_ID
    assert CORPUS_ID != english_set.CORPUS_ID
    assert set(CASE_IDS).isdisjoint(english_set.CASE_IDS + english_set.TWIN_IDS)
    assert english_set.fixture_set_digest() == ENGLISH_SET_DIGEST
    assert len(english_set.FIXTURES) == 18


def test_the_digest_is_pinned_as_a_literal() -> None:
    assert fixture_set_digest() == CONVERSATION_SET_DIGEST


def test_manifest_is_internally_consistent() -> None:
    assert_manifest_consistent()
    assert len(CASE_IDS) == len(set(CASE_IDS)) == len(CASES)


def test_twelve_rich_single_turns_of_two_to_five_sentences() -> None:
    assert len(RICH) == 12
    for case in RICH:
        assert 2 <= _sentences(case.turn) <= 5, case.case_id
        assert case.conversation is None, case.case_id
        assert case.arms == ARMS, case.case_id


def test_at_least_four_rich_turns_span_two_domains() -> None:
    two = [case for case in RICH if len({KEY_DOMAINS[key] for key in case.gold}) >= 2]
    assert len(two) >= 4, [case.case_id for case in two]
    for case in two:
        assert len({KEY_KINDS[key] for key in case.gold}) >= 2, "two hubs of one kind would compete"


def test_twelve_multi_turn_conversations_of_three_to_six_earlier_entries() -> None:
    assert len(MULTI) >= 12
    for case in MULTI:
        conversation = case.conversation or {}
        recent = conversation.get("recent") or []
        if case.kind == "refs_only":
            assert not recent and conversation.get("refs"), case.case_id
            continue
        assert 3 <= len(recent) <= 6, case.case_id
        assert all(entry["role"] in {"user", "assistant"} and entry["text"].strip() for entry in recent)
        # Inside the server's own bounds, so the fixture is `applied`, never `truncated`.
        assert all(len(entry["text"]) <= (600 if entry["role"] == "user" else 300) for entry in recent)
        assert sum(len(entry["text"]) for entry in recent) <= 2400, case.case_id
        assert len(conversation.get("focus") or "") <= 240
        assert len(conversation.get("refs") or []) <= 12
        assert any(entry["role"] == "user" for entry in recent), case.case_id


def test_every_rich_and_multi_turn_case_has_exactly_one_negative_twin() -> None:
    for case in (*RICH, *MULTI):
        twins = _twins_of(case)
        assert len(twins) == 1, case.case_id
        twin = twins[0]
        assert twin.group == case.group
        assert twin.twin_mode in {"unrelated_conversation", "empty_turn"}
        if twin.twin_mode == "unrelated_conversation":
            assert twin.turn == case.turn
            assert twin.conversation != case.conversation
        else:
            assert twin.turn != case.turn
            # Post-hoc correction (design.md): a content-free twin keeps the case's
            # conversation but its focus, if any, names nothing.
            assert {k: v for k, v in (twin.conversation or {}).items() if k != "focus"} == {
                k: v for k, v in (case.conversation or {}).items() if k != "focus"
            }
            assert (twin.conversation or {}).get("focus") in (None, "closing pleasantries")
            assert not ANAPHORS.search(twin.turn) or case.kind.startswith("rich"), twin.case_id
        assert set(twin.gold).isdisjoint(case.poison), twin.case_id


def test_at_least_twenty_pronoun_bearing_negatives_guard_the_anaphoric_carry() -> None:
    """Ruling C1 on #1463: ordinary turns carrying a pronoun, a demonstrative or
    a temporal deictic that points back at nothing, sent with a carry case's
    earlier turns, must never be carried and never serve that subject."""
    assert len(PRONOUN_NEGATIVES) >= 20
    for negative in PRONOUN_NEGATIVES:
        case = case_by_id(negative.pairs_with)
        assert case.kind == "anaphoric_carry" and negative.kind == "anaphoric_carry_twin"
        assert ANAPHORS.search(negative.turn) or re.search(r"\b(next|other)\b", negative.turn, re.I), negative.case_id
        assert negative.conversation["recent"] == case.conversation["recent"], negative.case_id
        assert negative.conversation["focus"] == "closing pleasantries", negative.case_id
        assert negative.arms == ARMS
        assert negative.expected_status == "unresolved" and negative.expected_carried_by is None
        assert set(case.gold) <= set(negative.poison), negative.case_id
        assert set(case.must_include) <= set(negative.must_exclude), negative.case_id


def test_a_twin_never_expects_more_than_its_case_resolves() -> None:
    for case in (*RICH, *MULTI, *ATTACHMENTS):
        for twin in _twins_of(case):
            assert twin.expected_carried_by is None, twin.case_id
            assert twin.expected_status in {"unresolved", "ambiguous", "resolved"}, twin.case_id
            assert twin.expected_status != "partial", twin.case_id
            if case.gold:
                assert set(case.gold) - set(twin.gold), f"{twin.case_id} must not serve all of its case's gold"


def test_at_least_three_drowning_cases_a_long_conversation_about_another_subject() -> None:
    drowning = [case for case in MULTI if case.kind == "drowning"]
    assert len(drowning) >= 3
    for case in drowning:
        recent = case.conversation["recent"]
        assert len(recent) == 6, case.case_id
        assert case.poison and case.gold
        for key in case.poison:
            alias = _title_of(key).casefold()
            assert sum(alias in entry["text"].casefold() for entry in recent) >= 4, (case.case_id, key)
        for key in case.gold:
            assert _title_of(key).casefold() in case.turn.casefold(), (case.case_id, key)
            assert all(_title_of(key).casefold() not in entry["text"].casefold() for entry in recent)
        assert not ANAPHORS.search(case.turn), case.case_id
        assert case.expected_carried_by is None
        assert case.expected_status == "resolved"
        assert case.must_exclude, case.case_id


def test_at_least_three_topic_switches_the_newest_subject_is_gold_the_older_poison() -> None:
    switches = [case for case in MULTI if case.kind == "topic_switch"]
    assert len(switches) >= 3
    for case in switches:
        users = [entry["text"].casefold() for entry in case.conversation["recent"] if entry["role"] == "user"]
        assert len(users) >= 2, case.case_id
        (gold,) = case.gold
        assert _title_of(gold).casefold() in users[-1], case.case_id
        assert all(_title_of(gold).casefold() not in text for text in users[:-1]), case.case_id
        assert case.poison and all(_title_of(key).casefold() in " ".join(users[:-1]) for key in case.poison)
        assert all(_title_of(key).casefold() not in users[-1] for key in case.poison)
        assert ANAPHORS.search(case.turn), case.case_id
        assert case.expected_carried_by == "conversation"
        assert case.expected_status == "partial"
        assert dict(case.expected_origin) == {gold: "conversation"}
        assert case.must_exclude, case.case_id


def test_every_target_behaviour_the_design_names_has_a_case() -> None:
    kinds = {case.kind for case in MULTI}
    assert kinds >= {"promotion", "tie_break", "anaphoric_carry", "drowning", "topic_switch", "refs_only"}
    promotion = next(case for case in MULTI if case.kind == "promotion")
    assert len(promotion.gold) == 2 and promotion.expected_status == "resolved"
    tie = next(case for case in MULTI if case.kind == "tie_break")
    assert tie.expected_disambiguated_by == "conversation" and len(tie.gold) == 1 and tie.poison
    carry = next(case for case in MULTI if case.kind == "anaphoric_carry")
    assert carry.expected_carried_by == "conversation" and carry.expected_status == "partial"
    assert dict(carry.expected_origin) == {carry.gold[0]: "conversation"} and len(carry.gold) == 1
    assert ANAPHORS.search(carry.turn)
    refs = next(case for case in MULTI if case.kind == "refs_only")
    assert refs.expected_status == "unresolved" and refs.expected_carried_by is None and refs.poison


def test_the_ambiguity_twin_keeps_the_ambiguity() -> None:
    tie = next(case for case in MULTI if case.kind == "tie_break")
    (twin,) = _twins_of(tie)
    assert twin.expected_status == "ambiguous" and twin.expected_disambiguated_by is None


def test_the_withheld_and_absent_pair_differ_only_by_the_withheld_page() -> None:
    withheld = case_by_id(next(case.case_id for case in CASES if case.kind == "withheld_ref"))
    (absent,) = [case for case in CASES if case.kind == "absent_ref"]
    assert withheld.pairs_with == absent.case_id and absent.pairs_with == withheld.case_id
    assert withheld.caller == absent.caller == "restricted"
    assert withheld.turn == absent.turn
    assert WITHHELD_KEY in withheld.conversation["refs"]
    assert WITHHELD_KEY not in absent.conversation["refs"]
    assert [ref for ref in withheld.conversation["refs"] if ref != WITHHELD_KEY] == absent.conversation["refs"]
    assert withheld.gold == absent.gold and WITHHELD_KEY in withheld.poison and WITHHELD_KEY in absent.poison
    assert {case.caller for case in CASES if case.kind not in {"withheld_ref", "absent_ref"}} == {"owner"}
    assert set(CALLERS) == {"owner", "restricted"}
    assert (withheld.expected_status, withheld.expected_carried_by) == (absent.expected_status, absent.expected_carried_by)


def test_two_attachment_cases_with_cue_only_focus_and_one_blind_twin() -> None:
    assert len(ATTACHMENTS) == 2
    for case in ATTACHMENTS:
        assert len(case.turn.split()) <= 6 and not case.conversation.get("recent"), case.case_id
        focus = case.conversation["focus"]
        assert 0 < len(focus) <= 240
        assert set(case.conversation) == {"focus"}, "cue-only: no recent entries, no refs"
        assert case.gold and set(dict(case.expected_origin).values()) == {"focus"}
        assert set(dict(case.expected_origin)) == set(case.gold)
        assert case.arms == ("a", "c", "d"), "arms c and d carry the cues; arm a is the abstention control"
        assert case.expected_status == "resolved" and case.expected_carried_by is None
        assert dict(case.arm_expectations)["a"]["status"] == "unresolved"
        assert dict(case.arm_expectations)["a"]["origin"] == {}
        for key in case.gold:
            assert _title_of(key).casefold() in focus.casefold(), (case.case_id, key)
            assert _title_of(key).casefold() not in case.turn.casefold()
    blind = [twin for case in ATTACHMENTS for twin in _twins_of(case)]
    assert len(blind) == 1
    twin = blind[0]
    assert twin.turn == case_by_id(twin.pairs_with).turn
    assert twin.expected_status == "unresolved" and twin.gold == ()
    assert twin.conversation["focus"] != case_by_id(twin.pairs_with).conversation["focus"]


def test_the_attachment_twin_focus_names_nothing_in_the_corpus() -> None:
    (twin,) = [twin for case in ATTACHMENTS for twin in _twins_of(case)]
    names = {_title_of(key).casefold() for key in KEY_KINDS}
    words = set(re.findall(r"[a-z]+", twin.conversation["focus"].casefold()))
    corpus_words: set[str] = set()
    for name in names:
        corpus_words.update(re.findall(r"[a-z]+", name))
    assert words.isdisjoint(corpus_words), sorted(words & corpus_words)


def test_arms_are_the_four_designed_arms() -> None:
    assert ARMS == ("a", "b", "c", "d")
    for case in CASES:
        assert set(case.arms) <= set(ARMS) and "a" in case.arms, case.case_id
    conversation = {
        "focus": "one line",
        "recent": [{"role": "user", "text": "earlier"}],
        "refs": ["e_ottilie_marsh"],
    }
    paths = {"e_ottilie_marsh": "Knowledge Base/People/Ottilie Marsh.md"}
    case = dataclasses.replace(CASES[0], conversation=conversation)
    assert conversation_for_arm(case, "a", paths) is None
    assert conversation_for_arm(case, "b", paths) == {
        "recent": [{"role": "user", "text": "earlier"}],
        "refs": ["Knowledge Base/People/Ottilie Marsh.md"],
    }
    assert conversation_for_arm(case, "c", paths) == {"focus": "one line"}
    assert conversation_for_arm(case, "d", paths) == {
        "focus": "one line",
        "recent": [{"role": "user", "text": "earlier"}],
        "refs": ["Knowledge Base/People/Ottilie Marsh.md"],
    }
    assert conversation_for_arm(dataclasses.replace(case, conversation=None), "d", paths) is None
    with pytest.raises(FixtureError):
        conversation_for_arm(case, "e", paths)


def test_every_case_preregisters_its_gold_poison_facts_status_carry_and_origin() -> None:
    for case in CASES:
        assert isinstance(case.gold, tuple) and isinstance(case.poison, tuple)
        assert isinstance(case.must_include, tuple) and isinstance(case.must_exclude, tuple)
        assert case.expected_status in {"resolved", "partial", "ambiguous", "unresolved"}, case.case_id
        assert case.expected_carried_by in {None, "conversation"}, case.case_id
        origins = dict(case.expected_origin)
        assert set(origins) <= set(case.gold), case.case_id
        assert set(origins.values()) <= set(ORIGINS), case.case_id
        served = case.expected_status in {"resolved", "partial"}
        assert (set(origins) == set(case.gold)) if served else not origins, case.case_id
        if case.gold and case.expected_status in {"resolved", "partial"}:
            assert case.must_include, f"{case.case_id}: served gold needs a must-include fact"
        if case.poison:
            assert case.must_exclude or case.kind in {"refs_only_twin", "absent_ref", "withheld_ref"}, case.case_id
        assert set(case.gold).isdisjoint(case.poison), case.case_id
        for arm, override in (case.arm_expectations or {}).items():
            assert arm in case.arms, (case.case_id, arm)
            assert set(override) <= {"status", "carried_by", "origin", "disambiguated_by"}
            assert set(dict(override.get("origin") or {})) <= set(case.gold)


def test_a_carry_case_serves_exactly_one_anchor_and_only_a_carry_is_conversation_origin() -> None:
    for case in CASES:
        if case.expected_carried_by == "conversation":
            assert len(case.gold) == 1 and case.expected_status == "partial", case.case_id
            assert dict(case.expected_origin) == {case.gold[0]: "conversation"}
        else:
            assert "conversation" not in dict(case.expected_origin).values(), case.case_id
        for override in (case.arm_expectations or {}).values():
            if "conversation" in dict(override.get("origin") or {}).values():
                assert case.expected_carried_by == "conversation"


def test_the_manifest_names_only_declared_keys_with_a_kind_and_domain() -> None:
    referenced = {key for case in CASES for key in (*case.gold, *case.poison)}
    referenced |= {ref for case in CASES for ref in ((case.conversation or {}).get("refs") or [])}
    assert referenced <= set(KEY_KINDS) and referenced <= set(KEY_DOMAINS)
    assert {"entity", "hub", "resource", "records_collection"} <= set(KEY_KINDS.values())
    assert WITHHELD_KEY in KEY_KINDS


def test_digest_is_stable_and_every_field_moves_it() -> None:
    original = fixture_set_digest()
    assert fixture_set_digest() == original
    for case in CASES:
        if case.gold:
            assert fixture_set_digest(_with(case, gold=(*case.gold, "an_extra_key"))) != original, case.case_id
            assert fixture_set_digest(_with(case, gold=case.gold[:-1])) != original, case.case_id
    tie = next(case for case in MULTI if case.kind == "tie_break")
    carry = next(case for case in MULTI if case.kind == "anaphoric_carry")
    attachment = ATTACHMENTS[0]
    for case, change in (
        (tie, {"poison": ()}),
        (tie, {"must_include": ("an extra fact",)}),
        (tie, {"must_exclude": ("an extra fact",)}),
        (tie, {"expected_status": "partial"}),
        (tie, {"expected_disambiguated_by": None}),
        (carry, {"expected_carried_by": None}),
        (carry, {"expected_origin": ((carry.gold[0], "turn"),)}),
        (carry, {"arms": ("a", "b")}),
        (carry, {"turn": carry.turn + " Thanks."}),
        (carry, {"conversation": {**carry.conversation, "refs": ["an_extra_key"]}}),
        (carry, {"arm_expectations": {"c": {"status": "resolved"}}}),
        (attachment, {"conversation": {"focus": "another cue"}}),
        (attachment, {"caller": "restricted"}),
        (attachment, {"group": "multi_turn"}),
    ):
        assert fixture_set_digest(_with(case, **change)) != original, (case.case_id, change)


def test_manifest_refuses_a_missing_twin_a_bad_pair_or_a_bad_value() -> None:
    victim = next(case for case in MULTI if case.kind == "drowning")
    (twin,) = _twins_of(victim)
    with pytest.raises(FixtureError):
        assert_manifest_consistent(tuple(case for case in CASES if case.case_id != twin.case_id))
    with pytest.raises(FixtureError):
        assert_manifest_consistent((*CASES, CASES[0]))
    with pytest.raises(FixtureError):
        assert_manifest_consistent(_with(twin, pairs_with="V1"))
    with pytest.raises(FixtureError):
        assert_manifest_consistent(_with(victim, gold=("no_such_page",)))
    with pytest.raises(FixtureError):
        dataclasses.replace(victim, expected_status="carried")
    with pytest.raises(FixtureError):
        dataclasses.replace(victim, expected_carried_by="retrieval")
    with pytest.raises(FixtureError):
        dataclasses.replace(victim, expected_origin=((victim.gold[0], "agent_choice"),))
    with pytest.raises(FixtureError):
        dataclasses.replace(victim, turn="  ")
    with pytest.raises(FixtureError):
        dataclasses.replace(victim, caller="stranger")


def _title_of(key: str) -> str:
    return title_of(key)


def test_no_fixture_text_names_a_project_anchor_by_accident() -> None:
    """The default vault carries project anchors named by ordinary words; a turn
    that says one of them would resolve an anchor no case pre-registers."""
    for text in LEAK_CHECKED_TEXTS:
        assert not PROJECT_WORDS.search(text), text


# --------------------------------------------------------------------------- #
# The built corpus
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("conversation-corpus")
    return root, build_corpus(root)


def test_build_maps_every_page_key_to_a_real_page(corpus) -> None:
    root, manifest = corpus
    assert manifest.corpus_id == CORPUS_ID
    assert manifest.fixture_set_digest == fixture_set_digest()
    assert set(manifest.key_to_path) == set(KEY_KINDS)
    for relative in manifest.key_to_path.values():
        assert relative.startswith("Knowledge Base/")
        assert (root / relative).is_file(), relative
    assert len(manifest.corpus_hash) == len(manifest.logical_hash) == 64


def test_every_gold_and_poison_key_is_an_anchor_of_its_declared_kind(corpus) -> None:
    root, manifest = corpus
    index = working_set_index.WorkingSetIndex(root)
    index.rebuild()
    anchors = {anchor.path: anchor for anchor in index.anchors()}
    referenced = {key for case in CASES for key in (*case.gold, *case.poison)}
    assert referenced
    for key in referenced:
        anchor = anchors.get(manifest.key_to_path[key])
        expected = KEY_KINDS[key]
        if expected == "note":
            continue  # an ordinary page: a decoy, never an anchor
        assert anchor is not None, key
        assert anchor.kind == {"records_collection": "collection"}.get(expected, expected), key
    assert sum(1 for kind in KEY_KINDS.values() if kind == "records_collection") == 1


def test_the_corpus_holds_only_the_declared_invented_entities(corpus) -> None:
    root, _manifest = corpus
    index = working_set_index.WorkingSetIndex(root)
    index.rebuild()
    assert {anchor.title for anchor in index.anchors() if anchor.kind == "entity"} == set(ENTITY_NAMES)


def test_no_fixture_text_leaks_into_the_corpus(corpus) -> None:
    root, _manifest = corpus
    assert len(LEAK_CHECKED_TEXTS) >= len(CASES)
    assert english_set.find_verbatim_leaks(root, turns=LEAK_CHECKED_TEXTS) == ()
    assert english_set.find_normalized_leaks(root, turns=LEAK_CHECKED_TEXTS) == ()
    expected = {case.turn for case in CASES} | {
        entry["text"] for case in CASES for entry in ((case.conversation or {}).get("recent") or [])
    }
    expected |= {case.conversation["focus"] for case in CASES if (case.conversation or {}).get("focus")}
    assert expected <= set(LEAK_CHECKED_TEXTS)


def _page_text(root: Path, path: str) -> str:
    """A page's text; a Records collection's is its manifest plus its item files."""

    target = root / path
    if target.name == "_collection.md":
        return " ".join(
            item.read_text(encoding="utf-8") for item in sorted(target.parent.rglob("*.md"))
        ).casefold()
    return target.read_text(encoding="utf-8").casefold()


def test_must_include_facts_live_on_gold_pages_and_must_exclude_facts_on_others(corpus) -> None:
    root, manifest = corpus
    pages = {key: _page_text(root, path) for key, path in manifest.key_to_path.items()}
    for case in CASES:
        for phrase in case.must_include:
            holders = {key for key, text in pages.items() if phrase.casefold() in text}
            assert holders & set(case.gold), (case.case_id, phrase, holders)
        for phrase in case.must_exclude:
            holders = {key for key, text in pages.items() if phrase.casefold() in text}
            assert holders and holders <= set(case.poison), (case.case_id, phrase, holders)
            assert holders.isdisjoint(case.gold), (case.case_id, phrase)


def test_the_withheld_page_is_governed_and_withheld_from_the_restricted_audience(
    corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, manifest = corpus
    from epistemic.corpora.context_activation_conversation import RESTRICTED_AUDIENCE

    from exomem import commands, lexstore, working_set_runtime
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

    withheld = manifest.key_to_path[WITHHELD_KEY]
    visible = manifest.key_to_path["h_kestrel"]
    governance = root / "Knowledge Base" / "_Governance"
    assert list((governance / "scopes").glob("*.yaml")) and list((governance / "rules").glob("*.yaml"))
    restricted = RequestPrincipal(audience_id=RESTRICTED_AUDIENCE, surface="mcp", purpose=None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    lexstore.ensure_fresh(root)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild()
    with request_scope(owner_principal()):
        picked = commands.op_activate_context(root, turn="zqxwvu plonktastic", anchor=withheld)
    assert picked["anchors"] and picked["anchors"][0]["path"] == withheld
    messages = []
    for ref in (withheld, "Knowledge Base/Products/no-such-page.md"):
        with pytest.raises(ValueError) as refusal, request_scope(restricted):
            commands.op_activate_context(root, turn="zqxwvu plonktastic", anchor=ref)
        messages.append(str(refusal.value))
    assert messages[0] == messages[1]
    with request_scope(restricted):
        commands.op_activate_context(root, turn="zqxwvu plonktastic", anchor=visible)


def test_privacy_gate_passes_for_the_module_and_every_built_page(corpus) -> None:
    root, _manifest = corpus
    import epistemic.corpora.context_activation_conversation as module

    pages = sorted((root / "Knowledge Base").rglob("*.md"))
    assert pages
    assert_public_artifacts_clean([Path(module.__file__), *pages])


def test_build_is_deterministic(corpus, tmp_path: Path) -> None:
    _root, manifest = corpus
    again = build_corpus(tmp_path / "again")
    assert again.key_to_path == manifest.key_to_path
    assert again.fixture_set_digest == manifest.fixture_set_digest
    assert again.logical_hash == manifest.logical_hash


def test_conversation_case_is_a_frozen_value() -> None:
    case = CASES[0]
    assert isinstance(case, ConversationCase)
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.turn = "x"  # type: ignore[misc]


def test_a_content_free_twin_carries_no_focus_that_names_the_earlier_subject() -> None:
    """Post-hoc correction: a twin tests a content-free turn, so its focus must not
    be a confound. The ruling that focus is current-turn evidence stays pinned by
    V29, whose focus names the subject and whose arms (c) and (d) resolve it."""
    for case in CASES:
        if case.twin_mode == "empty_turn" and (case.conversation or {}).get("focus"):
            assert case.conversation["focus"] == "closing pleasantries", case.case_id
    v29 = case_by_id("V29")
    assert v29.group == "attachment" and v29.arms == ("a", "c", "d")
    assert v29.expected_status == "resolved" and dict(v29.expected_origin) == {"e_ottilie_marsh": "focus"}
    assert v29.expectation("a")["status"] == "unresolved"
    assert "Ottilie Marsh" in v29.conversation["focus"]
