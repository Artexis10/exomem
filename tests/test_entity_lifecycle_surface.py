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


def _strings_in(value: object) -> set[str]:
    """Every string anywhere in a response, so a leak cannot hide in a new key."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        found: set[str] = set()
        for key, item in value.items():
            found |= _strings_in(key) | _strings_in(item)
        return found
    if isinstance(value, (list, tuple)):
        found = set()
        for item in value:
            found |= _strings_in(item)
        return found
    return set()


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

    # The closure-only read may report that work remains; it may not hand back
    # the ninth batch in any field.  Redacting only the projection while the
    # page bodies and the raw review evidence still travel would make the bound
    # a suggestion rather than an API boundary.
    assert item["pages"] == []
    assert item["truncation"] == {"pages": [], "disclosed": False}
    evidence = item["candidate_evidence"]
    for withheld in ("disconnected_contexts", "batch_fingerprint", "remaining_disconnected_count"):
        assert withheld not in evidence, withheld
    deferred_meta = _candidate(tmp_path).reasons[0]["meta"]
    deferred_paths = {
        str(context["path"]) for context in deferred_meta["disconnected_contexts"]
    }
    assert len(deferred_paths) == 8
    # Paths and the batch fingerprint are the identifying values; a raw
    # context_hash is a digest of the matched clause alone, so contexts sharing
    # a clause share a hash and it cannot distinguish a deferred context from an
    # already-connected one. The fingerprint is a digest over the exact deferred
    # rows, so it IS unique to batch nine and is the value an agent would need
    # to bind to it.
    deferred_fingerprint = str(deferred_meta["batch_fingerprint"])
    leaked = _strings_in(item) & (deferred_paths | {deferred_fingerprint})
    assert leaked == set(), sorted(leaked)

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


def test_eighth_hydration_recheck_is_closure_only(tmp_path: Path) -> None:
    """Built through work_item so the binding is one the validator actually accepts.

    A hand-written binding can carry shapes the real path rejects, which makes a
    green projection test say nothing about the live boundary.
    """
    _hydration_corpus(tmp_path, identity="cobalt workshop", contexts=16)
    item = curation.work_item(tmp_path, review_ref=_candidate(tmp_path).ref)
    binding = item["entity_candidate"]
    assert binding["candidate_state"] == "hydration"
    assert len(binding["first_disconnected_context_batch"]) == 8
    assert binding["remaining_disconnected_count"] == 8

    ordinary = curation.project_entity_candidate_binding(binding, hydration_recheck=7)
    closure = curation.project_entity_candidate_binding(binding, hydration_recheck=8)

    assert len(ordinary["first_disconnected_context_batch"]) == 8
    assert ordinary["executable"] is True
    assert ordinary["closure_only"] is False
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
            "review_ref": "<same-review-ref>",
            "hydration_recheck": "<next-ordinal-1-through-8>",
        },
        "placeholders": True,
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

    # Every case pinned by id, state and route. Asserting only that cases exist
    # lets one be renamed, restated or silently dropped while the count holds.
    assert [case["id"] for case in value["cases"]] == [
        "generic-promotion",
        "existing-identity-hydration",
        "ambiguous-identity-stop",
        "frequency-matched-twin",
        "open-registry-family-metadata",
    ]
    assert {
        case["id"]: (case.get("candidate_state"), case["expected_route"])
        for case in value["cases"]
    } == {
        "generic-promotion": ("promotion", "governed-create-entity"),
        "existing-identity-hydration": ("hydration", "governed-edit-or-accept-relation"),
        "ambiguous-identity-stop": ("ambiguous", "no-executable-default"),
        "frequency-matched-twin": ("quiet", "absent"),
        "open-registry-family-metadata": (None, "traversal-only"),
    }

    # D3: the canonical kind is a singular open leaf, the folder is a plural
    # projection of it, and the parent family is traversal only. The case
    # mirrors the shape bootstrap serves at entity_registry.types[*].
    family_case = next(
        case for case in value["cases"] if case["id"] == "open-registry-family-metadata"
    )
    assert set(family_case["entity_type"]) == {"id", "label", "folder", "family"}
    assert family_case["entity_type"]["id"] == "guild"
    assert family_case["entity_type"]["folder"] == "Guilds"
    assert family_case["entity_type"]["family"] == "organization"
    assert family_case["entity_type"]["id"] != family_case["entity_type"]["folder"]


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
    assert len(reminder) < 1800

    # The ordinary read is a balanced/maximal behaviour. Unqualified, the hook
    # tells a light or off session to spend a category read it never opted into,
    # which is the nudge the prominence levels exist to withhold. The qualifier
    # has to sit in the cadence sentence itself, not merely somewhere in the
    # paragraph.
    cadence = reminder.split("review_memory", 1)[0].rsplit(". ", 1)[-1].casefold()
    assert "at balanced/maximal" in cadence, cadence


def _lifecycle_block(path: Path) -> str:
    """The one block in each carrier that states the recurrence cadence.

    Scoped, not whole-file: `operations.md` also says "do not repeat the same
    advice within one conversation" hundreds of lines away, which satisfies a
    file-wide "do not repeat" check even when the cadence sentence beside
    `entity_recurrence` has been inverted.
    """
    text = path.read_text(encoding="utf-8")
    if path.name == "operations.md":
        block = text.split("## recurring entity lifecycle", 1)[1].split("\n## ", 1)[0]
    else:
        block = text.split(
            "- **Recurring-identity maintenance boundary", 1
        )[1].split("\n- ", 1)[0]
    return " ".join(block.split())


def test_every_carrier_file_states_the_cadence_bounds_on_its_own() -> None:
    """Per FILE, not concatenated: a pair that is only jointly complete is not.

    Asserting skill+operations together lets either file drop the cadence, the
    once-per bound, the do-not-rescan rule or the three-candidate cap while the
    other one covers for it -- and an agent that loads only one of them is then
    told something the tests never checked.
    """
    for path in (
        # The intent-router restructure moved the cadence bullet out of SKILL.md
        # into the engagement reference the router points at; operations.md still
        # carries the operation detail. Both distributions ship both files.
        Path("src/exomem/_scaffold/_Schema/references/engagement.md"),
        Path("plugins/claude-code/skills/exomem/references/engagement.md"),
        Path("src/exomem/_scaffold/_Schema/references/operations.md"),
        Path("plugins/claude-code/skills/exomem/references/operations.md"),
    ):
        block = _lifecycle_block(path)
        folded = block.casefold()
        assert "limit=3" in block, path
        assert "once per session" in folded, path
        assert "at most three candidates" in folded, path
        assert ("do not rescan" in folded) or ("do not repeat" in folded), path


def test_portable_skill_and_operation_reference_carry_the_same_lifecycle() -> None:
    roots = (
        Path("src/exomem/_scaffold/_Schema"),
        Path("plugins/claude-code/skills/exomem"),
    )
    for root in roots:
        # Same move as above: the engagement reference now carries what the
        # router used to state inline.
        engagement = (root / "references/engagement.md").read_text(encoding="utf-8")
        operations = (root / "references/operations.md").read_text(encoding="utf-8")
        combined = engagement + "\n" + operations
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
        # The spec's unavailable-skip rule: a surface that cannot request the
        # category must skip, not invent a substitute. Without it the pasted
        # block reads as an unconditional instruction.
        folded = block.casefold()
        assert "unavailable" in folded and "skip" in folded, block
        # The block is bounded by the 1,500-byte web custom-instructions limit,
        # so it defers the decision order to the bootstrap payload it already
        # instructs the agent to follow. That deferral is only honest if the
        # instruction to follow bootstrap is actually present.
        assert 'bootstrap(profile="compact")' in block
        assert "follow it" in folded
        for restated in ("stop ambiguity", "hydrate before create", "resolve first"):
            assert restated not in folded, restated


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

    # 5.2's last leg: a separately confirmed hydration batch buys exactly one
    # bounded same-identity continuation, on the same review ref, and it is a
    # continuation rather than a fresh scan.
    target = _entity(tmp_path, title="cobalt workshop", slug="cobalt-workshop")
    for index, body in enumerate(
        ("I work with cobalt workshop.", "I use cobalt workshop.", "I attend cobalt workshop."),
        start=3,
    ):
        _note(tmp_path, index, body)
    next_session = attention.attention(
        tmp_path,
        categories=route["categories"],
        limit=route["limit"],
        record_surfacing=False,
    )
    hydration = next(
        item
        for item in next_session.items
        if item.reasons[0]["meta"]["identity"] == "cobalt workshop"
    )
    work = curation.work_item(tmp_path, review_ref=hydration.ref)
    assert work["entity_candidate"]["candidate_state"] == "hydration"
    _apply_hydration_batch(tmp_path, target=target, item=work, ordinal=1)
    continuation = curation.work_item(
        tmp_path, review_ref=hydration.ref, hydration_recheck=1
    )
    assert continuation["entity_candidate"]["review_ref"] == hydration.ref
    assert lifecycle["hydration_continuation"]["same_identity_only"] is True
    assert lifecycle["hydration_continuation"]["fresh_confirmation_per_batch"] is True


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


def test_ninth_binding_rebuilt_from_the_closure_response_is_not_proposable(
    tmp_path: Path,
) -> None:
    """The closure read must not be re-assemblable into an executable ninth batch.

    Withholding the batch is only a bound if the response cannot be turned back
    into one.  An agent that keeps the ordinal-8 payload and re-proposes from it
    is exactly the ninth mutation the eight-batch budget exists to refuse.
    """
    target = _hydration_corpus(tmp_path, identity="silver forum", contexts=72)
    review_ref = _candidate(tmp_path).ref
    assert review_ref is not None
    item = curation.work_item(tmp_path, review_ref=review_ref)
    for ordinal in range(1, 9):
        _apply_hydration_batch(tmp_path, target=target, item=item, ordinal=ordinal)
        item = curation.work_item(
            tmp_path, review_ref=review_ref, hydration_recheck=ordinal
        )

    rebuilt = dict(item["entity_candidate"])
    rebuilt.pop("closure_only", None)
    rebuilt.pop("deferred_remaining_count", None)
    rebuilt["executable"] = True
    plan = {
        "version": 1,
        "title": "Connect a ninth batch the closure read never disclosed",
        "entity_candidate": rebuilt,
        "steps": [
            {
                "step_id": "ninth",
                "kind": "edit",
                "args": {
                    "path": "Knowledge Base/Notes/context-64.md",
                    "why": "Attempt a ninth hydration mutation this session.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": "silver forum",
                        "new_string": f"[[{target.removesuffix('.md')}|silver forum]]",
                        "expected_hash": content_hash(
                            (tmp_path / "Knowledge Base/Notes/context-64.md").read_text(
                                encoding="utf-8"
                            )
                        ),
                    },
                },
            }
        ],
    }

    with pytest.raises(curation.CurationError) as raised:
        curation.propose(tmp_path, plan)
    assert raised.value.code in {
        "CURATION_ENTITY_CANDIDATE_STALE",
        "CURATION_ENTITY_BATCH_STALE",
    }, raised.value.code


def _relation_candidate(root: Path, *, source: str, target: str):  # noqa: ANN202
    # Relation review became graph-native with the governed relation vocabulary,
    # so the queue projects the built graph rather than rescanning files. These
    # fixtures write markdown directly instead of going through a product write,
    # so nothing has built the graph for them yet.
    from exomem import epistemic_graph

    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    review = commands.op_review_memory(root, mode="relation-queue")
    candidate = next(
        row
        for group in review["groups"]
        for row in group["items"]
        if row["from"] == source and row["to"] == target
    )
    expected_hash = next(
        group["content_hash"] for group in review["groups"] if group["path"] == source
    )
    return candidate, expected_hash


def _hydration_with_relation(root: Path, identity: str = "cobalt workshop") -> str:
    """A hydration corpus whose first context both mentions and wikilinks the target.

    The plain mention keeps the line a disconnected context; the separate
    wikilink line raises an unresolved-relation candidate on the same page. That
    is the only shape in which the accept-relation hydration route is reachable.
    """
    target = _entity(root, title=identity, slug=identity.replace(" ", "-"))
    stem = target.removesuffix(".md")
    _write(
        root,
        "Knowledge Base/Notes/context-00.md",
        "---\ntype: insight\ntitle: Context 0\nstatus: active\n---\n"
        f"# Context 0\n\nI work with {identity}.\n\nSee also [[{stem}]] for the roster.\n",
    )
    for index, body in enumerate((f"I use {identity}.", f"I attend {identity}."), start=1):
        _note(root, index, body)
    return target


def test_hydration_accepts_a_reviewed_relation_to_the_bound_target(tmp_path: Path) -> None:
    target = _hydration_with_relation(tmp_path)
    item = curation.work_item(tmp_path, review_ref=_candidate(tmp_path).ref)
    binding = item["entity_candidate"]
    assert binding["candidate_state"] == "hydration"
    assert item["allowed_candidate_step_kinds"] == ["accept-relation", "edit"]
    source = "Knowledge Base/Notes/context-00.md"
    assert source in {row["path"] for row in binding["first_disconnected_context_batch"]}
    candidate, expected_hash = _relation_candidate(tmp_path, source=source, target=target)

    proposal = curation.propose(
        tmp_path,
        {
            "version": 1,
            "title": "Accept one reviewed relation to the bound Entity",
            "entity_candidate": binding,
            "steps": [
                {
                    "step_id": "relation",
                    "kind": "accept-relation",
                    "args": {
                        "ref": candidate["ref"],
                        "expected_hash": expected_hash,
                        "why": "Connect the reviewed context to the bound Entity.",
                        "expected_fingerprint": candidate["fingerprint"],
                    },
                }
            ],
        },
    )
    result = curation.apply(
        tmp_path,
        run_id=proposal["run_id"],
        plan_id=proposal["plan_id"],
        expected_plan_fingerprint=proposal["plan_fingerprint"],
        why="Confirm one reviewed hydration relation.",
    )
    while result["phase"] != "completed":
        result = curation.resume(
            tmp_path, run_id=proposal["run_id"], plan_id=proposal["plan_id"]
        )
    assert result["phase"] == "completed"
    assert "## Relations" in (tmp_path / source).read_text(encoding="utf-8")


def test_hydration_refuses_a_relation_to_a_different_entity(tmp_path: Path) -> None:
    """X1: the relation-target containment guard, exercised rather than assumed."""
    _hydration_with_relation(tmp_path)
    other = _entity(tmp_path, title="tin syndicate", slug="tin-syndicate")
    stem = other.removesuffix(".md")
    _write(
        tmp_path,
        "Knowledge Base/Notes/context-03.md",
        "---\ntype: insight\ntitle: Context 3\nstatus: active\n---\n"
        f"# Context 3\n\nI work with cobalt workshop.\n\nSee also [[{stem}]] elsewhere.\n",
    )
    item = curation.work_item(tmp_path, review_ref=_candidate(tmp_path).ref)
    binding = item["entity_candidate"]
    source = "Knowledge Base/Notes/context-03.md"
    assert source in {row["path"] for row in binding["first_disconnected_context_batch"]}
    candidate, expected_hash = _relation_candidate(tmp_path, source=source, target=other)

    with pytest.raises(curation.CurationError, match="CURATION_ENTITY_BATCH_STALE"):
        curation.propose(
            tmp_path,
            {
                "version": 1,
                "title": "Accept a relation pointing away from the bound Entity",
                "entity_candidate": binding,
                "steps": [
                    {
                        "step_id": "relation",
                        "kind": "accept-relation",
                        "args": {
                            "ref": candidate["ref"],
                            "expected_hash": expected_hash,
                            "why": "Attempt a relation outside the reviewed targets.",
                            "expected_fingerprint": candidate["fingerprint"],
                        },
                    }
                ],
            },
        )


def test_hydration_refuses_an_edit_outside_the_reviewed_batch(tmp_path: Path) -> None:
    """X6: an edit on a context the review never batched must fail closed."""
    target = _hydration_corpus(tmp_path, identity="silver forum", contexts=12)
    item = curation.work_item(tmp_path, review_ref=_candidate(tmp_path).ref)
    binding = item["entity_candidate"]
    batched = {row["path"] for row in binding["first_disconnected_context_batch"]}
    outside = next(
        path
        for path in (f"Knowledge Base/Notes/context-{index:02d}.md" for index in range(12))
        if path not in batched
    )
    source = (tmp_path / outside).read_text(encoding="utf-8")

    with pytest.raises(curation.CurationError, match="CURATION_ENTITY_BATCH_STALE"):
        curation.propose(
            tmp_path,
            {
                "version": 1,
                "title": "Connect a context the review never batched",
                "entity_candidate": binding,
                "steps": [
                    {
                        "step_id": "outside",
                        "kind": "edit",
                        "args": {
                            "path": outside,
                            "why": "Attempt an unreviewed hydration context.",
                            "operation": {
                                "kind": "replace_string",
                                "old_string": "silver forum",
                                "new_string": f"[[{target.removesuffix('.md')}|silver forum]]",
                                "expected_hash": content_hash(source),
                            },
                        },
                    }
                ],
            },
        )


def test_promotion_refuses_a_name_that_is_not_the_bound_identity(tmp_path: Path) -> None:
    """X3: the promotion name must preserve the reviewed normalized identity."""
    _promotion(tmp_path)
    item = curation.work_item(tmp_path, review_ref=_candidate(tmp_path).ref)

    with pytest.raises(curation.CurationError, match="CURATION_ENTITY_ROUTE_INVALID"):
        curation.propose(
            tmp_path,
            {
                "version": 1,
                "title": "Promote under a name the review never resolved",
                "entity_candidate": item["entity_candidate"],
                "steps": [
                    {
                        "step_id": "promote",
                        "kind": "create-entity",
                        "args": {
                            "entity_type": "organization",
                            "name": "bronze consortium",
                            "summary": "A different identity than the reviewed one.",
                        },
                    }
                ],
            },
        )


def test_malformed_review_ref_is_invalid_rather_than_stale(tmp_path: Path) -> None:
    """A ref that was never well-formed is a caller error, not a drifted review.

    Reporting it as STALE tells the agent to re-read and retry, which can never
    succeed, and hides a malformed argument behind a concurrency story.
    """
    _promotion(tmp_path)

    with pytest.raises(curation.CurationError) as raised:
        curation.work_item(tmp_path, review_ref="not-a-review-ref")
    assert raised.value.code == "CURATION_ENTITY_REVIEW_REF_INVALID"


@pytest.mark.parametrize(
    "missing,reasons",
    [
        ("candidate_state", ["ordinary_identity_recurs"]),
        ("grammar_version", ["ordinary_identity_recurs"]),
        # Both lanes qualified. The ordinary-text grammar DID match, so a None
        # grammar is a broken signal even though the wikilink reason is also
        # present, and the wikilink lane must not be taken as a fallback.
        (
            "grammar_version",
            ["unresolved_identity_recurs", "ordinary_identity_recurs"],
        ),
    ],
)
def test_candidate_meta_without_state_or_grammar_fails_closed(
    missing: str, reasons: list[str]
) -> None:
    """A silent default turns a signal we could not read into an executable route.

    Defaulting `candidate_state` to "promotion" means a candidate whose state is
    missing is offered a create-entity plan; defaulting the grammar version
    seals a binding against a grammar the review never named.
    """
    meta = {
        # The ordinary-text lane: this candidate DID match the text grammar, so
        # a missing grammar_version here is a broken signal rather than the
        # wikilink lane, and must still fail closed.
        "reasons": reasons,
        "candidate_state": "hydration",
        "identity": "cobalt workshop",
        "signal_version": "c" * 64,
        "grammar_version": "identity-frames-v1",
        "predicate_table_digest": "e" * 64,
        "registry_fingerprint": "f" * 64,
        "disconnected_contexts": [],
        "remaining_disconnected_count": 0,
        "resolution_candidates": [],
    }
    meta.pop(missing)

    class _Item:
        ref = "exomem://review/" + "a" * 24
        fingerprint = "b" * 24
        reasons = [{"category": "entity_recurrence", "meta": meta}]

    with pytest.raises(curation.CurationError) as raised:
        curation._entity_candidate_binding(_Item(), registry_fallback="f" * 64)
    assert raised.value.code == "CURATION_ENTITY_CANDIDATE_INVALID"


def test_entity_candidates_never_enter_due_state(tmp_path: Path) -> None:
    """f21: the recurrence category is explicit-request-only, never a due nag.

    A candidate that reached due-state would arrive unasked on ordinary results,
    which is precisely the nudge the no-nudge architecture withholds until the
    existing evidence gate authorizes it. Naming the category here means the
    absence is asserted, not merely a side effect of nobody wiring it up.
    """
    from exomem import due_state

    assert "entity_recurrence" not in due_state.PROJECTION_CATEGORIES
    assert "entity_recurrence" not in due_state.DELTA_CATEGORIES
    assert "entity_recurrence" not in due_state.PAGE_DELTA_CATEGORIES

    _promotion(tmp_path)
    assert _candidate(tmp_path).ref is not None
    assert "entity_recurrence" not in json.dumps(due_state.recompute(tmp_path))


def test_item_by_ref_still_resolves_a_non_entity_partitioned_category(
    tmp_path: Path,
) -> None:
    """The entity narrowing must not have taken the shared triage path with it.

    `entity_candidate_by_ref` is a narrowed lookup beside `item_by_ref`; had the
    narrowing been applied to the shared resolver instead, every other
    partitioned category would silently stop resolving. The vault therefore has
    to actually produce a non-entity item -- a loop over an empty report asserts
    nothing at all.
    """
    _write(
        tmp_path,
        "Knowledge Base/Notes/superseded-head.md",
        "---\n"
        "type: insight\n"
        "title: Superseded head\n"
        "status: superseded\n"
        "superseded_by: '[[Knowledge Base/Notes/successor-that-was-never-written]]'\n"
        "---\n"
        "# Superseded head\n\nThe successor this points at does not exist.\n",
    )

    report = attention.attention(
        tmp_path, categories=["supersession_integrity"], limit=5, record_surfacing=False
    )
    assert len(report.items) >= 1, report
    for item in report.items:
        assert item.categories == ["supersession_integrity"]
        assert attention.item_by_ref(tmp_path, item.ref).ref == item.ref


def test_wikilink_only_candidate_binds_its_own_grammar_lane(tmp_path: Path) -> None:
    """The unresolved-wikilink lane is a real lane, not a malformed signal.

    `audit` emits `grammar_version: None` for a candidate that never matched the
    ordinary-text grammar, so a fail-closed check that only asks "is it present"
    refuses the whole wikilink lane. The lane has to be recognised and bound by
    name instead -- a candidate whose grammar we genuinely cannot place still
    fails closed.
    """
    for index in range(3):
        _write(
            tmp_path,
            f"Knowledge Base/Notes/wiki-{index:02d}.md",
            "---\ntype: insight\ntitle: Wiki "
            f"{index}\nstatus: active\n---\n# Wiki {index}\n\n"
            "The roster names [[Amber Guild]] and the minutes name "
            "[[Amber Guild]] again.\n",
        )
    candidate = _candidate(tmp_path)
    meta = candidate.reasons[0]["meta"]
    assert meta["grammar_version"] is None
    assert meta["predicate_table_digest"] is None
    assert "ordinary_identity_recurs" not in meta["reasons"]
    assert meta["candidate_state"] == "promotion"

    item = curation.work_item(tmp_path, review_ref=candidate.ref)
    binding = item["entity_candidate"]
    assert binding["candidate_state"] == "promotion"
    assert binding["grammar_identity"] == {
        "version": "unresolved-wikilink-v1",
        "predicate_table_digest": None,
    }

    proposal = curation.propose(
        tmp_path,
        {
            "version": 1,
            "title": "Promote a wikilink-only recurring identity",
            "entity_candidate": binding,
            "steps": [
                {
                    "step_id": "promote",
                    "kind": "create-entity",
                    "args": {
                        "entity_type": "organization",
                        "name": binding["identity"],
                        "summary": "A recurring identity named only by wikilinks.",
                    },
                }
            ],
        },
    )
    result = curation.apply(
        tmp_path,
        run_id=proposal["run_id"],
        plan_id=proposal["plan_id"],
        expected_plan_fingerprint=proposal["plan_fingerprint"],
        why="Confirm the reviewed wikilink-lane promotion.",
    )
    while result["phase"] != "completed":
        result = curation.resume(
            tmp_path, run_id=proposal["run_id"], plan_id=proposal["plan_id"]
        )
    assert result["phase"] == "completed"
