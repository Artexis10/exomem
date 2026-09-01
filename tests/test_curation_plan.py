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
    assert (
        curation.operation_id(expected_plan_id, 0, "fix-finnish")
        == hashlib.sha256(f"{expected_plan_id}0fix-finnish".encode()).hexdigest()
    )


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
