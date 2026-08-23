"""f27 ``lifecycle_routing_replay``: corpus, loader gate, assertions, driver.

The family's claim is that a real agent, given ordinary working language with
every store-bearing utterance removed, leaves the same durable state an expert
session left. Three things have to be true before any of that can be measured,
and they are what this module holds apart:

**The expectation is authored.** The corpus is a pure function of its
arguments — a seeded vault, an ordered transcript, and per-turn annotations of
the consequences an expert lands. The expected end-state is the *fold* of those
annotations and never the output of an agent, so a run can be wrong without the
yardstick moving.

**The stimulus is clean.** A turn that names the store, or the act of storing,
would be teaching the answer. The gate refuses one at corpus construction and
again at scenario load, and the red fixture proves the refusal rather than
asserting it in prose.

**The harness is provable offline.** Every driver test here injects a runner and
replays a **fabricated** stream-json transcript. None of it is evidence about
the product's behaviour; it is evidence that the parse → project → evaluate →
report path runs end to end before a single live invocation is spent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS_MODULE = ROOT / "benchmarks" / "epistemic" / "corpora" / "lifecycle_replay.py"


# --------------------------------------------------------------------------
# 2.1 — the corpus folds its own annotations, and says nothing personal.
# --------------------------------------------------------------------------


def test_corpus_folds_its_annotations_into_the_expected_end_state() -> None:
    """The end-state is derived, not restated beside the turns it comes from."""

    from epistemic.corpora.lifecycle_replay import replay_corpus

    corpus = replay_corpus()
    expected = corpus.expected

    filed = {
        consequence.title
        for turn in corpus.turns
        for consequence in turn.consequences
        if consequence.tier == "intent"
    }
    assert set(expected.plan_items) == {corpus.normalized(title) for title in filed}

    appended = {
        (consequence.title, consequence.event_type)
        for turn in corpus.turns
        for consequence in turn.consequences
        if consequence.tier == "outcome"
    }
    assert set(expected.records) == {
        (corpus.normalized(title), corpus.normalized(event_type))
        for title, event_type in appended
    }

    moved = {
        consequence.title: consequence.status
        for turn in corpus.turns
        for consequence in turn.consequences
        if consequence.tier == "transition"
    }
    assert expected.transitions == {
        corpus.normalized(title): status for title, status in moved.items()
    }
    # The fold reaches the plan item, not only the transition table: an item a
    # later turn completed carries that status as its final one.
    for title, status in moved.items():
        assert expected.plan_items[corpus.normalized(title)].final_status == status


def test_corpus_carries_six_deliverables_and_three_turns_that_land_nothing() -> None:
    """A tentative claim, an elapsed-time remark and a deferral, each annotated none."""

    from epistemic.corpora.lifecycle_replay import replay_corpus

    corpus = replay_corpus()
    assert len(corpus.deliverables) == 6
    quiet = [turn for turn in corpus.turns if not turn.consequences]
    assert len(quiet) >= 3
    assert {turn.lands for turn in quiet} == {"none"}
    kinds = {turn.quiet_kind for turn in quiet}
    assert {"tentative", "elapsed_time", "deferral"} <= kinds


def test_no_two_expected_events_share_a_title_and_event_type() -> None:
    """So ``occurred_on`` can never be the discriminator between two records."""

    from epistemic.corpora.lifecycle_replay import replay_corpus

    corpus = replay_corpus()
    keys = [
        (consequence.title, consequence.event_type)
        for turn in corpus.turns
        for consequence in turn.consequences
        if consequence.tier == "outcome"
    ]
    assert len(keys) == len(set(keys))
    stated = [record for record in corpus.expected.records.values() if record.occurred_on]
    assert stated, "at least one utterance must state its own date"


def test_corpus_digest_is_content_bound() -> None:
    """A digest that does not move with the corpus pins nothing."""

    from epistemic.corpora.lifecycle_replay import corpus_digest, replay_corpus

    digest = corpus_digest()
    assert len(digest) == 64 and digest == corpus_digest()
    corpus = replay_corpus()
    mutated = corpus.replace_turn_text(corpus.turns[0].turn_id, "something else entirely")
    assert mutated.digest() != digest


def test_corpus_ships_no_personal_or_product_vocabulary() -> None:
    """The scaffold no-leak policy, applied to the corpus it now has to cover."""

    from exomem.public_artifact_privacy import assert_public_artifacts_clean

    assert CORPUS_MODULE.is_file()
    assert_public_artifacts_clean(
        [CORPUS_MODULE], labels={CORPUS_MODULE: "benchmarks/epistemic/corpora/lifecycle_replay.py"}
    )


# --------------------------------------------------------------------------
# 2.2 — the store-bearing gate.
# --------------------------------------------------------------------------


def test_the_gate_refuses_a_store_bearing_turn_naming_the_turn_and_the_token() -> None:
    from epistemic.corpora.lifecycle_replay import (
        StoreBearingUtterance,
        assert_no_store_bearing_utterance,
    )

    with pytest.raises(StoreBearingUtterance) as excinfo:
        assert_no_store_bearing_utterance((("t-99", "save this one"),))
    message = str(excinfo.value)
    assert "t-99" in message and "save" in message


def test_the_gate_admits_ordinary_working_language() -> None:
    """The vocabulary names the store and the act of storing, and nothing else."""

    from epistemic.corpora.lifecycle_replay import assert_no_store_bearing_utterance

    assert_no_store_bearing_utterance(
        (
            ("t-a", "the second one turned out really well, that's done"),
            ("t-b", "it's been a week since I touched the fifth"),
            ("t-c", "I decided earlier that the previous approach was wrong"),
        )
    )


def test_every_corpus_turn_passes_its_own_gate() -> None:
    from epistemic.corpora.lifecycle_replay import (
        assert_no_store_bearing_utterance,
        replay_corpus,
    )

    corpus = replay_corpus()
    assert_no_store_bearing_utterance((turn.turn_id, turn.text) for turn in corpus.turns)


def test_the_retrieve_nudge_regex_is_cited_as_the_sibling_and_not_reused() -> None:
    """The sibling matches ``earlier``/``previous``; this family measures those."""

    import re

    from epistemic.corpora.lifecycle_replay import STORE_BEARING_RE

    source = CORPUS_MODULE.read_text(encoding="utf-8")
    assert "_KB_BEARING_RE" in source
    assert "src/exomem/_hooks/exomem_retrieve_nudge.py" in source
    for admitted in ("earlier", "previous", "history", "decision"):
        assert re.search(STORE_BEARING_RE, admitted) is None


# --------------------------------------------------------------------------
# 1.1-1.3 — the amendment, the receipt, and the registry mirror.
# --------------------------------------------------------------------------

PREREGISTRATION = ROOT / "benchmarks" / "epistemic" / "PREREGISTRATION.md"
SEQUENCE3 = ROOT / "benchmarks" / "epistemic" / "fixtures" / "sequence3"
FIXTURES = ROOT / "benchmarks" / "epistemic" / "fixtures"
SEQUENCE_THREE_FAMILIES = ("f27",)
SEQUENCE_THREE_ASSERTIONS = (
    "lifecycle_consequence_landed_unprompted",
    "no_structured_write_beyond_expectation",
)


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as if the founder had acknowledged sequence 3.

    The gate itself is asserted un-patched below. What this buys is the ability
    to execute the trajectory now, which is the only way to know whether the
    family is partial for the reason the amendment claims.
    """

    from epistemic import runner as runner_module
    from epistemic import schema as schema_module

    monkeypatch.setattr(schema_module, "require_family_released", lambda *a, **k: None)
    monkeypatch.setattr(runner_module, "require_family_released", lambda *a, **k: None)


def test_the_document_registers_f27_and_both_assertions() -> None:
    """§1, §2 and §4 all know the family; §7 is where the authority comes from."""

    from epistemic.registry import (
        parse_preregistered_assertions,
        parse_preregistered_families,
    )

    text = PREREGISTRATION.read_text(encoding="utf-8")
    families = dict(parse_preregistered_families(text))
    assert families["f27"] == "lifecycle_routing_replay"
    names = parse_preregistered_assertions(text)
    for assertion in SEQUENCE_THREE_ASSERTIONS:
        assert assertion in names
        assert f"**{assertion}**" in text[text.index("## 4.") : text.index("## 5.")]


def test_the_amendment_entry_states_what_sequence_three_commits_to() -> None:
    """A §7 entry that omits its own commitments cannot be acknowledged."""

    text = PREREGISTRATION.read_text(encoding="utf-8")
    entry = text[
        text.index("- 2026-08-23 — **Lifecycle-routing replay family amendment (sequence 3).**") :
    ]
    for phrase in (
        "2026-08-23",
        "f27 `lifecycle_routing_replay`",
        "store-bearing",
        "harness fault",
        "expected partial",
        "adds **no** catastrophic assertions",
        "no budget constant",
        "f27 MUST NOT support a comparative run, score, or claim",
    ):
        assert phrase in entry, phrase


def test_the_registry_mirrors_the_document_for_f27() -> None:
    from epistemic.registry import (
        AMENDMENT_INTRODUCED_FAMILIES,
        ASSERTION_REGISTRY,
        COMPOSES_ABSENCE_META,
        PREREGISTERED_FAMILIES,
        REQUIRES_ITEM_PAIR,
    )
    from epistemic.schema import UNPROMPTED_FAMILIES

    assert ("f27", "lifecycle_routing_replay") in PREREGISTERED_FAMILIES
    assert AMENDMENT_INTRODUCED_FAMILIES["f27"] == 3
    for assertion in SEQUENCE_THREE_ASSERTIONS:
        assert assertion in ASSERTION_REGISTRY
    # D1/D6: f27 is stimulated by user turns and asserts over state, so none of
    # these three registries may move. Pinned rather than trusted to review.
    assert "f27" not in UNPROMPTED_FAMILIES
    assert not (set(SEQUENCE_THREE_ASSERTIONS) & COMPOSES_ABSENCE_META)
    assert not (set(SEQUENCE_THREE_ASSERTIONS) & REQUIRES_ITEM_PAIR)


def test_the_sequence_three_receipt_folds_the_working_chain() -> None:
    """The receipt binds sequence 2's document to the amended one, and is pending."""

    from protocol.contracts import (
        validate_working_preregistration,
        working_amendment_receipts,
    )

    receipts = working_amendment_receipts(ROOT)
    assert [receipt.sequence for receipt in receipts] == [1, 2, 3]
    sequence_three = receipts[2]
    assert sequence_three.acknowledgment_status == "pending"
    assert sequence_three.ratifier is None
    assert sequence_three.catastrophic_set_decision is None
    assert sequence_three.parent_contract_sha256 == receipts[1].contract_sha256
    assert validate_working_preregistration(ROOT) == sequence_three.contract_sha256


def test_the_withhold_gate_refuses_f27_naming_sequence_three() -> None:
    from epistemic import amendments
    from protocol.contracts import AmendmentAcknowledgmentPendingError

    amendments.reset_cache()
    assert "f27" in amendments.withheld_family_ids(ROOT)
    assert amendments.amendment_sequence_for("f27") == 3
    with pytest.raises(
        AmendmentAcknowledgmentPendingError,
        match=r"amendment sequence 3 .*pending.*f27",
    ):
        amendments.require_family_released("f27", repo_root=ROOT)


def test_the_withheld_red_fixture_refuses_at_load() -> None:
    from epistemic.schema import ScenarioLoadError, load_scenario

    path = FIXTURES / "red-sequence3-withheld-family.yaml"
    with pytest.raises(ScenarioLoadError, match=r"sequence 3.*f27"):
        load_scenario(path)


def test_the_shipped_f27_scenario_refuses_to_load_while_pending() -> None:
    from epistemic.schema import ScenarioLoadError, load_scenario

    scenarios = sorted(SEQUENCE3.glob("*.yaml"))
    assert len(scenarios) == 1
    for path in scenarios:
        with pytest.raises(ScenarioLoadError, match="sequence 3"):
            load_scenario(path)


def test_the_store_bearing_red_fixture_refuses_at_scenario_load(released: None) -> None:
    """Released, so the refusal under test is the gate and not the receipt."""

    from epistemic.schema import ScenarioLoadError, load_scenario

    path = FIXTURES / "red-sequence3-store-bearing-utterance.yaml"
    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(path)
    message = str(excinfo.value)
    assert "save" in message and "t02-store-bearing" in message


#: The op sequence every f27 phase carries, in order and in full. The seed
#: snapshot is the second op and precedes every turn: the runner reaches
#: snapshots cumulatively across phases, so a phase that omitted its own seed
#: would silently be scored against the previous arm's POST-RUN vault. Pinned as
#: a sequence rather than as `ops[0] == "configure" and ops[-1] == "snapshot"`,
#: which the fixture satisfies with the seed op deleted.
PHASE_OP_SEQUENCE = ["configure", "snapshot", *["agent_turn"] * 10, "snapshot"]


def _assert_phase_ops_pinned(phase) -> None:
    """The one check both the shipped fixture and the mutated one go through."""

    assert [op.op for op in phase.ops] == PHASE_OP_SEQUENCE
    assert phase.ops[1].ref == f"s-{phase.phase_id}-seed"


def _fixture_without_the_hooked_seed(tmp_path) -> Path:
    """The shipped fixture with only the hooked phase's seed snapshot removed."""

    source = SEQUENCE3 / "f27-lifecycle-routing-replay.yaml"
    text = source.read_text(encoding="utf-8")
    mutated = text.replace("      - op: snapshot\n        ref: s-hooked-seed\n", "")
    assert mutated != text, "the seed op the mutation removes is no longer in the fixture"
    path = tmp_path / "f27-no-hooked-seed.yaml"
    path.write_text(mutated, encoding="utf-8")
    return path


def test_the_shipped_scenario_replays_the_corpus_turn_for_turn(released: None) -> None:
    from epistemic.corpora.lifecycle_replay import CORPUS_ID, replay_corpus
    from epistemic.schema import load_scenario

    corpus = replay_corpus()
    scenario = load_scenario(SEQUENCE3 / "f27-lifecycle-routing-replay.yaml")
    assert scenario.family_id == "f27"
    assert scenario.kind == "operational"
    assert [phase.phase_id for phase in scenario.phases] == ["hookless", "hooked"]
    for phase in scenario.phases:
        _assert_phase_ops_pinned(phase)
        turns = [op for op in phase.ops if op.op == "agent_turn"]
        assert [op.ref for op in turns] == [turn.turn_id for turn in corpus.turns]
        assert [op.detail for op in turns] == [turn.text for turn in corpus.turns]
        assert [expectation.assertion for expectation in phase.expect] == list(
            SEQUENCE_THREE_ASSERTIONS
        )
        assert {expectation.subject for expectation in phase.expect} == {CORPUS_ID}


def test_the_turn_for_turn_pin_refuses_a_phase_missing_its_seed_op(
    released: None, tmp_path
) -> None:
    """F1(ii). The round-1 pin passed on this fixture; run the pin itself on it.

    Asserted by putting the mutated phase through ``_assert_phase_ops_pinned`` —
    the same function the shipped-fixture test uses — rather than by asserting
    an inequality. An inequality passes for any reason at all, including the pin
    having quietly stopped checking the thing.
    """

    from epistemic.schema import load_scenario

    scenario = load_scenario(_fixture_without_the_hooked_seed(tmp_path))
    hooked = next(phase for phase in scenario.phases if phase.phase_id == "hooked")
    hookless = next(phase for phase in scenario.phases if phase.phase_id == "hookless")

    _assert_phase_ops_pinned(hookless)
    with pytest.raises(AssertionError):
        _assert_phase_ops_pinned(hooked)


# --------------------------------------------------------------------------
# 3.1-3.3 — the paired assertions, over hand-built snapshots.
# --------------------------------------------------------------------------


def _projector():
    from epistemic.snapshot import ProjectorMeta

    return ProjectorMeta(
        name="exomem-vault-file-projector",
        version="0.3.0",
        author="benchmark-harness",
        endpoints_used=("filesystem:walk(vault)",),
        loc=1,
        loc_code=1,
    )


def _page(path: str):
    from epistemic.snapshot import StateItem

    return StateItem(
        id=path.removesuffix(".md"),
        kind="container",
        title=path.rsplit("/", 1)[-1],
        locator=path,
        locator_kind="file",
    )


def _plan_item(title: str, status: str):
    from epistemic.snapshot import CollectionItem

    return CollectionItem(
        key=title.casefold().replace(" ", "-"),
        natural_key={"title": title},
        lifecycle="active",
        status=status,
    )


def _record_item(title: str, event_type: str, occurred_on: str = "2026-08-01"):
    from epistemic.snapshot import CollectionItem

    return CollectionItem(
        key=f"{occurred_on}-{title.casefold().replace(' ', '-')}-{event_type}",
        natural_key={"occurred_on": occurred_on, "title": title, "event_type": event_type},
    )


def _collections(plan_items=(), record_items=(), extra=()):
    from epistemic.corpora.lifecycle_replay import (
        PLANNING_COLLECTION_ID,
        PLANNING_PATH,
        RECORDS_COLLECTION_ID,
        RECORDS_PATH,
    )
    from epistemic.snapshot import CollectionProjection

    return (
        CollectionProjection(
            id=PLANNING_COLLECTION_ID,
            profile="planning",
            manifest=PLANNING_PATH,
            title="Delivery plan",
            schema_version=1,
            storage_source="Items",
            natural_key=("title",),
            items=tuple(plan_items),
        ),
        CollectionProjection(
            id=RECORDS_COLLECTION_ID,
            profile="records",
            manifest=RECORDS_PATH,
            title="Delivery events",
            schema_version=1,
            storage_source="Events",
            natural_key=("occurred_on", "title", "event_type"),
            items=tuple(record_items),
        ),
        *extra,
    )


def _complete_replay():
    """Plan items, records and statuses exactly as the fold declares them."""

    from epistemic.corpora.lifecycle_replay import (
        INITIATIVE_TITLE,
        OUTCOME_TITLE,
        replay_corpus,
    )

    expected = replay_corpus().expected
    plan_items = [
        _plan_item(OUTCOME_TITLE, "planned"),
        _plan_item(INITIATIVE_TITLE, "planned"),
    ]
    plan_items += [
        _plan_item(item.title, item.final_status) for item in expected.plan_items.values()
    ]
    record_items = [
        _record_item(record.title, record.event_type, record.occurred_on or "2026-08-01")
        for record in expected.records.values()
    ]
    return plan_items, record_items


def _snapshot(*, plan_items=(), record_items=(), extra_collections=(), pages=(), phase="hookless"):
    from epistemic.corpora.lifecycle_replay import PLANNING_PATH, RECORDS_PATH
    from epistemic.snapshot import EpistemicStateSnapshot, StateItem

    items = [
        _page("Knowledge Base/log.md"),
        _page(PLANNING_PATH),
        _page(RECORDS_PATH),
        StateItem(
            id="surface-review_queue",
            kind="container",
            title="review_queue",
            raw={"surface": "review_queue", "projection": "unavailable"},
        ),
    ]
    items += [_page(path) for path in pages]
    return EpistemicStateSnapshot(
        provider="exomem",
        variant="native",
        phase=phase,
        taken_at="2026-08-23T00:00:00Z",
        items=tuple(items),
        collections=_collections(plan_items, record_items, extra_collections),
        projector=_projector(),
    )


_UNSET = object()


def _context(snapshot, subject=None, seeded=_UNSET):
    """A bound context whose ``prior`` is the seeded vault's own projection.

    The default is the seed rather than ``None``, because that is what a real
    trajectory reaches: the driver snapshots the seeded vault before turn 1, so
    the false-write dual always has a baseline to diff against.
    """

    from epistemic.assertions import AssertionContext
    from epistemic.corpora.lifecycle_replay import CORPUS_ID

    return AssertionContext(
        snapshot=snapshot,
        prior=_seed_snapshot(phase=snapshot.phase) if seeded is _UNSET else seeded,
        subject=CORPUS_ID if subject is None else subject,
        family="f27",
    )


def _coverage(snapshot, subject=None, seeded=_UNSET):
    from epistemic.assertions import lifecycle_consequence_landed_unprompted

    return lifecycle_consequence_landed_unprompted(_context(snapshot, subject, seeded))


def _extras(snapshot, subject=None, seeded=_UNSET):
    from epistemic.assertions import no_structured_write_beyond_expectation

    return no_structured_write_beyond_expectation(_context(snapshot, subject, seeded))


def test_coverage_passes_when_every_tier_is_complete() -> None:
    plan_items, record_items = _complete_replay()
    result = _coverage(_snapshot(plan_items=plan_items, record_items=record_items))
    assert result.outcome == "pass", result.evidence
    assert "intent 4/4" in result.evidence
    assert "outcome 5/5" in result.evidence
    assert "transition 3/3" in result.evidence


def test_coverage_fails_naming_the_tier_the_fraction_and_the_missing_key() -> None:
    from epistemic.corpora.lifecycle_replay import replay_corpus

    expected = replay_corpus().expected
    plan_items, record_items = _complete_replay()
    missing = next(iter(expected.transitions))
    plan_items = [
        _plan_item(item.natural_key["title"], "planned")
        if item.natural_key["title"].casefold() == missing
        else item
        for item in plan_items
    ]
    result = _coverage(_snapshot(plan_items=plan_items, record_items=record_items))
    assert result.outcome == "fail", result.evidence
    assert "transition 2/3" in result.evidence
    assert missing in result.evidence.casefold()
    # The other tiers are still reported: a fraction nobody can see is not a finding.
    assert "intent 4/4" in result.evidence and "outcome 5/5" in result.evidence


def test_coverage_matches_titles_only_after_the_pinned_normalisation() -> None:
    """NFKC + case fold + whitespace collapse, and nothing looser."""

    plan_items, record_items = _complete_replay()
    shouted = [
        _plan_item(f"  {item.natural_key['title'].upper()}  ", item.status or "planned")
        for item in plan_items
    ]
    assert _coverage(_snapshot(plan_items=shouted, record_items=record_items)).outcome == "pass"

    truncated = [
        _plan_item(item.natural_key["title"].replace(" ", ""), item.status or "planned")
        for item in plan_items
    ]
    assert _coverage(_snapshot(plan_items=truncated, record_items=record_items)).outcome == "fail"


def test_coverage_requires_a_present_valid_occurred_on() -> None:
    from epistemic.snapshot import CollectionItem

    plan_items, record_items = _complete_replay()
    undated = [
        CollectionItem(
            key=item.key,
            natural_key={k: v for k, v in item.natural_key.items() if k != "occurred_on"},
        )
        for item in record_items
    ]
    result = _coverage(_snapshot(plan_items=plan_items, record_items=undated))
    assert result.outcome == "fail" and "outcome 0/5" in result.evidence

    invalid = [
        CollectionItem(key=item.key, natural_key={**item.natural_key, "occurred_on": "someday"})
        for item in record_items
    ]
    assert _coverage(_snapshot(plan_items=plan_items, record_items=invalid)).outcome == "fail"


def test_coverage_compares_occurred_on_only_when_the_utterance_stated_one() -> None:
    from epistemic.corpora.lifecycle_replay import replay_corpus
    from epistemic.snapshot import CollectionItem

    expected = replay_corpus().expected
    plan_items, record_items = _complete_replay()
    stated = {
        (record.title, record.event_type)
        for record in expected.records.values()
        if record.occurred_on
    }
    moved = [
        CollectionItem(key=item.key, natural_key={**item.natural_key, "occurred_on": "2026-09-09"})
        for item in record_items
    ]
    unstated_moved = [
        item
        if (item.natural_key["title"], item.natural_key["event_type"]) not in stated
        else record_items[moved.index(item)]
        for item in moved
    ]
    # Every date the corpus did not state may move freely.
    only_unstated = [
        CollectionItem(
            key=item.key,
            natural_key={
                **item.natural_key,
                "occurred_on": item.natural_key["occurred_on"]
                if (item.natural_key["title"], item.natural_key["event_type"]) in stated
                else "2026-09-09",
            },
        )
        for item in record_items
    ]
    assert _coverage(_snapshot(plan_items=plan_items, record_items=only_unstated)).outcome == "pass"
    # The one the corpus did state may not.
    only_stated_moved = [
        CollectionItem(
            key=item.key,
            natural_key={
                **item.natural_key,
                "occurred_on": "2026-09-09"
                if (item.natural_key["title"], item.natural_key["event_type"]) in stated
                else item.natural_key["occurred_on"],
            },
        )
        for item in record_items
    ]
    assert unstated_moved is not None
    assert _coverage(
        _snapshot(plan_items=plan_items, record_items=only_stated_moved)
    ).outcome == "fail"


def test_both_assertions_are_blocked_on_an_unprojected_collections_section() -> None:
    from epistemic.snapshot import CollectionProjection, EpistemicStateSnapshot

    bare = EpistemicStateSnapshot(
        provider="exomem",
        variant="native",
        phase="hookless",
        taken_at="2026-08-23T00:00:00Z",
        items=(_page("Knowledge Base/log.md"),),
        projector=_projector(),
    )
    for result in (_coverage(bare), _extras(bare)):
        assert result.outcome == "blocked", result.evidence
        assert "collections" in result.evidence

    from epistemic.corpora.lifecycle_replay import PLANNING_COLLECTION_ID, PLANNING_PATH

    planning_only = EpistemicStateSnapshot(
        provider="exomem",
        variant="native",
        phase="hookless",
        taken_at="2026-08-23T00:00:00Z",
        items=(_page("Knowledge Base/log.md"),),
        collections=(
            CollectionProjection(
                id=PLANNING_COLLECTION_ID,
                profile="planning",
                manifest=PLANNING_PATH,
                title="Delivery plan",
                schema_version=1,
                natural_key=("title",),
            ),
        ),
        projector=_projector(),
    )
    for result in (_coverage(planning_only), _extras(planning_only)):
        assert result.outcome == "blocked", result.evidence
        assert "records" in result.evidence


def test_both_assertions_are_blocked_without_a_named_corpus() -> None:
    plan_items, record_items = _complete_replay()
    snapshot = _snapshot(plan_items=plan_items, record_items=record_items)
    for result in (_coverage(snapshot, subject=""), _extras(snapshot, subject="")):
        assert result.outcome == "blocked", result.evidence


def test_extras_pass_on_a_replay_that_wrote_exactly_the_expectation() -> None:
    plan_items, record_items = _complete_replay()
    result = _extras(_snapshot(plan_items=plan_items, record_items=record_items))
    assert result.outcome == "pass", result.evidence
    assert "0 extra" in result.evidence


def test_extras_fail_on_a_record_appended_for_a_tentative_claim() -> None:
    plan_items, record_items = _complete_replay()
    spurious = [*record_items, _record_item("Assembly checklist", "produced")]
    result = _extras(_snapshot(plan_items=plan_items, record_items=spurious))
    assert result.outcome == "fail", result.evidence
    assert "record" in result.evidence and "assembly checklist" in result.evidence.casefold()
    # The coverage half is unaffected: the two readouts are a pair, not a sum.
    assert _coverage(_snapshot(plan_items=plan_items, record_items=spurious)).outcome == "pass"


def test_extras_fail_on_a_plan_item_filed_for_a_deferral() -> None:
    plan_items, record_items = _complete_replay()
    spurious = [*plan_items, _plan_item("Crate diagram", "planned")]
    result = _extras(_snapshot(plan_items=spurious, record_items=record_items))
    assert result.outcome == "fail", result.evidence
    assert "crate diagram" in result.evidence.casefold()


def test_extras_fail_on_a_status_the_fold_never_assigned() -> None:
    plan_items, record_items = _complete_replay()
    wrong = [
        _plan_item(item.natural_key["title"], "cancelled")
        if item.natural_key["title"] == "Assembly checklist"
        else item
        for item in plan_items
    ]
    result = _extras(_snapshot(plan_items=wrong, record_items=record_items))
    assert result.outcome == "fail", result.evidence
    assert "cancelled" in result.evidence


def test_extras_fail_on_a_created_collection() -> None:
    from epistemic.snapshot import CollectionProjection

    plan_items, record_items = _complete_replay()
    created = CollectionProjection(
        id="9c1d2e30-5f6a-4b7c-8d9e-0a1b2c3d4e5f",
        profile="records",
        manifest="Knowledge Base/Records/Extra/_collection.md",
        title="Extra events",
        schema_version=1,
        natural_key=("title",),
    )
    result = _extras(
        _snapshot(plan_items=plan_items, record_items=record_items, extra_collections=(created,))
    )
    assert result.outcome == "fail", result.evidence
    assert "collection" in result.evidence and created.id in result.evidence


def test_extras_fail_on_a_page_outside_the_allowlist() -> None:
    plan_items, record_items = _complete_replay()
    result = _extras(
        _snapshot(
            plan_items=plan_items,
            record_items=record_items,
            pages=("Knowledge Base/Notes/Insights/batch-run-summary.md",),
        )
    )
    assert result.outcome == "fail", result.evidence
    assert "batch-run-summary" in result.evidence


def test_both_assertions_run_through_evaluate_scenario(released: None) -> None:
    """The pair, bound by the shipped fixture rather than by a hand-made context."""

    from epistemic.runner import evaluate_scenario
    from epistemic.schema import load_scenario

    scenario = load_scenario(SEQUENCE3 / "f27-lifecycle-routing-replay.yaml")
    plan_items, record_items = _complete_replay()
    complete = _snapshot(plan_items=plan_items, record_items=record_items, phase="hookless")
    seeds = {
        "s-hookless-seed": _seed_snapshot(phase="hookless"),
        "s-hooked-seed": _seed_snapshot(phase="hooked"),
    }
    partial = _snapshot(
        plan_items=plan_items[:-1],
        record_items=[*record_items, _record_item("Label sheet", "produced")],
        phase="hooked",
    )
    run = evaluate_scenario(
        scenario, snapshots={**seeds, "s-hookless": complete, "s-hooked": partial}
    )
    outcomes = {(bound.phase_id, bound.assertion): bound.result.outcome for bound in run.assertions}
    assert outcomes[("hookless", "lifecycle_consequence_landed_unprompted")] == "pass"
    assert outcomes[("hookless", "no_structured_write_beyond_expectation")] == "pass"
    assert outcomes[("hooked", "lifecycle_consequence_landed_unprompted")] == "fail"
    assert outcomes[("hooked", "no_structured_write_beyond_expectation")] == "fail"


# --------------------------------------------------------------------------
# 4.1-4.4 — the journey driver, proven offline.
#
# Every stream below is FABRICATED. It is well-formed against the documented
# stream-json shape and it is not, and must never be cited as, evidence of a
# live run. What it proves is that the harness parses, projects, evaluates and
# reports end to end before a single subscription turn is spent on it.
# --------------------------------------------------------------------------

import shutil  # noqa: E402
from dataclasses import dataclass  # noqa: E402


def _cli_or_skip() -> str:
    found = shutil.which("claude")
    if found is None:
        pytest.skip("no agent CLI is installed on this host")
    return found


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str = ""


def _fabricated_stream(
    session_id: str,
    *,
    tool_uses: tuple[tuple[str, dict], ...] = (),
    subtype: str = "success",
    is_error: bool = False,
    trailing_garbage: bool = False,
    result_text: str = "done",
) -> str:
    """A well-formed stream-json transcript. FABRICATED — never a live capture."""

    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": session_id})]
    for name, payload in tool_uses:
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": name, "input": payload}]},
                }
            )
        )
    if trailing_garbage:
        lines.append("{not json at all")
    lines.append(
        json.dumps(
            {
                "type": "result",
                "subtype": subtype,
                "is_error": is_error,
                "result": result_text,
                "duration_ms": 1234,
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "total_cost_usd": 0.01,
                "session_id": session_id,
                "num_turns": 2,
            }
        )
    )
    return "\n".join(lines) + "\n"


def _fabricated_hook_stream(session_id: str = "s") -> str:
    """A hook-bearing stream, FABRICATED but shaped after a recorded one.

    Shaped after a recorded ``claude 2.1.240 --include-hook-events`` stream
    captured 2026-08-23, with local paths, ids and prompt text redacted. Nothing
    here is a live capture: the shape is copied so the counter is exercised
    against the lines the CLI actually emits, which is exactly what the round-0
    counter got wrong.

    Three invocations, one firing: a UserPromptSubmit pair with empty output, a
    Stop pair that decided there was nothing to say (also empty), and a Stop pair
    carrying the product's own reminder. Only the last is a firing — a counter
    that could not tell them apart would report a runtime that never nudged and
    one that nudged every turn as the same number.

    The firing branch cannot be exercised live from inside this suite, so its
    shape is taken from the recorded ``hook_response`` line quoted in the
    orchestrator's live-smoke findings. The development run of 2026-08-23 did
    produce one: hooked turn 1, reported as ``capture_nudge_firings: 1``. The
    recorded stream itself is deliberately not committed — a real transcript in
    a fixture is a live capture masquerading as a unit test.
    """

    from exomem._hooks.exomem_capture_nudge import REMINDER

    def pair(hook: str, output: str) -> list[str]:
        started = {
            "type": "system",
            "subtype": "hook_started",
            "hook_id": "00000000-0000-0000-0000-000000000001",
            "hook_name": hook,
            "hook_event": hook,
            "session_id": session_id,
        }
        response = {
            "type": "system",
            "subtype": "hook_response",
            "hook_id": "00000000-0000-0000-0000-000000000001",
            "hook_name": hook,
            "hook_event": hook,
            "output": output,
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "outcome": "success",
            "session_id": session_id,
        }
        return [json.dumps(started), json.dumps(response)]

    blocked = json.dumps({"decision": "block", "reason": REMINDER})
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": session_id}),
        *pair("UserPromptSubmit", ""),
        *pair("Stop", ""),
        *pair("Stop", blocked),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "session_id": session_id,
                "num_turns": 2,
            }
        ),
    ]
    return "\n".join(lines) + "\n"


class _ScriptedAgent:
    """An offline stand-in that performs the writes a perfect agent would.

    It writes through the product's own API rather than editing markdown, so the
    projection the harness reads back is one the runtime actually produced. What
    it fabricates is the *transcript*, which the harness only counts tool uses
    and hook events from.
    """

    def __init__(self, vault, initiative: str, corpus, *, land: bool = True, spurious=()):
        self.vault = vault
        self.initiative = initiative
        self.corpus = corpus
        self.land = land
        self.spurious = spurious
        self.plan_ids: dict[str, str] = {}
        self.argvs: list[list[str]] = []

    def _file(self, title: str) -> None:
        from epistemic.corpora.lifecycle_replay import FILED_STATUS, PLANNING_PATH

        from exomem import planning

        added = planning.add(
            self.vault,
            PLANNING_PATH,
            item={
                "title": title,
                "status": FILED_STATUS,
                "commitment": "committed",
                "horizon": "week",
                "parent": self.initiative,
            },
            why=f"queue {title}",
        )
        self.plan_ids[title] = added["plan_id"]

    def _append(self, title: str, event_type: str, occurred_on: str) -> None:
        from epistemic.corpora.lifecycle_replay import RECORDS_PATH

        from exomem import records

        records.append_record(
            self.vault,
            RECORDS_PATH,
            item={"occurred_on": occurred_on, "title": title, "event_type": event_type},
            why=f"record that {title} was {event_type}",
        )

    def _settle(self, title: str, status: str) -> None:
        from epistemic.corpora.lifecycle_replay import PLANNING_COLLECTION_ID, PLANNING_PATH

        from exomem import planning, record_formats, records
        from exomem import structured_collections as collections

        assert PLANNING_COLLECTION_ID
        manifest = collections.load_manifest(self.vault, self.vault / PLANNING_PATH)
        snapshot = record_formats.load_adapter(self.vault, manifest).read()
        guards = records.lifecycle_guards(manifest, snapshot)
        plan_id = self.plan_ids[title]
        item = next(row for row in snapshot.records if row.identity.key == plan_id)
        planning.triage(
            self.vault,
            PLANNING_PATH,
            plan_id=plan_id,
            transition={"status": status},
            expected_container_hash=guards["expected_container_hash"],
            expected_item_version=item.source.hash,
            why=f"the deliverable is {status}",
        )

    def __call__(self, argv):
        from epistemic.corpora.lifecycle_replay import PLANNING_PATH

        self.argvs.append(list(argv))
        prompt = argv[argv.index("-p") + 1]
        turn = next((t for t in self.corpus.turns if t.text == prompt), None)
        # Every episode opens the way both live arms did: a read. It must not be
        # counted as a structured write, and a stand-in that never made one
        # could not prove that.
        tool_uses: list[tuple[str, dict]] = [
            ("mcp__exomem__plan_memory", {"action": "inspect", "collection": PLANNING_PATH})
        ]
        if turn is not None and self.land:
            for consequence in turn.consequences:
                if consequence.tier == "intent":
                    self._file(consequence.title)
                    tool_uses.append(
                        ("mcp__exomem__plan_memory", {"action": "add", "title": consequence.title})
                    )
                elif consequence.tier == "outcome":
                    self._append(
                        consequence.title,
                        consequence.event_type,
                        consequence.occurred_on or "2026-08-01",
                    )
                    tool_uses.append(
                        (
                            "mcp__exomem__record_memory",
                            {"action": "append", "title": consequence.title},
                        )
                    )
                else:
                    self._settle(consequence.title, consequence.status)
                    tool_uses.append(
                        (
                            "mcp__exomem__plan_memory",
                            {"action": "triage", "title": consequence.title},
                        )
                    )
        if turn is not None and turn.turn_id in dict(self.spurious):
            title, event_type = dict(self.spurious)[turn.turn_id]
            self._append(title, event_type, "2026-08-07")
            tool_uses.append(
                ("mcp__exomem__record_memory", {"action": "append", "title": title})
            )
        session_id = argv[argv.index("--session-id") + 1] if "--session-id" in argv else (
            argv[argv.index("--resume") + 1]
        )
        return _Proc(
            0,
            _fabricated_stream(session_id, tool_uses=tuple(tool_uses))
            + _fabricated_hook_stream(session_id),
        )


def _seeded(tmp_path):
    from epistemic.corpora.lifecycle_replay import seed_replay_vault

    vault = tmp_path / "vault"
    return vault, seed_replay_vault(vault)


def test_the_environment_floor_strips_the_session_variables() -> None:
    from epistemic.journeys import f27_replay as journey

    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/anyone",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_SSE_PORT": "1234",
        "CLAUDE_PID": "999",
        "EXOMEM_VAULT_PATH": "/somewhere/real",
    }
    floor, removed = journey.environment_floor(parent)
    assert set(removed) == {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_PID",
    }
    assert not any(key in floor for key in removed)
    assert floor["PATH"] == "/usr/bin"


def test_the_driver_refuses_when_no_agent_cli_is_installed() -> None:
    from epistemic.journeys import f27_replay as journey

    with pytest.raises(journey.AgentEnvelopeNotDiscovered):
        journey.discover_agent_envelope(which=lambda _name: None)


def test_the_driver_records_the_installed_cli_version() -> None:
    from epistemic.journeys import f27_replay as journey

    _cli_or_skip()
    envelope = journey.discover_agent_envelope()
    assert envelope.version
    assert envelope.executable.is_file()


def test_build_turn_argv_opens_a_session_and_then_resumes_it() -> None:
    from epistemic.journeys import f27_replay as journey

    arm = journey.ARMS["hookless"]
    common = dict(
        executable=Path("/usr/bin/claude"),
        arm=arm,
        mcp_config=Path("/w/mcp.json"),
        session_id="6f0b1a4e-2c3d-4e5f-8a9b-0c1d2e3f4a5b",
        model="sonnet",
        max_turns=8,
        append_system_prompt_file=Path("/w/custom-instructions.txt"),
        plugin_dir=None,
    )
    first = journey.build_turn_argv(prompt="hello", first=True, **common)
    later = journey.build_turn_argv(prompt="hello again", first=False, **common)

    assert first[:3] == ["/usr/bin/claude", "-p", "hello"]
    assert "--session-id" in first and "--resume" not in first
    assert "--resume" in later and "--session-id" not in later
    assert later[later.index("--resume") + 1] == common["session_id"]
    for flag in (
        "--strict-mcp-config",
        "--mcp-config",
        "--setting-sources",
        "--output-format",
        "--verbose",
        "--include-hook-events",
        "--allowedTools",
        "--max-turns",
        "--model",
    ):
        assert flag in first, flag
    assert first[first.index("--setting-sources") + 1] == "project"
    assert first[first.index("--output-format") + 1] == "stream-json"


def test_build_turn_argv_gives_each_arm_the_surface_its_users_get() -> None:
    from epistemic.journeys import f27_replay as journey

    hookless = journey.build_turn_argv(
        executable=Path("/usr/bin/claude"),
        arm=journey.ARMS["hookless"],
        prompt="p",
        mcp_config=Path("/w/mcp.json"),
        session_id="s",
        model="sonnet",
        max_turns=8,
        first=True,
        append_system_prompt_file=Path("/w/custom-instructions.txt"),
        plugin_dir=None,
    )
    hooked = journey.build_turn_argv(
        executable=Path("/usr/bin/claude"),
        arm=journey.ARMS["hooked"],
        prompt="p",
        mcp_config=Path("/w/mcp.json"),
        session_id="s",
        model="sonnet",
        max_turns=8,
        first=True,
        append_system_prompt_file=None,
        plugin_dir=Path("/repo/plugins/claude-code"),
    )
    assert hookless[hookless.index("--tools") + 1] == ""
    assert "--plugin-dir" not in hookless
    assert (
        hookless[hookless.index("--append-system-prompt-file") + 1]
        == "/w/custom-instructions.txt"
    )
    assert hookless[hookless.index("--allowedTools") + 1] == "mcp__exomem"

    assert hooked[hooked.index("--tools") + 1] == "Skill"
    assert hooked[hooked.index("--plugin-dir") + 1] == "/repo/plugins/claude-code"
    assert "--append-system-prompt-file" not in hooked
    assert hooked[hooked.index("--allowedTools") + 1] == "mcp__exomem Skill"


def test_every_flag_the_driver_uses_is_one_the_installed_cli_accepts() -> None:
    """Checked against the CLI's own help, never against a restated list."""

    from epistemic.journeys import f27_replay as journey

    _cli_or_skip()
    envelope = journey.discover_agent_envelope()
    declared = journey.declared_cli_options(envelope)
    assert "--mcp-config" in declared and "--setting-sources" in declared

    used = {
        token
        for arm_id, plugin_dir, block in (
            ("hookless", None, Path("/w/custom-instructions.txt")),
            ("hooked", Path("/repo/plugins/claude-code"), None),
        )
        for first in (True, False)
        for token in journey.build_turn_argv(
            executable=envelope.executable,
            arm=journey.ARMS[arm_id],
            prompt="p",
            mcp_config=Path("/w/mcp.json"),
            session_id="s",
            model="sonnet",
            max_turns=8,
            first=first,
            append_system_prompt_file=block,
            plugin_dir=plugin_dir,
        )
        if token.startswith("--")
    }
    undeclared = used - declared
    assert undeclared <= journey.UNDOCUMENTED_BUT_ACCEPTED, sorted(undeclared)
    # No stale pins: an option the driver stopped using may not sit in the
    # allowance pretending to be justified by a probe nobody re-ran.
    assert journey.UNDOCUMENTED_BUT_ACCEPTED <= used


@pytest.mark.parametrize(
    ("proc", "expected"),
    [
        (_Proc(2, "", "boom"), "exit code 2"),
        (_Proc(0, _fabricated_stream("s", subtype="error_during_execution")), "error"),
        (_Proc(0, _fabricated_stream("s", is_error=True)), "error"),
        (_Proc(0, _fabricated_stream("s", trailing_garbage=True)), "malformed"),
        (_Proc(0, _fabricated_stream("s", result_text="Not logged in")), "logged in"),
    ],
)
def test_a_failed_execution_blocks_the_arm_and_is_never_a_product_result(
    tmp_path, proc, expected
) -> None:
    from epistemic.journeys import f27_replay as journey

    result = journey.run_journey(
        out_dir=tmp_path / "run",
        arm_ids=("hookless",),
        runner_factory=lambda _ctx: (lambda _argv: proc),
        envelope=journey.AgentEnvelope(executable=Path("/usr/bin/claude"), version="test"),
        model="sonnet",
        taken_at="2026-08-23T00:00:00Z",
        prominence_writer=lambda _arm, _env: "recorded",
    )
    arm = result.arms[0]
    assert arm.harness_fault is True
    assert expected in (arm.fault_reason or "")
    assert arm.snapshot is None


def test_a_blocked_arm_evaluates_both_assertions_blocked(tmp_path, released: None) -> None:
    from epistemic.journeys import f27_replay as journey
    from epistemic.schema import load_scenario

    result = journey.run_journey(
        out_dir=tmp_path / "run",
        arm_ids=("hookless", "hooked"),
        runner_factory=lambda _ctx: (lambda _argv: _Proc(1, "", "not logged in")),
        envelope=journey.AgentEnvelope(executable=Path("/usr/bin/claude"), version="test"),
        model="sonnet",
        taken_at="2026-08-23T00:00:00Z",
        prominence_writer=lambda _arm, _env: "recorded",
    )
    scenario = load_scenario(SEQUENCE3 / "f27-lifecycle-routing-replay.yaml")
    run = journey.evaluate_replay(scenario, result, run_root=tmp_path / "run")
    assert {bound.result.outcome for bound in run.assertions} == {"blocked"}
    assert len(run.assertions) == 4
    for bound in run.assertions:
        assert "harness fault" in bound.result.evidence


def test_the_full_offline_path_runs_for_both_arms(tmp_path, released: None) -> None:
    """parse -> project -> evaluate -> report, on a FABRICATED transcript."""

    from epistemic.corpora.lifecycle_replay import replay_corpus
    from epistemic.journeys import f27_replay as journey
    from epistemic.schema import load_scenario

    corpus = replay_corpus()
    agents: dict[str, _ScriptedAgent] = {}

    def factory(ctx):
        agents[ctx.arm.arm_id] = _ScriptedAgent(
            ctx.vault,
            ctx.seeded["initiative"],
            corpus,
            land=True,
            spurious=(("t04-tentative", ("Assembly checklist", "produced")),)
            if ctx.arm.arm_id == "hooked"
            else (),
        )
        return agents[ctx.arm.arm_id]

    out = tmp_path / "run"
    result = journey.run_journey(
        out_dir=out,
        arm_ids=("hookless", "hooked"),
        runner_factory=factory,
        envelope=journey.AgentEnvelope(executable=Path("/usr/bin/claude"), version="test"),
        model="sonnet",
        taken_at="2026-08-23T00:00:00Z",
        prominence_writer=lambda _arm, _env: "recorded",
    )
    assert [arm.arm_id for arm in result.arms] == ["hookless", "hooked"]
    for arm in result.arms:
        assert arm.harness_fault is False, arm.fault_reason
        assert arm.snapshot is not None
        assert len(arm.transcripts) == len(corpus.turns)
    # One session per arm, opened once and resumed for every later turn.
    for agent in agents.values():
        assert sum(1 for argv in agent.argvs if "--session-id" in argv) == 1
        assert sum(1 for argv in agent.argvs if "--resume" in argv) == len(corpus.turns) - 1

    scenario = load_scenario(SEQUENCE3 / "f27-lifecycle-routing-replay.yaml")
    run = journey.evaluate_replay(scenario, result, run_root=out)
    outcomes = {(b.phase_id, b.assertion): b.result.outcome for b in run.assertions}
    assert outcomes[("hookless", "lifecycle_consequence_landed_unprompted")] == "pass"
    assert outcomes[("hookless", "no_structured_write_beyond_expectation")] == "pass"
    assert outcomes[("hooked", "lifecycle_consequence_landed_unprompted")] == "pass"
    assert outcomes[("hooked", "no_structured_write_beyond_expectation")] == "fail"

    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    by_arm = {row["arm"]: row for row in report["arms"]}
    assert by_arm["hookless"]["coverage"]["intent"] == {
        "landed": 4,
        "expected": 4,
        "missing": [],
    }
    # Coverage never appears without its dual, from the same run.
    for row in report["arms"]:
        assert set(row["coverage"]) == {"intent", "outcome", "transition"}
        assert isinstance(row["extras_count"], int)
        # Not `>= 0`: the round-0 counter returned zero on every real
        # transcript and a vacuous bound could not tell that from a quiet run.
        # One firing and three invocations per turn, per the shaped fixture.
        assert row["capture_nudge_firings"] == len(corpus.turns)
        assert row["hook_invocations"] == 3 * len(corpus.turns)
        assert row["structured_write_tool_uses"]["mcp__exomem__record_memory"] >= 1
        assert row["usage"]["input_tokens"] > 0
    assert by_arm["hooked"]["extras_count"] == 1
    assert "total" not in report and "score" not in report
    assert manifest["cli_version"] == "test"
    assert manifest["model"] == "sonnet"
    assert manifest["corpus_digest"] == corpus.digest()
    assert len(manifest["fixture_digest"]) == 64
    assert manifest["prominence"] == {"hookless": "maximal", "hooked": "balanced"}
    assert manifest["exomem_version"]


def test_dry_run_prints_argv_and_the_environment_delta_and_executes_nothing(
    tmp_path, capsys
) -> None:
    from epistemic.corpora.lifecycle_replay import replay_corpus
    from epistemic.journeys import f27_replay as journey

    def poison(_ctx):
        def refuse(_argv):
            raise AssertionError("a dry run must execute nothing")

        return refuse

    exit_code = journey.main(
        [
            "--arm",
            "both",
            "--out",
            str(tmp_path / "dry"),
            "--dry-run",
            "--model",
            "sonnet",
        ],
        runner_factory=poison,
        prominence_writer=lambda _arm, _env: pytest.fail("a dry run must set nothing"),
    )
    printed = capsys.readouterr().out
    assert exit_code == 0
    corpus = replay_corpus()
    for arm_id in ("hookless", "hooked"):
        assert f"arm {arm_id}" in printed
    for turn in corpus.turns:
        assert turn.turn_id in printed
    assert printed.count("claude -p") == 2 * len(corpus.turns)
    assert "env removed:" in printed
    assert not (tmp_path / "dry" / "report.json").exists()


# --------------------------------------------------------------------------
# The discriminating pairs the frozen registry requires of every assertion.
#
# Exported rather than duplicated in `test_epistemic_assertions.py`: two
# hand-built snapshots of the same fold would drift, and the day they did, the
# registry-wide pair check would be attesting to a fixture nobody maintains.
# --------------------------------------------------------------------------


def replay_complete_context():
    """A replay that landed every declared consequence and nothing else."""

    plan_items, record_items = _complete_replay()
    return _context(_snapshot(plan_items=plan_items, record_items=record_items))


def replay_incomplete_context():
    """The same replay with one filed intent missing."""

    plan_items, record_items = _complete_replay()
    return _context(_snapshot(plan_items=plan_items[:-1], record_items=record_items))


def replay_overwriting_context():
    """A complete replay that also appended a record for a tentative claim."""

    plan_items, record_items = _complete_replay()
    return _context(
        _snapshot(
            plan_items=plan_items,
            record_items=[*record_items, _record_item("Crate diagram", "produced")],
        )
    )


def test_the_driver_refuses_to_reuse_an_arm_directory(tmp_path) -> None:
    """A second run into the same --out is refused, not half-merged.

    Found while producing the sample artifacts: seeding on top of an existing
    arm directory surfaced as the product's own CREATE_ONLY_CONFLICT deep in a
    collection write, which reads like a product fault and is a harness one.
    """

    from epistemic.journeys import f27_replay as journey

    kwargs = dict(
        out_dir=tmp_path / "run",
        arm_ids=("hookless",),
        runner_factory=lambda _ctx: (lambda _argv: _Proc(0, "")),
        envelope=journey.AgentEnvelope(executable=Path("/usr/bin/claude"), version="test"),
        model="sonnet",
        taken_at="2026-08-23T00:00:00Z",
        prominence_writer=lambda _arm, _env: "recorded",
    )
    journey.run_journey(**kwargs)
    with pytest.raises(journey.JourneySetupError) as excinfo:
        journey.run_journey(**kwargs)
    assert "hookless" in str(excinfo.value)


# --------------------------------------------------------------------------
# Correction round 1. Every test below was written against the round-0 code and
# shown RED there first; the ids in the names match the correction packet.
# --------------------------------------------------------------------------


def _seed_snapshot(pages=(), phase="hookless"):
    """The projection of the seeded vault before turn 1.

    The false-write dual diffs against this, not against an empty vault: after
    `init_vault` the seed already carries a scaffold, and a scaffold page the
    harness itself laid is not something the agent wrote.
    """

    from epistemic.corpora.lifecycle_replay import PLANNING_PATH, RECORDS_PATH
    from epistemic.snapshot import EpistemicStateSnapshot

    seeded = [
        _page("Knowledge Base/log.md"),
        _page("Knowledge Base/index.md"),
        _page("Knowledge Base/Notes/index.md"),
        _page("Knowledge Base/Sources/index.md"),
        _page(PLANNING_PATH),
        _page(RECORDS_PATH),
        *[_page(path) for path in pages],
    ]
    return EpistemicStateSnapshot(
        provider="exomem",
        variant="native",
        phase=phase,
        taken_at="2026-08-23T00:00:00Z",
        items=tuple(seeded),
        collections=_collections(),
        projector=_projector(),
    )


# --- B1: the login lives under the parent HOME ---------------------------


def test_the_arm_environment_keeps_the_parent_home_and_adds_no_config_dir() -> None:
    """B1. Moving HOME hides ~/.claude and every live turn answers "Not logged in".

    Isolation is --setting-sources project, an out-of-tree cwd, the EXOMEM_* pins
    and EXOMEM_HOOK_HOME. It is not, and cannot be, the credential directory:
    CLAUDE_CONFIG_DIR moves the credentials too, and copying them is forbidden.
    """

    from epistemic.journeys import f27_replay as journey

    floor = {"PATH": "/usr/bin", "HOME": "/home/anyone", "EXOMEM_VAULT_PATH": "/somewhere/real"}
    env = journey.arm_environment(
        floor, arm=journey.ARMS["hooked"], workdir=Path("/w"), vault=Path("/w/vault")
    )
    assert env["HOME"] == floor["HOME"]
    assert "CLAUDE_CONFIG_DIR" not in env
    # The EXOMEM pins still take over from anything inherited.
    assert env["EXOMEM_VAULT_PATH"] == "/w/vault"
    assert env["EXOMEM_HOOK_HOME"] == str(Path("/w/hook-home"))


# --- B2: a cwd inside a repository hands the agent its memory files ------


def test_the_driver_refuses_an_out_directory_under_a_memory_file_or_repository(tmp_path) -> None:
    """B2. --setting-sources does not govern memory-file discovery.

    Measured 2026-08-23: from a cwd inside this repo the child's context carried
    this repo's CLAUDE.md *and* the operator's ~/.claude/CLAUDE.md; from an
    out-of-tree cwd it carried none. A memory file that names the store would
    corrupt both arms of a family whose whole claim is that nothing named it.
    """

    from epistemic.journeys import f27_replay as journey

    for marker in ("CLAUDE.md", ".claude", ".git"):
        tree = tmp_path / marker.replace(".", "") / "nested"
        tree.mkdir(parents=True)
        offender = tree.parent / marker
        if marker == "CLAUDE.md":
            offender.write_text("# memory\n", encoding="utf-8")
        else:
            offender.mkdir()
        with pytest.raises(journey.JourneySetupError) as excinfo:
            journey.refuse_unsafe_out_dir(tree / "run")
        assert str(offender) in str(excinfo.value), marker

    clean = tmp_path / "clean" / "run"
    journey.refuse_unsafe_out_dir(clean)


# --- B3: the seeded vault must satisfy the product's own vault check -----


def test_the_seeded_vault_satisfies_the_products_own_vault_check(tmp_path, monkeypatch) -> None:
    """B3. Without a schema contract the stdio server refuses to start.

    Asserted through `resolve_vault`, the function that actually raises, rather
    than through a `_Schema/` directory check of our own: a check the harness
    invented could pass while the server the arm talks to still refuses.
    """

    from epistemic.corpora.lifecycle_replay import seed_replay_vault

    from exomem.vault import resolve_vault

    vault = tmp_path / "vault"
    seed_replay_vault(vault)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    assert resolve_vault() == vault


def _poison_the_seed(monkeypatch, *, error: str, warning: str) -> None:
    """Make the seed emit a real graph-sync ERROR and WARNING, as the product does.

    Through ``logging.getLogger("exomem.graph_sync")`` rather than through the
    watch object, so the test exercises the same path
    ``src/exomem/graph_sync.py`` uses and not the harness's own bookkeeping.
    """

    import logging

    from epistemic.corpora import lifecycle_replay

    real_seed = lifecycle_replay._seed

    def poisoned(root, planning, records, collections, init_vault, watch):
        logger = logging.getLogger("exomem.graph_sync")
        logger.error(error)
        logger.warning(warning)
        return real_seed(root, planning, records, collections, init_vault, watch)

    monkeypatch.setattr(lifecycle_replay, "_seed", poisoned)


#: The product's own log template for the routine stopped-rebuild report, copied
#: from `src/exomem/graph_sync.py:2526`. Everything before the first `%s` is what
#: survives formatting unchanged, so that prefix is what a captured line is
#: matched on. Pinned against the source file by
#: `test_the_routine_rebuild_stopped_template_is_still_the_products_own`.
ROUTINE_REBUILD_STOPPED_TEMPLATE = (
    "graph rebuild stopped checkpoint_sha256=%s generation=%s"
)
_ROUTINE_REBUILD_STOPPED_PREFIX = ROUTINE_REBUILD_STOPPED_TEMPLATE.split("%s")[0]


def _is_routine_rebuild_stopped(line: str) -> bool:
    """One captured `<LEVEL> <logger>: <message>` line, of the routine class.

    The level is deliberately not part of the predicate. A handler level filters
    what it is offered; it cannot relabel a record, so the level a line carries
    is the product's choice and the class is the harness's question. Today every
    routine line is ERROR (`logger.exception`); a product that demoted it to
    WARNING tomorrow would not have changed what the seed observed.
    """

    if ": " not in line:
        return False
    level_and_logger, message = line.split(": ", 1)
    return level_and_logger.endswith(" exomem.graph_sync") and message.startswith(
        _ROUTINE_REBUILD_STOPPED_PREFIX
    )


def test_the_routine_rebuild_stopped_template_is_still_the_products_own() -> None:
    """F2. The clean-case predicate exempts one message class; pin it to source.

    A prefix match against a message the product could reword is a check with a
    silent failure mode: reworded, every future line stops matching and the clean
    case starts failing on routine noise — or, worse, the reword makes some other
    message match and real breakage is exempted. Read from
    `src/exomem/graph_sync.py`, where there are exactly two call sites at WARNING
    or above (`:2525` exception, `:3410` warning).
    """

    source = (ROOT / "src" / "exomem" / "graph_sync.py").read_text(encoding="utf-8")
    assert ROUTINE_REBUILD_STOPPED_TEMPLATE in source


def test_a_non_routine_error_is_not_clean(tmp_path, monkeypatch) -> None:
    """F2. The clean-case predicate has to fail on anything outside the class.

    The exemption is one message class, not the ERROR level and not the
    `exomem.graph_sync` logger: a different failure from the same logger at the
    same level is exactly what the seed warning exists to surface.
    """

    from epistemic.corpora.lifecycle_replay import seed_replay_vault

    _poison_the_seed(
        monkeypatch,
        error="graph projection moved under an in-flight proof",
        warning="abandoned-temporary sweep failed for family glob",
    )
    warnings = seed_replay_vault(tmp_path / "vault")["warnings"]
    unexpected = [line for line in warnings if not _is_routine_rebuild_stopped(line)]
    assert len(unexpected) == 2, warnings
    assert unexpected[0].startswith("ERROR exomem.graph_sync: graph projection moved")
    assert unexpected[1].startswith("WARNING exomem.graph_sync: abandoned-temporary sweep")


def test_a_clean_seed_captures_nothing_outside_the_routine_class(tmp_path) -> None:
    """F2. Asserted on the RETURN VALUE, because caplog cannot see this logger.

    The seed sets ``propagate = False`` on ``exomem.graph_sync`` for its
    duration — that is the mechanism, it is what keeps the traceback off stderr.
    pytest's ``caplog`` handler lives on the root logger, so a test that asserts
    ``caplog.records == []`` around the seed asserts the mechanism it was meant
    to check: it passes whether the seed was quiet or screaming. The observable
    that discriminates is what the seed hands back.

    Clean means "nothing outside the routine class", not an empty tuple and not
    "no ERROR". Measured on this host, 21 of 30 clean seeds record at least one
    ``graph rebuild stopped checkpoint_sha256=… generation=N`` line, 25 lines
    over 30 seeds, **all of them ERROR** — that message has exactly one call site
    and it is ``logger.exception`` at ``src/exomem/graph_sync.py:2525``. Neither
    `== ()` nor "no ERROR line" is a check here; both are dice. The class is what
    carries meaning, so the class is what is asserted, and the template is pinned
    against the product's own source by the test below so this cannot drift into
    matching something else.
    """

    from epistemic.corpora.lifecycle_replay import seed_replay_vault

    warnings = seed_replay_vault(tmp_path / "vault")["warnings"]
    assert [line for line in warnings if not _is_routine_rebuild_stopped(line)] == []


def test_the_seed_captures_an_error_and_a_warning_and_leaks_neither(
    tmp_path, caplog, monkeypatch
) -> None:
    """F2 + F3. Both levels are captured verbatim, and neither reaches the root.

    WARNING matters on its own evidence: ``src/exomem/graph_sync.py:3410`` logs
    the abandoned-temporary sweep at WARNING with ``exc_info=True``. A watch
    listening only at ERROR, behind ``propagate = False``, would delete that
    record from the world — off stderr where it used to be, and out of
    ``seed_warnings`` where it should now be.
    """

    import logging

    from epistemic.corpora.lifecycle_replay import seed_replay_vault

    _poison_the_seed(
        monkeypatch,
        error="graph rebuild stopped: projection moved",
        warning="abandoned temporary swept",
    )
    with caplog.at_level(logging.DEBUG):
        seeded = seed_replay_vault(tmp_path / "vault")

    # Containment, not equality: a real seed adds its own routine ERRORs
    # around these two, and pinning the whole tuple would pin that rate.
    assert "ERROR exomem.graph_sync: graph rebuild stopped: projection moved" in (
        seeded["warnings"]
    )
    assert "WARNING exomem.graph_sync: abandoned temporary swept" in seeded["warnings"]
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("exomem.graph_sync")
    ] == []


def test_the_watch_records_the_level_beside_the_message() -> None:
    """F2. The level a record carried is part of what the seed observed.

    It is not what "clean" is decided on — the message class is — but a captured
    line that dropped the level would make the manifest strictly less readable
    than the stderr it replaced, and would leave a reader unable to tell the
    routine ERROR from the sweep WARNING at all.

    Pinned on the handler directly rather than through a seed, because whether a
    real seed emits anything is a rate (21 of 30 on this host) and a format
    assertion that only runs when the dice fall right is not a pin.
    """

    import logging

    from epistemic.corpora.lifecycle_replay import _GraphSyncWatch

    watch = _GraphSyncWatch()
    logger = logging.getLogger("test.graph_sync_watch_format")
    logger.addHandler(watch)
    logger.propagate = False
    try:
        logger.warning("swept one abandoned temporary")
        logger.error("rebuild stopped")
    finally:
        logger.removeHandler(watch)

    assert watch.captured == [
        "WARNING test.graph_sync_watch_format: swept one abandoned temporary",
        "ERROR test.graph_sync_watch_format: rebuild stopped",
    ]


def test_the_graph_sync_watch_listens_at_warning(tmp_path) -> None:
    """F3. The level is the whole of the WARNING fix; pin it directly."""

    import logging

    from epistemic.corpora.lifecycle_replay import _GraphSyncWatch

    assert _GraphSyncWatch().level == logging.WARNING


# --- M1: a dry run is dry ------------------------------------------------


def test_a_dry_run_writes_nothing_and_prints_the_whole_environment_delta(
    tmp_path, capsys, monkeypatch
) -> None:
    """M1 + B1. The old dry run seeded two real vaults and printed EXOMEM_* only.

    The filter is what hid the HOME override that made every live turn fail, so
    the delta is now every key added, changed or removed.
    """

    from epistemic.journeys import f27_replay as journey

    # A parent that already pins the vault turns that key from an *addition*
    # into a *change*, which is the branch of `environment_delta` a dry run
    # would otherwise never print — and the one that would hide an operator's
    # own EXOMEM_* export being silently overridden under them.
    monkeypatch.setenv("EXOMEM_VAULT_PATH", "/parent/vault")

    out = tmp_path / "dry"
    exit_code = journey.main(
        ["--arm", "both", "--out", str(out), "--dry-run"],
        runner_factory=lambda _ctx: (lambda _argv: pytest.fail("a dry run must execute nothing")),
        prominence_writer=lambda _arm, _env: pytest.fail("a dry run must set nothing"),
    )
    assert exit_code == 0
    assert not out.exists() or not list(out.rglob("*")), sorted(
        p.name for p in out.rglob("*")
    )

    printed = capsys.readouterr().out
    assert "env removed:" in printed
    assert "env added:" in printed
    added = printed.split("env added:")[1].split("\n")[0]
    assert not re.search(r"\bHOME=", added), added
    assert "EXOMEM_VAULT_PATH" in printed
    assert "prominence" in printed
    changed = printed.split("env changed:")[1].split("\n")[0]
    assert "EXOMEM_VAULT_PATH='/parent/vault' ->" in changed, changed
    for turn in journey.replay_corpus().turns:
        assert turn.turn_id in printed


# --- M2: the false-write dual diffs against the seeded page set ----------


def test_a_stray_page_inside_a_collection_directory_is_an_extra() -> None:
    """M2(i). Prose written instead of a record is the corpus's own failure case."""

    plan_items, record_items = _complete_replay()
    stray = "Knowledge Base/Planning/Delivery/stray-prose-note.md"
    result = _extras(_snapshot(plan_items=plan_items, record_items=record_items, pages=(stray,)))
    assert result.outcome == "fail", result.evidence
    assert "stray-prose-note" in result.evidence


def test_an_item_file_under_the_declared_storage_subdirectory_is_not_an_extra() -> None:
    """M2(ii). The exemption is the storage directory the manifest declares."""

    plan_items, record_items = _complete_replay()
    owned = (
        "Knowledge Base/Planning/Delivery/Items/batch-layout-draft.md",
        "Knowledge Base/Records/Deliveries/Events/2026-08-01-packing-insert-delivered.md",
    )
    assert _extras(
        _snapshot(plan_items=plan_items, record_items=record_items, pages=owned)
    ).outcome == "pass"


def test_a_page_the_seed_laid_is_not_an_extra() -> None:
    """M2(iii). `init_vault` lays a scaffold; the agent did not write it."""

    plan_items, record_items = _complete_replay()
    scaffold = ("Knowledge Base/Entities/index.md",)
    snapshot = _snapshot(plan_items=plan_items, record_items=record_items, pages=scaffold)
    assert _extras(snapshot, seeded=_seed_snapshot(pages=scaffold)).outcome == "pass"


def test_a_page_written_outside_every_collection_is_an_extra() -> None:
    """M2(iv)."""

    plan_items, record_items = _complete_replay()
    result = _extras(
        _snapshot(
            plan_items=plan_items,
            record_items=record_items,
            pages=("Knowledge Base/Notes/Insights/batch-run-summary.md",),
        )
    )
    assert result.outcome == "fail", result.evidence
    assert "batch-run-summary" in result.evidence


def test_extras_are_blocked_without_the_seeded_snapshot() -> None:
    """M2. No seed projection, no baseline — and a missing baseline is not a pass."""

    plan_items, record_items = _complete_replay()
    result = _extras(_snapshot(plan_items=plan_items, record_items=record_items), seeded=None)
    assert result.outcome == "blocked", result.evidence
    assert "seed" in result.evidence


def test_the_projector_reads_the_declared_storage_subdirectory(tmp_path) -> None:
    """M2. `storage.source` is read from the manifest, never guessed."""

    from epistemic.corpora.lifecycle_replay import seed_replay_vault
    from epistemic.projectors.exomem_vault import VaultProjector

    vault = tmp_path / "vault"
    seed_replay_vault(vault)
    snapshot = VaultProjector(vault).project(phase="seed", taken_at="2026-08-23T00:00:00Z")
    by_profile = {collection.profile: collection for collection in snapshot.collections}
    assert by_profile["planning"].storage_source == "Items"
    assert by_profile["records"].storage_source == "Events"


# --- M3: the hook lines the CLI actually emits ---------------------------


def test_the_old_hook_event_subtype_counts_nothing() -> None:
    """M3. The round-0 counter keyed on a subtype claude 2.1.240 never emits."""

    from epistemic.journeys import f27_replay as journey

    never_emitted = json.dumps(
        {"type": "system", "subtype": "hook_event", "hook_event_name": "Stop"}
    )
    assert journey.count_hook_activity(never_emitted + "\n") == (0, 0)


def test_hook_activity_counts_responses_and_the_capture_nudge_firing() -> None:
    """M3. One hook event = one hook_response; a firing carries the REMINDER."""

    from epistemic.journeys import f27_replay as journey

    stream = _fabricated_hook_stream()
    invocations, firings = journey.count_hook_activity(stream)
    assert invocations == 3, stream
    assert firings == 1


# --- M4: --append-system-prompt-file is documented, just bracketed -------


def test_declared_cli_options_extract_bracketed_variants() -> None:
    """M4. `--append-system-prompt[-file]` declares two options, not one."""

    from epistemic.journeys import f27_replay as journey

    parsed = journey._long_options(
        "  --append-system-prompt[-file], --add-dir\n  --plugin-dir <path>\n"
    )
    assert "--append-system-prompt" in parsed
    assert "--append-system-prompt-file" in parsed
    assert "--add-dir" in parsed
    assert "--plugin-dir" in parsed


# --- m1: the corpus id is not optional -----------------------------------


def test_the_scenario_refuses_an_expectation_without_the_corpus_subject(released: None) -> None:
    """m1. Both assertions read their expectation out of the corpus `subject`."""

    from epistemic.registry import REQUIRES_SUBJECT
    from epistemic.schema import ScenarioLoadError, load_scenario

    assert {"lifecycle_consequence_landed_unprompted", "no_structured_write_beyond_expectation"} <= (
        set(REQUIRES_SUBJECT)
    )
    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(FIXTURES / "red-sequence3-subjectless-expectation.yaml")
    assert "subject" in str(excinfo.value)


# --- m2: the prefix the spec actually names ------------------------------


def test_the_environment_floor_strips_the_underscored_prefix_only() -> None:
    """m2. `CLAUDE_CODE_` — a variable merely starting with `CLAUDE_CODE` stays."""

    from epistemic.journeys import f27_replay as journey

    assert journey.SESSION_VARIABLE_PREFIXES == ("CLAUDE_CODE_",)
    floor, removed = journey.environment_floor(
        {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODEBASE_HINT": "keep", "PATH": "/usr/bin"}
    )
    assert set(removed) == {"CLAUDE_CODE_ENTRYPOINT"}
    assert floor["CLAUDE_CODEBASE_HINT"] == "keep"


# --- m3: a session id may never be reused --------------------------------


def test_two_runs_pinned_to_one_timestamp_use_different_session_ids(tmp_path) -> None:
    """m3. `--session-id` is refused if the id already exists, so it must be fresh."""

    from epistemic.journeys import f27_replay as journey

    def run(name):
        return journey.run_journey(
            out_dir=tmp_path / name,
            arm_ids=("hookless",),
            runner_factory=lambda _ctx: (lambda _argv: _Proc(0, "")),
            envelope=journey.AgentEnvelope(executable=Path("/usr/bin/claude"), version="test"),
            taken_at="2026-08-23T00:00:00Z",
            prominence_writer=lambda _arm, _env: "recorded",
            dry_run=True,
        )

    first = run("a").arms[0].session_id
    second = run("b").arms[0].session_id
    assert first != second


# --- m4: a read is not a write -------------------------------------------


def test_only_write_actions_count_as_structured_writes() -> None:
    """m4. Live, both arms opened with `plan_memory(action="inspect")` — a read."""

    from epistemic.journeys import f27_replay as journey
    from membench.trackc.witness_join import parse_stream_json_transcript

    result = journey.ArmResult(arm_id="hookless", session_id="s", prominence="maximal")
    result.transcripts = (
        parse_stream_json_transcript(
            _fabricated_stream(
                "s",
                tool_uses=(
                    ("mcp__exomem__plan_memory", {"action": "inspect"}),
                    ("mcp__exomem__plan_memory", {"action": "add"}),
                    ("mcp__exomem__record_memory", {"action": "query"}),
                    ("mcp__exomem__record_memory", {"action": "append"}),
                    ("Skill", {"command": "exomem"}),
                ),
            ).splitlines()
        ),
    )
    assert result.structured_write_tool_uses() == {
        "mcp__exomem__plan_memory": 1,
        "mcp__exomem__record_memory": 1,
    }
    assert result.tool_uses()["mcp__exomem__plan_memory"] == 2
    assert result.tool_uses()["Skill"] == 1


# --- the turn cap is not a crash -----------------------------------------


def test_a_turn_cap_exhaustion_is_named_as_such() -> None:
    """A blocked arm must tell the operator which kind of blocked it was."""

    from epistemic.journeys import f27_replay as journey

    stream = json.dumps(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "num_turns": 5,
            "errors": ["Reached maximum number of turns (4)"],
            "session_id": "s",
        }
    )
    from membench.trackc.witness_join import parse_stream_json_transcript

    transcript = parse_stream_json_transcript(stream.splitlines())
    reason = journey.fault_reason(_Proc(1, stream), transcript)
    assert reason is not None
    assert "error_max_turns" in reason
    assert "max-turns" in reason or "turn cap" in reason


# --- nits ----------------------------------------------------------------


def test_every_tier_carries_at_least_one_expected_consequence() -> None:
    """NIT. A tier of size zero reports 0/0 and passes vacuously for ever.

    Asserted by the corpus module itself, not only here: a future edit that drops
    the last transition must be refused where the corpus is built, so the guard
    holds for any corpus and not merely for the one this test happens to read.
    """

    from epistemic.corpora.lifecycle_replay import (
        DELIVERABLES,
        TIERS,
        build_corpus,
        replay_corpus,
    )

    corpus = replay_corpus()
    for tier in TIERS:
        assert corpus.expected.tier_size(tier) > 0, tier

    import dataclasses

    intent_only = tuple(
        dataclasses.replace(
            turn,
            consequences=tuple(c for c in turn.consequences if c.tier == "intent"),
        )
        for turn in corpus.turns
    )
    with pytest.raises(ValueError) as excinfo:
        build_corpus(corpus.corpus_id, DELIVERABLES, intent_only)
    assert "outcome" in str(excinfo.value)
    assert "transition" in str(excinfo.value)


def test_the_gate_refuses_an_inflected_store_verb() -> None:
    """NIT. D5's intent is inflection-insensitive; `recorded` was missing."""

    from epistemic.corpora.lifecycle_replay import (
        StoreBearingUtterance,
        assert_no_store_bearing_utterance,
    )

    with pytest.raises(StoreBearingUtterance) as excinfo:
        assert_no_store_bearing_utterance((("t-x", "The client recorded the sign-off."),))
    assert "recorded" in str(excinfo.value)


# --------------------------------------------------------------------------
# Correction round 2. Ids match the round-2 packet; each was RED on 0e8da68a.
# --------------------------------------------------------------------------


def test_a_phase_that_forgot_its_seed_snapshot_blocks_rather_than_scoring(
    released: None, tmp_path
) -> None:
    """F1. The runner reaches snapshots cumulatively — on purpose, for other families.

    ``runner.evaluate_scenario`` accumulates ``reached_snapshots`` across the
    whole scenario, so the ``REQUIRES_SNAPSHOT_PAIR`` gate is satisfied for the
    hooked phase by the *hookless* phase's two snapshots, and ``ctx.prior``
    becomes the hookless arm's POST-RUN projection. That semantics is deliberate
    and other families depend on it, so f27 refuses inside its own assertion
    rather than changing it: a baseline from another phase is not this phase's
    seed, and a dual with no baseline blocks.

    Both arms here wrote the identical stray page. Scored honestly the hooked
    arm fails on it; scored against the hookless arm's post-run vault the page
    is already in the "baseline" and the dual passes on the very write it exists
    to catch.
    """

    from epistemic.runner import evaluate_scenario
    from epistemic.schema import load_scenario

    scenario = load_scenario(_fixture_without_the_hooked_seed(tmp_path))
    plan_items, record_items = _complete_replay()
    stray = "Knowledge Base/Planning/Delivery/stray-prose-note.md"
    run = evaluate_scenario(
        scenario,
        snapshots={
            "s-hookless-seed": _seed_snapshot(phase="hookless"),
            # Page-only on purpose. If the hookless arm's post-run snapshot also
            # held records, guard 2 (a baseline holding more than the seed) would
            # reject it as a baseline and this test would pass whether or not
            # guard 1 exists. Writing only the stray page leaves guard 1 — the
            # phase check — as the only thing standing between the hooked arm and
            # a `pass` on the write it just made.
            "s-hookless": _snapshot(pages=(stray,), phase="hookless"),
            "s-hooked": _snapshot(
                plan_items=plan_items,
                record_items=record_items,
                pages=(stray,),
                phase="hooked",
            ),
        },
    )
    outcomes = {
        (bound.phase_id, bound.assertion): bound.result
        for bound in run.assertions
    }
    hookless = outcomes[("hookless", "no_structured_write_beyond_expectation")]
    hooked = outcomes[("hooked", "no_structured_write_beyond_expectation")]
    assert hookless.outcome == "fail", hookless.evidence
    assert "stray-prose-note" in hookless.evidence
    assert hooked.outcome == "blocked", hooked.evidence
    assert "'hookless'" in hooked.evidence and "'hooked'" in hooked.evidence


def test_a_post_run_snapshot_from_the_same_phase_is_not_a_seed(released: None) -> None:
    """F1. Same phase is necessary but not sufficient: the baseline is pre-turn.

    A phase carrying two post-run snapshots would satisfy a phase-equality check
    while handing the dual a baseline that already contains the run's writes. The
    seeded vault has no records and no plan item outside the two the seed files,
    so a baseline that has either was taken after a turn.
    """

    plan_items, record_items = _complete_replay()
    scored = _snapshot(plan_items=plan_items, record_items=record_items)
    result = _extras(scored, seeded=scored)
    assert result.outcome == "blocked", result.evidence
    assert "before turn 1" in result.evidence


def test_the_seed_snapshot_is_taken_in_the_arms_own_phase(tmp_path) -> None:
    """F1. The driver labels both of an arm's snapshots with the arm's phase.

    The seed used to be labelled ``<arm>-seed``, which is a ref name, not a
    phase: the scenario's phase is ``hookless``, and a snapshot claiming to come
    from a phase the scenario does not have is what let the per-phase check be
    written as a string-suffix convention instead of an observation.
    """

    from epistemic.journeys import f27_replay as journey

    result = journey.run_journey(
        out_dir=tmp_path / "run",
        arm_ids=("hookless",),
        runner_factory=lambda ctx: _ScriptedAgent(
            ctx.vault, ctx.seeded["initiative"], journey.replay_corpus()
        ),
        envelope=journey.AgentEnvelope(executable=Path("/usr/bin/claude"), version="test"),
        model="sonnet",
        taken_at="2026-08-23T00:00:00Z",
        prominence_writer=lambda _arm, _env: "recorded",
    )
    arm = result.arms[0]
    assert arm.seed_snapshot is not None
    assert arm.seed_snapshot.phase == "hookless"
    assert arm.snapshot is not None
    assert arm.snapshot.phase == "hookless"


def test_two_arms_with_no_snapshot_at_all_still_evaluate(released: None, tmp_path) -> None:
    """LOW. `_fault_snapshot` bound twice by identity is refused by the runner.

    An arm that faulted before its vault was seeded has neither snapshot, and
    both of its refs fall back to the fault. Binding one object under two refs
    trips ``_validate_inputs`` ("two snapshot refs point at the same observation
    object"), so a run that should have reported four blocked assertions raises
    instead. Only reachable from a hand-built ``JourneyResult`` today; that is
    the point of testing it, since the next fault that lands earlier gets here.
    """

    from epistemic.journeys import f27_replay as journey
    from epistemic.schema import load_scenario

    arms = tuple(
        journey.ArmResult(
            arm_id=arm_id,
            session_id="s",
            prominence="maximal",
            harness_fault=True,
            fault_reason="the vault could not be seeded",
        )
        for arm_id in ("hookless", "hooked")
    )
    result = journey.JourneyResult(
        arms=arms,
        envelope=journey.AgentEnvelope(executable=Path("/usr/bin/claude"), version="test"),
        model="sonnet",
        taken_at="2026-08-23T00:00:00Z",
        corpus=journey.replay_corpus(),
        out_dir=tmp_path / "run",
    )
    scenario = load_scenario(SEQUENCE3 / "f27-lifecycle-routing-replay.yaml")
    run = journey.evaluate_replay(scenario, result)
    assert len(run.assertions) == 4
    assert {bound.result.outcome for bound in run.assertions} == {"blocked"}
    for bound in run.assertions:
        assert "the vault could not be seeded" in bound.result.evidence
