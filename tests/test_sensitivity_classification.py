"""A sensitivity class is metadata beside a kept page, and classifying one rewrites nothing else.

Each test names the failure it catches. The fixture vault is the canonical file-mode vault, and the
document number below is synthetic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client

from exomem import (
    call_ledger,
    classify_page,
    commands,
    page_fields,
    pointer_write,
    sensitivity_classes,
    set_frontmatter_field,
    source_taxonomy,
    writer_lease,
)
from exomem import server as server_module
from exomem.vault import parse_frontmatter
from exomem.vocabulary import registry

SOURCE = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md"


def _overlay(vault: Path, text: str) -> None:
    sensitivity_classes.registry_path(vault).write_text(text, encoding="utf-8")
    registry.invalidate(vault)


def test_an_overlay_adds_a_class_a_capture_may_declare_and_never_replaces_a_shipped_one(
    vault: Path,
) -> None:
    """A vault class that the registry never reads, or an overlay that rewrites a shipped class
    and so changes what every policy naming that class selects."""
    _overlay(vault, "schema_version: 1\nentries:\n  legal:\n    label: Legal\n")
    assert sensitivity_classes.validated(vault, ["legal", "Identity"]) == ("legal", "identity")

    _overlay(vault, "schema_version: 1\nentries:\n  financial:\n    label: Money\n    status: deprecated\n")
    snapshot = registry.load(sensitivity_classes.SPEC, vault)

    assert [finding["code"] for finding in snapshot.findings] == ["invalid_registry_overlay"]
    assert sensitivity_classes.active_keys(vault) == ("identity", "health", "financial")


def test_a_plain_edit_of_a_note_takes_only_registered_classes_as_a_list(vault: Path) -> None:
    """A class written by an ordinary edit as a string or an unknown key, which every reader of
    the field then takes for an unclassified page."""
    note = "Knowledge Base/Notes/card-notes.md"
    (vault / note).parent.mkdir(parents=True, exist_ok=True)
    (vault / note).write_text("---\ntype: insight\ntitle: Card notes\n---\n\n# Card notes\n", encoding="utf-8")

    before = (vault / note).read_bytes()

    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as unknown:
        set_frontmatter_field.set_frontmatter_field(
            vault, path=note, field="sensitivity", value=["not-a-class"], why="tag it")
    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as bare:
        set_frontmatter_field.set_frontmatter_field(vault, path=note, field="sensitivity", value="identity", why="tag it")
    assert (vault / note).read_bytes() == before
    set_frontmatter_field.set_frontmatter_field(vault, path=note, field="sensitivity", value=["Identity"], why="tag it")

    assert unknown.value.code == sensitivity_classes.UNKNOWN_CODE and bare.value.code == "INVALID_SENSITIVITY"
    assert sensitivity_classes.classes_of(parse_frontmatter((vault / note).read_text(encoding="utf-8"))[0]) == ("identity",)


@pytest.fixture
def connector(vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A call to one public tool of a local server, as a connected agent makes it."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    call_ledger.reset_chain_cache()
    mcp = server_module.build_server(require_auth=False)

    def call(tool: str, operation: str, arguments: dict) -> dict:
        async def run():
            async with Client(mcp) as client:
                body = {"operation": operation, "arguments": arguments}
                return (await client.call_tool(tool, body, raise_on_error=False)).structured_content

        return asyncio.run(run())

    yield call
    writer_lease.reset_managers_for_tests()


def test_classifying_a_source_changes_only_its_sensitivity_field_and_a_revert_restores_every_byte(
    vault: Path, connector
) -> None:
    """A connected agent that cannot reach classify, or a classification that moves the page,
    rewrites its body or other frontmatter, or whose revert returns different bytes than the
    page held before."""
    path = vault / SOURCE
    before = path.read_bytes()
    arguments = {"path": SOURCE, "sensitivity": ["identity"], "reason": "an identity card scan"}

    preview = connector("inspect_memory", "files.classify", arguments)
    assert path.read_bytes() == before and preview["validate_only"], preview

    done = connector("manage_memory", "files.classify", arguments)
    after = path.read_text(encoding="utf-8")

    assert done["sensitivity"]["classes"] == ["identity"], done
    assert done["sensitivity"]["basis"] == "declared"
    assert after == before.decode("utf-8").replace(
        "ingested_into: []\n", "ingested_into: []\nsensitivity: [identity]\n"
    )
    assert (vault / done["path"]).exists()

    reverted = connector("manage_memory", "files.classify", {
        "path": SOURCE, "reason": "classified the wrong page", "revert_record": done["pointer_write"],
    })

    assert path.read_bytes() == before
    assert reverted["sensitivity"]["classes"] == []


def test_clearing_a_class_is_a_recorded_change_that_a_revert_undoes(vault: Path) -> None:
    """A clear that deletes the field, which a pointer write cannot revert, so the owner loses
    the class when they reverse a mistaken clear."""
    path = vault / SOURCE
    first = classify_page.classify(vault, path=SOURCE, sensitivity=["health"], reason="a lab result")
    classified = path.read_bytes()
    cleared = classify_page.classify(vault, path=SOURCE, sensitivity=[], reason="it was a newsletter")

    assert cleared["sensitivity"]["classes"] == [] and cleared["sensitivity"]["basis"] == "none"
    classify_page.classify(
        vault, path=SOURCE, reason="the clear was a mistake", revert_record=cleared["pointer_write"]
    )
    assert path.read_bytes() == classified
    assert first["pointer_write"] != cleared["pointer_write"]


def test_an_unknown_class_refuses_naming_the_registered_keys_and_writes_nothing(vault: Path) -> None:
    """A free-text class that lands on a Source, or a refusal that does not tell the agent which
    keys exist."""
    before = (vault / SOURCE).read_bytes()

    with pytest.raises(classify_page.ClassifyError) as refused:
        classify_page.classify(vault, path=SOURCE, sensitivity=["secret-stuff"], reason="guess")

    assert refused.value.code == sensitivity_classes.UNKNOWN_CODE
    assert "identity" in refused.value.reason and "financial" in refused.value.reason
    assert (vault / SOURCE).read_bytes() == before


def test_a_plain_edit_cannot_change_the_class_of_a_source_and_names_the_classify_route(
    vault: Path,
) -> None:
    """A correction that rewrites `sensitivity` on a captured Source without the registry check
    and the reason record of the classify operation."""
    classify_page.classify(vault, path=SOURCE, sensitivity=["identity"], reason="a card")
    before = (vault / SOURCE).read_bytes()

    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as plain:
        set_frontmatter_field.set_frontmatter_field(
            vault, path=SOURCE, field="sensitivity", value=[], why="drop it",
        )
    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as pointer:
        set_frontmatter_field.set_pointer_field(
            vault, path=SOURCE, field="sensitivity", value=[], why="drop it", expected_hash=None,
        )

    assert plain.value.code == "APPEND_ONLY" and "classify" in plain.value.reason
    assert pointer.value.code == "POINTER_FIELD_OWNED" and "classify" in pointer.value.reason
    assert (vault / SOURCE).read_bytes() == before


def test_a_revert_record_of_another_operation_is_not_a_classify_revert(vault: Path) -> None:
    """A classify revert that replays a project correction's record, so one operation undoes
    another's write."""
    path = vault / SOURCE
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "ingested_into: []\n", "ingested_into: []\nproject: harbour-survey\n"
        ),
        encoding="utf-8",
    )
    other = set_frontmatter_field.set_pointer_field(
        vault, path=SOURCE, field="project", value="coastal-survey", why="merged", expected_hash=None,
    )
    before = path.read_bytes()

    with pytest.raises(classify_page.ClassifyError) as refused:
        classify_page.classify(vault, path=SOURCE, reason="undo", revert_record=other.pointer_write)

    assert refused.value.code == "POINTER_RECORD_UNKNOWN"
    assert path.read_bytes() == before
    assert pointer_write.load_record(vault, other.pointer_write)["page"] == SOURCE


def test_the_page_field_registry_declares_sensitivity_as_a_classify_owned_list() -> None:
    """A pack entry whose writer or shape drifts, which would let a plain write change the
    field or store one class as a string."""
    assert page_fields.writers(None)["sensitivity"] == page_fields.WRITER_CLASSIFY
    assert "sensitivity" in page_fields.list_fields(None)


@pytest.mark.usefixtures("owner_scope")
def test_a_source_kind_default_survives_a_schema_save_and_shipped_kinds_declare_none(
    vault: Path,
) -> None:
    """A kind default that a registry save drops or rejects, or a shipped kind that classifies
    every capture of its kind."""
    commands.op_schema_memory(
        vault, subject="source-kinds", operation="save", expected_hash="none", why="id scans",
        proposal={"upsert": {"id-scan": {"label": "ID scan", "attributes": {"sensitivity": ["identity"]}}}},
    )
    taxonomy = source_taxonomy.load_taxonomy(vault)

    assert taxonomy.resolve_kind("id-scan").sensitivity == ("identity",)
    assert not any(definition.sensitivity for definition in source_taxonomy.builtin_kinds().values())
