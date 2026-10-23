"""Page metadata and its new topic keys land in the same governed write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exomem import commands, source_taxonomy
from exomem import schema as schema_module
from exomem import vault as vault_module


def test_remember_registers_topics_in_the_page_batch(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page must not land before its new topic, or split registry writes."""
    registry = source_taxonomy.registry_path(vault)
    batches = []
    real_batch = vault_module.batch_atomic_write

    def record_batch(writes, **kwargs):
        writes = list(writes)
        if any(write.path.name == "catalogue-description.md" for write in writes):
            batches.append([write.path for write in writes])
        return real_batch(writes, **kwargs)

    monkeypatch.setattr(vault_module, "batch_atomic_write", record_batch)
    result = commands.op_remember(
        vault,
        title="Catalogue description",
        content="## Observations\n\n- [finding] The controller keeps the pump stable.\n",
        status="draft",
        summary="A controller finding for the test rig.",
        topics=["health", "rig-control"],
    )
    frontmatter, _, _ = vault_module.parse_frontmatter((vault / result["path"]).read_text())
    assert frontmatter["summary"] == "A controller finding for the test rig."
    assert frontmatter["topics"] == ["health", "rig-control"]
    registered = yaml.safe_load(registry.read_text())["domains"]
    assert "rig-control" in registered
    assert len(batches) == 1
    assert batches[0].count(registry) == 1
    print(json.dumps({"path": result["path"], "summary": frontmatter["summary"],
                      "topics": frontmatter["topics"], "registered": "rig-control" in registered,
                      "page_batches": len(batches), "registry_writes": batches[0].count(registry)}))


def test_invalid_topics_refuse_without_a_page_or_registry_write(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """An invalid topic must not leave a page carrying an unregistered key."""
    before = {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    with pytest.raises(ValueError) as capture_error:
        commands.op_capture_source(
            vault, source_schema, title="Invalid capture topic", content="A test source.",
            source_type="session", domain="!!!",
        )
    with pytest.raises(ValueError) as error:
        commands.op_remember(
            vault, title="Invalid topic", content="A test finding.", status="draft", topics=["!!!"]
        )
    assert str(error.value).startswith("INVALID_SOURCE:")
    assert str(error.value).split(" (missing:")[0] == str(capture_error.value).split(" (missing:")[0]
    assert {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()} == before


def test_edit_memory_registers_a_new_topic(vault: Path) -> None:
    """The frontmatter edit path must register new topics, just as creation does."""
    path = "Knowledge Base/Notes/Insights/catalogue-edit.md"
    (vault / path).write_text(
        "---\ntype: insight\nstatus: draft\n---\n# Catalogue edit\n\nA test finding.\n"
    )
    result = commands.op_edit_memory(
        vault, path=path, why="Declare the finding's topic",
        operation={"kind": "patch_frontmatter", "field": "topics", "value": "rig-telemetry"},
    )
    frontmatter, _, _ = vault_module.parse_frontmatter((vault / path).read_text())
    assert result["new_value"] == frontmatter["topics"] == ["rig-telemetry"]
    assert "rig-telemetry" in yaml.safe_load(source_taxonomy.registry_path(vault).read_text())["domains"]
