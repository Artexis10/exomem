"""`init` — bootstrap a fresh Knowledge Base scaffold into an empty vault.

A friend with no KB needs the three load-bearing files (index.md, log.md,
_Schema/SKILL.md) to exist before the writers work. `init_vault` lays down the
whole Lovelace-style structure in one shot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import activation_manifest
from exomem import init as init_module
from exomem import vault as vault_module


def test_init_scaffolds_a_fresh_vault(tmp_path: Path) -> None:
    report = init_module.init_vault(tmp_path)
    kb = tmp_path / "Knowledge Base"

    # The three load-bearing files exist.
    assert (kb / "index.md").exists()
    assert (kb / "log.md").exists()
    # The shipped contract and the skill registry moved out of the note
    # namespace (#488); asserted at the new location, not merely dropped.
    shipped = vault_module.shipped_schema_target(tmp_path)
    assert (shipped / "SKILL.md").exists()
    assert (shipped / "workflow-skills" / "index.yaml").exists()
    assert not (kb / "_Schema" / "SKILL.md").exists()
    semantic_registry = kb / "_Schema" / "semantic-language-registry.yaml"
    assert semantic_registry.exists()
    assert semantic_registry.read_text(encoding="utf-8") == (
        "schema_version: 1\ncategories: {}\nkinds: {}\n"
    )
    assert (shipped / "workflow-skills" / "exomem-capture" / "SKILL.md").exists()

    # log.md carries the `---` separator the writers prepend after.
    assert "---" in (kb / "log.md").read_text(encoding="utf-8")

    # The typed folder tree is laid down.
    assert (kb / "Sources").is_dir()
    assert (kb / "Notes" / "Insights").is_dir()
    assert (kb / "Entities" / "Concepts").is_dir()
    assert (kb / "Entities" / "Organizations").is_dir()
    assert (kb / "Evidence").is_dir()

    # The report names what it created.
    assert report["vault"] == str(tmp_path)
    assert any("index.md" in p for p in report["created"])
    manifest = activation_manifest.load_manifest(tmp_path)
    assert manifest.pages == ()
    later = kb / "Notes/Insights/first-later-page.md"
    later.write_text("---\ntype: insight\nstatus: active\n---\n\n# Later\n", encoding="utf-8")
    assert not activation_manifest.is_grandfathered(tmp_path, later, manifest=manifest)


def test_force_init_snapshots_existing_compiled_pages_once_without_editing_them(
    tmp_path: Path,
) -> None:
    page = tmp_path / "Knowledge Base/Notes/Insights/existing.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: insight\nstatus: active\n---\n\n# Existing\n", encoding="utf-8")
    before = page.read_bytes()

    init_module.init_vault(tmp_path, force=True)
    manifest_path = activation_manifest.manifest_path(tmp_path)
    first_bytes = manifest_path.read_bytes()
    loaded = activation_manifest.load_manifest(tmp_path)
    assert loaded is not None
    assert [entry.path_at_activation for entry in loaded.pages] == [
        "Knowledge Base/Notes/Insights/existing.md"
    ]
    assert page.read_bytes() == before
    assert not (tmp_path / "Knowledge Base/.review-state.json").exists()
    assert "exomem_id:" not in page.read_text(encoding="utf-8")

    init_module.init_vault(tmp_path, force=True)
    assert manifest_path.read_bytes() == first_bytes
    assert page.read_bytes() == before


def test_force_init_reconciles_organizations_without_overwriting_entity_index(
    tmp_path: Path,
) -> None:
    init_module.init_vault(tmp_path)
    entity_index = tmp_path / "Knowledge Base" / "Entities" / "index.md"
    entity_index.write_text(
        "# Entity Catalog\n\n"
        "Custom operator prose.\n\n"
        "## By type\n\n"
        "- [[Knowledge Base/Entities/People/|People]] (0) — curated\n\n"
        "## Recent\n\n"
        "Keep this section.\n",
        encoding="utf-8",
    )

    init_module.init_vault(tmp_path, force=True)

    text = entity_index.read_text(encoding="utf-8")
    assert "# Entity Catalog" in text
    assert "Custom operator prose." in text
    assert "People]] (0) — curated" in text
    assert "## Recent\n\nKeep this section." in text
    assert "Entities/Organizations/|Organizations]] (0)" in text


def test_init_refuses_existing_kb_without_force(tmp_path: Path) -> None:
    (tmp_path / "Knowledge Base").mkdir()
    with pytest.raises(FileExistsError):
        init_module.init_vault(tmp_path)


def test_init_makes_a_resolvable_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After init, resolve_vault() finds it via EXOMEM_VAULT_PATH."""
    init_module.init_vault(tmp_path)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    assert vault_module.resolve_vault() == tmp_path


def test_init_via_cli(tmp_path: Path) -> None:
    """`python -m exomem init --vault <path>` scaffolds and returns 0;
    a second run refuses (returns 1)."""
    from exomem.__main__ import main

    assert main(["init", "--vault", str(tmp_path)]) == 0
    assert (vault_module.shipped_schema_target(tmp_path) / "SKILL.md").exists()
    # idempotency guard: second run refuses without --force.
    assert main(["init", "--vault", str(tmp_path)]) == 1


def test_init_vault_accepts_writes_and_stays_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a freshly-init'd vault accepts an `add` and audits clean —
    proves the scaffold ships the sub-indexes (Sources/Notes/Entities/index.md)
    the writers require, not just the folders."""
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    import datetime as dt

    from exomem import add as add_module
    from exomem import audit as audit_module
    from exomem import schema

    init_module.init_vault(tmp_path)
    ss = schema.load_source_schema(tmp_path)
    add_module.add(
        tmp_path,
        ss,
        content="A capture.",
        source_type="article",
        title="First Source",
        url="https://example.com",
        today=dt.date(2026, 5, 31),
    )

    kb = tmp_path / "Knowledge Base"
    new_sources = [p for p in (kb / "Sources").rglob("*.md") if p.name != "index.md"]
    assert new_sources, "the added source should be on disk"
    assert "## [2026-05-31] add" in (kb / "log.md").read_text(encoding="utf-8")
    report = audit_module.audit(tmp_path, categories=["broken_wikilink", "index_drift"])
    assert not report.findings, [f.as_dict() for f in report.findings]


# --- #488: the vault copy of the shipped contract must not freeze ------------


def _shipped(kb: Path) -> Path:
    # `kb` is the Knowledge Base; the shipped contract now sits beside the vault
    # root rather than inside it (#488).
    return vault_module.shipped_schema_target(kb.parent) / "SKILL.md"


def test_refresh_rewrites_a_stale_shipped_doc(tmp_path: Path) -> None:
    """install-skill redeploys its copy every upgrade; init is skipped once the
    KB exists, so the vault copy froze at the version that created it. Both are
    live — product_invoke resolves the vault copy, agents read the deployed one."""
    from exomem import init as init_module

    vault = tmp_path / "vault"
    vault.mkdir()
    init_module.init_vault(vault)
    kb = vault / "Knowledge Base"
    _shipped(kb).write_text("stale contract from an older release\n", encoding="utf-8")

    refreshed = init_module.refresh_shipped_schema(vault)

    assert ".exomem/schema/SKILL.md" in refreshed
    packaged = (Path(init_module.__file__).parent / "_scaffold" / "_Schema" / "SKILL.md")
    assert _shipped(kb).read_bytes() == packaged.read_bytes()


def test_refresh_is_a_no_op_when_already_current(tmp_path: Path) -> None:
    """No churn on a current vault — the file watcher sees no spurious mtimes."""
    from exomem import init as init_module

    vault = tmp_path / "vault"
    vault.mkdir()
    init_module.init_vault(vault)

    assert init_module.refresh_shipped_schema(vault) == []


def test_refresh_never_touches_per_vault_configuration(tmp_path: Path) -> None:
    """project-keys.yaml and the registries are the user's to edit, not ours.

    This is the line between the two halves of _Schema: shipped governance docs
    are product-owned, the YAML registries are per-vault configuration.
    """
    from exomem import init as init_module

    vault = tmp_path / "vault"
    vault.mkdir()
    init_module.init_vault(vault)
    kb = vault / "Knowledge Base"

    user_edits = {
        "project-keys.yaml": "my-own-project: Notes/Research/Mine\n",
        "relation-registry.yaml": "# my registry\n",
        "semantic-language-registry.yaml": "# my language\n",
        "traversal-profiles.yaml": "# my profiles\n",
    }
    for name, body in user_edits.items():
        (kb / "_Schema" / name).write_text(body, encoding="utf-8")
    _shipped(kb).write_text("stale\n", encoding="utf-8")

    init_module.refresh_shipped_schema(vault)

    for name, body in user_edits.items():
        assert (kb / "_Schema" / name).read_text(encoding="utf-8") == body


def test_refresh_on_a_vault_without_a_knowledge_base_is_inert(tmp_path: Path) -> None:
    from exomem import init as init_module

    vault = tmp_path / "vault"
    vault.mkdir()

    assert init_module.refresh_shipped_schema(vault) == []
