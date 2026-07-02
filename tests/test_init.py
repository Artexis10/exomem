"""`init` — bootstrap a fresh Knowledge Base scaffold into an empty vault.

A friend with no KB needs the three load-bearing files (index.md, log.md,
_Schema/SKILL.md) to exist before the writers work. `init_vault` lays down the
whole Lovelace-style structure in one shot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import init as init_module
from exomem import vault as vault_module


def test_init_scaffolds_a_fresh_vault(tmp_path: Path) -> None:
    report = init_module.init_vault(tmp_path)
    kb = tmp_path / "Knowledge Base"

    # The three load-bearing files exist.
    assert (kb / "index.md").exists()
    assert (kb / "log.md").exists()
    assert (kb / "_Schema" / "SKILL.md").exists()

    # log.md carries the `---` separator the writers prepend after.
    assert "---" in (kb / "log.md").read_text(encoding="utf-8")

    # The typed folder tree is laid down.
    assert (kb / "Sources").is_dir()
    assert (kb / "Notes" / "Insights").is_dir()
    assert (kb / "Entities" / "Concepts").is_dir()
    assert (kb / "Evidence").is_dir()

    # The report names what it created.
    assert report["vault"] == str(tmp_path)
    assert any("index.md" in p for p in report["created"])


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
    assert (tmp_path / "Knowledge Base" / "_Schema" / "SKILL.md").exists()
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
