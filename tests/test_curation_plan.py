from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest

from exomem import commands
from exomem.vault import content_hash


def _edit_step(**extra: object) -> dict[str, object]:
    step: dict[str, object] = {
        "step_id": "fix-finnish",
        "kind": "edit",
        "args": {
            "path": "Knowledge Base/Notes/Insights/finnish.md",
            "why": "Täsmennä johtopäätös.",
            "operation": {
                "kind": "replace_string",
                "old_string": "vanha",
                "new_string": "uusi",
                "expected_hash": "a" * 64,
            },
        },
    }
    step.update(extra)
    return step


def _plan(*steps: dict[str, object], **extra: object) -> dict[str, object]:
    plan: dict[str, object] = {
        "version": 1,
        "title": "Suomenkielinen täsmennys",
        "steps": list(steps or (_edit_step(),)),
    }
    plan.update(extra)
    return plan


def test_canonical_plan_json_preserves_unicode_and_derives_exact_identities() -> None:
    from exomem import curation

    validated = curation.validate_forward_plan(_plan())
    canonical = curation.canonical_json(validated)

    assert "Suomenkielinen täsmennys" in canonical
    assert "\\u00e4" not in canonical
    assert canonical == json.dumps(
        validated,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_plan_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert curation.plan_id(validated) == expected_plan_id
    assert curation.run_id(validated, today=dt.date(2026, 9, 1)) == (
        f"cur-20260901-{expected_plan_id[:12]}"
    )
    operation_bytes = curation.canonical_json_bytes(
        ["exomem-curation-operation-v1", expected_plan_id, 0, "fix-finnish"]
    )
    assert curation.operation_id(expected_plan_id, 0, "fix-finnish") == hashlib.sha256(
        operation_bytes
    ).hexdigest()


def test_operation_identity_uses_an_unambiguous_domain_separated_tuple() -> None:
    from exomem import curation

    plan_identity = "a" * 64
    assert curation.operation_id(plan_identity, 1, "23") != curation.operation_id(
        plan_identity, 12, "3"
    )
    operations = {
        curation.operation_id(plan_identity, ordinal, step_id)
        for ordinal, step_id in enumerate(("first", "second", "third"))
    }
    assert len(operations) == 3


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda value: value.update({"future": True}), "CURATION_UNKNOWN_FIELD"),
        (
            lambda value: value["steps"][0].update({"command": "edit_memory"}),
            "CURATION_UNKNOWN_FIELD",
        ),
        (
            lambda value: value["steps"][0]["args"].update({"callable": "os.unlink"}),
            "CURATION_UNKNOWN_FIELD",
        ),
        (lambda value: value["steps"][0].update({"kind": "shell"}), "INVALID_STEP_KIND"),
        (
            lambda value: value["steps"].append(
                {"step_id": "fix-finnish", "kind": "edit", "args": {}}
            ),
            "DUPLICATE_STEP_ID",
        ),
    ],
)
def test_plan_and_step_schemas_are_closed(mutate, code: str) -> None:  # noqa: ANN001
    from exomem import curation

    value = _plan()
    mutate(value)
    with pytest.raises(curation.CurationError, match=code):
        curation.validate_forward_plan(value)


@pytest.mark.parametrize("kind", ["unfold-recap-revisions", "unfold-duplicate"])
def test_a_fold_inverse_is_admitted_only_by_the_compensation_flag_never_by_the_records_one(kind: str) -> None:
    """The episode recorder seals with the records flag: it must not be able to seal the undo of a fold."""
    from exomem import curation

    value = _plan()
    value["steps"][0].update({"kind": kind})

    with pytest.raises(curation.CurationError, match="INVALID_STEP_KIND"):
        curation.validate_forward_plan(value, allow_records=True)
    # The kind passes with the compensation flag, so the refusal is the edit arguments that the step carries.
    with pytest.raises(curation.CurationError, match="CURATION_UNKNOWN_FIELD"):
        curation.validate_forward_plan(value, allow_fold_inverse=True)


@pytest.mark.parametrize(
    "path",
    [
        "Knowledge Base/Planning/project.md",
        "Knowledge Base/Records/event.md",
        "Knowledge Base/workflow-contract/state.md",
        "Knowledge Base/_Schema/SKILL.md",
        "Knowledge Base/_Governance/authority.json",
        "Knowledge Base/_Adoption/run.json",
        "Knowledge Base/_trash/2026-09-01/item.md",
        "Knowledge Base/Sources/raw.md",
        "Knowledge Base/Evidence/proof.md",
    ],
)
def test_plan_refuses_protected_administrative_and_append_only_targets(path: str) -> None:
    from exomem import curation

    value = _plan()
    value["steps"][0]["args"]["path"] = path
    with pytest.raises(curation.CurationError, match="CURATION_TARGET_PROTECTED"):
        curation.validate_forward_plan(value)


def test_plan_caps_steps_and_canonical_bytes() -> None:
    from exomem import curation

    too_many = _plan(*[_edit_step(step_id=f"s-{index}") for index in range(65)])
    with pytest.raises(curation.CurationError, match="CURATION_PLAN_TOO_LARGE"):
        curation.validate_forward_plan(too_many)

    huge = _plan()
    huge["title"] = "å" * (curation.MAX_PLAN_BYTES + 1)
    with pytest.raises(curation.CurationError, match="CURATION_PLAN_TOO_LARGE"):
        curation.validate_forward_plan(huge)


def test_plan_fingerprint_binds_plan_manifest_and_registry_identities() -> None:
    from exomem import curation

    value = curation.validate_forward_plan(_plan())
    manifest = [{"path": "Knowledge Base/Notes/Insights/finnish.md", "before": "a" * 64}]
    registries = {"relations": "b" * 64, "entities": "c" * 64, "schema": "d" * 64}

    baseline = curation.plan_fingerprint(value, manifest, registries)
    assert baseline != curation.plan_fingerprint(
        value,
        [{**manifest[0], "before": "e" * 64}],
        registries,
    )
    assert baseline != curation.plan_fingerprint(
        value,
        manifest,
        {**registries, "relations": "f" * 64},
    )


def _remember_page(vault, *, title: str, slug: str, sentence: str) -> str:  # noqa: ANN001
    arguments = {
        "title": title,
        "slug": slug,
        "content": f"## Observations\n\n- [finding] {sentence} ^{slug}\n",
        "note_type": "insight",
    }
    validation = commands.op_remember(vault, validate_only=True, **arguments)
    result = commands.op_remember(
        vault,
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validation["draft_hash"],
        relation_review_reason="The fixture has no honest relation for this isolated target.",
        **arguments,
    )
    return result["path"]


def test_work_item_uses_recorded_pages_exact_hashes_and_discloses_truncation(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    from exomem import curation

    path = _remember_page(
        vault,
        title="Kieliriippumaton päätelmä",
        slug="language-neutral-conclusion",
        sentence="Pidä sisältö muuttumattomana myös suomeksi.",
    )
    source = (vault / path).read_text(encoding="utf-8")
    monkeypatch.setattr(
        curation,
        "_optional_model_call",
        lambda *_args, **_kwargs: pytest.fail("work-item called a model"),
    )

    item = curation.work_item(vault, paths=[path], max_chars_per_page=80)

    assert item["action"] == "work-item"
    assert item["pages"][0]["path"] == path
    assert item["pages"][0]["content_hash"] == content_hash(source)
    assert item["pages"][0]["content"] == source[:80]
    assert item["pages"][0]["truncated"] is True
    assert item["truncation"]["pages"] == [path]
    assert "Kieliriippumaton" in source
    assert set(item["step_schemas"]) == set(curation.STEP_KINDS)
    assert "fold-recap-revisions" in item["step_schemas"]
    assert "unfold-recap-revisions" not in item["step_schemas"]
    assert "fold-duplicate" in item["step_schemas"]
    assert "unfold-duplicate" not in item["step_schemas"]
    assert set(item["registry_ids"]) == {"entities", "relations", "schemas"}


def test_work_item_requires_explicit_bounded_refs_or_paths(vault) -> None:  # noqa: ANN001
    from exomem import curation

    with pytest.raises(curation.CurationError, match="CURATION_WORK_ITEM_EMPTY"):
        curation.work_item(vault)
    with pytest.raises(curation.CurationError, match="CURATION_WORK_ITEM_TOO_LARGE"):
        curation.work_item(vault, paths=[f"Knowledge Base/p-{index}.md" for index in range(13)])


def test_proposal_runs_real_preflights_in_order_and_seals_preimages(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    from exomem import curation

    first = _remember_page(
        vault, title="First curation target", slug="first-curation-target", sentence="Keep old one."
    )
    second = _remember_page(
        vault,
        title="Second curation target",
        slug="second-curation-target",
        sentence="Keep old two.",
    )
    paths = [first, second]
    observed: list[str] = []
    real = commands.op_edit_memory

    def recording(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        observed.append(kwargs["path"])
        return real(*args, **kwargs)

    monkeypatch.setattr(commands, "op_edit_memory", recording)
    steps = []
    for index, path in enumerate(paths):
        source = (vault / path).read_text(encoding="utf-8")
        steps.append(
            {
                "step_id": f"edit-{index}",
                "kind": "edit",
                "args": {
                    "path": path,
                    "why": f"Clarify target {index}.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": f"old {'one' if index == 0 else 'two'}",
                        "new_string": f"new {'one' if index == 0 else 'two'}",
                        "expected_hash": content_hash(source),
                        "relation_disposition": "reviewed_none",
                        "relation_review_reason": "No honest new relation is introduced.",
                    },
                },
            }
        )

    proposed = curation.propose(vault, {"version": 1, "title": "Ordered edits", "steps": steps})
    preview = curation.preview(vault, run_id=proposed["run_id"])

    assert observed == paths
    assert [item["path"] for item in preview["binding_manifest"]] == paths
    assert all(item["preimage"] for item in preview["binding_manifest"])
    assert all(item["prepared"]["transition_token"] for item in preview["binding_manifest"])
    assert preview["plan_id"] == proposed["plan_id"]
    assert preview["plan_fingerprint"] == proposed["plan_fingerprint"]
    assert preview["binding_health"] == "current"


def test_proposal_refuses_stale_agent_hash_without_storing_rebound_plan(vault) -> None:  # noqa: ANN001
    from exomem import curation

    path = _remember_page(
        vault,
        title="Stale curation target",
        slug="stale-curation-target",
        sentence="Reviewed text.",
    )
    before = (vault / path).read_text(encoding="utf-8")
    expected = content_hash(before)
    (vault / path).write_text(
        before.replace("Reviewed text", "Externally changed"), encoding="utf-8"
    )
    plan = {
        "version": 1,
        "title": "Stale edit",
        "steps": [
            {
                "step_id": "stale",
                "kind": "edit",
                "args": {
                    "path": path,
                    "why": "Must not silently rebind.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": "Reviewed text",
                        "new_string": "Revised text",
                        "expected_hash": expected,
                    },
                },
            }
        ],
    }

    with pytest.raises(curation.CurationError, match="CURATION_BINDING_STALE"):
        curation.propose(vault, plan)
    runs = vault / "Knowledge Base" / "_Governance" / "curation" / "runs"
    assert not runs.exists() or not list(runs.iterdir())


def test_create_proposal_seals_expected_absence_and_preview_detects_collision(vault) -> None:  # noqa: ANN001
    from exomem import curation

    plan = {
        "version": 1,
        "title": "Create one",
        "steps": [
            {
                "step_id": "create-unicode",
                "kind": "create-note",
                "args": {
                    "title": "Uusi päätelmä",
                    "slug": "new-unicode-conclusion",
                    "content": (
                        "## Observations\n\n"
                        "- [finding] Säilytä Unicode muuttumattomana. ^unicode-preserved\n"
                    ),
                    "relation_disposition": "reviewed_none",
                    "relation_review_reason": "No honest relation exists yet.",
                },
            }
        ],
    }
    proposed = curation.propose(vault, plan)
    preview = curation.preview(vault, run_id=proposed["run_id"])
    destination = preview["binding_manifest"][0]["path"]
    assert preview["binding_manifest"][0]["expected_absent"] is True
    assert not (vault / destination).exists()

    (vault / destination).parent.mkdir(parents=True, exist_ok=True)
    (vault / destination).write_text("collision", encoding="utf-8")
    drifted = curation.preview(vault, run_id=proposed["run_id"])
    assert drifted["binding_health"] == "stale"
    assert drifted["blockers"][0]["code"] == "CURATION_BINDING_STALE"


def test_create_note_vocabulary_preparation_is_actionable_and_decision_bound(vault) -> None:  # noqa: ANN001
    from exomem import curation

    registry = vault / "Knowledge Base/_Schema/source-taxonomy.yaml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("domains:\n  health:\n    path_label: Health\n", encoding="utf-8")
    args = {
        "title": "Curation wealth experiment",
        "slug": "curation-wealth",
        "content": "## Hypothesis\n\nA bounded trial.\n",
        "note_type": "experiment",
        "domain": "wealth",
        "started": "2026-05-18",
        "duration": "one day",
        "relation_disposition": "reviewed_none",
        "relation_review_reason": "No honest relation exists for this fixture.",
    }
    plan = {
        "version": 1,
        "title": "Curation vocabulary decision",
        "steps": [{"step_id": "create-wealth", "kind": "create-note", "args": args}],
    }

    with pytest.raises(curation.CurationError, match="CURATION_VOCABULARY_PREPARATION_REQUIRED") as caught:
        curation.propose(vault, plan)
    assert "evidence_fingerprint" in caught.value.reason
    with pytest.raises(ValueError, match="CURATION_VOCABULARY_PREPARATION_REQUIRED") as public:
        commands.op_maintain_memory(vault, mode="curation", curation_action="propose", plan=plan)
    assert "evidence_fingerprint" in str(public.value)
    runs = vault / "Knowledge Base/_Governance/curation/runs"
    assert not runs.exists() or not list(runs.iterdir())

    preparation = commands.op_remember(vault, validate_only=True, **args)
    args["vocabulary_decision"] = {
        "evidence_fingerprint": preparation["vocabulary_preparation"]["evidence_fingerprint"],
        "outcome": "create",
        "canonical": "wealth",
    }
    proposed = curation.propose(vault, plan)
    result = curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved bounded vocabulary decision.",
    )

    assert result["phase"] == "completed"
    path = result["step"]["path"]
    assert (vault / path).exists()


def test_fold_proposal_refuses_a_head_that_is_not_the_single_live_page(vault, earlier_episode) -> None:  # noqa: ANN001
    """Only the episode's live page can hold the history, so a superseded page is no head."""
    from test_curation_execution import _fold_step

    from exomem import curation

    pages = earlier_episode(3)
    step = _fold_step(pages[:-1], vault)  # the second page is the head, the first its only revision

    with pytest.raises(curation.CurationError, match="single live page"):
        curation.propose(vault, _plan(step))


def test_fold_proposal_refuses_a_page_of_another_episode(vault, earlier_episode) -> None:  # noqa: ANN001
    """One step folds one episode: a revision of another episode, however superseded, is refused."""
    from test_curation_execution import _fold_step

    from exomem import curation

    pages = earlier_episode(3)
    other = earlier_episode(2, key="ep-" + "b2" * 16)
    step = _fold_step(pages, vault)
    step["args"]["revisions"].append(_fold_step(other, vault)["args"]["revisions"][0])

    with pytest.raises(curation.CurationError, match="not a superseded recap revision of this episode"):
        curation.propose(vault, _plan(step))


def test_fold_proposal_refuses_revisions_listed_newest_first(vault, earlier_episode) -> None:  # noqa: ANN001
    """The history is written oldest first, so the plan must name the revisions in that order."""
    from test_curation_execution import _fold_step

    from exomem import curation

    pages = earlier_episode(4)
    step = _fold_step(pages, vault)
    step["args"]["revisions"].reverse()

    with pytest.raises(curation.CurationError, match="oldest first"):
        curation.propose(vault, _plan(step))


def test_fold_proposal_refuses_the_head_among_its_revisions(vault, earlier_episode) -> None:  # noqa: ANN001
    """The live page is never a revision to retire, whatever its hash says."""
    from test_curation_execution import _fold_step

    from exomem import curation

    pages = earlier_episode(2)
    step = _fold_step(pages, vault)
    step["args"]["revisions"].append(step["args"]["head"])

    with pytest.raises(curation.CurationError, match="never the head"):
        curation.propose(vault, _plan(step))


def test_fold_proposal_needs_the_whole_namespace_content_authority(vault, earlier_episode) -> None:  # noqa: ANN001
    """A caller that cannot read the whole namespace cannot prove which revisions a hidden page cites."""
    from test_curation_execution import _fold_step

    from exomem import curation
    from exomem.governance.principal import RequestPrincipal, request_scope

    pages = earlier_episode(3)
    step = _fold_step(pages, vault)

    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        with pytest.raises(curation.CurationError, match="SOURCE_IDENTITY_FORBIDDEN"):
            curation.propose(vault, _plan(step))


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("different bytes", "CURATION_STEP_INVALID"),
        ("a Source and an Evidence page", "CURATION_STEP_INVALID"),
        ("a protected copy", "CURATION_TARGET_PROTECTED"),
        ("a protected canonical page", "CURATION_TARGET_PROTECTED"),
        ("an append-only page citing the copy", "CURATION_TARGET_PROTECTED"),
    ],
)
def test_duplicate_fold_proposal_refuses_what_could_retire_content_or_rewrite_what_it_must_not(
    vault, owner_scope, setup: str, code: str  # noqa: ANN001
) -> None:
    """A fold names two pages that hold the same bytes, neither raw-protected, and no page it cannot rewrite cites the copy."""
    from duplicate_fixtures import evidence_filing, source_filing
    from test_curation_execution import _fold_duplicate_step

    from exomem import curation

    data = b"\x89PNG photo of the notice"
    canonical = evidence_filing(vault, "Case", "letters", "notice.png", data, protected=setup == "a protected canonical page")
    copy = evidence_filing(
        vault, "Case", "exhibits", "exhibit-a.png", b"\x89PNG another picture" if setup == "different bytes" else data,
        protected=setup == "a protected copy",
    )
    if setup == "a Source and an Evidence page":
        copy = source_filing(vault, "same-bytes", "A captured passage.", original=data)
    if setup == "an append-only page citing the copy":
        source_filing(vault, "citer", f"See [[{copy.removesuffix('.md')}]] for the photo.")

    with pytest.raises(curation.CurationError, match=code):
        curation.propose(vault, _plan(_fold_duplicate_step(vault, canonical, copy)))
