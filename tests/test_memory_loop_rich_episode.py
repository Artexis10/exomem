"""The synthetic rich-episode fixture (close-memory-loop task 1.3).

The verify clause is two things: expected destinations and provenance are
specified before any write, and nothing in the fixture needs a private name.
Both are pinned here, along with the fixture's two other obligations:

**Expected red on the current runtime.** An untouched world fails every
capture positive. The trial bake itself runs through the public episode
surface as a ``records`` route ``append-record`` leaf.

**Not vacuous.** A scripted capture through the product's own writers
(plumbing, not ordinary-agent evidence) satisfies every expectation, and each
matched wrong capture fails exactly the expectation that guards it.
"""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from epistemic.memory_loop import contract
from epistemic.memory_loop import observation as obs
from epistemic.memory_loop import rich_episode as fx
from epistemic.memory_loop.contract import read_state

from exomem.public_artifact_privacy import assert_public_artifacts_clean

pytestmark = pytest.mark.timeout(600)



# --------------------------------------------------------------------------- #
# Destinations and provenance are specified before any write
# --------------------------------------------------------------------------- #


def test_the_destination_and_provenance_spec_is_consistent() -> None:
    fx.assert_manifest_consistent()


def test_the_module_pins_match_the_digests() -> None:
    assert fx.fixture_sha256() == fx.FIXTURE_SHA256
    assert fx.evaluator_sha256() == fx.EVALUATOR_SHA256
    assert fx.actor_sha256() == fx.ACTOR_SHA256


def test_every_candidate_names_its_home_route_disposition_and_provenance() -> None:
    from exomem import episode_model

    for candidate in fx.CANDIDATES:
        assert candidate.homes and candidate.routes and candidate.dispositions, candidate.key
        assert set(candidate.routes) <= set(episode_model._ROUTES)  # noqa: SLF001
        assert set(candidate.dispositions) <= set(episode_model._DISPOSITIONS)  # noqa: SLF001
        assert candidate.provenance in contract.PROVENANCE
        assert candidate.checked_by


def test_the_episode_covers_every_shape_the_task_names() -> None:
    by_key = {candidate.key: candidate for candidate in fx.CANDIDATES}
    # An existing supplier, hydrated rather than duplicated.
    assert by_key["delivery-day"].homes == ("existing:org_wrenfold",)
    # A dynamic equipment type: registered by the world, not a core type.
    from exomem import entity_types

    assert "equipment" not in entity_types.ENTITY_TYPE_IDS
    assert [seed.type_id for seed in fx.WORLD.types] == ["equipment"]
    assert by_key["oven"].routes == ("entity",)
    # Direct and reported product facts, sharing a home, with distinct provenance.
    assert (by_key["rye-label"].provenance, by_key["rye-blend"].provenance) == ("direct", "reported")
    assert by_key["rye-blend"].attributed_to and by_key["rye-blend"].uncertain
    assert by_key["rye-label"].same_home_as == ("rye-blend",)
    # Prior related sourcing and comparison material.
    assert {seed.key for seed in fx.WORLD.notes} == {"note_comparison", "note_sourcing"}
    # An experiment event, never a causal conclusion.
    assert by_key["trial-bake"].provenance == "event" and by_key["trial-bake"].uncertain
    assert set(by_key["trial-bake"].routes) <= {"records", "experiment"}
    # An ambiguous-owner negative and a possibility that plans nothing.
    assert by_key["ambiguous-owner"].homes == ("none",)
    assert "routed" not in by_key["ambiguous-owner"].dispositions
    assert by_key["possibility"].dispositions == ("no_capture",)


def test_a_reported_claim_without_a_source_is_refused() -> None:
    blend = next(candidate for candidate in fx.CANDIDATES if candidate.key == "rye-blend")
    broken = replace(blend, attributed_to=None)

    with pytest.raises(contract.FixtureError, match="names its source"):
        contract.validate_candidates(
            (broken,), world_keys=fx.WORLD.keys(), expectation_keys=[item.key for item in fx.EXPECTATIONS]
        )


def test_a_route_the_product_does_not_define_is_refused() -> None:
    oven = next(candidate for candidate in fx.CANDIDATES if candidate.key == "oven")

    with pytest.raises(contract.FixtureError, match="unknown route"):
        contract.validate_candidates(
            (replace(oven, routes=("equipment_page",)),),
            world_keys=fx.WORLD.keys(),
            expectation_keys=[item.key for item in fx.EXPECTATIONS],
        )


def test_the_actor_view_withholds_the_destination_spec() -> None:
    view = fx.actor_view()
    assert set(view) == {"fixture_id", "turns", "later_turn"}
    text = repr(view)
    for candidate in fx.CANDIDATES:
        assert candidate.statement not in text
        for home in candidate.homes:
            assert home not in text
        for route in candidate.routes:
            assert route not in text
    for expectation in fx.EXPECTATIONS:
        assert expectation.key not in text and expectation.reason not in text


def _run_record(**overrides) -> obs.NoNudgeObservation:
    record = {
        "artifact_type": obs.ARTIFACT_TYPE,
        "schema_version": 1,
        "fixture_id": fx.FIXTURE_ID,
        "actor_sha256": fx.ACTOR_SHA256,
        "pre_capture_sha256": fx.PRE_CAPTURE_SHA256,
        "evaluator_sha256": fx.EVALUATOR_SHA256,
        "host_initiation": {
            "client": {"client": "generic-mcp", "adapter_version": "0", "lifecycle": "best_effort"},
            "input_origin": "synthetic_fixture",
            "delivered_turns_sha256": obs.turns_sha256(fx.TURNS),
        },
        "agent_decisions": [],
        "leaf_effects": [],
        "publication": {"status": "not_observed"},
    }
    host = overrides.pop("host", {})
    record["host_initiation"].update(host)
    record.update(overrides)
    return obs.load_observation(record)


def test_a_run_bound_to_the_module_pins_is_not_void() -> None:
    assert fx.void_reasons(_run_record()) == ()


def test_a_run_bound_to_an_edited_evaluator_is_void() -> None:
    assert fx.void_reasons(_run_record(evaluator_sha256="e" * 64)) == (
        "evaluator_sha256 differs from the fixture's frozen digest",
    )


@pytest.mark.parametrize("origin", ["original_private", "reconstructed"])
def test_a_private_or_reconstructed_run_is_refused_by_the_public_fixture(origin: str) -> None:
    reasons = fx.void_reasons(_run_record(host={"input_origin": origin}))

    assert len(reasons) == 1 and "public synthetic fixture" in reasons[0]


# --------------------------------------------------------------------------- #
# No private names, no nudges, nothing clinical
# --------------------------------------------------------------------------- #


def _fixture_texts() -> list[str]:
    texts = [*fx.TURNS, fx.LATER_TURN]
    for seed in fx.WORLD.entities:
        texts.extend((seed.name, seed.summary, *seed.aliases))
    for seed in fx.WORLD.notes:
        texts.extend((seed.title, seed.observation))
    for seed in fx.WORLD.records:
        texts.append(seed.title)
        texts.extend(value for _key, row in seed.items for _field, value in row)
    for seed in fx.WORLD.types:
        texts.extend((seed.label, seed.guidance))
    return texts


def test_every_proper_name_is_declared_as_invented() -> None:
    allowed = (*fx.INVENTED_NAMES, *fx.ORDINARY_CAPITALIZED)
    for text in _fixture_texts():
        assert contract.undeclared_capitalized_words(text, allowed) == (), text


def test_the_actor_turns_carry_no_nudge() -> None:
    for turn in (*fx.TURNS, fx.LATER_TURN):
        assert contract.find_nudges(turn) == ()


def test_the_fixture_passes_the_privacy_gate_and_names_nothing_clinical() -> None:
    assert_public_artifacts_clean([Path(fx.__file__)])
    for text in _fixture_texts():
        assert contract.find_medical_terms(text) == ()


# --------------------------------------------------------------------------- #
# The world, through the product's writers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("rich-episode") / "vault"
    return root, fx.build_pre_capture(root)


def _copy(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, world = built
    root = tmp_path / "vault"
    shutil.copytree(source, root)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    return root, world


def test_the_pre_capture_state_is_frozen(built) -> None:
    _root, world = built
    assert world.logical_sha256 == fx.PRE_CAPTURE_SHA256
    assert world.spec_sha256 == fx.pre_capture_spec_sha256()


def test_the_world_registers_equipment_and_shares_the_owner_name(built) -> None:
    from exomem import entity_types

    root, world = built
    assert entity_types.load_entity_types(root).resolve("equipment").id == "equipment"
    state = read_state(root)
    carriers = sorted(page.path for page in state.entities() if "Corran" in page.aliases)
    assert carriers == sorted([world.key_to_path["person_corran"], world.key_to_path["org_corran"]])


def test_the_pre_capture_state_does_not_already_carry_a_positive(built) -> None:
    root, _world = built
    bodies = " ".join(page.body for page in read_state(root).pages.values()).casefold()
    for marker in ("thursday", "12.5", "two harvests", "tessel", "steam", "78%"):
        assert marker not in bodies


# --------------------------------------------------------------------------- #
# Expected red on the current runtime
# --------------------------------------------------------------------------- #


def test_current_runtime_an_untouched_world_fails_every_capture_positive(built) -> None:
    root, world = built
    state = read_state(root)

    check = fx.check_capture(world, state, state)

    failed = set(check.failed())
    positives = {item.key for item in fx.EXPECTATIONS if item.polarity == "positive"}
    negatives = {item.key for item in fx.EXPECTATIONS if item.polarity == "negative"}
    assert failed == positives - {
        "rich-episode/one-supplier",
        "rich-episode/supplier-intact",
        "rich-episode/bake-history-kept",
        "rich-episode/person-intact",
        "rich-episode/kitchen-intact",
        "rich-episode/one-lowmere",
    }
    assert not failed & negatives


# --------------------------------------------------------------------------- #
# The trial bake as an episode Records leaf
# --------------------------------------------------------------------------- #


def _trial_bake_leaf(root: Path) -> dict:
    """The trial bake as an ``append-record`` episode leaf, in the shape the
    episode Records leaf takes."""

    from exomem import commands

    collection = fx.BAKE_LOG.manifest_path
    snapshot = commands.op_record_memory(root, action="inspect", collection=collection)["snapshot"]
    return {
        "leaf_key": "trial-bake",
        "effect_revision": 1,
        "kind": "append-record",
        "args": {
            "collection": collection,
            "item": {
                "baked_on": "2026-09-23",
                "loaf": "rye test loaf",
                "flour": "Wrenfold stoneground rye",
                "oven": "Tessel deck oven",
                "hydration": "78%",
                "outcome": "denser crumb, cause unclear",
            },
            "why": "The episode logged the trial bake.",
            "expected_container_hash": snapshot,
        },
    }


def test_the_episode_surface_runs_the_trial_bake_as_a_records_leaf(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trial bake runs through ``episode_memory`` as a ``records`` route
    ``append-record`` leaf: preparation accepts it, resume appends it through
    the Records writer, and the fixture's own Records expectations hold. This
    was the fixture's expected red until the episode Records leaf landed
    (close-memory-loop 3.5)."""

    from exomem import commands, episode_workflow
    from exomem import schema as schema_module
    from exomem.governance.principal import owner_principal, request_scope

    root, world = _copy(built, tmp_path, monkeypatch)
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    before = read_state(root)
    _recap(
        root,
        subject="Bread week",
        summary="Logged the 78% rye test loaf in the new oven.",
        worked_on=["Baked the 78% rye test loaf"],
    )
    leaf = _trial_bake_leaf(root)
    episode = "ep-" + "b2" * 16

    def _episode(**kwargs: object) -> dict:
        with request_scope(owner_principal(surface="mcp")):
            return commands.op_episode_memory(
                root, schema_module.load_source_schema(root), episode=episode, **kwargs
            )

    prepared = _episode(
        action="prepare",
        candidate="trial-bake",
        proposal={
            "route": "records",
            "title": "Rye test loaf",
            "alternatives": [],
            "evidence": "complete",
            "reason": "A bake the episode observed.",
            "leaves": [leaf],
        },
    )
    (candidate,) = prepared["candidates"]
    assert [item["kind"] for item in candidate["leaves"]] == ["append-record"]
    reviewed = _episode(action="disposition", candidate="trial-bake", disposition="routed", reason="Observed.")
    resumed = _episode(
        action="resume",
        input_revision=reviewed["input_revision"],
        journal_digest=reviewed["journal_digest"],
    )

    assert resumed["status"] == "ok", (resumed.get("blocked"), resumed.get("stale"))
    check = fx.check_capture(world, before, read_state(root))
    assert "rich-episode/trial-recorded" not in check.failed()
    assert "rich-episode/bake-history-kept" not in check.failed()
    assert "rich-episode/no-future-records" not in check.failed()


# --------------------------------------------------------------------------- #
# Not vacuous: scripted product writes (plumbing, not agent evidence)
# --------------------------------------------------------------------------- #

TODAY = dt.date(2026, 9, 24)


def _command(name: str):
    from exomem import commands

    return next(item for item in commands.PRODUCT_COMMANDS if item.name == name)


def _edit(root: Path, path: str, old: str, new: str) -> None:
    from exomem import writer_lease
    from exomem.vault import content_hash

    text = (root / path).read_text(encoding="utf-8")
    writer_lease.invoke_command(
        _command("edit_memory"),
        root,
        path=path,
        why="scripted fixture capture",
        operation={
            "kind": "replace_string",
            "old_string": old,
            "new_string": new,
            "expected_hash": content_hash(text),
        },
    )


def _edit_reviewed(root: Path, path: str, old: str, new: str) -> None:
    """An edit to a page whose saved relation review must be renewed first."""

    from exomem import commands
    from exomem.vault import content_hash

    operation = {
        "kind": "replace_string",
        "old_string": old,
        "new_string": new,
        "expected_hash": content_hash((root / path).read_text(encoding="utf-8")),
    }
    preview = commands.op_edit_memory(
        root, path=path, why="scripted wrong capture", operation={**operation, "validate_only": True}
    )["semantic"]
    commands.op_edit_memory(
        root,
        path=path,
        why="scripted wrong capture",
        operation={
            **operation,
            "transition_token": preview["transition_token"],
            "relation_disposition": "reviewed_none",
            "relation_review_hash": preview["relation_review_hash"],
            "relation_review_reason": "The scripted wrong capture adds no relation.",
        },
    )


def _append_body(root: Path, path: str, extra: str) -> None:
    from exomem import writer_lease
    from exomem.vault import content_hash

    text = (root / path).read_text(encoding="utf-8")
    writer_lease.invoke_command(
        _command("edit_memory"),
        root,
        path=path,
        why="scripted fixture capture",
        operation={
            "kind": "replace_body",
            "new_body": text.split("\n---\n", 1)[1].rstrip() + f"\n\n{extra}\n",
            "expected_hash": content_hash(text),
        },
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
            "relation_review_reason": "The scripted capture adds its relation separately.",
        }
    return note.note(
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        **reviewed,
        **arguments,
    ).path


RYE_BODY = (
    "## Observations\n\n"
    "- [finding] The sack label says milled July 2026, 12.5% protein.\n"
    "- [assumption] The miller at Wrenfold said it is a blend of two harvests; not seen in writing.\n"
)


def _scripted_capture(
    root: Path,
    world,
    *,
    rye_body: str = RYE_BODY,
    outcome: str = "denser crumb, cause unclear",
    rye_title: str = "Wrenfold stoneground rye",
) -> None:
    from exomem import commands, link

    link.link(
        root,
        entity_type="equipment",
        name="Tessel deck oven",
        summary="Deck oven with two stone decks and steam injection, up to 300 °C; arrived 22 September 2026.",
        today=TODAY,
    )
    _edit(root, world.key_to_path["org_wrenfold"], "Stone mill in the next county.", "Stone mill in the next county. Delivers rye on Thursdays.")
    rye = _note(root, rye_title, rye_title.casefold().replace(" ", "-"), rye_body)
    _append_body(root, rye, "## Relations\n\n- about_entity [[Wrenfold Mill]]")
    collection = fx.BAKE_LOG.manifest_path
    snapshot = commands.op_record_memory(root, action="inspect", collection=collection)["snapshot"]
    commands.op_record_memory(
        root,
        action="append",
        collection=collection,
        item={
            "baked_on": "2026-09-23",
            "loaf": "rye test loaf",
            "flour": "Wrenfold stoneground rye",
            "oven": "Tessel deck oven",
            "hydration": "78%",
            "outcome": outcome,
        },
        item_key="30000000-0000-4000-8000-000000000012",
        expected_container_hash=snapshot,
        why="scripted fixture capture",
    )


def test_a_scripted_correct_capture_satisfies_every_expectation(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world)

    check = fx.check_capture(world, before, read_state(root))

    assert check.accepted, [result for result in check.results if result.outcome == "fail"]


def test_a_reported_blend_stated_as_fact_fails_attribution(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(
        root,
        world,
        rye_body=(
            "## Observations\n\n"
            "- [finding] The sack label says milled July 2026, 12.5% protein.\n"
            "- [fact] The rye is a blend of two harvests.\n"
        ),
    )

    assert fx.check_capture(world, before, read_state(root)).failed() == ("rich-episode/blend-attributed",)


def test_a_causal_outcome_fails_the_experiment_guard(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world, outcome="denser crumb because of the new flour")

    assert fx.check_capture(world, before, read_state(root)).failed() == ("rich-episode/no-causal-claim",)


def test_product_facts_copied_onto_the_comparison_fail_scope_and_home(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world)
    comparison = world.key_to_path["note_comparison"]
    _edit_reviewed(
        root,
        comparison,
        "delivery reliability for rye flour.",
        "delivery reliability for rye flour.\n- [finding] The July 2026 milling tests at 12.5% protein.",
    )

    failed = set(fx.check_capture(world, before, read_state(root)).failed())
    assert failed == {"rich-episode/comparison-untouched", "rich-episode/rye-facts-one-home"}


def test_resolving_the_ambiguous_owner_fails(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world)
    _edit(
        root,
        world.key_to_path["person_corran"],
        "Baker who shares the community kitchen.",
        "Baker who shares the community kitchen. Has lent out a proofing cabinet.",
    )

    assert set(fx.check_capture(world, before, read_state(root)).failed()) == {
        "rich-episode/owner-not-on-person",
        "rich-episode/owner-unresolved-everywhere",
    }


def test_a_duplicate_supplier_fails_hydration(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import link

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world)
    link.link(root, entity_type="organization", name="Wrenfold Mill Ltd", summary="Rye supplier.", today=TODAY)

    assert fx.check_capture(world, before, read_state(root)).failed() == ("rich-episode/one-supplier",)


def test_a_blend_attributed_by_its_section_heading_keeps_its_source(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source carried by a heading qualifies the bullets beneath it."""

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(
        root,
        world,
        rye_body=(
            "## Observations\n\n"
            "- [finding] The sack label says milled July 2026, 12.5% protein.\n\n"
            "## What the miller said (unconfirmed)\n\n"
            "- [assumption] A blend of two harvests.\n"
        ),
    )

    assert fx.check_capture(world, before, read_state(root)).accepted


def test_a_product_home_titled_for_the_flour_is_still_the_product_home(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world, rye_title="Wrenfold stoneground flour")

    assert fx.check_capture(world, before, read_state(root)).accepted


# --------------------------------------------------------------------------- #
# Critic probes (round 2)
# --------------------------------------------------------------------------- #


def _recap(root: Path, **fields) -> dict:
    """An ``episode_memory`` recap: retained input, never a destination."""

    from exomem import commands
    from exomem import schema as schema_module
    from exomem.governance.principal import owner_principal, request_scope

    with request_scope(owner_principal(surface="mcp")):
        return commands.op_episode_memory(
            root, schema_module.load_source_schema(root), action="record", episode="ep-" + "b2" * 16, **fields
        )


def test_probe_p5_a_correct_capture_with_an_episode_recap_passes(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world)
    _recap(
        root,
        subject="Bread week",
        summary="New Tessel oven; the Wrenfold rye sack says 12.5% protein, milled July 2026.",
        worked_on=["Logged the 78% test loaf"],
        said=["their miller told me it's a blend from two harvests"],
    )

    check = fx.check_capture(world, before, read_state(root))
    assert check.accepted, [result for result in check.results if result.outcome == "fail"]


def test_probe_p6_an_owner_resolved_on_a_new_page_fails(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import link

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world)
    link.link(root, entity_type="equipment", name="Proofing cabinet", summary="On loan from Corran Hale.", today=TODAY)

    assert fx.check_capture(world, before, read_state(root)).failed() == ("rich-episode/owner-unresolved-everywhere",)


def test_an_unresolved_owner_name_on_the_cabinet_is_not_a_resolution(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import link

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world)
    link.link(root, entity_type="equipment", name="Proofing cabinet", summary="On loan from 'Corran'; which Corran is unclear.", today=TODAY)

    assert fx.check_capture(world, before, read_state(root)).accepted


@pytest.mark.parametrize(
    "line",
    [
        "- [assumption] Per the miller, the rye is a blend of two harvests.",
        "- [assumption] Likely a blend of two harvests, according to Wrenfold.",
    ],
)
def test_per_likely_and_the_supplier_name_attribute_the_blend(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(
        root,
        world,
        rye_body=f"## Observations\n\n- [finding] The sack label says milled July 2026, 12.5% protein.\n{line}\n",
    )

    assert fx.check_capture(world, before, read_state(root)).accepted


def test_probe_r3e_a_bake_log_line_noting_the_possibility_is_not_planning(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tentative intent stays quiet: the trial bake's own line may note the
    possibility without it counting as a commitment."""

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world, outcome="denser crumb, cause unclear; might try Lowmere's rye next month")

    check = fx.check_capture(world, before, read_state(root))
    assert check.outcome("rich-episode/no-planning") == "pass"
    assert check.accepted, [result for result in check.results if result.outcome == "fail"]


def test_a_possibility_recorded_as_a_future_record_is_misrouted(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records hold observed outcomes: a structured row dated after the turn is
    intent misrouted into Records, while a line noting it stays quiet."""

    from exomem import commands

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _scripted_capture(root, world)
    collection = fx.BAKE_LOG.manifest_path
    snapshot = commands.op_record_memory(root, action="inspect", collection=collection)["snapshot"]
    commands.op_record_memory(
        root,
        action="append",
        collection=collection,
        item={"baked_on": "2026-10-15", "loaf": "rye trial", "flour": "Lowmere rye", "note": "try next month"},
        item_key="30000000-0000-4000-8000-000000000013",
        expected_container_hash=snapshot,
        why="scripted wrong capture",
    )

    assert fx.check_capture(world, before, read_state(root)).failed() == ("rich-episode/no-future-records",)


def test_the_trial_bake_has_only_the_route_its_check_can_see() -> None:
    trial = next(candidate for candidate in fx.CANDIDATES if candidate.key == "trial-bake")
    assert trial.routes == ("records",)
