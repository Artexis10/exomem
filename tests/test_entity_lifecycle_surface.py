"""Agent-facing recurring-identity lifecycle and governed-curation contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import attention, commands, curation, find
from exomem._hooks import exomem_capture_nudge
from exomem.capabilities import ActiveSurfaceDescriptor, active_surface
from exomem.vault import content_hash


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    find.clear_cache()
    return path


def _note(root: Path, index: int, body: str) -> Path:
    return _write(
        root,
        f"Knowledge Base/Notes/context-{index:02d}.md",
        "---\n"
        "type: insight\n"
        f"title: Context {index}\n"
        "status: active\n"
        "---\n"
        f"# Context {index}\n\n{body}\n",
    )


def _promotion(root: Path, identity: str = "amber guild") -> None:
    for index, body in enumerate(
        (
            f"{identity} is an organization.",
            f"organization: {identity}.",
            f"Membership: {identity}.",
        )
    ):
        _note(root, index, body)


def _entity(root: Path, *, title: str, slug: str) -> str:
    path = f"Knowledge Base/Entities/Organizations/{slug}.md"
    _write(
        root,
        path,
        "---\n"
        "type: entity\n"
        f"title: {title}\n"
        "entity_type: organization\n"
        "status: active\n"
        "---\n"
        f"# {title}\n",
    )
    return path


def _hydration(root: Path, identity: str = "cobalt workshop") -> str:
    target = _entity(root, title=identity, slug=identity.replace(" ", "-"))
    for index, body in enumerate(
        (
            f"I work with {identity}.",
            f"I use {identity}.",
            f"I attend {identity}.",
        )
    ):
        _note(root, index, body)
    return target


def _candidate(root: Path):  # noqa: ANN202
    report = attention.attention(
        root,
        categories=["entity_recurrence"],
        limit=3,
        record_surfacing=False,
    )
    assert len(report.items) == 1
    return report.items[0]


def _hydration_corpus(root: Path, *, identity: str, contexts: int) -> str:
    target = _entity(root, title=identity, slug=identity.replace(" ", "-"))
    predicates = ("work with", "use", "attend")
    for index in range(contexts):
        _note(root, index, f"I {predicates[index % len(predicates)]} {identity}.")
    return target


def _apply_hydration_batch(
    root: Path,
    *,
    target: str,
    item: dict[str, object],
    ordinal: int,
) -> tuple[list[str], dict[str, object]]:
    binding = item["entity_candidate"]
    assert isinstance(binding, dict)
    identity = binding["identity"]
    assert isinstance(identity, str)
    contexts = binding["first_disconnected_context_batch"]
    assert isinstance(contexts, list)
    steps: list[dict[str, object]] = []
    for index, context in enumerate(contexts):
        assert isinstance(context, dict)
        path = context["path"]
        assert isinstance(path, str)
        source = (root / path).read_text(encoding="utf-8")
        assert identity in source
        steps.append(
            {
                "step_id": f"batch-{ordinal}-context-{index}",
                "kind": "edit",
                "args": {
                    "path": path,
                    "why": "Connect one exact reviewed hydration context.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": identity,
                        "new_string": f"[[{target.removesuffix('.md')}|{identity}]]",
                        "expected_hash": content_hash(source),
                    },
                },
            }
        )
    proposal = curation.propose(
        root,
        {
            "version": 1,
            "title": f"Connect reviewed hydration batch {ordinal}",
            "entity_candidate": binding,
            "steps": steps,
        },
    )
    result = curation.apply(
        root,
        run_id=proposal["run_id"],
        plan_id=proposal["plan_id"],
        expected_plan_fingerprint=proposal["plan_fingerprint"],
        why=f"Confirm exact hydration batch {ordinal}.",
    )
    while result["phase"] != "completed":
        result = curation.resume(
            root,
            run_id=proposal["run_id"],
            plan_id=proposal["plan_id"],
        )
    return [str(context["path"]) for context in contexts], result


def test_entity_candidate_review_ref_round_trips_through_item_lookup(tmp_path: Path) -> None:
    _promotion(tmp_path)
    candidate = _candidate(tmp_path)

    resolved = attention.item_by_ref(
        tmp_path,
        candidate.ref or "",
        expected_fingerprint=candidate.fingerprint,
    )

    assert resolved.ref == candidate.ref
    assert resolved.fingerprint == candidate.fingerprint
    assert resolved.categories == ["entity_recurrence"]
    assert resolved.reasons[0]["meta"]["candidate_state"] == "promotion"


def test_curation_work_item_binds_the_exact_entity_candidate(tmp_path: Path) -> None:
    _promotion(tmp_path)
    candidate = _candidate(tmp_path)

    item = curation.work_item(tmp_path, review_ref=candidate.ref)

    binding = item["entity_candidate"]
    assert binding == {
        "review_ref": candidate.ref,
        "review_fingerprint": candidate.fingerprint,
        "candidate_state": "promotion",
        "identity": "amber guild",
        "signal_version": candidate.reasons[0]["meta"]["signal_version"],
        "first_disconnected_context_batch": [
            {
                "path": context["path"],
                "context_hash": context["context_hash"],
            }
            for context in candidate.reasons[0]["meta"]["disconnected_contexts"]
        ],
        "remaining_disconnected_count": 0,
        "batch_fingerprint": candidate.reasons[0]["meta"]["batch_fingerprint"],
        "target_refs": [],
        "grammar_identity": {
            "version": "identity-frames-v1",
            "predicate_table_digest": candidate.reasons[0]["meta"][
                "predicate_table_digest"
            ],
        },
        "registry_identity": candidate.reasons[0]["meta"]["registry_fingerprint"],
    }
    assert [page["path"] for page in item["pages"]] == [
        row["path"] for row in binding["first_disconnected_context_batch"]
    ]
    assert item["allowed_candidate_step_kinds"] == ["create-entity"]
    assert item["mutation_authority"] == "restructure_execution"
    assert item["unknown_kind_route"] == {
        "tool": "schema_memory",
        "operation": "save-entity-types",
        "authority": "restructure_execution",
        "refresh_candidate_after_save": True,
    }


def test_candidate_plan_refuses_stale_binding_before_publication(tmp_path: Path) -> None:
    _promotion(tmp_path)
    candidate = _candidate(tmp_path)
    item = curation.work_item(tmp_path, review_ref=candidate.ref)
    binding = item["entity_candidate"]
    plan = {
        "version": 1,
        "title": "Promote a stable recurring identity",
        "entity_candidate": binding,
        "steps": [
            {
                "step_id": "promote",
                "kind": "create-entity",
                "args": {
                    "entity_type": "organization",
                    "name": "amber guild",
                    "summary": "A stable group represented by recurring durable contexts.",
                },
            }
        ],
    }
    stale = json.loads(json.dumps(plan))
    stale["entity_candidate"]["signal_version"] = "0" * 64

    with pytest.raises(curation.CurationError, match="CURATION_ENTITY_CANDIDATE_STALE"):
        curation.propose(tmp_path, stale)

    governance = tmp_path / "Knowledge Base/_Governance/curation/runs"
    assert not governance.exists()


def test_valid_promotion_plan_seals_candidate_binding(tmp_path: Path) -> None:
    _promotion(tmp_path)
    candidate = _candidate(tmp_path)
    binding = curation.work_item(tmp_path, review_ref=candidate.ref)["entity_candidate"]
    proposed = curation.propose(
        tmp_path,
        {
            "version": 1,
            "title": "Promote one recurring identity",
            "entity_candidate": binding,
            "steps": [
                {
                    "step_id": "promote",
                    "kind": "create-entity",
                    "args": {
                        "entity_type": "organization",
                        "name": "amber guild",
                        "summary": "A stable identity supported by reusable contexts.",
                    },
                }
            ],
        },
    )

    preview = curation.preview(tmp_path, run_id=proposed["run_id"])
    assert preview["entity_candidate"] == binding
    assert preview["binding_health"] == "current"


def test_candidate_change_after_proposal_refuses_before_apply_mutation(tmp_path: Path) -> None:
    _promotion(tmp_path)
    candidate = _candidate(tmp_path)
    binding = curation.work_item(tmp_path, review_ref=candidate.ref)["entity_candidate"]
    proposed = curation.propose(
        tmp_path,
        {
            "version": 1,
            "title": "Promotion that must stay bound",
            "entity_candidate": binding,
            "steps": [
                {
                    "step_id": "promote",
                    "kind": "create-entity",
                    "args": {
                        "entity_type": "organization",
                        "name": "amber guild",
                        "summary": "A stable identity supported by reusable contexts.",
                    },
                }
            ],
        },
    )
    _note(tmp_path, 9, "Uses: amber guild.")

    stale_preview = curation.preview(tmp_path, run_id=proposed["run_id"])
    assert stale_preview["binding_health"] == "stale"
    assert stale_preview["blockers"][0]["code"] == "CURATION_ENTITY_CANDIDATE_STALE"

    with pytest.raises(curation.CurationError, match="CURATION_ENTITY_CANDIDATE_STALE"):
        curation.apply(
            tmp_path,
            run_id=proposed["run_id"],
            plan_id=proposed["plan_id"],
            expected_plan_fingerprint=proposed["plan_fingerprint"],
            why="This must not approve changed evidence.",
        )

    assert not (
        tmp_path / "Knowledge Base/Entities/Organizations/amber-guild.md"
    ).exists()


@pytest.mark.parametrize("drift", ["target-deleted", "registry-changed"])
def test_hydration_target_or_registry_drift_refuses_before_mutation(
    tmp_path: Path,
    drift: str,
) -> None:
    target = _hydration(tmp_path)
    candidate = _candidate(tmp_path)
    item = curation.work_item(tmp_path, review_ref=candidate.ref)
    binding = item["entity_candidate"]
    context_path = binding["first_disconnected_context_batch"][0]["path"]
    source = (tmp_path / context_path).read_text(encoding="utf-8")
    plan = {
        "version": 1,
        "title": "Hydration bound to target and registry",
        "entity_candidate": binding,
        "steps": [
            {
                "step_id": "connect",
                "kind": "edit",
                "args": {
                    "path": context_path,
                    "why": "Connect the exact reviewed context.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": "cobalt workshop",
                        "new_string": (
                            f"[[{target.removesuffix('.md')}|cobalt workshop]]"
                        ),
                        "expected_hash": content_hash(source),
                    },
                },
            }
        ],
    }
    proposed = curation.propose(tmp_path, plan)

    if drift == "target-deleted":
        (tmp_path / target).unlink()
        find.clear_cache()
    else:
        _write(
            tmp_path,
            "Knowledge Base/_Schema/entity-types.yaml",
            "schema_version: 1\n"
            "entity_types:\n"
            "  venue:\n"
            "    folder: Venues\n"
            "    label: Venue\n"
            "    aliases: []\n"
            "    cue_nouns: [venue]\n"
            "    capture_guidance: A stable recurring place.\n",
        )

    preview = curation.preview(tmp_path, run_id=proposed["run_id"])
    assert preview["binding_health"] == "stale"
    assert preview["blockers"][0]["code"] == "CURATION_ENTITY_CANDIDATE_STALE"
    with pytest.raises(curation.CurationError, match="CURATION_ENTITY_CANDIDATE_STALE"):
        curation.apply(
            tmp_path,
            run_id=proposed["run_id"],
            plan_id=proposed["plan_id"],
            expected_plan_fingerprint=proposed["plan_fingerprint"],
            why="Do not approve drifted lifecycle evidence.",
        )
    assert "[[" not in (tmp_path / context_path).read_text(encoding="utf-8")


def test_hydration_plan_is_limited_to_the_reviewed_batch(tmp_path: Path) -> None:
    target = _hydration(tmp_path)
    candidate = _candidate(tmp_path)
    item = curation.work_item(tmp_path, review_ref=candidate.ref)
    binding = item["entity_candidate"]
    context_path = binding["first_disconnected_context_batch"][0]["path"]
    before = (tmp_path / context_path).read_text(encoding="utf-8")
    old = next(
        phrase
        for phrase in ("cobalt workshop", "Cobalt Workshop")
        if phrase in before
    )
    plan = {
        "version": 1,
        "title": "Connect one reviewed hydration context",
        "entity_candidate": binding,
        "steps": [
            {
                "step_id": "connect-context",
                "kind": "edit",
                "args": {
                    "path": context_path,
                    "why": "Connect the reviewed context to the canonical Entity.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": old,
                        "new_string": f"[[{target.removesuffix('.md')}|{old}]]",
                        "expected_hash": content_hash(before),
                    },
                },
            }
        ],
    }

    proposed = curation.propose(tmp_path, plan)
    applied = curation.apply(
        tmp_path,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Apply the exact reviewed hydration batch.",
    )

    assert applied["phase"] == "completed"
    assert applied["step"]["path"] == context_path
    assert f"[[{target.removesuffix('.md')}|" in (
        tmp_path / context_path
    ).read_text(encoding="utf-8")
    refreshed = _candidate(tmp_path)
    assert refreshed.ref == candidate.ref
    assert refreshed.fingerprint != candidate.fingerprint
    assert refreshed.reasons[0]["meta"]["candidate_state"] == "hydration"
    assert refreshed.reasons[0]["meta"]["disconnected_context_count"] == 2


def test_three_confirmed_hydration_batches_converge_and_removed_link_reopens(
    tmp_path: Path,
) -> None:
    target = _hydration_corpus(tmp_path, identity="cobalt workshop", contexts=18)
    initial = _candidate(tmp_path)
    review_ref = initial.ref
    assert review_ref is not None
    item = curation.work_item(tmp_path, review_ref=review_ref)
    connected: list[str] = []

    for ordinal, expected_size in ((1, 8), (2, 8), (3, 2)):
        batch, terminal = _apply_hydration_batch(
            tmp_path,
            target=target,
            item=item,
            ordinal=ordinal,
        )
        assert terminal["phase"] == "completed"
        assert len(batch) == expected_size
        assert not set(batch).intersection(connected)
        connected.extend(batch)
        item = curation.work_item(
            tmp_path,
            review_ref=review_ref,
            hydration_recheck=ordinal,
        )

    assert len(connected) == 18
    assert item["entity_candidate"] == {
        "review_ref": review_ref,
        "candidate_state": "quiet",
        "closed": True,
        "executable": False,
        "closure_only": True,
        "deferred_remaining_count": 0,
    }
    assert attention.attention(
        tmp_path,
        categories=["entity_recurrence"],
        limit=3,
        record_surfacing=False,
    ).items == []

    reopened_path = connected[0]
    source = (tmp_path / reopened_path).read_text(encoding="utf-8")
    _write(
        tmp_path,
        reopened_path,
        source.replace(
            f"[[{target.removesuffix('.md')}|cobalt workshop]]",
            "cobalt workshop",
        ),
    )
    reopened = _candidate(tmp_path)
    assert reopened.ref == review_ref
    assert reopened.reasons[0]["meta"]["candidate_state"] == "hydration"
    assert reopened.reasons[0]["meta"]["disconnected_context_count"] == 1


def test_nine_hydration_batches_defer_exactly_one_batch_to_next_session(
    tmp_path: Path,
) -> None:
    target = _hydration_corpus(tmp_path, identity="silver forum", contexts=72)
    first = _candidate(tmp_path)
    review_ref = first.ref
    assert review_ref is not None
    item = curation.work_item(tmp_path, review_ref=review_ref)
    connected: list[str] = []
    rechecks = 0

    for ordinal in range(1, 9):
        batch, terminal = _apply_hydration_batch(
            tmp_path,
            target=target,
            item=item,
            ordinal=ordinal,
        )
        assert terminal["phase"] == "completed"
        assert len(batch) == 8
        assert not set(batch).intersection(connected)
        connected.extend(batch)
        rechecks += 1
        item = curation.work_item(
            tmp_path,
            review_ref=review_ref,
            hydration_recheck=ordinal,
        )

    assert len(connected) == 64
    assert rechecks == 8
    closure = item["entity_candidate"]
    assert closure["closure_only"] is True
    assert closure["executable"] is False
    assert closure["first_disconnected_context_batch"] == []
    assert closure["deferred_remaining_count"] == 8

    next_session = curation.work_item(tmp_path, review_ref=review_ref)
    resumed = next_session["entity_candidate"]
    assert len(resumed["first_disconnected_context_batch"]) == 8
    assert resumed["remaining_disconnected_count"] == 0
    assert "closure_only" not in resumed
    assert not set(
        context["path"] for context in resumed["first_disconnected_context_batch"]
    ).intersection(connected)


def test_ambiguous_candidate_has_no_executable_plan(tmp_path: Path) -> None:
    first = _entity(tmp_path, title="river archive", slug="river-archive-one")
    second = _entity(tmp_path, title="river archive", slug="river-archive-two")
    for index, body in enumerate(
        (
            "I work with river archive.",
            "I use river archive.",
            "I attend river archive.",
        )
    ):
        _note(tmp_path, index, body)
    candidate = _candidate(tmp_path)
    item = curation.work_item(tmp_path, review_ref=candidate.ref)

    assert item["entity_candidate"]["candidate_state"] == "ambiguous"
    assert item["entity_candidate"]["target_refs"] == [first, second]
    assert item["allowed_candidate_step_kinds"] == []
    with pytest.raises(curation.CurationError, match="CURATION_ENTITY_AMBIGUOUS"):
        curation.propose(
            tmp_path,
            {
                "version": 1,
                "title": "Must not choose",
                "entity_candidate": item["entity_candidate"],
                "steps": [
                    {
                        "step_id": "choose",
                        "kind": "create-entity",
                        "args": {
                            "entity_type": "organization",
                            "name": "river archive",
                            "summary": "Must not execute.",
                        },
                    }
                ],
            },
        )


@pytest.mark.parametrize(
    "steps,code",
    [
        (
            [
                {
                    "step_id": "duplicate",
                    "kind": "create-entity",
                    "args": {
                        "entity_type": "organization",
                        "name": "amber guild",
                        "summary": "One.",
                    },
                },
                {
                    "step_id": "duplicate-again",
                    "kind": "create-entity",
                    "args": {
                        "entity_type": "organization",
                        "name": "amber guild two",
                        "summary": "Two.",
                    },
                },
            ],
            "CURATION_ENTITY_DUPLICATE_CREATE",
        ),
        (
            [
                {
                    "step_id": "direct-registry",
                    "kind": "edit",
                    "args": {
                        "path": "Knowledge Base/_Schema/entity-types.yaml",
                        "why": "Bypass the governed registry save.",
                        "operation": {
                            "kind": "replace_string",
                            "old_string": "old",
                            "new_string": "new",
                            "expected_hash": "a" * 64,
                        },
                    },
                }
            ],
            "CURATION_TARGET_PROTECTED",
        ),
    ],
)
def test_candidate_plan_refuses_duplicate_create_and_direct_registry_mutation(
    tmp_path: Path,
    steps: list[dict[str, object]],
    code: str,
) -> None:
    _promotion(tmp_path)
    candidate = _candidate(tmp_path)
    binding = curation.work_item(tmp_path, review_ref=candidate.ref)["entity_candidate"]
    plan = {
        "version": 1,
        "title": "Invalid candidate route",
        "entity_candidate": binding,
        "steps": steps,
    }

    with pytest.raises(curation.CurationError, match=code):
        curation.propose(tmp_path, plan)


def test_eighth_hydration_recheck_is_closure_only() -> None:
    binding = {
        "review_ref": "exomem://review/" + "a" * 24,
        "review_fingerprint": "b" * 24,
        "candidate_state": "hydration",
        "identity": "cobalt workshop",
        "signal_version": "c" * 64,
        "first_disconnected_context_batch": [
            {"path": f"Knowledge Base/Notes/context-{index}.md", "context_hash": f"h-{index}"}
            for index in range(8)
        ],
        "remaining_disconnected_count": 8,
        "batch_fingerprint": "d" * 64,
        "target_refs": ["Knowledge Base/Entities/Organizations/cobalt-workshop.md"],
        "grammar_identity": {
            "version": "identity-frames-v1",
            "predicate_table_digest": "e" * 64,
        },
        "registry_identity": "f" * 64,
    }

    ordinary = curation.project_entity_candidate_binding(binding, hydration_recheck=7)
    closure = curation.project_entity_candidate_binding(binding, hydration_recheck=8)

    assert len(ordinary["first_disconnected_context_batch"]) == 8
    assert ordinary["executable"] is True
    assert closure["first_disconnected_context_batch"] == []
    assert closure["batch_fingerprint"] is None
    assert closure["executable"] is False
    assert closure["closure_only"] is True
    assert closure["deferred_remaining_count"] == 16


@pytest.mark.parametrize(
    "level,ordinary_mode",
    [("off", "explicit-only"), ("light", "explicit-only"), ("balanced", "once"), ("maximal", "once")],
)
def test_bootstrap_carries_capability_honest_bounded_entity_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: str,
    ordinary_mode: str,
) -> None:
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)
    payload = commands.op_bootstrap(tmp_path)
    lifecycle = payload["entity_registry"]["lifecycle"]

    assert lifecycle["available"] is True
    assert lifecycle["ordinary_read"] == {
        "mode": ordinary_mode,
        "boundary": "first-turn-after-primary-work-before-final-response",
        "maximum_per_session": 1 if ordinary_mode == "once" else 0,
        "route": {
            "tool": "review_memory",
            "arguments": {
                "mode": "attention",
                "categories": ["entity_recurrence"],
                "limit": 3,
            },
        },
    }
    assert lifecycle["bounds"] == {
        "candidates_per_read": 3,
        "contexts_per_candidate": 8,
        "hydration_mutations_per_session": 8,
        "hydration_rechecks_per_session": 8,
    }
    assert lifecycle["decision_order"] == [
        "resolve-exact-and-alias",
        "stop-on-ambiguity",
        "hydrate-one-match-before-duplicate",
        "promote-only-no-match",
    ]
    assert lifecycle["general_mutation_recheck"]["maximum_per_session"] == 1
    assert lifecycle["hydration_continuation"]["fresh_confirmation_per_batch"] is True
    assert lifecycle["hydration_continuation"]["route"] == {
        "tool": "maintain_memory",
        "arguments": {
            "mode": "curation",
            "curation_action": "work-item",
            "review_ref": "same-review-ref",
            "hydration_recheck": "next-ordinal-1-through-8",
        },
    }
    assert lifecycle["hydration_continuation"]["eighth_recheck"] == "closure-only"


def test_bootstrap_skips_recurrence_when_category_read_is_not_callable(tmp_path: Path) -> None:
    descriptor = ActiveSurfaceDescriptor(
        surface="mcp",
        profile="reduced",
        tier2_enabled=False,
        product_commands=("bootstrap", "ask_memory"),
    )
    with active_surface(descriptor):
        payload = commands.op_bootstrap(tmp_path)

    lifecycle = payload["entity_registry"]["lifecycle"]
    assert lifecycle == {
        "available": False,
        "ordinary_read": "unavailable-skip",
        "unavailable_reason": "The active surface cannot request an explicit review category.",
        "forbidden_substitutes": ["local-scan", "model-inference", "embedding", "due-state"],
    }


def test_open_registry_projection_reports_leaf_folder_and_family(tmp_path: Path) -> None:
    registry = tmp_path / "Knowledge Base/_Schema/entity-types.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        "schema_version: 1\n"
        "entity_types:\n"
        "  community:\n"
        "    folder: Communities\n"
        "    label: Community\n"
        "    aliases: [group]\n"
        "    cue_nouns: [community]\n"
        "    capture_guidance: A stable recurring group.\n"
        "    parent: organization\n",
        encoding="utf-8",
    )

    community = next(
        item
        for item in commands.op_bootstrap(tmp_path)["entity_registry"]["types"]
        if item["id"] == "community"
    )

    assert community["id"] == "community"
    assert community["folder"] == "Communities"
    assert community["family"] == "organization"


def test_hosted_v5_entity_lifecycle_input_is_generic_and_bounded() -> None:
    path = Path("tests/fixtures/hosted_v5_contributions/recurring_entity_lifecycle.json")
    value = json.loads(path.read_text(encoding="utf-8"))

    assert value["family"] == "recurring-entity-lifecycle"
    assert value["primitive"] == "stable-recurring-identity"
    assert value["ordinary_read"]["arguments"]["categories"] == ["entity_recurrence"]
    assert value["bounds"] == {
        "candidates_per_read": 3,
        "contexts_per_candidate": 8,
        "hydration_mutations_per_session": 8,
        "hydration_rechecks_per_session": 8,
    }
    assert value["authority"] == {
        "candidate_read": "structural_suggestions",
        "registry_save": "restructure_execution",
        "curation_apply": "restructure_execution",
        "standalone_relation_acceptance": "link_acceptance",
    }
    assert "community" not in value["primitive"]


def test_hook_rearms_the_exact_ordinary_entity_read_without_becoming_a_decider() -> None:
    reminder = exomem_capture_nudge.REMINDER
    folded = reminder.casefold()

    assert "after primary work" in folded
    assert "before the final response" in folded
    assert "once per session" in folded
    assert "entity_recurrence" in reminder
    assert "review_memory" in reminder
    assert "limit=3" in reminder
    assert "active agent" in folded
    assert "no local scan" in folded
    assert "no model" in folded
    assert "terminal receipt" in folded
    assert "closure-only eighth recheck" in folded
    assert len(reminder) < 1600


def test_portable_skill_and_operation_reference_carry_the_same_lifecycle() -> None:
    roots = (
        Path("src/exomem/_scaffold/_Schema"),
        Path("plugins/claude-code/skills/exomem"),
    )
    for root in roots:
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        operations = (root / "references/operations.md").read_text(encoding="utf-8")
        combined = skill + "\n" + operations
        prose = " ".join(combined.split())
        assert "entity_recurrence" in combined
        assert "after primary work" in prose
        assert "before the final response" in prose
        assert "at most three candidates" in prose
        assert "eight contexts" in prose
        assert "eighth recheck" in prose
        assert "closure-only" in prose
        assert "resolve exact and alias" in prose
        assert "stop on ambiguity" in prose
        assert "hydrate" in prose
        assert "save-entity-types" in combined
        assert "person | organization | concept | library | decision" not in combined


def test_hookless_custom_instruction_blocks_name_the_ordinary_entity_boundary() -> None:
    text = Path("docs/prominence.md").read_text(encoding="utf-8")
    maximal = text.split("### Maximal", 1)[1].split("### Balanced", 1)[0]
    balanced = text.split("### Balanced", 1)[1].split("### Light", 1)[0]

    for block in (maximal, balanced):
        assert "entity_recurrence" in block
        assert "after primary work" in block
        assert "before the final response" in block
        assert "once per chat" in block
        assert "limit=3" in block


def test_hookless_client_spends_one_ordinary_read_and_one_general_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_PROMINENCE", "balanced")
    positive = (
        "amber guild is an organization.",
        "organization: amber guild.",
        "Membership: amber guild.",
    )
    twin = (
        "We passed willow marker again.",
        "The notes mention willow marker again.",
        "A summary repeated willow marker again.",
    )
    for index, (signal, incidental) in enumerate(zip(positive, twin, strict=True)):
        _note(tmp_path, index, f"{signal}\n\n{incidental}")

    lifecycle = commands.op_bootstrap(tmp_path)["entity_registry"]["lifecycle"]
    route = lifecycle["ordinary_read"]["route"]["arguments"]
    ordinary_remaining = lifecycle["ordinary_read"]["maximum_per_session"]
    general_remaining = lifecycle["general_mutation_recheck"]["maximum_per_session"]
    calls: list[str] = []

    def explicit_read(reason: str):  # noqa: ANN202
        calls.append(reason)
        return attention.attention(
            tmp_path,
            categories=route["categories"],
            limit=route["limit"],
            record_surfacing=False,
        )

    # The prompt subject is deliberately unrelated. The carrier boundary, not
    # topical entity wording, spends the one ordinary read after primary work.
    first_prompt = "Please format the answer concisely."
    assert first_prompt
    first = explicit_read("ordinary-after-primary") if ordinary_remaining else None
    ordinary_remaining -= 1
    second = explicit_read("ordinary-second-prompt") if ordinary_remaining > 0 else None
    general = explicit_read("general-mutation") if general_remaining else None
    general_remaining -= 1
    repeated_general = (
        explicit_read("general-mutation-again") if general_remaining > 0 else None
    )

    assert first is not None
    assert [item.reasons[0]["meta"]["identity"] for item in first.items] == [
        "amber guild"
    ]
    assert "willow marker" not in repr(first.items).casefold()
    assert second is None
    assert general is not None
    assert repeated_general is None
    assert calls == ["ordinary-after-primary", "general-mutation"]


def test_three_batch_and_nine_batch_carrier_budgets_are_exact(tmp_path: Path) -> None:
    lifecycle = commands.op_bootstrap(tmp_path)["entity_registry"]["lifecycle"]

    def journey(total_batches: int) -> tuple[int, int, bool, int]:
        mutations = 0
        rechecks = 0
        closure_only = False
        next_session = 0
        mutation_cap = lifecycle["bounds"]["hydration_mutations_per_session"]
        recheck_cap = lifecycle["bounds"]["hydration_rechecks_per_session"]
        for ordinal in range(1, min(total_batches, mutation_cap) + 1):
            # Each iteration represents a separately confirmed plan with a
            # terminal receipt.  Without either, the carrier authorizes no call.
            mutations += 1
            if rechecks >= recheck_cap:
                break
            rechecks += 1
            closure_only = ordinal == mutation_cap
        if total_batches > mutation_cap:
            next_session = total_batches - mutation_cap
        return mutations, rechecks, closure_only, next_session

    assert journey(3) == (3, 3, False, 0)
    assert journey(9) == (8, 8, True, 1)
