"""Notes experiment domain vocabulary is resolved before its destination."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from exomem import commands, mutation_terminal, note, semantic_writes, vocabulary_resolution
from exomem import vault as vault_module


def test_experiment_validation_reuses_canonical_domain_projection(vault: Path) -> None:
    result = note.note(
        vault,
        content="# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        note_type="experiment",
        title="Canonical health destination",
        domain="health",
        started="2026-05-18",
        duration="one day",
        status="draft",
        today=dt.date(2026, 5, 18),
        validate_only=True,
    )

    assert result.destination.startswith("Knowledge Base/Notes/Experiments/Health/")
    assert result.as_dict()["vocabulary_resolution"] == {
        "family": "domain",
        "requested": "health",
        "canonical": "health",
        "destination": "Health",
        "match_kind": "exact",
        "snapshot": result.as_dict()["vocabulary_resolution"]["snapshot"],
    }


def test_domain_binding_reuses_one_legacy_projection_spelling(vault: Path) -> None:
    parent = vault / "Knowledge Base" / "Notes" / "Experiments"
    (parent / "health").mkdir(parents=True)

    binding = vocabulary_resolution.resolve_notes_domain(vault, "Health")

    assert binding.canonical == "health"
    assert binding.destination == "health"
    assert binding.as_dict()["requested"] == "Health"


def test_domain_binding_reuses_a_registered_alias_legacy_projection(vault: Path) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text("domains:\n  health:\n    aliases: [wellness]\n", encoding="utf-8")
    parent = vault / "Knowledge Base" / "Notes" / "Experiments"
    (parent / "Wellness").mkdir(parents=True)

    binding = vocabulary_resolution.resolve_notes_domain(vault, "wellness")

    assert (binding.canonical, binding.destination, binding.match_kind) == (
        "health",
        "Wellness",
        "alias",
    )


def test_domain_binding_refuses_canonical_and_alias_legacy_siblings(vault: Path) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text("domains:\n  health:\n    aliases: [wellness]\n", encoding="utf-8")
    parent = vault / "Knowledge Base" / "Notes" / "Experiments"
    (parent / "Health").mkdir(parents=True)
    (parent / "Wellness").mkdir()

    with pytest.raises(vocabulary_resolution.VocabularyResolutionError) as error:
        vocabulary_resolution.resolve_notes_domain(vault, "wellness")

    assert error.value.code == "AMBIGUOUS_DOMAIN_DESTINATION"


def test_domain_binding_refuses_equivalent_legacy_siblings(vault: Path) -> None:
    parent = vault / "Knowledge Base" / "Notes" / "Experiments"
    (parent / "Health").mkdir(parents=True)
    (parent / "health").mkdir()

    with pytest.raises(vocabulary_resolution.VocabularyResolutionError) as error:
        vocabulary_resolution.resolve_notes_domain(vault, "health")

    assert error.value.code == "AMBIGUOUS_DOMAIN_DESTINATION"


def test_domain_binding_refuses_duplicate_alias_owners(vault: Path) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text(
        "domains:\n"
        "  alpha:\n"
        "    aliases: [shared]\n"
        "  beta:\n"
        "    aliases: [shared]\n",
        encoding="utf-8",
    )

    with pytest.raises(vocabulary_resolution.VocabularyResolutionError) as error:
        vocabulary_resolution.resolve_notes_domain(vault, "shared")

    assert error.value.code == "INVALID_DOMAIN_TAXONOMY"


def test_domain_binding_refuses_alias_that_claims_a_builtin_canonical(vault: Path) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text("domains:\n  distinct:\n    aliases: [health]\n", encoding="utf-8")

    with pytest.raises(vocabulary_resolution.VocabularyResolutionError) as error:
        vocabulary_resolution.resolve_notes_domain(vault, "health")

    assert error.value.code == "INVALID_DOMAIN_TAXONOMY"


def test_domain_binding_refuses_malformed_registry(vault: Path) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text("domains: [not-a-mapping]\n", encoding="utf-8")

    with pytest.raises(vocabulary_resolution.VocabularyResolutionError) as error:
        vocabulary_resolution.resolve_notes_domain(vault, "health")

    assert error.value.code == "INVALID_DOMAIN_TAXONOMY"


@pytest.mark.parametrize(
    "contents",
    (
        "[]\n",
        "domains:\n  health:\n    path_label: Health\n  health:\n    path_label: Wellness\n",
        "domains:\n  health:\n    aliases: 123\n",
    ),
)
def test_domain_binding_refuses_strict_registry_shape_and_duplicate_keys(
    vault: Path, contents: str
) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text(contents, encoding="utf-8")

    with pytest.raises(vocabulary_resolution.VocabularyResolutionError) as error:
        vocabulary_resolution.resolve_notes_domain(vault, "health")

    assert error.value.code == "INVALID_DOMAIN_TAXONOMY"


def test_domain_binding_refuses_an_unreadable_registry(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text("domains: {}\n", encoding="utf-8")

    def unreadable(*_args, **_kwargs):  # noqa: ANN001
        raise OSError("synthetic unreadable registry")

    monkeypatch.setattr(vault_module, "read_guarded_text", unreadable)
    with pytest.raises(vocabulary_resolution.VocabularyResolutionError) as error:
        vocabulary_resolution.resolve_notes_domain(vault, "health")

    assert error.value.code == "INVALID_DOMAIN_TAXONOMY"
    assert error.value.reason == "domain taxonomy is unreadable"


def test_domain_binding_normalizes_unicode_to_the_canonical_projection(vault: Path) -> None:
    binding = vocabulary_resolution.resolve_notes_domain(vault, "Ｆｏｏｄ")

    assert binding.canonical == "food"
    assert binding.destination == "Food"
    assert binding.match_kind == "normalized"


def test_domain_binding_reuses_food_and_a_reviewed_registry_alias(vault: Path) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text(
        "domains:\n"
        "  wellbeing:\n"
        "    aliases: [well-being]\n"
        "    path_label: Wellbeing\n",
        encoding="utf-8",
    )

    food = vocabulary_resolution.resolve_notes_domain(vault, "Food")
    alias = vocabulary_resolution.resolve_notes_domain(vault, "well-being")

    assert (food.canonical, food.destination) == ("food", "Food")
    assert (alias.canonical, alias.destination, alias.match_kind) == (
        "wellbeing",
        "Wellbeing",
        "alias",
    )


def test_experiment_write_preserves_registered_domain_path_label(vault: Path) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text(
        "domains:\n"
        "  health:\n"
        "    path_label: Clinical Hälsa\n",
        encoding="utf-8",
    )

    result = note.note(
        vault,
        content="# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        note_type="experiment",
        title="Registered label trial",
        domain="Health",
        started="2026-05-18",
        duration="one day",
        status="draft",
        today=dt.date(2026, 5, 18),
    )

    assert "/Clinical Hälsa/" in result.path
    assert (vault / result.path).is_file()
    assert not (vault / "Knowledge Base" / "Notes" / "Experiments" / "ClinicalHlsa").exists()
    assert "experiment, health," in (vault / "Knowledge Base" / "index.md").read_text(encoding="utf-8")
    assert "scope=domain=health" in (vault / "Knowledge Base" / "log.md").read_text(encoding="utf-8")


def test_nearby_domain_requires_evidence_bound_decision(vault: Path) -> None:
    with pytest.raises(vocabulary_resolution.VocabularyResolutionError) as error:
        vocabulary_resolution.resolve_notes_domain(vault, "wealth")

    preparation = error.value.details["vocabulary_preparation"]
    binding = vocabulary_resolution.resolve_notes_domain(
        vault,
        "wealth",
        decision={
            "evidence_fingerprint": preparation["evidence_fingerprint"],
            "outcome": "create",
            "canonical": "wealth",
        },
    )

    assert binding.canonical == "wealth"
    assert binding.match_kind == "decision-create"


def test_experiment_preparation_is_non_mutating_and_supports_all_decisions(vault: Path) -> None:
    kwargs = {
        "content": "# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        "note_type": "experiment",
        "title": "Prepared neighbouring trial",
        "domain": "wealth",
        "started": "2026-05-18",
        "duration": "one day",
        "status": "draft",
        "today": dt.date(2026, 5, 18),
        "validate_only": True,
    }
    preparation = note.note(vault, **kwargs)
    payload = preparation.as_dict()
    assert payload["mutated"] is False
    assert payload["vocabulary_decision"] == "required"
    assert "destination" not in payload and "draft_token" not in payload

    decision = {
        "evidence_fingerprint": payload["vocabulary_preparation"]["evidence_fingerprint"],
        "outcome": "create",
        "canonical": "wealth",
    }
    validation = note.note(vault, vocabulary_decision=decision, **kwargs)
    assert validation.as_dict()["vocabulary_resolution"]["canonical"] == "wealth"

    deferred = note.note(
        vault,
        vocabulary_decision={**decision, "outcome": "defer", "canonical": None},
        **kwargs,
    )
    assert deferred.as_dict() == {
        "mutated": False,
        "vocabulary_preparation": {"family": "domain", "requested": "wealth"},
        "vocabulary_decision": "deferred",
    }


def test_definition_overlap_requires_a_pre_destination_decision(vault: Path) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text(
        "domains:\n"
        "  development:\n"
        "    description: Software engineering practice and delivery work\n",
        encoding="utf-8",
    )

    with pytest.raises(vocabulary_resolution.VocabularyResolutionError) as error:
        vocabulary_resolution.resolve_notes_domain(vault, "software-engineering")

    assert error.value.code == "VOCABULARY_DECISION_REQUIRED"
    assert error.value.details["vocabulary_preparation"]["candidates"][0]["canonical"] == "development"


def test_experiment_draft_refuses_registry_change_before_commit(vault: Path) -> None:
    kwargs = {
        "content": "# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        "note_type": "experiment",
        "title": "Registry stale trial",
        "domain": "health",
        "started": "2026-05-18",
        "duration": "one day",
        "status": "draft",
        "today": dt.date(2026, 5, 18),
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text("domains:\n  health:\n    path_label: Health\n", encoding="utf-8")

    with pytest.raises(note.NoteError) as error:
        note.note(
            vault,
            draft_id=validation.draft_id,
            draft_hash=validation.draft_hash,
            draft_token=validation.draft_token,
            **kwargs,
        )

    assert error.value.code == "STALE_VOCABULARY_BINDING"


def test_legacy_experiment_draft_token_requires_fresh_vocabulary_validation(vault: Path) -> None:
    kwargs = {
        "content": "# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        "note_type": "experiment",
        "title": "Legacy vocabulary token",
        "domain": "health",
        "started": "2026-05-18",
        "duration": "one day",
        "status": "draft",
        "today": dt.date(2026, 5, 18),
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    current = semantic_writes.DraftToken.decode(validation.draft_token)
    legacy_token = semantic_writes.DraftToken(
        current.writer,
        current.operation,
        current.destination,
        current.render_date,
        current.registrations,
        render_stamp=current.render_stamp,
        version=2,
    ).encode()

    with pytest.raises(note.NoteError) as error:
        note.note(vault, draft_token=legacy_token, **kwargs)

    assert error.value.code == "STALE_VOCABULARY_BINDING"
    assert not (vault / validation.destination).exists()


def test_experiment_draft_refuses_new_equivalent_folder_before_commit(vault: Path) -> None:
    kwargs = {
        "content": "# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        "note_type": "experiment",
        "title": "Directory stale trial",
        "domain": "health",
        "started": "2026-05-18",
        "duration": "one day",
        "status": "draft",
        "today": dt.date(2026, 5, 18),
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    (vault / "Knowledge Base" / "Notes" / "Experiments" / "health").mkdir(parents=True)

    with pytest.raises(note.NoteError) as error:
        note.note(
            vault,
            draft_id=validation.draft_id,
            draft_hash=validation.draft_hash,
            draft_token=validation.draft_token,
            **kwargs,
        )

    assert error.value.code == "STALE_VOCABULARY_BINDING"


def test_relation_reviewed_experiment_commits_canonical_domain_metadata(vault: Path) -> None:
    kwargs = {
        "content": "# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        "note_type": "experiment",
        "title": "Reviewed canonical trial",
        "domain": "Health",
        "started": "2026-05-18",
        "duration": "one day",
        "today": dt.date(2026, 5, 18),
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    assert validation.applicability == "full"

    result = note.note(
        vault,
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No honest relation exists in this fixture corpus.",
        **kwargs,
    )

    text = (vault / result.path).read_text(encoding="utf-8")
    assert "domain: health" in text
    assert result.as_dict()["vocabulary_resolution"]["canonical"] == "health"


@pytest.mark.parametrize("status", ["draft", "active"])
def test_domain_guard_refuses_registry_race_in_both_creation_branches(
    vault: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    kwargs = {
        "content": "# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        "note_type": "experiment",
        "title": f"{status} guard race",
        "domain": "health",
        "started": "2026-05-18",
        "duration": "one day",
        "status": status,
        "today": dt.date(2026, 5, 18),
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    real_batch = vault_module.batch_atomic_write
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)

    def change_registry_then_write(*args, **kwargs):
        registry.write_text("domains:\n  health:\n    path_label: Health\n", encoding="utf-8")
        return real_batch(*args, **kwargs)

    monkeypatch.setattr(vault_module, "batch_atomic_write", change_registry_then_write)
    commit = {
        "draft_id": validation.draft_id,
        "draft_hash": validation.draft_hash,
        "draft_token": validation.draft_token,
    }
    if status == "active":
        commit.update(
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No honest relation exists in this fixture corpus.",
        )

    with pytest.raises(note.NoteError) as error:
        note.note(vault, **commit, **kwargs)

    assert error.value.code == "STALE_VOCABULARY_BINDING"


def test_remember_validation_projects_the_same_domain_resolution(vault: Path) -> None:
    result = commands.op_remember(
        vault,
        content="# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        title="Remembered canonical health destination",
        note_type="experiment",
        domain="Health",
        started="2026-05-18",
        duration="one day",
        status="draft",
        validate_only=True,
    )

    assert result["vocabulary_resolution"]["requested"] == "Health"
    assert result["vocabulary_resolution"]["canonical"] == "health"
    assert result["vocabulary_resolution"]["destination"] == "Health"


def test_compact_terminal_keeps_valid_vocabulary_resolution() -> None:
    resolution = {
        "family": "domain",
        "requested": "Health",
        "canonical": "health",
        "destination": "Health",
        "match_kind": "normalized",
        "snapshot": "a" * 64,
    }
    terminal = mutation_terminal.committed_terminal(
        {
            "mutated": True,
            "draft_id": "01JAAAAAAAAAAAAAAAAAAAAAAA",
            "path": "Knowledge Base/Notes/Experiments/Health/trial.md",
            "vocabulary_resolution": resolution,
        },
        request_id="r-1",
        receipt_id="rc-1",
        idempotency_key=None,
    )

    assert mutation_terminal.project_terminal(terminal)["vocabulary_resolution"] == resolution

    # Portable receipt recovery intentionally removes the leaf. Its validated
    # terminal projection must still retain the bound identity.
    terminal["vocabulary_resolution"] = resolution
    terminal["leaf_result"] = {}
    assert mutation_terminal.project_terminal(terminal)["vocabulary_resolution"] == resolution
    assert mutation_terminal.project_terminal(terminal, "full")["vocabulary_resolution"] == resolution


def test_vocabulary_preparation_bounds_candidate_display_and_discloses_omissions(vault: Path) -> None:
    registry = vault / "Knowledge Base/_Schema/source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    entries = "\n".join(
        f"  candidate-{index}:\n"
        f"    label: {'L' * 500}\n"
        f"    description: wealth {'x' * 50_000}\n"
        for index in range(10)
    )
    registry.write_text(f"domains:\n{entries}", encoding="utf-8")

    with pytest.raises(vocabulary_resolution.VocabularyResolutionError) as error:
        vocabulary_resolution.resolve_notes_domain(vault, "wealth")

    evidence = error.value.details["vocabulary_preparation"]
    assert len(evidence["candidates"]) == 8
    assert evidence["candidate_truncation"]["disclosed"] is True
    assert evidence["candidate_truncation"]["omitted"] >= 2
    assert all(len(candidate["label"]) <= 256 for candidate in evidence["candidates"])
    assert all(len(candidate["description"]) <= 1024 for candidate in evidence["candidates"])
    assert all(candidate["truncated"] == {"label": True, "description": True} for candidate in evidence["candidates"])
    assert len(json.dumps(evidence, ensure_ascii=False).encode("utf-8")) < 16_000
    assert evidence["evidence_fingerprint"] == vocabulary_resolution._snapshot(
        {key: value for key, value in evidence.items() if key != "evidence_fingerprint"}
    )


def test_vocabulary_binding_stays_within_public_receipt_bounds(vault: Path) -> None:
    binding = vocabulary_resolution.resolve_notes_domain(vault, "health")
    assert vocabulary_resolution.valid_public_resolution(binding.as_dict())

    with pytest.raises(vocabulary_resolution.VocabularyResolutionError, match="bounded receipt"):
        vocabulary_resolution.resolve_notes_domain(vault, " " * 300 + "health")

    registry = vault / "Knowledge Base/_Schema/source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text(
        "domains:\n  health:\n    path_label: " + "H" * 300 + "\n",
        encoding="utf-8",
    )
    with pytest.raises(vocabulary_resolution.VocabularyResolutionError, match="path_label exceeds"):
        vocabulary_resolution.resolve_notes_domain(vault, "health")


def test_crafted_unbounded_vocabulary_token_is_rejected_before_note_validation_or_commit(
    vault: Path,
) -> None:
    kwargs = {
        "content": "# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        "note_type": "experiment",
        "title": "Crafted bound refusal",
        "domain": "health",
        "started": "2026-05-18",
        "duration": "one day",
        "status": "draft",
    }
    validation = commands.op_remember(vault, validate_only=True, **kwargs)
    token = semantic_writes.DraftToken.decode(validation["draft_token"])
    assert token.vocabulary_binding is not None
    crafted_binding = {**token.vocabulary_binding, "requested": " " * 300 + "health"}
    crafted = semantic_writes.DraftToken(
        token.writer,
        token.operation,
        token.destination,
        token.render_date,
        token.registrations,
        render_stamp=token.render_stamp,
        vocabulary_binding=crafted_binding,
    ).encode()

    for validate_only in (True, False):
        with pytest.raises(ValueError, match="INVALID_DRAFT_TOKEN"):
            commands.op_remember(
                vault,
                validate_only=validate_only,
                draft_id=validation["draft_id"],
                draft_hash=validation["draft_hash"],
                draft_token=crafted,
                **kwargs,
            )
    assert not (vault / validation["destination"]).exists()


@pytest.mark.parametrize("path_label", ["H" * 256, "é" * 128])
def test_unrepresentable_domain_path_label_refuses_before_draft_or_commit(
    vault: Path, path_label: str
) -> None:
    registry = vault / "Knowledge Base/_Schema/source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text(
        f"domains:\n  health:\n    path_label: {path_label}\n", encoding="utf-8"
    )
    kwargs = {
        "content": "# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        "note_type": "experiment",
        "title": "Path projection refusal",
        "domain": "health",
        "started": "2026-05-18",
        "duration": "one day",
        "status": "draft",
    }

    with pytest.raises(ValueError, match="portable component limit"):
        commands.op_remember(vault, validate_only=True, **kwargs)
    assert not (vault / "Knowledge Base/Notes/Experiments").exists()


def test_representable_unicode_domain_path_label_validates_and_commits(vault: Path) -> None:
    path_label = "é" * 127
    registry = vault / "Knowledge Base/_Schema/source-taxonomy.yaml"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text(
        f"domains:\n  health:\n    path_label: {path_label}\n", encoding="utf-8"
    )
    kwargs = {
        "content": "# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        "note_type": "experiment",
        "title": "Path projection boundary",
        "domain": "health",
        "started": "2026-05-18",
        "duration": "one day",
        "status": "draft",
    }
    validation = commands.op_remember(vault, validate_only=True, **kwargs)
    committed = commands.op_remember(
        vault,
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        **kwargs,
    )

    assert committed["vocabulary_resolution"]["destination"] == path_label
    assert (vault / committed["path"]).is_file()
