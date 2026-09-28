"""Sources and artifact adoption share the Notes domain seam before projection."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest
import yaml

from exomem import add as add_module
from exomem import (
    client_artifacts,
    commands,
    mutation_terminal,
    note,
    vocabulary_resolution,
)
from exomem import reclassify_source as rc
from exomem import schema as schema_module

TODAY = dt.date(2026, 8, 18)
SOURCES = Path("Knowledge Base") / "Sources"


def _registry(vault: Path, text: str) -> None:
    registry = vault / "Knowledge Base" / "_Schema" / "source-taxonomy.yaml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(text, encoding="utf-8")


def _capture(vault: Path, source_schema: schema_module.SourceSchema, **kwargs: object) -> dict:
    kwargs.setdefault("content", "Body text for a captured source.")
    kwargs.setdefault("source_type", "article")
    kwargs.setdefault("url", "https://example.com/article")
    return commands.op_capture_source(vault, source_schema, **kwargs)  # type: ignore[arg-type]


def _front(vault: Path, rel: str) -> dict:
    text = (vault / rel).read_text(encoding="utf-8")
    front, _, _ = text.partition("\n---\n")
    return yaml.safe_load(front.removeprefix("---\n"))


def _children(path: Path) -> list[str]:
    return sorted(child.name for child in path.iterdir() if child.is_dir())


def test_source_capture_reuses_one_legacy_domain_spelling(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    articles = vault / SOURCES / "Articles"
    (articles / "health").mkdir(parents=True)

    captured = _capture(vault, source_schema, title="Sleep trial", domain="Health")

    assert captured["source"]["path"].startswith("Knowledge Base/Sources/Articles/health/")
    assert "Health" not in _children(articles)
    assert _front(vault, captured["source"]["path"])["domain"] == "health"


def test_source_capture_refuses_equivalent_legacy_domain_siblings(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    articles = vault / SOURCES / "Articles"
    (articles / "Health").mkdir(parents=True)
    (articles / "health").mkdir()

    with pytest.raises(ValueError, match="AMBIGUOUS_DOMAIN_DESTINATION"):
        _capture(vault, source_schema, title="Sleep trial", domain="health")

    assert _children(articles) == ["Health", "health"]
    assert list((articles / "Health").iterdir()) == []


def test_source_capture_refuses_alias_owner_chosen_by_entry_order(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    _registry(
        vault,
        "domains:\n"
        "  alpha:\n    aliases: [shared]\n"
        "  beta:\n    aliases: [shared]\n",
    )

    with pytest.raises(ValueError) as refused:
        _capture(vault, source_schema, title="Shared subject", domain="shared")

    message = str(refused.value)
    assert message.startswith("INVALID_DOMAIN_TAXONOMY: duplicate domain alias owner")
    assert "(Knowledge Base/_Schema/source-taxonomy.yaml)" in message
    assert "capture without `domain`" in message

    assert not (vault / SOURCES / "Articles" / "Beta").exists()


def test_malformed_registry_refuses_a_domain_but_not_a_domainless_capture(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    _registry(vault, "domains:\n  health:\n    aliases: 123\n")

    with pytest.raises(ValueError) as refused:
        _capture(vault, source_schema, title="Sleep trial", domain="health")
    message = str(refused.value)
    assert message.startswith("INVALID_DOMAIN_TAXONOMY: domain taxonomy is malformed")
    assert "(Knowledge Base/_Schema/source-taxonomy.yaml)" in message
    assert "capture without `domain` to preserve the material now" in message
    assert not (vault / SOURCES / "Articles" / "Health").exists()

    captured = _capture(vault, source_schema, title="Sleep trial")
    assert captured["source"]["path"].startswith("Knowledge Base/Sources/Articles/")


def test_a_registry_that_declares_no_domain_section_is_not_malformed(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    _registry(vault, "source_kinds:\n  field-notebook:\n    path_label: Field Notes\n")

    captured = _capture(vault, source_schema, title="Kelp survey", domain="marine-biology")

    assert captured["source"]["path"].startswith(
        "Knowledge Base/Sources/Articles/Marine Biology/"
    )


def test_a_failed_capture_leaves_no_domain_directory(
    vault: Path, source_schema: schema_module.SourceSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic commit refusal")

    monkeypatch.setattr(add_module, "batch_atomic_write", refused)

    with pytest.raises(RuntimeError, match="synthetic commit refusal"):
        add_module.add(
            vault,
            source_schema,
            content="Body.",
            title="Kelp survey",
            source_type="research-report",
            domain="marine-biology",
            today=TODAY,
        )

    assert not (vault / SOURCES / "Reports").exists()


def test_a_registry_change_after_resolution_refuses_the_commit(
    vault: Path, source_schema: schema_module.SourceSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(vault, "domains:\n  health:\n    aliases: [wellness]\n")
    resolve = vocabulary_resolution.resolve_source_domain

    def resolve_then_relabel(*args: object, **kwargs: object):  # noqa: ANN202
        binding = resolve(*args, **kwargs)
        _registry(vault, "domains:\n  health:\n    aliases: [wellness]\n    path_label: Wellbeing\n")
        return binding

    monkeypatch.setattr(vocabulary_resolution, "resolve_source_domain", resolve_then_relabel)

    with pytest.raises(ValueError, match="PATH_GUARD"):
        _capture(vault, source_schema, title="Sleep trial", domain="wellness")

    assert not (vault / SOURCES / "Articles" / "Health").exists()


def test_source_and_notes_resolve_an_alias_to_one_identity_record(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    _registry(vault, "domains:\n  health:\n    aliases: [wellness]\n")

    captured = _capture(vault, source_schema, title="Sleep trial", domain="wellness")
    experiment = note.note(
        vault,
        content="# Trial\n\n## Hypothesis\n\nA bounded trial.\n",
        note_type="experiment",
        title="Sleep trial",
        domain="wellness",
        started="2026-05-18",
        duration="one day",
        status="draft",
        today=dt.date(2026, 5, 18),
        validate_only=True,
    ).as_dict()["vocabulary_resolution"]

    source = captured["source"]
    assert source["path"].startswith("Knowledge Base/Sources/Articles/Health/")
    assert _front(vault, source["path"])["domain"] == "health"
    assert source["vocabulary_resolution"] == experiment
    assert experiment == {
        "family": "domain",
        "requested": "wellness",
        "canonical": "health",
        "destination": "Health",
        "match_kind": "alias",
        "snapshot": experiment["snapshot"],
    }
    assert captured["vocabulary_resolution"] == source["vocabulary_resolution"]


def test_source_capture_receipt_survives_the_compact_terminal(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Sleep trial", domain="Health")
    terminal = mutation_terminal.committed_terminal(
        captured, request_id="r-1", receipt_id="rc-1", idempotency_key=None
    )

    resolution = mutation_terminal.project_terminal(terminal)["vocabulary_resolution"]

    assert resolution["family"] == "domain"
    assert (resolution["requested"], resolution["canonical"]) == ("Health", "health")
    assert resolution["destination"] == "Health"
    assert resolution["match_kind"] == "normalized"


def test_domainless_capture_carries_no_vocabulary_resolution(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Sleep trial")

    assert "vocabulary_resolution" not in captured
    assert "vocabulary_resolution" not in captured["source"]


def _stage(tmp_path: Path, file_id: str, filename: str, payload: bytes):  # noqa: ANN202
    def stage(_file, _budget, **_kwargs):  # noqa: ANN001, ANN202
        staged = tmp_path / f"stage-{file_id}-{filename}"
        staged.write_bytes(payload)
        return client_artifacts.StagedArtifact(
            file_id=file_id,
            path=staged,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            content_type="application/octet-stream",
            filename=filename,
        )

    return stage


def test_adoption_resolves_the_legacy_spelling_it_commits_into(
    vault: Path,
    source_schema: schema_module.SourceSchema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = vault / SOURCES / "Reports"
    (reports / "health").mkdir(parents=True)
    monkeypatch.setattr(
        client_artifacts, "stage_artifact", _stage(tmp_path, "file-b", "b.bin", b"selected")
    )

    result = client_artifacts.capture_source_artifacts(
        vault,
        source_schema=source_schema,
        title="Sleep export",
        source_type="research-report",
        domain="Health",
        files=[{"download_url": "https://files.example/b", "file_id": "file-b"}],
        adoption={"key": "legacy-domain", "trigger": "selected", "selected_file_id": "file-b"},
    )

    row = result["files"][0]
    assert row["outcome"] == "stored", row
    assert row["adoption"]["destination"] == "Knowledge Base/Sources/Reports/health"
    assert _children(reports) == ["health"]
    assert result["vocabulary_resolution"]["canonical"] == "health"


def test_adoption_refuses_an_ambiguous_domain_before_fetch(
    vault: Path,
    source_schema: schema_module.SourceSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = vault / SOURCES / "Reports"
    (reports / "Health").mkdir(parents=True)
    (reports / "health").mkdir()
    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda *_args, **_kwargs: pytest.fail("an ambiguous destination must not fetch"),
    )

    result = client_artifacts.capture_source_artifacts(
        vault,
        source_schema=source_schema,
        title="Sleep export",
        source_type="research-report",
        domain="health",
        files=[{"download_url": "https://files.example/b", "file_id": "file-b"}],
        adoption={"key": "ambiguous-domain", "trigger": "selected", "selected_file_id": "file-b"},
    )

    assert result["files"][0]["code"] == "AMBIGUOUS_DOMAIN_DESTINATION"
    assert "capture without `domain`" in result["files"][0]["reason"]


def test_reclassify_keeps_a_source_in_its_legacy_domain_spelling(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    articles = vault / SOURCES / "Articles"
    (articles / "health").mkdir(parents=True)
    captured = _capture(vault, source_schema, title="Sleep trial", domain="health")

    result = rc.reclassify(
        vault,
        path=captured["source"]["path"],
        domain="Health",
        reason="restating the same subject",
        today=TODAY,
    )

    assert result.relocated is False
    assert result.new_path == captured["source"]["path"]
    assert _children(articles) == ["health"]


def test_reclassify_into_a_domain_reuses_its_legacy_spelling(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    (vault / SOURCES / "Reports" / "health").mkdir(parents=True)
    captured = _capture(vault, source_schema, title="Sleep trial", domain="travel")

    preview = rc.propose(vault, captured["source"]["path"], source_kind="research-report", domain="Health")
    result = rc.reclassify(
        vault,
        path=captured["source"]["path"],
        source_kind="research-report",
        domain="Health",
        reason="it is a written investigation about health",
        today=TODAY,
    )

    assert preview.destination == result.new_path
    assert result.new_path.startswith("Knowledge Base/Sources/Reports/health/")
    assert _children(vault / SOURCES / "Reports") == ["health"]


def test_evidence_scope_never_inherits_domain_aliases_or_slug_folding(
    vault: Path,
) -> None:
    _registry(vault, "domains:\n  health:\n    aliases: [inc-42, ops]\n")

    upper = commands.op_preserve(
        vault, content="first", filename="a.txt", scope="INC-42", category="Ops"
    )
    lower = commands.op_preserve(
        vault, content="second", filename="b.txt", scope="inc-42", category="ops"
    )

    assert upper["path"].startswith("Knowledge Base/Evidence/INC-42/Ops/")
    assert lower["path"].startswith("Knowledge Base/Evidence/inc-42/ops/")
    assert not (vault / "Knowledge Base" / "Evidence" / "Health").exists()
    assert not (vault / "Knowledge Base" / "Evidence" / "health").exists()
    assert "vocabulary_resolution" not in upper


def test_a_kind_only_reclassify_moves_a_domain_source_despite_a_malformed_registry(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """The caller supplies no domain, so a registry fault refuses nothing here.

    Only operations that supply a domain are refused; the source keeps the
    domain it already carries and moves with its corrected kind.
    """
    captured = _capture(vault, source_schema, title="Sleep trial", domain="health")
    _registry(vault, "domains:\n  health:\n    aliases: 123\n")

    preview = rc.propose(vault, captured["source"]["path"], source_kind="research-report")
    result = rc.reclassify(
        vault,
        path=captured["source"]["path"],
        source_kind="research-report",
        reason="it is a written investigation",
        today=TODAY,
    )

    assert result.relocated is True
    assert result.new_path.startswith("Knowledge Base/Sources/Reports/Health/")
    assert preview.destination == result.new_path


def test_a_reclassify_that_supplies_a_domain_names_the_malformed_registry(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Sleep trial", domain="health")
    _registry(vault, "domains:\n  health:\n    aliases: 123\n")

    with pytest.raises(rc.ReclassifyError) as refused:
        rc.reclassify(
            vault,
            path=captured["source"]["path"],
            domain="travel",
            reason="it is about a trip",
            today=TODAY,
        )

    assert refused.value.code == "INVALID_DOMAIN_TAXONOMY"
    assert "(Knowledge Base/_Schema/source-taxonomy.yaml)" in str(refused.value)
