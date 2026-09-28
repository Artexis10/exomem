"""Generic synthetic decomposition/replay acceptance (close-memory-loop task 1.7).

The verify clause has four parts, each pinned here:

* **partition before destination** -- read from the observation record:
  every routed candidate was decomposed, with all four axes, before the
  first destination, and objects the fixture separates stay in separate
  partitions (:func:`decomposition.partition_verdict`);
* **canonical homes for stable clusters without detail fragmentation** --
  checked in the vault a capture leaves;
* **preserved attribution and uncertainty**, and an old episode's own time;
* **public artifacts without private names, medical conclusions or private
  source paths**.

Expected red on the current runtime: the episode ledger has no field for a
partition, so a ledger-derived record leaves partitioning unmeasured, and an
untouched world fails every capture positive. Scripted writes through the
product's writers (plumbing, not agent evidence) make the vault side pass,
and matched wrong captures fail their own guards.
"""

from __future__ import annotations

import datetime as dt
import shutil
from itertools import combinations
from pathlib import Path

import pytest
from epistemic.memory_loop import contract
from epistemic.memory_loop import decomposition as fx
from epistemic.memory_loop import observation as obs
from epistemic.memory_loop.contract import read_state

from exomem.public_artifact_privacy import assert_public_artifacts_clean

pytestmark = pytest.mark.timeout(600)

FIXTURE_SHA256 = "f7b5381f145cf9f2c7042ab5a3c34919a5e1d9bebefe5b16d2947c89ed516fe9"
EVALUATOR_SHA256 = "f58ffe3392e90988ff84b9caff5878222aa4c6f83623c6d6136b8aa9488ca49f"
ACTOR_SHA256 = "5d231888bf6d0dea5fcc4fc91f6522e1147d52540f0e4040394716a49da9ab53"
PRE_CAPTURE_SHA256 = "49186c3596cc46383672e7e2d507b455e61c8da9f9dbcfd750b577d0d8dfd0ce"


# --------------------------------------------------------------------------- #
# The fixture
# --------------------------------------------------------------------------- #


def test_the_fixture_is_consistent() -> None:
    fx.assert_manifest_consistent()


def test_the_digests_are_frozen() -> None:
    assert fx.fixture_sha256() == FIXTURE_SHA256
    assert fx.evaluator_sha256() == EVALUATOR_SHA256
    assert fx.actor_sha256() == ACTOR_SHA256


def test_the_five_objects_the_task_names_are_distinct_partitions() -> None:
    roles = {name: partition.epistemic_role for name, partition in fx.PARTITIONS.items()}
    assert set(roles) == {"preference", "flat-week", "spring-2024", "baseline", "fenn-view"}
    assert "preference" in roles["preference"] and "product" in roles["preference"]
    assert "repeated" in roles["flat-week"] and "sensory" in roles["flat-week"]
    assert "historical" in roles["spring-2024"] and "recovery" in roles["spring-2024"]
    assert "baseline" in roles["baseline"]
    assert "attributed" in roles["fenn-view"]
    for (a, first), (b, second) in combinations(fx.PARTITIONS.items(), 2):
        differing = [
            axis
            for axis in ("retrieval_question", "subject", "temporal_episode", "epistemic_role")
            if getattr(first, axis) != getattr(second, axis)
        ]
        assert differing, (a, b)


def test_the_interpretation_names_a_family_speaker_and_stays_uncertain() -> None:
    view = next(candidate for candidate in fx.CANDIDATES if candidate.key == "fenn-view")
    assert view.provenance == "attributed" and view.uncertain
    assert "brother" in view.attributed_to
    assert view.same_home_as == ("flat-mornings",)


def test_the_open_page_is_an_alternative_not_a_default() -> None:
    homes = {candidate.key: candidate.homes for candidate in fx.CANDIDATES}
    on_open = {key for key, options in homes.items() if f"existing:{fx.CURRENTLY_OPEN}" in options}
    assert on_open == {"preference", "product-observations"}


def test_the_actor_view_withholds_partitions_homes_and_checks() -> None:
    view = fx.actor_view()
    assert set(view) == {"fixture_id", "turns", "later_turn"}
    text = repr(view)
    for partition in fx.PARTITIONS.values():
        assert partition.retrieval_question not in text and partition.epistemic_role not in text
    for candidate in fx.CANDIDATES:
        assert candidate.statement not in text
        assert all(home not in text for home in candidate.homes)
    for expectation in fx.EXPECTATIONS:
        assert expectation.key not in text and expectation.reason not in text


def _public_texts() -> list[str]:
    texts = [*fx.TURNS, fx.LATER_TURN, fx.LATER_USE.useful, *fx.LATER_USE.wrong]
    texts += [text for seed in fx.WORLD.notes for text in (seed.title, seed.observation)]
    texts += [candidate.statement for candidate in fx.CANDIDATES]
    texts += [
        getattr(partition, axis)
        for partition in fx.PARTITIONS.values()
        for axis in ("retrieval_question", "subject", "temporal_episode", "epistemic_role")
    ]
    return texts


def test_public_material_has_no_private_names_medical_conclusions_or_source_paths() -> None:
    allowed = (*fx.INVENTED_NAMES, *fx.ORDINARY_CAPITALIZED)
    for text in [*fx.TURNS, fx.LATER_TURN]:
        assert contract.undeclared_capitalized_words(text, allowed) == (), text
        assert contract.find_nudges(text) == (), text
    for text in _public_texts():
        assert contract.find_medical_terms(text) == (), text
        assert "/" not in text or "Knowledge Base" not in text
    assert_public_artifacts_clean([Path(fx.__file__)])


# --------------------------------------------------------------------------- #
# Partition before destination, from the observation record
# --------------------------------------------------------------------------- #

_TEXTS = {
    "c-pref": "Settled on the Lowfield oolong over the Brightwater house blend",
    "c-tin": "The Lowfield tin keeps it fresher and the second steep holds",
    "c-flat": "Flat taste on Monday, Wednesday and Thursday after an overnight kettle",
    "c-2024": "Spring 2024 flat month that cleared with the filter jug",
    "c-base": "Tells steeps apart blind nine times out of ten",
    "c-fenn": "Fenn thinks limescale in the new kettle",
}
_PARTITION_OF = {
    "c-pref": "preference",
    "c-tin": "preference",
    "c-flat": "flat-week",
    "c-2024": "spring-2024",
    "c-base": "baseline",
    "c-fenn": "fenn-view",
}


def _record(*, partition_ids=None, decompose_after_destination=(), skip=()) -> obs.NoNudgeObservation:
    partition_ids = partition_ids or _PARTITION_OF
    decisions: list[dict] = [{"seq": 1, "phase": "activation", "initiator": "agent"}]
    seq = 1
    late: list[dict] = []
    for key, text in _TEXTS.items():
        if key in skip:
            continue
        seq += 1
        decision = {
            "seq": seq,
            "phase": "decomposition",
            "initiator": "agent",
            "candidate_key": key,
            "text": text,
            "partition": {
                "partition_id": partition_ids[key],
                "retrieval_question": "agent's question",
                "subject": "agent's subject",
                "temporal_episode": "agent's time",
                "epistemic_role": "agent's role",
            },
        }
        (late if key in decompose_after_destination else decisions).append(decision)
    seq += 1
    decisions.append(
        {"seq": seq, "phase": "destination", "initiator": "agent", "candidate_key": "c-flat", "route": "focused_note", "title": "Flat mornings"}
    )
    for decision in late:
        seq += 1
        decisions.append({**decision, "seq": seq})
    seq += 1
    decisions.append(
        {"seq": seq, "phase": "destination", "initiator": "agent", "candidate_key": "c-fenn", "route": "semantic_unit", "title": "Flat mornings"}
    )
    return obs.load_observation(
        {
            "artifact_type": obs.ARTIFACT_TYPE,
            "schema_version": 1,
            "fixture_id": fx.FIXTURE_ID,
            "actor_sha256": fx.actor_sha256(),
            "pre_capture_sha256": "a" * 64,
            "evaluator_sha256": fx.evaluator_sha256(),
            "host_initiation": {
                "client": {"client": "generic-mcp", "adapter_version": "0", "lifecycle": "best_effort"},
                "input_origin": "synthetic_fixture",
                "delivered_turns_sha256": obs.turns_sha256(fx.TURNS),
            },
            "agent_decisions": decisions,
            "leaf_effects": [],
            "publication": {"status": "not_observed"},
        }
    )


def test_partitioning_every_object_before_any_destination_passes() -> None:
    verdict = fx.partition_verdict(_record())

    assert verdict.outcome == "pass", verdict.reasons


def test_one_recorded_partition_for_observation_and_interpretation_fails() -> None:
    merged = {**_PARTITION_OF, "c-fenn": "flat-week"}

    verdict = fx.partition_verdict(_record(partition_ids=merged))

    assert verdict.outcome == "fail"
    assert any("merges" in reason for reason in verdict.reasons)


def test_a_candidate_decomposed_after_the_first_destination_fails() -> None:
    verdict = fx.partition_verdict(_record(decompose_after_destination=("c-fenn",)))

    assert verdict.outcome == "fail"
    assert any("c-fenn" in reason for reason in verdict.reasons)


def test_an_object_never_considered_fails() -> None:
    verdict = fx.partition_verdict(_record(skip=("c-2024",)))

    assert verdict.outcome == "fail"
    assert any("spring-2024" in reason for reason in verdict.reasons)


def test_no_recorded_partition_is_unmeasured_not_passed() -> None:
    record = _record()
    stripped = record.model_copy(
        update={"agent_decisions": tuple(item for item in record.agent_decisions if item.phase != "decomposition")}
    )

    assert fx.partition_verdict(stripped).outcome == "unmeasured"


def test_a_text_that_mentions_another_object_is_not_counted_as_it() -> None:
    assert fx.identify("Fenn thinks limescale explains the Wednesday cup") == "fenn-view"
    assert fx.identify("tasting notes, continued") is None


def test_current_runtime_the_episode_ledger_cannot_record_a_partition() -> None:
    """Expected red. The candidate proposal has no partition field, so an
    observation adapted from the ledger alone has nothing for
    ``partition_verdict`` to read, and partitioning stays unmeasured until
    the pre-destination decomposition carrier (task 3.4) lands."""

    from exomem import episode_model

    proposal = {
        "route": "focused_note",
        "title": "Flat mornings",
        "alternatives": [],
        "evidence": "complete",
        "reason": "A distinct cluster.",
        "leaves": [],
        "partition": {"retrieval_question": "When has my tea tasted flat?"},
    }
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_PROPOSAL_INVALID"):
        episode_model._proposal(proposal, "c")  # noqa: SLF001


# --------------------------------------------------------------------------- #
# The vault side
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("decomposition") / "vault"
    return root, fx.build_pre_capture(root)


def _copy(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, world = built
    root = tmp_path / "vault"
    shutil.copytree(source, root)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    return root, world


def test_the_pre_capture_state_is_frozen(built) -> None:
    root, world = built
    assert world.logical_sha256 == PRE_CAPTURE_SHA256
    bodies = " ".join(page.body for page in read_state(root).pages.values()).casefold()
    for marker in ("flat", "2024", "filter jug", "limescale", "tin keeps", "nine times", "settled", "chose", "decided"):
        assert marker not in bodies


def test_current_runtime_an_untouched_world_fails_every_capture_positive(built) -> None:
    root, world = built
    state = read_state(root)

    failed = set(fx.check_capture(world, state, state).failed())

    positives = {item.key for item in fx.EXPECTATIONS if item.polarity == "positive"}
    assert failed == positives - {"decomposition/open-page-intact"}


TODAY = dt.date(2026, 9, 24)
FLAT_WEEK = (
    "## Observations\n\n"
    "- [finding] Tea tasted flat on Monday, Wednesday and Thursday of the week of 21 September "
    "2026, each the first cup after the kettle sat overnight.\n"
    "- [assumption] Fenn thinks it is limescale in the new kettle; he has not checked.\n"
)
SPRING = (
    "## Observations\n\n"
    "- [finding] In spring 2024, after moving flats, tea tasted flat for about a month; it came "
    "back once I used the filter jug.\n"
)
BASELINE = (
    "## Observations\n\n"
    "- [finding] Normally I tell a first steep from a second steep blind about nine times out of ten.\n"
)
PREFERENCE = (
    "- [preference] Settled on the Lowfield oolong over the Brightwater house blend.\n"
    "- [finding] The Lowfield tin keeps it fresher and the second steep holds up."
)


def _note(root: Path, title: str, slug: str, body: str) -> str:
    from exomem import note

    arguments = {
        "vault_root": root,
        "content": body,
        "note_type": "insight",
        "title": title,
        "slug": slug,
        "sources": [],
        "tags": [],
        "today": TODAY,
    }
    validation = note.note(validate_only=True, **arguments)
    reviewed = {}
    if getattr(validation.creation_validation, "reviewed_none_required", False):
        reviewed = {
            "relation_disposition": "reviewed_none",
            "relation_review_hash": validation.draft_hash,
            "relation_review_reason": "The scripted capture needs no relation.",
        }
    return note.note(
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        **reviewed,
        **arguments,
    ).path


def _append(root: Path, path: str, extra: str) -> None:
    from exomem import commands
    from exomem.vault import content_hash

    text = (root / path).read_text(encoding="utf-8")
    operation = {
        "kind": "replace_body",
        "new_body": text.split("\n---\n", 1)[1].rstrip() + f"\n{extra}\n",
        "expected_hash": content_hash(text),
    }
    preview = commands.op_edit_memory(
        root, path=path, why="scripted capture", operation={**operation, "validate_only": True}
    )["semantic"]
    review = {}
    if preview.get("relation_review_hash"):
        review = {
            "relation_disposition": "reviewed_none",
            "relation_review_hash": preview["relation_review_hash"],
            "relation_review_reason": "The scripted capture adds no relation.",
        }
    commands.op_edit_memory(
        root,
        path=path,
        why="scripted capture",
        operation={**operation, "transition_token": preview["transition_token"], **review},
    )


def _capture(root: Path, world, *, flat: str = FLAT_WEEK, spring: str | None = SPRING, baseline: str | None = BASELINE) -> None:
    _append(root, world.key_to_path["note_tea_prefs"], PREFERENCE)
    _note(root, "Flat-tasting mornings, September 2026", "flat-tasting-mornings-2026", flat)
    if spring is not None:
        _note(root, "Spring 2024 flat-taste episode", "spring-2024-flat-taste", spring)
    if baseline is not None:
        _note(root, "Tasting baseline", "tasting-baseline", baseline)


def _failed(root: Path, world, before) -> set[str]:
    return set(fx.check_capture(world, before, read_state(root)).failed())


def test_a_scripted_decomposed_capture_passes(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _capture(root, world)

    assert _failed(root, world, before) == set()


def test_the_old_episode_and_baseline_may_share_this_weeks_home(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    combined = (
        FLAT_WEEK
        + "\n## Spring 2024\n\n- [finding] Tea tasted flat for about a month after moving flats; it came back once I used the filter jug.\n"
        + "\n## Baseline\n\n- [finding] Normally I tell steeps apart blind about nine times out of ten.\n"
    )
    _capture(root, world, flat=combined, spring=None, baseline=None)

    assert _failed(root, world, before) == set()


def test_dumping_everything_onto_the_open_page_fails(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    everything = PREFERENCE + "\n" + "\n".join(
        line for body in (FLAT_WEEK, SPRING, BASELINE) for line in body.splitlines() if line.startswith("- ")
    )
    _append(root, world.key_to_path["note_tea_prefs"], everything)

    failed = _failed(root, world, before)
    assert "decomposition/open-page-holds-only-its-cluster" in failed
    assert {
        "decomposition/this-week-has-one-home",
        "decomposition/old-episode-has-a-home",
        "decomposition/baseline-has-a-home",
        "decomposition/no-detail-fragmentation",
    } <= failed


def test_the_interpretation_stated_as_fact_fails(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _capture(
        root,
        world,
        flat=FLAT_WEEK.replace(
            "- [assumption] Fenn thinks it is limescale in the new kettle; he has not checked.",
            "- [fact] Limescale in the new kettle is flattening the tea.",
        ),
    )

    assert _failed(root, world, before) == {"decomposition/interpretation-attributed"}


def test_an_old_episode_dated_only_by_its_title_keeps_its_time(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _capture(
        root,
        world,
        spring=(
            "## Observations\n\n- [finding] Tea tasted flat for about a month after moving flats; it "
            "came back once I used the filter jug.\n"
        ),
    )

    assert _failed(root, world, before) == set()


def test_an_old_episode_without_its_time_fails(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _append(root, world.key_to_path["note_tea_prefs"], PREFERENCE)
    _note(root, "Flat-tasting mornings, September 2026", "flat-tasting-mornings-2026", FLAT_WEEK)
    _note(root, "Tasting baseline", "tasting-baseline", BASELINE)
    _note(
        root,
        "Flat taste after moving flats",
        "flat-taste-after-moving",
        "## Observations\n\n- [finding] Tea tasted flat for about a month after moving flats; it "
        "came back once I used the filter jug.\n",
    )

    assert _failed(root, world, before) == {
        "decomposition/old-episode-keeps-its-time",
        "decomposition/old-episode-has-a-home",
    }


def test_a_page_per_morning_fragments(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _append(root, world.key_to_path["note_tea_prefs"], PREFERENCE)
    for day in ("Monday", "Wednesday", "Thursday"):
        _note(
            root,
            f"Flat tea on {day}",
            f"flat-tea-{day.casefold()}",
            f"## Observations\n\n- [finding] Tea tasted flat on {day}, the first cup after an overnight kettle.\n",
        )

    failed = _failed(root, world, before)
    # Three pages is within the page ceiling; the split is caught because this
    # week's observations no longer share one home.
    assert "decomposition/this-week-has-one-home" in failed
    assert "decomposition/no-detail-fragmentation" not in failed


def test_more_pages_than_clusters_fails_the_ceiling(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _capture(root, world)
    _note(root, "Lowfield tin", "lowfield-tin", "## Observations\n\n- [finding] The tin keeps the tea fresher.\n")

    assert _failed(root, world, before) == {"decomposition/no-detail-fragmentation"}
