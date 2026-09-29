"""Named-synthesis versus minor-refinement pair (close-memory-loop task 1.6).

Pinned here, and kept apart:

**Frozen before effects.** Each episode's actor input, the shared
pre-capture world and each episode's evaluator side are digested and pinned;
the actor view carries turns only, so destinations, links and scope rules
are withheld from the actor. An exact private replay is never scored against
this public pair.

**Expected red on the current runtime.** With no capture, the synthesis has
no home and the refinement is nowhere.

**Not vacuous.** Scripted writes through the product's writers (plumbing,
not agent evidence) satisfy each episode, and the matched wrong shapes (the
thesis appended to the nearest page, an untruthful or generic link, the
refinement's label promoted to a page) fail the guard that names them.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest
from epistemic.memory_loop import contract
from epistemic.memory_loop import observation as obs
from epistemic.memory_loop import synthesis as fx
from epistemic.memory_loop.contract import read_state

from exomem.public_artifact_privacy import assert_public_artifacts_clean

pytestmark = pytest.mark.timeout(600)



# --------------------------------------------------------------------------- #
# Frozen before effects; the evaluator withheld from the actor
# --------------------------------------------------------------------------- #


def test_the_pair_is_consistent_and_its_world_is_five_antecedents() -> None:
    fx.assert_manifest_consistent()
    assert len(fx.WORLD.notes) == 5 and not fx.WORLD.entities and not fx.WORLD.records


def test_the_module_pins_match_the_digests() -> None:
    assert fx.fixture_set_sha256() == fx.FIXTURE_SET_SHA256
    for episode_id in ("synthesis", "refinement"):
        assert fx.evaluator_sha256(episode_id) == fx.EVALUATOR_SHA256[episode_id]
        assert fx.actor_sha256(episode_id) == fx.ACTOR_SHA256[episode_id]


def test_the_actor_view_withholds_destinations_links_and_scope_rules() -> None:
    for item in fx.MANIFEST:
        view = fx.actor_view(item.episode_id)
        assert set(view) == {"episode_id", "turns", "later_turn"}
        text = repr(view)
        for candidate in item.candidates:
            assert candidate.statement not in text
            assert all(home not in text for home in candidate.homes)
            assert all(route not in text for route in candidate.routes)
        for expectation in item.expectations:
            assert expectation.key not in text and expectation.reason not in text
        assert item.later_use.useful not in text


def test_the_pair_is_matched_one_new_home_against_none() -> None:
    synthesis = fx.expectations_for("synthesis").candidates[0]
    refinement = fx.expectations_for("refinement").candidates[0]
    assert synthesis.homes == ("new:owner-custody",) and synthesis.routes == ("focused_note", "entity")
    assert refinement.homes == ("existing:ant_offline",)
    assert set(refinement.routes) == {"existing_page", "semantic_unit"}
    # The refinement also coins a label; the label is not a page.
    assert "thirty-day queue" in fx.episode("refinement").turns[0]


def test_the_actor_turns_carry_no_nudge_and_name_only_invented_things() -> None:
    allowed = (*fx.INVENTED_NAMES, *fx.ORDINARY_CAPITALIZED)
    texts = [turn for item in fx.EPISODES for turn in (*item.turns, item.later_turn)]
    texts += [text for seed in fx.WORLD.notes for text in (seed.title, seed.observation)]
    for item in fx.EPISODES:
        for turn in (*item.turns, item.later_turn):
            assert contract.find_nudges(turn) == (), turn
    for text in texts:
        assert contract.undeclared_capitalized_words(text, allowed) == (), text
        assert contract.find_medical_terms(text) == ()
    assert_public_artifacts_clean([Path(fx.__file__)])


def _record(origin: str, episode_id: str = "synthesis", **decisions) -> obs.NoNudgeObservation:
    return obs.load_observation(
        {
            "artifact_type": obs.ARTIFACT_TYPE,
            "schema_version": 1,
            "fixture_id": f"{fx.FIXTURE_SET_ID}/{episode_id}",
            "actor_sha256": fx.ACTOR_SHA256[episode_id],
            "pre_capture_sha256": fx.PRE_CAPTURE_SHA256,
            "evaluator_sha256": fx.EVALUATOR_SHA256[episode_id],
            "host_initiation": {
                "client": {"client": "generic-mcp", "adapter_version": "0", "lifecycle": "best_effort"},
                "input_origin": origin,
                "delivered_turns_sha256": obs.turns_sha256(fx.episode(episode_id).turns),
            },
            "agent_decisions": decisions.get("agent_decisions", []),
            "leaf_effects": decisions.get("leaf_effects", []),
            "publication": {"status": "not_observed"},
        }
    )


def test_a_synthetic_run_binds_to_the_pair() -> None:
    assert fx.void_reasons(_record("synthetic_fixture"), episode_id="synthesis") == ()


@pytest.mark.parametrize("origin", ["original_private", "reconstructed"])
def test_a_private_or_reconstructed_replay_is_never_scored_against_the_public_pair(origin: str) -> None:
    reasons = fx.void_reasons(_record(origin), episode_id="synthesis")

    assert len(reasons) == 1 and "exact private replay" in reasons[0]
    assert "local" in fx.EXACT_PRIVATE_REPLAY


# --------------------------------------------------------------------------- #
# First-pass focus, read from the observation record
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("synthesis-pair") / "vault"
    return root, fx.build_pre_capture(root)


def _decisions(world, *, route: str, inspected: int, target: str | None = None) -> list[dict]:
    alternatives = sorted(world.key_to_path[key] for key in fx.ANTECEDENT_KEYS[:inspected])
    destination = {
        "seq": 2,
        "phase": "destination",
        "initiator": "agent",
        "candidate_key": "thesis",
        "route": route,
        "text": "The owner-custody principle",
        "alternatives": alternatives,
    }
    if target is None:
        destination["title"] = "Owner-custody principle"
    else:
        destination["target"] = target
    return [{"seq": 1, "phase": "activation", "initiator": "agent"}, destination]


def test_first_pass_focus_passes_for_a_focused_home_chosen_after_inspection(built) -> None:
    _root, world = built
    record = _record("synthetic_fixture", agent_decisions=_decisions(world, route="focused_note", inspected=5))

    result = fx.first_pass_focus(record, world)

    assert result.outcome == "pass" and "5/5" in result.detail


def test_first_pass_focus_fails_when_the_thesis_first_goes_to_the_nearest_page(built) -> None:
    _root, world = built
    nearest = world.key_to_path["ant_offline"]
    record = _record(
        "synthetic_fixture",
        agent_decisions=_decisions(world, route="semantic_unit", inspected=5, target=nearest),
    )

    assert fx.first_pass_focus(record, world).outcome == "fail"


def test_first_pass_focus_fails_without_inspecting_any_antecedent(built) -> None:
    _root, world = built
    record = _record("synthetic_fixture", agent_decisions=_decisions(world, route="focused_note", inspected=0))

    assert fx.first_pass_focus(record, world).outcome == "fail"


# --------------------------------------------------------------------------- #
# The world, and expected red on the current runtime
# --------------------------------------------------------------------------- #


def test_the_pre_capture_state_is_frozen(built) -> None:
    root, world = built
    assert world.logical_sha256 == fx.PRE_CAPTURE_SHA256
    assert set(world.key_to_path) == set(fx.ANTECEDENT_KEYS)
    bodies = " ".join(page.body for page in read_state(root).pages.values()).casefold()
    for marker in ("custody", "guest", "30 days", "thirty"):
        assert marker not in bodies


def test_current_runtime_an_untouched_world_fails_both_positives(built) -> None:
    root, world = built
    state = read_state(root)

    synthesis = set(fx.check_capture("synthesis", world, state, state).failed())
    refinement = set(fx.check_capture("refinement", world, state, state).failed())

    assert synthesis == {
        "synthesis/only-the-thesis-is-new",
        "synthesis/focused-home",
        "synthesis/thesis-on-home",
        "synthesis/linked-to-the-antecedents",
    }
    assert refinement == {"refinement/on-its-existing-home"}


# --------------------------------------------------------------------------- #
# Not vacuous: scripted product writes (plumbing, not agent evidence)
# --------------------------------------------------------------------------- #

TODAY = dt.date(2026, 9, 24)
THESIS_BODY = (
    "## Observations\n\n"
    "- [insight] The owner keeps custody of their notes at every step; Tallyloom is only a guest.\n"
    "- [requirement] A feature that moves notes out of the owner's custody must justify that openly.\n"
)


def _copy(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, world = built
    root = tmp_path / "vault"
    shutil.copytree(source, root)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    return root, world


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
            "relation_review_reason": "The scripted capture adds its relations separately.",
        }
    return note.note(
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        **reviewed,
        **arguments,
    ).path


def _replace_body(root: Path, path: str, new_body: str) -> None:
    """A body replacement through the same handshake an agent uses: a
    reviewed-none disposition is sent only when the product asks for one."""

    from exomem import commands
    from exomem.vault import content_hash

    operation = {
        "kind": "replace_body",
        "new_body": new_body,
        "expected_hash": content_hash((root / path).read_text(encoding="utf-8")),
    }
    preview = commands.op_edit_memory(
        root, path=path, why="scripted capture", operation={**operation, "validate_only": True}
    )["semantic"]
    try:
        commands.op_edit_memory(
            root,
            path=path,
            why="scripted capture",
            operation={**operation, "transition_token": preview["transition_token"]},
        )
    except ValueError as error:
        if "RELATION_DISPOSITION" not in str(error):
            raise
        commands.op_edit_memory(
            root,
            path=path,
            why="scripted capture",
            operation={
                **operation,
                "transition_token": preview["transition_token"],
                "relation_disposition": "reviewed_none",
                "relation_review_hash": preview["relation_review_hash"],
                "relation_review_reason": "The scripted capture adds no relation here.",
            },
        )


def _append(root: Path, path: str, extra: str) -> None:
    body = (root / path).read_text(encoding="utf-8").split("\n---\n", 1)[1].rstrip()
    _replace_body(root, path, f"{body}\n\n{extra}\n")


def _thesis(root: Path, world, relation: str = "derived_from") -> str:
    path = _note(root, "Owner-custody principle", "owner-custody-principle", THESIS_BODY)
    bullets = "\n".join(
        f"- {relation} [[{Path(world.key_to_path[key]).stem}]]" for key in fx.ANTECEDENT_KEYS
    )
    _append(root, path, f"## Relations\n\n{bullets}")
    return path


def _check(episode_id: str, root: Path, world, before) -> contract.CaptureCheck:
    return fx.check_capture(episode_id, world, before, read_state(root))


def test_a_scripted_focused_thesis_with_truthful_links_passes(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _thesis(root, world)

    check = _check("synthesis", root, world, before)
    assert check.accepted, [result for result in check.results if result.outcome == "fail"]


def test_promoting_the_recurring_product_to_an_entity_is_not_fragmentation(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import link

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _thesis(root, world)
    link.link(root, entity_type="concept", name="Tallyloom", summary="A local-first notes app.", today=TODAY)

    assert _check("synthesis", root, world, before).accepted


def test_a_split_thesis_fails_the_single_home(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _thesis(root, world)
    _note(root, "Feature custody test", "feature-custody-test", "## Observations\n\n- [requirement] Features must justify leaving owner custody.\n")

    failed = set(_check("synthesis", root, world, before).failed())
    assert failed == {"synthesis/only-the-thesis-is-new", "synthesis/focused-home", "synthesis/thesis-on-home"}


def test_a_thesis_appended_to_the_nearest_page_fails_home_and_scope(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _append(root, world.key_to_path["ant_offline"], THESIS_BODY.replace("## Observations\n\n", ""))

    failed = set(_check("synthesis", root, world, before).failed())
    assert failed == {
        "synthesis/only-the-thesis-is-new",
        "synthesis/focused-home",
        "synthesis/thesis-on-home",
        "synthesis/linked-to-the-antecedents",
        "synthesis/ant_offline-keeps-scope",
    }


def test_an_untruthful_supersession_link_fails(built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _thesis(root, world, relation="supersedes")

    failed = set(_check("synthesis", root, world, before).failed())
    assert failed == {"synthesis/linked-to-the-antecedents", "synthesis/no-untruthful-link"}


def test_generic_links_alone_are_not_truthful_typed_links(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _thesis(root, world, relation="relates_to")

    assert _check("synthesis", root, world, before).failed() == ("synthesis/linked-to-the-antecedents",)


def test_a_backlink_on_an_antecedent_keeps_its_scope(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _thesis(root, world)
    _append(root, world.key_to_path["ant_export"], "See [[Owner-custody principle]] for why.")

    check = _check("synthesis", root, world, before)
    assert check.outcome("synthesis/ant_export-keeps-scope") == "pass"


def test_a_scripted_refinement_on_its_home_passes_and_a_label_page_fails(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _append(
        root,
        world.key_to_path["ant_offline"],
        "- [design] Queued changes wait up to 30 days before the sync-overdue warning (the thirty-day queue).",
    )
    check = _check("refinement", root, world, before)
    assert check.accepted, [result for result in check.results if result.outcome == "fail"]

    _note(
        root,
        "Thirty-day queue",
        "thirty-day-queue",
        "## Observations\n\n- [design] Offline changes queue for up to 30 days.\n",
    )
    assert _check("refinement", root, world, before).failed() == ("refinement/no-new-page",)


# --------------------------------------------------------------------------- #
# Critic probes (round 2)
# --------------------------------------------------------------------------- #


def _thesis_linked(root: Path, world, keys, relation: str = "derived_from") -> str:
    path = _note(root, "Owner-custody principle", "owner-custody-principle", THESIS_BODY)
    bullets = "\n".join(f"- {relation} [[{Path(world.key_to_path[key]).stem}]]" for key in keys)
    _append(root, path, f"## Relations\n\n{bullets}")
    return path


def test_probe_p3_a_thesis_with_one_untruthful_edge_fails(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _thesis_linked(root, world, ("ant_offline",), relation="blocks")

    assert "synthesis/linked-to-the-antecedents" in _check("synthesis", root, world, before).failed()


def test_probe_p3b_a_frontmatter_supersession_of_the_antecedents_fails(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    path = _thesis_linked(root, world, fx.ANTECEDENT_KEYS)
    contract._edit_frontmatter(
        root,
        path,
        "supersedes",
        [f"[[{Path(world.key_to_path[key]).stem}]]" for key in fx.ANTECEDENT_KEYS],
        "scripted wrong capture",
    )

    assert "synthesis/no-untruthful-link" in _check("synthesis", root, world, before).failed()


def test_three_of_five_antecedent_links_fail_and_four_pass(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _thesis_linked(root, world, fx.ANTECEDENT_KEYS[:3])
    assert _check("synthesis", root, world, before).failed() == ("synthesis/linked-to-the-antecedents",)

    root, world = _copy(built, tmp_path / "four", monkeypatch)
    before = read_state(root)
    _thesis_linked(root, world, fx.ANTECEDENT_KEYS[:4])
    assert _check("synthesis", root, world, before).accepted


def test_a_link_in_the_wrong_direction_is_not_truthful(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``derived_from`` runs from the thesis to its antecedents, not back."""

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _note(root, "Owner-custody principle", "owner-custody-principle", THESIS_BODY)
    for key in fx.ANTECEDENT_KEYS:
        _append(root, world.key_to_path[key], "## Relations\n\n- derived_from [[owner-custody-principle]]")

    assert "synthesis/linked-to-the-antecedents" in _check("synthesis", root, world, before).failed()


def test_antecedents_that_implement_the_thesis_link_it_truthfully(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _note(root, "Owner-custody principle", "owner-custody-principle", THESIS_BODY)
    for key in fx.ANTECEDENT_KEYS:
        _append(root, world.key_to_path[key], "## Relations\n\n- implements [[owner-custody-principle]]")

    check = _check("synthesis", root, world, before)
    assert check.accepted, [result for result in check.results if result.outcome == "fail"]


def test_probe_p9_a_refinement_beside_the_recurring_product_entity_passes(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import link

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _append(
        root,
        world.key_to_path["ant_offline"],
        "- [design] Queued changes wait up to 30 days before the sync-overdue warning (the thirty-day queue).",
    )
    link.link(root, entity_type="concept", name="Tallyloom", summary="A local-first notes app.", today=TODAY)

    assert _check("refinement", root, world, before).accepted


def test_a_label_promoted_to_a_concept_entity_still_fragments(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import link

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    _append(
        root,
        world.key_to_path["ant_offline"],
        "- [design] Queued changes wait up to 30 days before the sync-overdue warning (the thirty-day queue).",
    )
    link.link(root, entity_type="concept", name="Thirty-day queue", summary="Offline changes queue for 30 days.", today=TODAY)

    assert _check("refinement", root, world, before).failed() == ("refinement/no-new-page",)


def test_probe_p11_a_concept_entity_thesis_is_a_focused_home(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import link

    root, world = _copy(built, tmp_path, monkeypatch)
    before = read_state(root)
    path = link.link(
        root,
        entity_type="concept",
        name="Owner-custody principle",
        summary=(
            "The owner keeps custody of their notes at every step; the app is only a guest. "
            "A feature that moves notes out of custody must justify it openly."
        ),
        today=TODAY,
    ).path
    bullets = "\n".join(f"- derived_from [[{Path(world.key_to_path[key]).stem}]]" for key in fx.ANTECEDENT_KEYS)
    _append(root, path, f"## Relations\n\n{bullets}")

    check = _check("synthesis", root, world, before)
    assert check.accepted, [result for result in check.results if result.outcome == "fail"]
